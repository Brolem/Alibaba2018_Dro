"""日前调度器：批处理能量平移 + 并网/爬坡 + BESS + PV + 双侧 Γ-budget RO。

用 PySCIPOpt 建线性模型。核心思想：数据中心有一个“必须满足”的固定在线负荷，
还有一批“可延迟”的批处理能量，可以在柔性窗口内搬运到便宜时段；再加上 BESS 充放电、
本地 PV 出力，目标是最小化总购电成本，并在并网/爬坡/鲁棒预算下保证可行。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .inputs import HourlyInput

try:  # PySCIPOpt 只在 scip_env 里；这样本模块在其它环境也能被 import（只是不能求解）。
    from pyscipopt import Model
except ImportError:  # pragma: no cover
    Model = None


@dataclass(frozen=True)
class BatchShiftResult:
    baseline_cost: float      # 不搬移、不用 BESS/PV 的基线总购电成本
    optimal_cost: float       # 优化后的总购电成本
    cost_reduction: float     # 总成本下降比例 = (baseline-optimal)/baseline
    batch: list[float]        # 每个小时的批处理功率（MW）
    grid: list[float]         # 每个小时的购电功率（MW）
    bess_charge: list[float]  # 每小时 BESS 充电功率（MW）
    bess_discharge: list[float]  # 每小时 BESS 放电功率（MW）
    feasible: bool = True     # 是否找到可行解


def _infeasible_result() -> BatchShiftResult:
    return BatchShiftResult(
        baseline_cost=0.0,
        optimal_cost=0.0,
        cost_reduction=0.0,
        batch=[],
        grid=[],
        bess_charge=[],
        bess_discharge=[],
        feasible=False,
    )


def solve_batch_shift(
    inputs: list[HourlyInput],
    *,
    g_max_mw: float | None = None,
    r_max_mw: float | None = None,
    p_grid_initial_mw: float | None = None,
    bess_power_mw: float | None = None,
    bess_energy_mwh: float | None = None,
    bess_efficiency: float = 0.90,
    soc_min: float = 0.10,
    soc_max: float = 0.90,
    soc_initial: float = 0.50,
    pv_capacity_mw: float | None = None,
    pv_robustness_budget: float = 0.0,
    pv_relative_error: float = 0.243,
    robustness_budget: float = 0.0,
    energy_uncertainty_fraction: float = 0.079,
) -> BatchShiftResult:
    """求解一次日前调度。

    参数（除 inputs 外都是可选；None 表示不启用该约束/设备）：
    - g_max_mw: 并网功率上限（MW）。
    - r_max_mw: 并网爬坡上限（MW/小时）。
    - p_grid_initial_mw: 第一个小时之前的购电功率，用于第一个小时的爬坡约束。
    - bess_power_mw / bess_energy_mwh: BESS 功率（MW）与能量容量（MWh），必须成对给。
    - bess_efficiency / soc_min / soc_max / soc_initial: BESS 往返效率与 SOC 边界/初值。
    - pv_capacity_mw: 本地 PV 容量（MW）；用系统太阳形状缩放。
    - pv_robustness_budget (Γ_pv) / pv_relative_error (ε): 能源侧鲁棒预算，把 PV 按
      (1-Γ_pv·ε) 打折，代表“为 PV 预测误差留裕量”。
    - robustness_budget (Γ) / energy_uncertainty_fraction (δ): 算力侧鲁棒预算，把批处理
      总能量放大到 (1+Γ·δ) 倍，代表“为作业到达/时长不确定多排能量”。
    """

    if Model is None:
        raise RuntimeError("pyscipopt is required; run inside scip_env")
    if (bess_power_mw is None) != (bess_energy_mwh is None):
        raise ValueError("bess_power_mw and bess_energy_mwh must be given together")

    model = Model("batch_shift")
    model.hideOutput()

    hours = len(inputs)
    # 必须满足的固定负荷 = 在线负荷 + 基座功率（不参与延迟）
    p_must = inputs[0].online_mw + inputs[0].base_mw

    # 决策变量：每小时批处理功率，上界是该小时的柔性窗口能量
    batch = {
        item.hour: model.addVar(
            lb=0.0,
            ub=item.batch_window_mwh,
            name=f"batch_{item.hour}",
        )
        for item in inputs
    }

    # 购电功率初始表达式：固定负荷 + 批处理；后面再叠 BESS 和 PV
    p_grid = {item.hour: p_must + batch[item.hour] for item in inputs}
    p_ch: dict[int, object] = {}
    p_dis: dict[int, object] = {}

    if bess_power_mw is not None:
        # 往返效率 η 拆成充放电各 √η
        eta = math.sqrt(bess_efficiency)
        p_ch = {
            t: model.addVar(lb=0.0, ub=bess_power_mw, name=f"pch_{t}")
            for t in range(hours)
        }
        p_dis = {
            t: model.addVar(lb=0.0, ub=bess_power_mw, name=f"pdis_{t}")
            for t in range(hours)
        }
        energy = {
            t: model.addVar(
                lb=soc_min * bess_energy_mwh,
                ub=soc_max * bess_energy_mwh,
                name=f"bess_energy_{t}",
            )
            for t in range(hours + 1)
        }
        model.addCons(energy[0] == soc_initial * bess_energy_mwh)
        # SOC 递推：充电进 η·p_ch，放电出 p_dis/η
        for t in range(hours):
            model.addCons(
                energy[t + 1] == energy[t] + eta * p_ch[t] - p_dis[t] / eta
            )
        # 末态回到初态，禁止靠“期末卖空”占便宜
        model.addCons(energy[hours] == energy[0])
        for t in range(hours):
            p_grid[t] = p_grid[t] + p_ch[t] - p_dis[t]

    if pv_capacity_mw is not None:
        # 能源侧鲁棒：PV 按 (1 - Γ_pv·ε) 打折，模拟最坏情况下的风光缺口
        effective_capacity = pv_capacity_mw * (
            1.0 - pv_robustness_budget * pv_relative_error
        )
        pv = _pv_profile(inputs, effective_capacity)
        for item in inputs:
            p_grid[item.hour] = p_grid[item.hour] - pv[item.hour]

    # 算力侧鲁棒：批处理总能量放大到 (1 + Γ·δ) 倍，模拟作业到达/时长偏多
    total_energy = sum(item.batch_baseline_mwh for item in inputs)
    total_energy *= 1.0 + robustness_budget * energy_uncertainty_fraction
    # 能量守恒：所有批处理能量必须在窗口内排完（只平移、不消失）
    model.addCons(
        sum(batch[item.hour] for item in inputs) == total_energy,
        name="batch_energy_conservation",
    )

    # 并网上限与“不反向送电”
    if g_max_mw is not None:
        for item in inputs:
            model.addCons(p_grid[item.hour] <= g_max_mw, name=f"grid_limit_{item.hour}")
    for item in inputs:
        model.addCons(p_grid[item.hour] >= 0.0, name=f"grid_nonneg_{item.hour}")

    # 爬坡约束：相邻小时购电变化不超过 R_max
    if r_max_mw is not None:
        previous = p_grid_initial_mw
        for item in inputs:
            current = p_grid[item.hour]
            if previous is not None:
                model.addCons(current - previous <= r_max_mw, name=f"ramp_up_{item.hour}")
                model.addCons(current - previous >= -r_max_mw, name=f"ramp_down_{item.hour}")
            previous = current

    # 目标：最小化总购电成本（电价 × 每小时购电功率）
    model.setObjective(
        sum(item.dam_lz_houston_usd_per_mwh * p_grid[item.hour] for item in inputs),
        "minimize",
    )
    model.optimize()

    if model.getStatus() != "optimal":
        return _infeasible_result()

    batch_values = [model.getVal(batch[item.hour]) for item in inputs]
    grid_values = [model.getVal(p_grid[item.hour]) for item in inputs]
    charge_values = [model.getVal(p_ch[t]) if p_ch else 0.0 for t in range(hours)]
    discharge_values = [model.getVal(p_dis[t]) if p_dis else 0.0 for t in range(hours)]
    optimal_cost = sum(
        item.dam_lz_houston_usd_per_mwh * grid_values[index]
        for index, item in enumerate(inputs)
    )
    baseline_cost = sum(
        item.dam_lz_houston_usd_per_mwh * (p_must + item.batch_baseline_mwh)
        for item in inputs
    )

    return BatchShiftResult(
        baseline_cost=baseline_cost,
        optimal_cost=optimal_cost,
        cost_reduction=(
            (baseline_cost - optimal_cost) / baseline_cost if baseline_cost else 0.0
        ),
        batch=batch_values,
        grid=grid_values,
        bess_charge=charge_values,
        bess_discharge=discharge_values,
        feasible=True,
    )


def _peak_load(inputs: list[HourlyInput]) -> float:
    """基线峰值负荷 = 固定负荷 + 批处理基线功率的最大值。"""
    p_must = inputs[0].online_mw + inputs[0].base_mw
    return max(p_must + item.batch_baseline_mwh for item in inputs)


def _pv_profile(inputs: list[HourlyInput], pv_capacity_mw: float) -> list[float]:
    """把 ERCO 系统太阳预测形状缩放到本地 PV 容量。"""

    solar = [item.forecast_erco_solar_generation_mwh for item in inputs]
    peak = max(solar)
    if peak <= 0:
        return [0.0] * len(inputs)
    return [pv_capacity_mw * value / peak for value in solar]


def sweep_grid_limit(
    inputs: list[HourlyInput],
    *,
    g_max_fractions: list[float],
    r_max_fraction: float | None = None,
) -> list[tuple[float, bool, float]]:
    """扫并网上限 G_max（按峰值负荷的倍数）。"""

    p_peak = _peak_load(inputs)
    p_must = inputs[0].online_mw + inputs[0].base_mw
    r_max_mw = r_max_fraction * p_peak if r_max_fraction is not None else None
    p_grid_initial = p_must + inputs[0].batch_baseline_mwh

    rows: list[tuple[float, bool, float]] = []
    for fraction in g_max_fractions:
        result = solve_batch_shift(
            inputs,
            g_max_mw=fraction * p_peak,
            r_max_mw=r_max_mw,
            p_grid_initial_mw=p_grid_initial,
        )
        rows.append((fraction, result.feasible, result.cost_reduction))
    return rows


def sweep_ramp_limit(
    inputs: list[HourlyInput],
    *,
    g_max_fraction: float,
    r_max_fractions: list[float],
) -> list[tuple[float, bool, float]]:
    """扫爬坡上限 R_max（固定 G_max，按峰值负荷的倍数/小时）。"""

    p_peak = _peak_load(inputs)
    p_must = inputs[0].online_mw + inputs[0].base_mw
    g_max_mw = g_max_fraction * p_peak
    p_grid_initial = p_must + inputs[0].batch_baseline_mwh

    rows: list[tuple[float, bool, float]] = []
    for fraction in r_max_fractions:
        result = solve_batch_shift(
            inputs,
            g_max_mw=g_max_mw,
            r_max_mw=fraction * p_peak,
            p_grid_initial_mw=p_grid_initial,
        )
        rows.append((fraction, result.feasible, result.cost_reduction))
    return rows


def sweep_bess_power(
    inputs: list[HourlyInput],
    *,
    g_max_fraction: float,
    r_max_fraction: float,
    power_fractions: list[float],
    energy_hours: float = 2.0,
    bess_efficiency: float = 0.90,
) -> list[tuple[float, bool, float, float]]:
    """扫 BESS 功率（能量固定为若干小时×功率），返回成本下降。"""

    p_peak = _peak_load(inputs)
    p_must = inputs[0].online_mw + inputs[0].base_mw
    g_max_mw = g_max_fraction * p_peak
    r_max_mw = r_max_fraction * p_peak
    p_grid_initial = p_must + inputs[0].batch_baseline_mwh

    rows: list[tuple[float, bool, float, float]] = []
    for fraction in power_fractions:
        power = fraction * p_peak
        result = solve_batch_shift(
            inputs,
            g_max_mw=g_max_mw,
            r_max_mw=r_max_mw,
            p_grid_initial_mw=p_grid_initial,
            bess_power_mw=power,
            bess_energy_mwh=energy_hours * power,
            bess_efficiency=bess_efficiency,
        )
        rows.append((fraction, result.feasible, result.cost_reduction, power))
    return rows


def sweep_pv_capacity(
    inputs: list[HourlyInput],
    *,
    g_max_fraction: float,
    r_max_fraction: float,
    pv_fractions: list[float],
    bess_power_fraction: float = 0.0,
    bess_energy_hours: float = 2.0,
) -> list[tuple[float, bool, float, float]]:
    """扫本地 PV 容量（按必须满足负荷的倍数），返回成本下降。"""

    p_peak = _peak_load(inputs)
    p_must = inputs[0].online_mw + inputs[0].base_mw
    g_max_mw = g_max_fraction * p_peak
    r_max_mw = r_max_fraction * p_peak
    p_grid_initial = p_must + inputs[0].batch_baseline_mwh

    rows: list[tuple[float, bool, float, float]] = []
    for fraction in pv_fractions:
        capacity = fraction * p_must
        result = solve_batch_shift(
            inputs,
            g_max_mw=g_max_mw,
            r_max_mw=r_max_mw,
            p_grid_initial_mw=p_grid_initial,
            bess_power_mw=bess_power_fraction * p_peak,
            bess_energy_mwh=bess_energy_hours * bess_power_fraction * p_peak,
            pv_capacity_mw=capacity,
        )
        rows.append((fraction, result.feasible, result.cost_reduction, capacity))
    return rows


def sweep_robustness_budget(
    inputs: list[HourlyInput],
    *,
    g_max_fraction: float,
    r_max_fraction: float,
    budgets: list[float],
    energy_uncertainty_fraction: float = 0.079,
    bess_power_fraction: float = 0.5,
    bess_energy_hours: float = 2.0,
    pv_fraction: float = 1.0,
) -> list[tuple[float, bool, float]]:
    """扫算力侧鲁棒预算 Γ（批处理总能量不确定）。"""

    p_peak = _peak_load(inputs)
    p_must = inputs[0].online_mw + inputs[0].base_mw
    g_max_mw = g_max_fraction * p_peak
    r_max_mw = r_max_fraction * p_peak
    p_grid_initial = p_must + inputs[0].batch_baseline_mwh

    rows: list[tuple[float, bool, float]] = []
    for budget in budgets:
        result = solve_batch_shift(
            inputs,
            g_max_mw=g_max_mw,
            r_max_mw=r_max_mw,
            p_grid_initial_mw=p_grid_initial,
            bess_power_mw=bess_power_fraction * p_peak,
            bess_energy_mwh=bess_energy_hours * bess_power_fraction * p_peak,
            pv_capacity_mw=pv_fraction * p_must,
            robustness_budget=budget,
            energy_uncertainty_fraction=energy_uncertainty_fraction,
        )
        rows.append((budget, result.feasible, result.cost_reduction))
    return rows


def sweep_pv_robustness(
    inputs: list[HourlyInput],
    *,
    g_max_fraction: float,
    r_max_fraction: float,
    budgets: list[float],
    pv_relative_error: float = 0.243,
    bess_power_fraction: float = 0.5,
    bess_energy_hours: float = 2.0,
    pv_fraction: float = 1.0,
) -> list[tuple[float, bool, float]]:
    """扫能源侧鲁棒预算 Γ_pv（PV 预测误差不确定）。"""

    p_peak = _peak_load(inputs)
    p_must = inputs[0].online_mw + inputs[0].base_mw
    g_max_mw = g_max_fraction * p_peak
    r_max_mw = r_max_fraction * p_peak
    p_grid_initial = p_must + inputs[0].batch_baseline_mwh

    rows: list[tuple[float, bool, float]] = []
    for budget in budgets:
        result = solve_batch_shift(
            inputs,
            g_max_mw=g_max_mw,
            r_max_mw=r_max_mw,
            p_grid_initial_mw=p_grid_initial,
            bess_power_mw=bess_power_fraction * p_peak,
            bess_energy_mwh=bess_energy_hours * bess_power_fraction * p_peak,
            pv_capacity_mw=pv_fraction * p_must,
            pv_robustness_budget=budget,
            pv_relative_error=pv_relative_error,
        )
        rows.append((budget, result.feasible, result.cost_reduction))
    return rows

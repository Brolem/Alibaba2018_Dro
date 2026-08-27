"""生成 2024 校准日块和 SAA 训练/验证/回放场景 manifest。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from alibaba2018_dro.eia_history import load_houston_dam_prices
from alibaba2018_dro.scenarios import (
    CALIBRATION_BLOCKS_FILENAME,
    SAA_MANIFEST_FILENAME,
    write_calibration_day_blocks,
    write_saa_scenario_manifest,
)


RAW_ENERGY = ROOT / "data" / "raw" / "energy"
PROCESSED = ROOT / "data" / "processed"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--residual-csv",
        type=Path,
        default=PROCESSED / "energy" / "residuals" / "joint_residuals_2024.csv",
    )
    parser.add_argument(
        "--dam-price-archive",
        type=Path,
        default=RAW_ENERGY / "ercot_2024_historical_dam_load_zone_and_hub_prices.zip",
    )
    parser.add_argument(
        "--aggregate-workload-csv",
        type=Path,
        default=PROCESSED / "workload" / "aggregate_workload_8d.csv",
    )
    parser.add_argument(
        "--workload-manifest",
        type=Path,
        default=PROCESSED / "workload" / "nominal_workload_manifest.json",
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=PROCESSED / "scenarios",
    )
    args = parser.parse_args()

    price_rows = load_houston_dam_prices(args.dam_price_archive, year=2024)
    block_manifest = write_calibration_day_blocks(
        residual_csv=args.residual_csv,
        dam_price_rows=price_rows,
        dam_price_source=args.dam_price_archive,
        output_directory=args.output_directory,
    )
    scenario_manifest = write_saa_scenario_manifest(
        calibration_csv=args.output_directory / CALIBRATION_BLOCKS_FILENAME,
        workload_csv=args.aggregate_workload_csv,
        workload_manifest=args.workload_manifest,
        output_path=args.output_directory / SAA_MANIFEST_FILENAME,
    )
    print("calibration_day_blocks:", block_manifest["usable_day_block_count"])
    print("calibration_blocks_by_fold:", block_manifest["usable_day_block_count_by_fold"])
    print("SAA_sample_sizes:", scenario_manifest["shared_protocol"]["nested_saa_sample_sizes"])
    print("written:", args.output_directory / CALIBRATION_BLOCKS_FILENAME)
    print("written:", args.output_directory / SAA_MANIFEST_FILENAME)


if __name__ == "__main__":
    main()

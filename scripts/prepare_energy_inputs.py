from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alibaba2018_dro.energy import (
    sha256_file,
    write_study_inputs,
)
from alibaba2018_dro.eia_history import (
    build_december_context,
    load_erco_history,
    load_houston_dam_prices,
)


RAW_ENERGY_DIRECTORY = PROJECT_ROOT / "data" / "raw" / "energy"
PROCESSED_ENERGY_DIRECTORY = PROJECT_ROOT / "data" / "processed" / "energy"
DEFAULT_OUTPUT_DIRECTORY = PROCESSED_ENERGY_DIRECTORY / "windows"


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    """读取一张带表头的 CSV，返回全部行；缺失文件直接报错。"""
    if not path.is_file():
        raise FileNotFoundError(f"missing input CSV: {path}")
    with path.open("r", encoding="utf-8", newline="") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames is None:
            raise ValueError(f"input CSV has no header: {path}")
        return list(reader)


def _source_argument(value: str) -> tuple[str, Path]:
    """解析命令行 --source ID=PATH 参数，返回 (来源标识, 文件路径)。"""
    source_id, separator, path_text = value.partition("=")
    if not separator or not source_id or not path_text:
        raise argparse.ArgumentTypeError(
            "--source must use stable_source_id=path"
        )
    path = Path(path_text)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"source file does not exist: {path}")
    return source_id, path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize 1,062-hour ERCOT Houston inputs "
            "from auditable local source records."
        )
    )
    parser.add_argument(
        "--annual-2025",
        type=Path,
        default=PROCESSED_ENERGY_DIRECTORY / "ercot_2025_houston_hourly.csv",
        help="Shared 2025 ERCOT Houston annual table (processed data).",
    )
    parser.add_argument(
        "--ercot-2024-dam",
        type=Path,
        default=(
            RAW_ENERGY_DIRECTORY
            / "ercot_2024_historical_dam_load_zone_and_hub_prices.zip"
        ),
        help="Official ERCOT 2024 annual DAM archive (raw data).",
    )
    parser.add_argument(
        "--eia-history",
        type=Path,
        default=RAW_ENERGY_DIRECTORY / "eia_930_erco_full_history.xlsx",
        help=(
            "EIA-930 workbook used to construct leakage-free ERCO forecasts "
            "(raw data)."
        ),
    )
    parser.add_argument(
        "--source",
        action="append",
        type=_source_argument,
        required=True,
        metavar="ID=PATH",
        help=(
            "Raw public source to hash, e.g. "
            "eia_930_erco=data/raw/energy/eia_930_erco_full_history.xlsx. "
            "Repeat for each source; IDs never contain a local path."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
        help="Version-controlled compact energy input directory.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    source_hashes = {
        source_id: sha256_file(path) for source_id, path in args.source
    }
    if len(source_hashes) != len(args.source):
        raise ValueError("duplicate --source identifiers are not allowed")
    eia_history = load_erco_history(args.eia_history)
    december_2024 = build_december_context(
        load_houston_dam_prices(args.ercot_2024_dam, year=2024),
        eia_history,
        year=2024,
    )
    manifest = write_study_inputs(
        annual_2025=_read_csv_rows(args.annual_2025),
        december_2024=december_2024,
        eia_history=eia_history,
        output_directory=args.output_dir,
        source_hashes=source_hashes,
    )
    print(
        "materialized "
        f"{len(manifest['outputs'])} paper energy inputs in {args.output_dir}"
    )


if __name__ == "__main__":
    main()

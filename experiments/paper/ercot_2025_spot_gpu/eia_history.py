"""Paper-local reader for the public EIA-930 ERCO history workbook."""

from __future__ import annotations

import datetime as dt
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET


_XLSX_NAMESPACE = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RELATIONSHIPS_NAMESPACE = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_OFFICE_RELATIONSHIP = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_COLUMN_REFERENCE = re.compile(r"([A-Z]+)[0-9]+$")
_EIA_COLUMNS = (
    "BA",
    "UTC time",
    "Local date",
    "NG: SUN",
    "NG: WND",
    "CO2 Emissions Intensity for Consumed Electricity",
)
_ERCOT_PRICE_COLUMNS = (
    "Delivery Date",
    "Hour Ending",
    "Repeated Hour Flag",
    "Settlement Point",
    "Settlement Point Price",
)
_MONTH_SHEETS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _shared_strings(archive: ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(text.text or "" for text in item.iter(f"{_XLSX_NAMESPACE}t"))
        for item in root.findall(f"{_XLSX_NAMESPACE}si")
    ]


def _worksheet_path(archive: ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        relationship.attrib["Id"]: relationship.attrib["Target"]
        for relationship in relationships.findall(
            f"{_RELATIONSHIPS_NAMESPACE}Relationship"
        )
    }
    for sheet in workbook.findall(f"{_XLSX_NAMESPACE}sheets/{_XLSX_NAMESPACE}sheet"):
        if sheet.attrib.get("name") == sheet_name:
            relationship_id = sheet.attrib.get(f"{_OFFICE_RELATIONSHIP}id")
            if relationship_id is None or relationship_id not in targets:
                raise ValueError(f"worksheet relationship missing for {sheet_name}")
            target = targets[relationship_id].lstrip("/")
            return target if target.startswith("xl/") else f"xl/{target}"
    raise ValueError(f"worksheet not found: {sheet_name}")


def _column_index(cell_reference: str) -> int:
    match = _COLUMN_REFERENCE.fullmatch(cell_reference)
    if match is None:
        raise ValueError(f"invalid XLSX cell reference: {cell_reference}")
    index = 0
    for letter in match.group(1):
        index = index * 26 + ord(letter) - ord("A") + 1
    return index - 1


def _cell_value(cell: ET.Element, shared_strings: Sequence[str]) -> str | float | None:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.iter(f"{_XLSX_NAMESPACE}t"))
    value = cell.find(f"{_XLSX_NAMESPACE}v")
    if value is None or value.text is None:
        return None
    if cell_type == "s":
        try:
            return shared_strings[int(value.text)]
        except (IndexError, ValueError) as error:
            raise ValueError("invalid XLSX shared-string index") from error
    if cell_type in ("str", "b", "e"):
        return value.text
    try:
        return float(value.text)
    except ValueError:
        return value.text


def iter_xlsx_rows(
    source: Path | BytesIO,
    sheet_name: str,
    *,
    columns: Iterable[str] | None = None,
) -> Iterator[dict[str, str | float]]:
    """Yield selected nonempty rows from one XLSX worksheet without dependencies."""

    if isinstance(source, BytesIO):
        source.seek(0)
    with ZipFile(source) as archive:
        shared_strings = _shared_strings(archive)
        worksheet_path = _worksheet_path(archive, sheet_name)
        selected_columns = set(columns) if columns is not None else None
        header_by_index: dict[int, str] | None = None
        with archive.open(worksheet_path) as worksheet_file:
            for _, element in ET.iterparse(worksheet_file, events=("end",)):
                if element.tag != f"{_XLSX_NAMESPACE}row":
                    continue
                cells: dict[int, str | float] = {}
                for cell in element.findall(f"{_XLSX_NAMESPACE}c"):
                    reference = cell.attrib.get("r")
                    if reference is None:
                        raise ValueError("XLSX cell is missing its reference")
                    value = _cell_value(cell, shared_strings)
                    if value is not None:
                        cells[_column_index(reference)] = value
                if header_by_index is None:
                    header_by_index = {index: str(value) for index, value in cells.items()}
                    if selected_columns is not None:
                        missing = selected_columns.difference(header_by_index.values())
                        if missing:
                            raise ValueError(
                                f"{sheet_name} is missing columns: {sorted(missing)}"
                            )
                elif cells:
                    yield {
                        header: value
                        for index, value in cells.items()
                        if (header := header_by_index.get(index)) is not None
                        and (selected_columns is None or header in selected_columns)
                    }
                element.clear()
        if header_by_index is None:
            raise ValueError(f"worksheet has no header row: {sheet_name}")


def _excel_datetime(value: object, *, label: str) -> dt.datetime:
    try:
        serial = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be an Excel date serial") from error
    if not math.isfinite(serial):
        raise ValueError(f"{label} must be finite")
    return dt.datetime(1899, 12, 30) + dt.timedelta(days=serial)


def _optional_number(value: object, *, label: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be numeric") from error
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _xlsx_payload_from_archive(path: Path) -> BytesIO:
    with ZipFile(path) as archive:
        workbook_names = [
            name
            for name in archive.namelist()
            if not name.endswith("/") and name.lower().endswith(".xlsx")
        ]
        if len(workbook_names) != 1:
            raise ValueError(
                f"{path.name} must contain exactly one XLSX workbook"
            )
        return BytesIO(archive.read(workbook_names[0]))


def load_houston_dam_prices(
    path: Path,
    *,
    year: int,
) -> list[dict[str, object]]:
    """Load one ERCOT annual archive and retain LZ_HOUSTON DAM prices."""

    payload = _xlsx_payload_from_archive(path)
    rows: list[dict[str, object]] = []
    for sheet_name in _MONTH_SHEETS:
        for source_row in iter_xlsx_rows(
            payload,
            sheet_name,
            columns=_ERCOT_PRICE_COLUMNS,
        ):
            if source_row.get("Settlement Point") != "LZ_HOUSTON":
                continue
            try:
                delivery_date = dt.datetime.strptime(
                    str(source_row.get("Delivery Date")), "%m/%d/%Y"
                ).date()
            except ValueError as error:
                raise ValueError("ERCOT Delivery Date must use MM/DD/YYYY") from error
            if delivery_date.year != year:
                continue
            rows.append(
                {
                    "delivery_date": delivery_date.isoformat(),
                    "hour_ending": str(source_row.get("Hour Ending")),
                    "repeated_hour_flag": str(
                        source_row.get("Repeated Hour Flag", "")
                    ),
                    "dam_lz_houston_usd_per_mwh": _optional_number(
                        source_row.get("Settlement Point Price"),
                        label="ERCOT Settlement Point Price",
                    ),
                }
            )
    if not rows:
        raise ValueError(f"ERCOT source contains no LZ_HOUSTON rows for {year}")
    return rows


def load_erco_history(path: Path) -> list[dict[str, object]]:
    """Load all ERCO observations for causal forecast construction."""

    rows: list[dict[str, object]] = []
    for source_row in iter_xlsx_rows(
        path, "Published Hourly Data", columns=_EIA_COLUMNS
    ):
        if source_row.get("BA") != "ERCO":
            continue
        utc_time = _excel_datetime(source_row.get("UTC time"), label="EIA UTC time")
        local_date = _excel_datetime(
            source_row.get("Local date"), label="EIA local date"
        ).date()
        rows.append(
            {
                "timestamp_utc": utc_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "local_date": local_date.isoformat(),
                "erco_solar_generation_mwh": _optional_number(
                    source_row.get("NG: SUN"), label="EIA NG: SUN"
                ),
                "erco_wind_generation_mwh": _optional_number(
                    source_row.get("NG: WND"), label="EIA NG: WND"
                ),
                "erco_consumed_co2_intensity_lbs_per_kwh": _optional_number(
                    source_row.get("CO2 Emissions Intensity for Consumed Electricity"),
                    label="EIA consumed CO2 intensity",
                ),
            }
        )
    rows.sort(key=lambda row: str(row["timestamp_utc"]))
    timestamps = [str(row["timestamp_utc"]) for row in rows]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("EIA ERCO history contains duplicate UTC timestamps")
    if not rows:
        raise ValueError("EIA workbook contains no ERCO rows")
    return rows


def build_december_context(
    price_rows: Sequence[Mapping[str, object]],
    eia_history: Sequence[Mapping[str, object]],
    *,
    year: int,
) -> list[dict[str, object]]:
    """Pair one December of LZ_HOUSTON DAM prices with EIA ERCO actuals."""

    month_prefix = f"{year:04d}-12-"
    prices_by_date: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    eia_by_date: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in price_rows:
        delivery_date = str(row.get("delivery_date", ""))
        if delivery_date.startswith(month_prefix):
            prices_by_date[delivery_date].append(row)
    for row in eia_history:
        local_date = str(row.get("local_date", ""))
        if local_date.startswith(month_prefix):
            eia_by_date[local_date].append(row)
    if set(prices_by_date) != set(eia_by_date):
        raise ValueError("December ERCOT and EIA local-date sets differ")

    result: list[dict[str, object]] = []
    for local_date in sorted(prices_by_date):
        daily_prices = prices_by_date[local_date]
        daily_eia = sorted(
            eia_by_date[local_date], key=lambda row: str(row["timestamp_utc"])
        )
        if len(daily_prices) != len(daily_eia):
            raise ValueError(
                f"December record counts differ for {local_date}: "
                f"{len(daily_prices)} != {len(daily_eia)}"
            )
        for price, eia in zip(daily_prices, daily_eia, strict=True):
            result.append(
                {
                    "timestamp_utc": eia["timestamp_utc"],
                    "local_date": local_date,
                    "dam_lz_houston_usd_per_mwh": _optional_number(
                        price.get("dam_lz_houston_usd_per_mwh"),
                        label="ERCOT LZ_HOUSTON DAM price",
                    ),
                    "erco_solar_generation_mwh": eia.get(
                        "erco_solar_generation_mwh"
                    ),
                    "erco_wind_generation_mwh": eia.get(
                        "erco_wind_generation_mwh"
                    ),
                    "erco_consumed_co2_intensity_lbs_per_kwh": eia.get(
                        "erco_consumed_co2_intensity_lbs_per_kwh"
                    ),
                }
            )
    if len(result) != 31 * 24:
        raise ValueError(
            f"December {year} context must contain 744 hours, found {len(result)}"
        )
    return result

from __future__ import annotations

import csv
import re
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parent / "data"
ANSWER_COLUMNS = ["Correct Answer", "Wrong 1", "Wrong 2", "Wrong 3"]
NUMERIC_PATTERN = re.compile(r"^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$")


def is_numeric_value(value: str) -> bool:
    normalized = value.strip().replace("−", "-")
    if not normalized:
        return False
    return bool(NUMERIC_PATTERN.fullmatch(normalized))


def row_matches(row: dict[str, str]) -> bool:
    if is_numeric_value(row.get("Correct Answer", "")):
        return True
    return all(is_numeric_value(row.get(column, "")) for column in ANSWER_COLUMNS)


def scan_csv(csv_path: Path) -> int:
    with csv_path.open("r", encoding="utf-8", newline="") as source_handle:
        reader = csv.DictReader(source_handle)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    match_count = 0
    for row in rows:
        if row_matches(row):
            row["Type"] = "estimate"
            match_count += 1

    if match_count == 0:
        return 0

    with csv_path.open("w", encoding="utf-8", newline="") as output_handle:
        writer = csv.DictWriter(output_handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return match_count


def main() -> None:
    total_matches = 0
    input_files = sorted(path for path in DATA_DIR.glob("*.csv") if not path.name.endswith("_where_found.csv"))
    for csv_path in input_files:
        match_count = scan_csv(csv_path)
        total_matches += match_count
        print(f"{csv_path.name}: {match_count} matches")
    print(f"Total numeric-question matches: {total_matches}")


if __name__ == "__main__":
    main()
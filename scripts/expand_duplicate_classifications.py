#!/usr/bin/env python3
"""Expand unique-record classifications back to all original records."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple


def read_table(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    if path.suffix.lower() == ".jsonl":
        rows: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"Invalid JSONL at line {line_number}: {exc}") from exc
        columns = sorted({key for row in rows for key in row.keys()})
        return rows, columns

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise SystemExit(f"File has no header: {path}")
        return list(reader), list(reader.fieldnames)


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("classifications", type=Path, help="CSV or JSONL classifications for unique records")
    parser.add_argument("duplicate_map", type=Path, help="duplicate_map.csv from prepare_issue_batches.py")
    parser.add_argument("--out", type=Path, required=True, help="Output CSV for all original records")
    parser.add_argument("--classification-id-column", default="record_id")
    args = parser.parse_args()

    classifications, classification_columns = read_table(args.classifications)
    duplicate_rows, _ = read_table(args.duplicate_map)

    by_unique_id = {}
    for row in classifications:
        unique_id = str(row.get(args.classification_id_column, "")).strip()
        if not unique_id:
            raise SystemExit(f"Classification row missing {args.classification_id_column}: {row}")
        by_unique_id[unique_id] = row

    expanded: List[Dict[str, Any]] = []
    missing: List[str] = []
    for mapping in duplicate_rows:
        unique_id = str(mapping.get("unique_record_id", "")).strip()
        original_id = str(mapping.get("original_record_id", "")).strip()
        classification = by_unique_id.get(unique_id)
        if not classification:
            missing.append(unique_id)
            continue
        output_row = dict(classification)
        output_row["record_id"] = original_id
        output_row["unique_record_id"] = unique_id
        output_row["cache_key"] = mapping.get("cache_key", "")
        expanded.append(output_row)

    if missing:
        unique_missing = sorted(set(missing))
        preview = ", ".join(unique_missing[:10])
        raise SystemExit(f"Missing classifications for {len(unique_missing)} unique IDs: {preview}")

    output_columns = ["record_id", "unique_record_id", "cache_key"]
    output_columns.extend(column for column in classification_columns if column not in output_columns)
    write_csv(args.out, expanded, output_columns)
    print(json.dumps({"expanded_records": len(expanded), "output": str(args.out)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

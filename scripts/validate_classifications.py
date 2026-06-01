#!/usr/bin/env python3
"""Validate issue taxonomy classification outputs."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


def read_table(path: Path, encoding: str = "utf-8-sig") -> Tuple[List[Dict[str, Any]], List[str]]:
    if path.suffix.lower() == ".jsonl":
        rows = []
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

    with path.open("r", encoding=encoding, newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise SystemExit(f"File has no header: {path}")
        return list(reader), list(reader.fieldnames)


def load_taxonomy(path: Optional[Path]) -> Tuple[Set[str], Set[Tuple[str, str]]]:
    if not path:
        return set(), set()
    taxonomy = json.loads(path.read_text(encoding="utf-8"))
    l1_codes: Set[str] = set()
    pairs: Set[Tuple[str, str]] = set()
    for label in taxonomy.get("labels", []):
        l1_code = str(label.get("l1_code", "")).strip()
        if not l1_code:
            continue
        l1_codes.add(l1_code)
        for child in label.get("children", []):
            l2_code = str(child.get("l2_code", "")).strip()
            if l2_code:
                pairs.add((l1_code, l2_code))
    return l1_codes, pairs


def parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def parse_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("classifications", type=Path, help="Classification CSV or JSONL")
    parser.add_argument("--taxonomy", type=Path, help="Frozen taxonomy JSON")
    parser.add_argument("--records", type=Path, help="records_normalized.jsonl or other source record table")
    parser.add_argument("--out", type=Path, help="Directory for validation_summary.json and invalid_rows.csv")
    parser.add_argument("--id-column", default="record_id")
    parser.add_argument("--l1-column", default="l1_code")
    parser.add_argument("--l2-column", default="l2_code")
    parser.add_argument("--confidence-column", default="confidence")
    parser.add_argument("--review-column", default="review_flag")
    parser.add_argument("--min-confidence", type=float, default=0.72)
    args = parser.parse_args()

    rows, columns = read_table(args.classifications)
    required = [args.id_column, args.l1_column, args.l2_column, args.confidence_column]
    missing_columns = [column for column in required if column not in columns]
    if missing_columns:
        raise SystemExit(f"Missing classification columns: {', '.join(missing_columns)}")

    valid_l1, valid_pairs = load_taxonomy(args.taxonomy)
    invalid_rows: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    duplicate_ids: Set[str] = set()
    l1_counts: Counter[str] = Counter()
    l2_counts: Counter[str] = Counter()
    low_confidence_count = 0
    review_count = 0
    invalid_pair_count = 0
    missing_value_count = 0

    for index, row in enumerate(rows, 1):
        problems: List[str] = []
        record_id = str(row.get(args.id_column, "")).strip()
        l1 = str(row.get(args.l1_column, "")).strip()
        l2 = str(row.get(args.l2_column, "")).strip()
        confidence = parse_float(row.get(args.confidence_column))

        if not record_id or not l1 or not l2 or confidence is None:
            missing_value_count += 1
            problems.append("missing_required_value")
        if record_id in seen_ids:
            duplicate_ids.add(record_id)
            problems.append("duplicate_record_id")
        seen_ids.add(record_id)

        if confidence is not None and confidence < args.min_confidence:
            low_confidence_count += 1
            problems.append("low_confidence")
        if args.review_column in row and parse_bool(row.get(args.review_column)):
            review_count += 1

        if valid_l1 and l1 not in valid_l1:
            invalid_pair_count += 1
            problems.append("unknown_l1")
        if valid_pairs and (l1, l2) not in valid_pairs:
            invalid_pair_count += 1
            problems.append("invalid_l1_l2_pair")

        l1_counts[l1] += 1
        l2_counts[f"{l1}/{l2}"] += 1

        if problems:
            invalid_rows.append({"row_number": index, "problems": ";".join(sorted(set(problems))), **row})

    coverage_missing = []
    expected_record_count = None
    if args.records:
        record_rows, record_columns = read_table(args.records, encoding="utf-8")
        source_id_column = "original_record_id" if "original_record_id" in record_columns else args.id_column
        expected_ids = {str(row.get(source_id_column, "")).strip() for row in record_rows if row.get(source_id_column)}
        expected_record_count = len(expected_ids)
        coverage_missing = sorted(expected_ids - seen_ids)

    summary = {
        "classification_rows": len(rows),
        "unique_record_ids": len(seen_ids),
        "expected_record_count": expected_record_count,
        "coverage_missing_count": len(coverage_missing),
        "duplicate_record_id_count": len(duplicate_ids),
        "missing_value_count": missing_value_count,
        "low_confidence_count": low_confidence_count,
        "review_flag_count": review_count,
        "invalid_label_issue_count": invalid_pair_count,
        "invalid_rows_count": len(invalid_rows),
        "top_l1": l1_counts.most_common(20),
        "top_l2": l2_counts.most_common(30),
        "coverage_missing_preview": coverage_missing[:20],
    }

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "validation_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        if invalid_rows:
            fields = ["row_number", "problems"] + columns
            write_csv(args.out / "invalid_rows.csv", invalid_rows, fields)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

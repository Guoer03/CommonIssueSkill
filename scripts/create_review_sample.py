#!/usr/bin/env python3
"""Create a risk-based review sample for issue classifications."""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def read_table(path: Path, encoding: str = "utf-8-sig") -> Tuple[List[Dict[str, Any]], List[str]]:
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

    with path.open("r", encoding=encoding, newline="") as f:
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


def parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def parse_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def add_reason(selected: Dict[str, List[str]], record_id: str, reason: str) -> None:
    if reason not in selected[record_id]:
        selected[record_id].append(reason)


def sample_from_rows(rows: List[Dict[str, Any]], count: int, rng: random.Random, id_column: str) -> List[Dict[str, Any]]:
    if count <= 0 or not rows:
        return []
    keyed = sorted(rows, key=lambda row: str(row.get(id_column, "")))
    if len(keyed) <= count:
        return keyed
    return rng.sample(keyed, count)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("classifications", type=Path, help="Full classification CSV or JSONL")
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    parser.add_argument("--records", type=Path, help="records_normalized.jsonl or source records")
    parser.add_argument("--id-column", default="record_id")
    parser.add_argument("--l1-column", default="l1_code")
    parser.add_argument("--l2-column", default="l2_code")
    parser.add_argument("--l1-name-column", default="l1_name")
    parser.add_argument("--l2-name-column", default="l2_name")
    parser.add_argument("--confidence-column", default="confidence")
    parser.add_argument("--review-column", default="review_flag")
    parser.add_argument("--min-confidence", type=float, default=0.72)
    parser.add_argument("--per-l1", type=int, default=2, help="Extra stratified samples per L1")
    parser.add_argument("--high-frequency-rate", type=float, default=0.03)
    parser.add_argument("--high-frequency-min-count", type=int, default=20)
    parser.add_argument("--random-size", type=int, default=0, help="Extra random records after risk and strata")
    parser.add_argument("--fallback-pattern", default=r"其他|其它|需复核|待复核|兜底|unknown|other")
    parser.add_argument("--seed", type=int, default=20260531)
    args = parser.parse_args()

    rows, columns = read_table(args.classifications)
    required = [args.id_column, args.l1_column, args.l2_column, args.confidence_column]
    missing_columns = [column for column in required if column not in columns]
    if missing_columns:
        raise SystemExit(f"Missing classification columns: {', '.join(missing_columns)}")

    source_by_id: Dict[str, Dict[str, Any]] = {}
    source_columns: List[str] = []
    if args.records:
        source_rows, source_columns = read_table(args.records, encoding="utf-8")
        source_id_column = "original_record_id" if "original_record_id" in source_columns else args.id_column
        source_by_id = {
            str(row.get(source_id_column, "")).strip(): row
            for row in source_rows
            if str(row.get(source_id_column, "")).strip()
        }

    rng = random.Random(args.seed)
    selected: Dict[str, List[str]] = defaultdict(list)
    fallback_re = re.compile(args.fallback_pattern, re.IGNORECASE)

    l1_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    l2_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        record_id = str(row.get(args.id_column, "")).strip()
        if not record_id:
            continue

        confidence = parse_float(row.get(args.confidence_column))
        l1 = str(row.get(args.l1_column, "")).strip()
        l2 = str(row.get(args.l2_column, "")).strip()
        l1_groups[l1].append(row)
        l2_groups[f"{l1}/{l2}"].append(row)

        if args.review_column in row and parse_bool(row.get(args.review_column)):
            add_reason(selected, record_id, "review_flag")
        if confidence is None or confidence < args.min_confidence:
            add_reason(selected, record_id, "low_confidence")

        fallback_text = " ".join(
            str(row.get(column, ""))
            for column in [args.l1_column, args.l1_name_column, args.l2_column, args.l2_name_column]
            if column in row
        )
        if fallback_re.search(fallback_text):
            add_reason(selected, record_id, "fallback_label")

    for l2_key, group_rows in sorted(l2_groups.items()):
        if len(group_rows) < args.high_frequency_min_count:
            continue
        count = max(1, int(round(len(group_rows) * args.high_frequency_rate)))
        for row in sample_from_rows(group_rows, count, rng, args.id_column):
            add_reason(selected, str(row.get(args.id_column, "")).strip(), f"high_frequency_l2:{l2_key}")

    for l1, group_rows in sorted(l1_groups.items()):
        candidates = [row for row in group_rows if str(row.get(args.id_column, "")).strip() not in selected]
        for row in sample_from_rows(candidates, args.per_l1, rng, args.id_column):
            add_reason(selected, str(row.get(args.id_column, "")).strip(), f"stratified_l1:{l1}")

    if args.random_size > 0:
        remaining = [row for row in rows if str(row.get(args.id_column, "")).strip() not in selected]
        for row in sample_from_rows(remaining, args.random_size, rng, args.id_column):
            add_reason(selected, str(row.get(args.id_column, "")).strip(), "random")

    selected_ids = set(selected.keys())
    output_rows: List[Dict[str, Any]] = []
    for row in rows:
        record_id = str(row.get(args.id_column, "")).strip()
        if record_id not in selected_ids:
            continue
        output_row = {"review_reasons": ";".join(selected[record_id]), **row}
        source = source_by_id.get(record_id, {})
        for column in source_columns:
            if column not in output_row:
                output_row[column] = source.get(column, "")
        output_rows.append(output_row)

    review_reason_counts: Counter[str] = Counter()
    for reasons in selected.values():
        review_reason_counts.update(reasons)

    report = {
        "classification_rows": len(rows),
        "review_sample_rows": len(output_rows),
        "review_sample_rate": round(len(output_rows) / len(rows), 6) if rows else 0,
        "min_confidence": args.min_confidence,
        "per_l1": args.per_l1,
        "high_frequency_rate": args.high_frequency_rate,
        "high_frequency_min_count": args.high_frequency_min_count,
        "random_size": args.random_size,
        "review_reason_counts": review_reason_counts.most_common(),
        "l1_counts": Counter(str(row.get(args.l1_column, "")).strip() for row in rows).most_common(),
        "l2_counts_top": Counter(
            f"{str(row.get(args.l1_column, '')).strip()}/{str(row.get(args.l2_column, '')).strip()}"
            for row in rows
        ).most_common(30),
    }

    output_fields = ["review_reasons"]
    output_fields.extend(column for column in columns if column not in output_fields)
    for column in source_columns:
        if column not in output_fields:
            output_fields.append(column)

    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(args.out / "review_sample.csv", output_rows, output_fields)
    (args.out / "review_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"review_sample_rows": len(output_rows), "output": str(args.out)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Prepare issue records for high-throughput taxonomy classification."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


FIELD_ALIASES = {
    "record_id": ["record_id", "id", "ID", "编号", "序号", "工单编号", "问题ID", "问题编号"],
    "overview": ["overview", "issue_overview", "summary", "title", "标题", "问题概述", "概述"],
    "detail": ["detail", "issue_detail", "description", "desc", "问题明细", "问题详情", "详情", "明细"],
    "root_cause": ["root_cause", "cause", "reason", "问题根因", "根因", "原因", "故障原因"],
    "solution": ["solution", "resolution", "fix", "处理方案", "解决方案", "解决办法", "处置方案"],
}


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def read_rows(path: Path, encoding: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        rows = []
        with path.open("r", encoding=encoding) as f:
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
            raise SystemExit("CSV input has no header row")
        return list(reader), list(reader.fieldnames)


def pick_column(columns: List[str], canonical: str, explicit: Optional[str]) -> Optional[str]:
    if explicit:
        if explicit not in columns:
            raise SystemExit(f"Column not found for {canonical}: {explicit}")
        return explicit
    lower_map = {column.lower(): column for column in columns}
    for alias in FIELD_ALIASES[canonical]:
        if alias in columns:
            return alias
        if alias.lower() in lower_map:
            return lower_map[alias.lower()]
    return None


def combined_text(record: Dict[str, str]) -> str:
    parts = [
        ("问题概述", record["overview"]),
        ("问题明细", record["detail"]),
        ("问题根因", record["root_cause"]),
        ("解决方案", record["solution"]),
    ]
    return "\n".join(f"{name}: {value}" for name, value in parts if value)


def cache_key(record: Dict[str, str]) -> str:
    payload = json.dumps(
        {
            "overview": record["overview"],
            "detail": record["detail"],
            "root_cause": record["root_cause"],
            "solution": record["solution"],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_sample(records: List[Dict[str, Any]], sample_size: int, seed: int) -> List[Dict[str, Any]]:
    if sample_size <= 0 or len(records) <= sample_size:
        return records

    buckets: Dict[str, List[Dict[str, Any]]] = {
        "short": [],
        "medium": [],
        "long": [],
        "missing_root_cause": [],
    }
    for record in records:
        text_len = len(record["combined_text"])
        if not record["root_cause"]:
            buckets["missing_root_cause"].append(record)
        elif text_len < 120:
            buckets["short"].append(record)
        elif text_len < 600:
            buckets["medium"].append(record)
        else:
            buckets["long"].append(record)

    rng = random.Random(seed)
    sample: List[Dict[str, Any]] = []
    nonempty = [items for items in buckets.values() if items]
    base_take = max(1, sample_size // max(1, len(nonempty)))
    for items in nonempty:
        keyed = sorted(items, key=lambda row: row["cache_key"])
        rng.shuffle(keyed)
        sample.extend(keyed[:base_take])

    if len(sample) < sample_size:
        chosen = {record["unique_record_id"] for record in sample}
        remaining = [record for record in records if record["unique_record_id"] not in chosen]
        remaining = sorted(remaining, key=lambda row: row["cache_key"])
        rng.shuffle(remaining)
        sample.extend(remaining[: sample_size - len(sample)])

    return sorted(sample[:sample_size], key=lambda row: row["unique_record_id"])


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def write_batches(out_dir: Path, records: List[Dict[str, Any]], batch_size: int) -> int:
    batches_dir = out_dir / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)
    batch_count = 0
    for index in range(0, len(records), batch_size):
        batch_count += 1
        batch_records = records[index : index + batch_size]
        payload = {
            "batch_id": f"batch_{batch_count:05d}",
            "records": [
                {
                    "record_id": row["unique_record_id"],
                    "overview": row["overview"],
                    "detail": row["detail"],
                    "root_cause": row["root_cause"],
                    "solution": row["solution"],
                }
                for row in batch_records
            ],
        }
        path = batches_dir / f"batch_{batch_count:05d}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return batch_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Input CSV or JSONL file")
    parser.add_argument("--out", type=Path, required=True, help="Output working directory")
    parser.add_argument("--encoding", default="utf-8-sig", help="Input file encoding")
    parser.add_argument("--id-column")
    parser.add_argument("--overview-column")
    parser.add_argument("--detail-column")
    parser.add_argument("--root-cause-column")
    parser.add_argument("--solution-column")
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--sample-size", type=int, default=800)
    parser.add_argument("--seed", type=int, default=20260531)
    args = parser.parse_args()

    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")

    rows, columns = read_rows(args.input, args.encoding)
    if not rows:
        raise SystemExit("Input contains no records")

    column_map = {
        "record_id": pick_column(columns, "record_id", args.id_column),
        "overview": pick_column(columns, "overview", args.overview_column),
        "detail": pick_column(columns, "detail", args.detail_column),
        "root_cause": pick_column(columns, "root_cause", args.root_cause_column),
        "solution": pick_column(columns, "solution", args.solution_column),
    }

    missing_required = [name for name in ["overview", "detail", "root_cause", "solution"] if not column_map[name]]
    if missing_required:
        raise SystemExit(f"Missing required columns: {', '.join(missing_required)}")

    args.out.mkdir(parents=True, exist_ok=True)

    normalized: List[Dict[str, Any]] = []
    unique_by_key: Dict[str, Dict[str, Any]] = {}
    duplicate_map: List[Dict[str, Any]] = []

    unique_counter = 0
    for row_index, row in enumerate(rows, 1):
        original_id = normalize_text(row.get(column_map["record_id"], "")) if column_map["record_id"] else ""
        if not original_id:
            original_id = f"row_{row_index:06d}"

        record = {
            "original_record_id": original_id,
            "overview": normalize_text(row.get(column_map["overview"], "")),
            "detail": normalize_text(row.get(column_map["detail"], "")),
            "root_cause": normalize_text(row.get(column_map["root_cause"], "")),
            "solution": normalize_text(row.get(column_map["solution"], "")),
        }
        record["combined_text"] = combined_text(record)
        record["cache_key"] = cache_key(record)

        if record["cache_key"] not in unique_by_key:
            unique_counter += 1
            unique_id = f"u_{unique_counter:06d}"
            unique_record = {
                "unique_record_id": unique_id,
                **record,
                "duplicate_count": 0,
                "text_length": len(record["combined_text"]),
            }
            unique_by_key[record["cache_key"]] = unique_record
        unique_record = unique_by_key[record["cache_key"]]
        unique_record["duplicate_count"] += 1

        normalized_row = {**record, "unique_record_id": unique_record["unique_record_id"]}
        normalized.append(normalized_row)
        duplicate_map.append(
            {
                "original_record_id": original_id,
                "unique_record_id": unique_record["unique_record_id"],
                "cache_key": record["cache_key"],
                "is_representative": "true" if unique_record["duplicate_count"] == 1 else "false",
            }
        )

    unique_records = sorted(unique_by_key.values(), key=lambda row: row["unique_record_id"])
    sample = stable_sample(unique_records, args.sample_size, args.seed)
    batch_count = write_batches(args.out, unique_records, args.batch_size)

    write_jsonl(args.out / "records_normalized.jsonl", normalized)
    write_jsonl(args.out / "records_unique.jsonl", unique_records)
    write_csv(
        args.out / "duplicate_map.csv",
        duplicate_map,
        ["original_record_id", "unique_record_id", "cache_key", "is_representative"],
    )
    write_csv(
        args.out / "sample_for_taxonomy.csv",
        sample,
        [
            "unique_record_id",
            "overview",
            "detail",
            "root_cause",
            "solution",
            "duplicate_count",
            "text_length",
        ],
    )

    manifest = {
        "input": str(args.input),
        "column_map": column_map,
        "total_records": len(normalized),
        "unique_records": len(unique_records),
        "duplicates_removed": len(normalized) - len(unique_records),
        "sample_records": len(sample),
        "batch_size": args.batch_size,
        "batch_count": batch_count,
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

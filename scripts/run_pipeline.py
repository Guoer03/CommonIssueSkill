#!/usr/bin/env python3
"""Run the two-stage issue classification pipeline."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from pipeline_core import (
    ContractError,
    derive_final_status,
    enrich_candidate_pool,
    normalize_name,
    parse_final_xml,
    parse_topk_xml,
    select_rag_for_stages,
    validate_final_selection,
)


SKILL_DIR = Path(__file__).resolve().parents[1]
REFERENCE_DIR = SKILL_DIR / "references"
DEFAULT_CLASSIFICATION_OPTIONS = REFERENCE_DIR / "classification_options.json"

FIELD_ALIASES = {
    "problem_overview": ["problem_overview", "问题概述", "overview", "summary", "title"],
    "probelm_details": ["probelm_details", "problem_details", "问题明细", "detail", "details", "description", "desc"],
    "solution_details": ["solution_details", "解决方案", "solution", "resolution", "fix"],
}

RECORD_SCHEMA_FIELDS = ["id", "problem_overview", "probelm_details", "solution_details", "user_solution"]

EXPORT_COLUMNS = [
    "record_index",
    "record_id",
    "selected_level_1",
    "selected_level_2",
    "confidence",
    "status",
    "needs_review",
    "mapping_justification",
    "topk_candidates",
]


class RunnerError(RuntimeError):
    pass


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON file {path}: {exc}") from exc


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def connect(workdir: Path) -> sqlite3.Connection:
    path = workdir / "state.sqlite"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists records (
            record_index integer primary key,
            record_id text,
            record_text text not null,
            rag_pool_json text not null default '[]',
            topk_status text not null default 'pending',
            final_status text not null default 'pending',
            needs_review integer not null default 0,
            error text
        );

        create table if not exists topk_results (
            record_index integer primary key,
            raw_xml text not null,
            candidates_json text not null,
            created_at real not null
        );

        create table if not exists final_results (
            record_index integer primary key,
            raw_xml text not null,
            selected_level_1 text,
            selected_level_2 text,
            confidence real not null,
            mapping_justification text not null,
            status text not null,
            needs_review integer not null,
            created_at real not null
        );
        """
    )
    conn.commit()


def read_rows(path: Path) -> List[Dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        rows: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"Invalid JSONL at line {line_number}: {exc}") from exc
                if not isinstance(value, dict):
                    raise SystemExit(f"JSONL line {line_number} must be an object")
                rows.append(value)
        return rows
    if suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            if not reader.fieldnames:
                raise SystemExit(f"Input table has no header: {path}")
            return list(reader)
    raise SystemExit(f"Unsupported input file: {path}")


def first_value(row: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        if key in row and str(row.get(key, "")).strip():
            return str(row.get(key, "")).strip()
    return ""


def merge_user_solution(record: Mapping[str, str]) -> str:
    parts = []
    for field in ["problem_overview", "probelm_details", "solution_details"]:
        value = str(record.get(field, "")).strip()
        if value:
            parts.append(f"{field}: {value}")
    return "\n".join(parts)


def build_record_payload(row: Mapping[str, Any], record_column: Optional[str], record_id: str = "") -> Dict[str, str]:
    if record_column:
        value = str(row.get(record_column, "")).strip()
        if value:
            return {
                "id": record_id,
                "problem_overview": "",
                "probelm_details": "",
                "solution_details": "",
                "user_solution": value,
            }

    record = {
        "problem_overview": first_value(row, FIELD_ALIASES["problem_overview"]),
        "probelm_details": first_value(row, FIELD_ALIASES["probelm_details"]),
        "solution_details": first_value(row, FIELD_ALIASES["solution_details"]),
    }
    user_solution = merge_user_solution(record)
    if not user_solution:
        user_solution = "\n".join(f"{key}: {value}" for key, value in row.items() if str(value).strip())
    return {
        "id": record_id,
        "problem_overview": record["problem_overview"],
        "probelm_details": record["probelm_details"],
        "solution_details": record["solution_details"],
        "user_solution": user_solution,
    }


def load_record_payload(value: str) -> Dict[str, str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return {field: str(parsed.get(field, "")).strip() for field in RECORD_SCHEMA_FIELDS}
    return {
        "id": "",
        "problem_overview": "",
        "probelm_details": "",
        "solution_details": "",
        "user_solution": value,
    }


def load_rag_map(path: Optional[Path]) -> Dict[str, List[Dict[str, Any]]]:
    if not path:
        return {}
    rows = read_rows(path)
    rag_map: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        record_id = str(row.get("record_id", row.get("id", ""))).strip()
        if not record_id:
            continue
        if "rag_pool" in row:
            try:
                rag_map[record_id] = json.loads(str(row["rag_pool"]))
            except json.JSONDecodeError:
                rag_map[record_id] = []
        else:
            rag_map.setdefault(record_id, []).append(dict(row))
    return rag_map


def command_init(args: argparse.Namespace) -> None:
    workdir = args.workdir
    workdir.mkdir(parents=True, exist_ok=True)
    options = read_json(args.classification_options)
    if not isinstance(options, dict) or not options:
        raise SystemExit(
            "classification_options must be a non-empty JSON object. "
            "Fill references/classification_options.json or pass --classification-options."
        )
    write_json(workdir / "classification_options.json", options)

    rows = read_rows(args.input)
    rag_map = load_rag_map(args.rag_jsonl)
    conn = connect(workdir)
    create_schema(conn)
    with conn:
        conn.execute("delete from records")
        conn.execute("delete from topk_results")
        conn.execute("delete from final_results")
        for index, row in enumerate(rows):
            record_id = str(row.get("record_id") or row.get("id") or index).strip()
            record_payload = build_record_payload(row, args.record_column, record_id=record_id)
            conn.execute(
                """
                insert into records(record_index, record_id, record_text, rag_pool_json)
                values (?, ?, ?, ?)
                """,
                (
                    index,
                    record_id,
                    json.dumps(record_payload, ensure_ascii=False),
                    json.dumps(rag_map.get(record_id, []), ensure_ascii=False),
                ),
            )
    manifest = {
        "input": str(args.input),
        "classification_options": str(args.classification_options),
        "record_count": len(rows),
        "batch_size": args.batch_size,
        "mode": "workspace",
    }
    write_json(workdir / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


def normalize_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def api_key(env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if not value:
        raise SystemExit(f"Missing API key env var: {env_name}")
    return value


def call_chat_api(
    endpoint: str,
    key: str,
    model: str,
    prompt: str,
    timeout: float,
    temperature: float,
    max_tokens: int,
) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RunnerError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RunnerError(f"API request failed: {exc}") from exc
    try:
        data = json.loads(body)
        return str(data["choices"][0]["message"]["content"])
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise RunnerError(f"Unexpected API response: {body[:1000]}") from exc


def run_with_retries(
    label: str,
    max_retries: int,
    operation: Any,
    sleep_seconds: float = 1.0,
) -> Any:
    attempts = max(0, max_retries) + 1
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= attempts:
                raise
            wait = min(sleep_seconds * (2 ** (attempt - 1)), 8)
            print(f"{label} attempt {attempt} failed: {exc}; retrying in {wait:.1f}s", file=sys.stderr)
            if wait > 0:
                time.sleep(wait)
    raise RunnerError(f"{label} retry loop exhausted")


def load_template(name: str) -> str:
    path = REFERENCE_DIR / name
    return path.read_text(encoding="utf-8")


def load_prompt_parts(names: Sequence[str]) -> str:
    return "\n\n".join(load_template(name).strip() for name in names)


def load_topk_prompt_template() -> str:
    return load_prompt_parts(["topk_prompt.md", "topk_io_contract.md"])


def pending_topk_records(conn: sqlite3.Connection, limit: int) -> List[sqlite3.Row]:
    return conn.execute(
        """
        select record_index, record_id, record_text, rag_pool_json
        from records
        where topk_status in ('pending', 'failed')
        order by record_index
        limit ?
        """,
        (limit,),
    ).fetchall()


def pending_final_records(conn: sqlite3.Connection, limit: int) -> List[sqlite3.Row]:
    return conn.execute(
        """
        select r.record_index, r.record_id, r.record_text, r.rag_pool_json, t.candidates_json
        from records r
        join topk_results t on t.record_index = r.record_index
        where r.final_status in ('pending', 'failed') and r.topk_status = 'succeeded'
        order by r.record_index
        limit ?
        """,
        (limit,),
    ).fetchall()


def batch(items: Sequence[Any], size: int) -> Iterable[List[Any]]:
    for index in range(0, len(items), size):
        yield list(items[index : index + size])


def dynamic_candidate_pool(candidates: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted((dict(item) for item in candidates), key=lambda row: float(row.get("confidence", 0)), reverse=True)
    if not ordered:
        return []
    top1 = float(ordered[0].get("confidence", 0))
    top2 = float(ordered[1].get("confidence", 0)) if len(ordered) > 1 else 0.0
    keep = 3 if top1 >= 0.85 and top1 - top2 >= 0.15 else 6
    return ordered[:keep]


def candidate_pool_for_final(candidates: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "level_1": str(candidate.get("level_1", "")).strip(),
            "level_2": str(candidate.get("level_2", "")).strip(),
            "inline_features": list(candidate.get("inline_features", [])),
        }
        for candidate in candidates
    ]


def command_topk(args: argparse.Namespace) -> None:
    options = read_json(args.workdir / "classification_options.json")
    conn = connect(args.workdir)
    create_schema(conn)
    rows = pending_topk_records(conn, args.limit)
    if not rows:
        print("No pending topk records")
        return

    template = load_topk_prompt_template()
    endpoint = normalize_endpoint(args.base_url)
    key = api_key(args.api_key_env)
    processed = 0
    for group in batch(rows, args.batch_size):
        payload = {
            "records": [load_record_payload(row["record_text"]) for row in group],
            "classification_options": options,
            "rag_results": {
                str(offset): json.loads(row["rag_pool_json"])[: args.topk_rag_k]
                for offset, row in enumerate(group)
            },
        }
        prompt = template + "\n\n当前输入：\n" + json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            def run_topk_batch() -> int:
                raw_xml = call_chat_api(endpoint, key, args.model, prompt, args.timeout, args.temperature, args.max_tokens)
                parsed = parse_topk_xml(raw_xml)
                by_index = {row["record_index"]: row for row in parsed}
                with conn:
                    for offset, source_row in enumerate(group):
                        parsed_row = by_index.get(offset)
                        if parsed_row is None:
                            raise ContractError(f"topk output missing record_index {offset}")
                        candidates = enrich_candidate_pool(parsed_row["candidates"], options)
                        topk_status = "unresolved" if not candidates else "succeeded"
                        conn.execute(
                            "replace into topk_results(record_index, raw_xml, candidates_json, created_at) values (?, ?, ?, ?)",
                            (
                                source_row["record_index"],
                                raw_xml,
                                json.dumps(candidates, ensure_ascii=False),
                                time.time(),
                            ),
                        )
                        conn.execute(
                            "update records set topk_status = ?, final_status = case when ? = 'unresolved' then 'unresolved' else final_status end, needs_review = case when ? = 'unresolved' then 1 else needs_review end, error = null where record_index = ?",
                            (topk_status, topk_status, topk_status, source_row["record_index"]),
                        )
                return len(group)

            processed += run_with_retries("topk batch", args.max_retries, run_topk_batch)
            print(f"topk processed {processed}/{len(rows)}", file=sys.stderr)
        except Exception as exc:
            with conn:
                for source_row in group:
                    conn.execute(
                        "update records set topk_status = 'failed', error = ? where record_index = ?",
                        (str(exc), source_row["record_index"]),
                    )
            if not args.continue_on_failure:
                raise
            print(f"topk failed batch: {exc}", file=sys.stderr)


def command_final(args: argparse.Namespace) -> None:
    conn = connect(args.workdir)
    create_schema(conn)
    rows = pending_final_records(conn, args.limit)
    if not rows:
        print("No pending final records")
        return

    template = load_template("final_prompt.md")
    endpoint = normalize_endpoint(args.base_url)
    key = api_key(args.api_key_env)
    processed = 0
    for group in batch(rows, args.batch_size):
        final_inputs = []
        source_by_offset: Dict[int, sqlite3.Row] = {}
        candidate_by_offset: Dict[int, List[Dict[str, Any]]] = {}
        for offset, row in enumerate(group):
            candidates = dynamic_candidate_pool(json.loads(row["candidates_json"]))
            rag_pool = json.loads(row["rag_pool_json"])
            rag = select_rag_for_stages(rag_pool, candidates, topk_k=args.topk_rag_k, final_k=args.final_rag_k)
            source_by_offset[offset] = row
            candidate_by_offset[offset] = candidates
            final_inputs.append(
                {
                    "record_index": offset,
                    "record": load_record_payload(row["record_text"]),
                    "candidate_pool": candidate_pool_for_final(candidates),
                    "rag_results": rag["final_rag"],
                }
            )
        prompt = template + "\n\n当前输入：\n" + json.dumps({"items": final_inputs}, ensure_ascii=False, indent=2)
        try:
            def run_final_batch() -> int:
                raw_xml = call_chat_api(endpoint, key, args.model, prompt, args.timeout, args.temperature, args.max_tokens)
                parsed = parse_final_xml(raw_xml)
                by_index = {row["record_index"]: row for row in parsed}
                with conn:
                    for offset, source_row in source_by_offset.items():
                        final_row = by_index.get(offset)
                        if final_row is None:
                            raise ContractError(f"final output missing record_index {offset}")
                        validated = validate_final_selection(final_row, candidate_by_offset[offset])
                        status = derive_final_status(candidate_by_offset[offset], validated, args.review_threshold)
                        conn.execute(
                            """
                            replace into final_results(
                                record_index, raw_xml, selected_level_1, selected_level_2, confidence,
                                mapping_justification, status, needs_review, created_at
                            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                source_row["record_index"],
                                raw_xml,
                                validated.get("selected_level_1", ""),
                                validated.get("selected_level_2", ""),
                                float(validated.get("confidence", 0)),
                                validated.get("mapping_justification", ""),
                                status["status"],
                                int(status["needs_review"]),
                                time.time(),
                            ),
                        )
                        conn.execute(
                            "update records set final_status = ?, needs_review = ?, error = null where record_index = ?",
                            (status["status"], int(status["needs_review"]), source_row["record_index"]),
                        )
                return len(group)

            processed += run_with_retries("final batch", args.max_retries, run_final_batch)
            print(f"final processed {processed}/{len(rows)}", file=sys.stderr)
        except Exception as exc:
            with conn:
                for source_row in group:
                    conn.execute(
                        "update records set final_status = 'failed', needs_review = 1, error = ? where record_index = ?",
                        (str(exc), source_row["record_index"]),
                    )
            if not args.continue_on_failure:
                raise
            print(f"final failed batch: {exc}", file=sys.stderr)


def command_export(args: argparse.Namespace) -> None:
    conn = connect(args.workdir)
    rows = conn.execute(
        """
        select r.record_index, r.record_id, f.selected_level_1, f.selected_level_2,
               f.confidence, coalesce(f.status, r.final_status) as status,
               r.needs_review, f.mapping_justification, t.candidates_json
        from records r
        left join final_results f on f.record_index = r.record_index
        left join topk_results t on t.record_index = r.record_index
        order by r.record_index
        """
    ).fetchall()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "record_index": row["record_index"],
                    "record_id": row["record_id"],
                    "selected_level_1": row["selected_level_1"] or "",
                    "selected_level_2": row["selected_level_2"] or "",
                    "confidence": row["confidence"] if row["confidence"] is not None else "",
                    "status": row["status"] or "",
                    "needs_review": "true" if row["needs_review"] else "false",
                    "mapping_justification": row["mapping_justification"] or "",
                    "topk_candidates": row["candidates_json"] or "[]",
                }
            )
    print(str(args.out))


def add_api_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=os.environ.get("ISSUE_CLASSIFIER_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--model", default=os.environ.get("ISSUE_CLASSIFIER_MODEL"), required=False)
    parser.add_argument("--api-key-env", default="ISSUE_CLASSIFIER_API_KEY")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--topk-rag-k", type=int, default=3)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--continue-on-failure", action="store_true")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a stateful run workspace")
    init_parser.add_argument("--input", type=Path, required=True)
    init_parser.add_argument("--classification-options", type=Path, default=DEFAULT_CLASSIFICATION_OPTIONS)
    init_parser.add_argument("--workdir", type=Path, required=True)
    init_parser.add_argument("--record-column")
    init_parser.add_argument("--rag-jsonl", type=Path)
    init_parser.add_argument("--batch-size", type=int, default=20)
    init_parser.set_defaults(func=command_init)

    topk_parser = subparsers.add_parser("topk", help="Run topk stage")
    topk_parser.add_argument("--workdir", type=Path, required=True)
    add_api_args(topk_parser)
    topk_parser.set_defaults(func=command_topk)

    final_parser = subparsers.add_parser("final", help="Run final stage")
    final_parser.add_argument("--workdir", type=Path, required=True)
    final_parser.add_argument("--final-rag-k", type=int, default=5)
    final_parser.add_argument("--review-threshold", type=float, default=0.72)
    add_api_args(final_parser)
    final_parser.set_defaults(func=command_final)

    export_parser = subparsers.add_parser("export", help="Export final CSV")
    export_parser.add_argument("--workdir", type=Path, required=True)
    export_parser.add_argument("--out", type=Path, required=True)
    export_parser.set_defaults(func=command_export)

    args = parser.parse_args()
    if args.command in {"topk", "final"} and not args.model:
        raise SystemExit("Missing --model or ISSUE_CLASSIFIER_MODEL")
    args.func(args)


if __name__ == "__main__":
    main()

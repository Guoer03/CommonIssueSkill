#!/usr/bin/env python3
"""Core contract helpers for the issue classification pipeline."""

from __future__ import annotations

import copy
import re
import unicodedata
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Mapping, Sequence, Tuple


class ContractError(ValueError):
    """Raised when model output violates the pipeline contract."""


def normalize_name(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("／", "/").replace("\\", "/")
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_xml(text: str) -> ET.Element:
    stripped = text.strip()
    if not stripped:
        raise ContractError("empty XML output")
    try:
        return ET.fromstring(stripped)
    except ET.ParseError as exc:
        raise ContractError(f"invalid XML output: {exc}") from exc


def _result_nodes(root: ET.Element) -> List[ET.Element]:
    if root.tag == "result":
        return [root]
    if root.tag == "results":
        return list(root.findall("result"))
    raise ContractError(f"expected <result> or <results>, got <{root.tag}>")


def _text(node: ET.Element, path: str, default: str = "") -> str:
    child = node.find(path)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def _required_text(node: ET.Element, path: str) -> str:
    value = _text(node, path)
    if not value:
        raise ContractError(f"missing <{path}>")
    return value


def _parse_record_index(node: ET.Element) -> int:
    raw = _required_text(node, "record_index")
    try:
        index = int(raw)
    except ValueError as exc:
        raise ContractError(f"invalid record_index: {raw}") from exc
    if index < 0:
        raise ContractError(f"record_index must be non-negative: {index}")
    return index


def _parse_confidence(value: Any, default: float | None = None) -> float:
    text = "" if value is None else str(value).strip()
    if not text:
        if default is None:
            raise ContractError("missing confidence")
        return default
    try:
        confidence = float(text)
    except ValueError as exc:
        raise ContractError(f"invalid confidence: {text}") from exc
    if confidence < 0 or confidence > 1:
        raise ContractError(f"confidence out of range: {confidence}")
    return confidence


def parse_topk_xml(text: str) -> List[Dict[str, Any]]:
    root = _parse_xml(text)
    rows: List[Dict[str, Any]] = []
    for result in _result_nodes(root):
        candidates_node = result.find("candidates")
        candidates: List[Dict[str, Any]] = []
        if candidates_node is not None:
            for candidate in candidates_node.findall("candidate"):
                level_1 = normalize_name(_required_text(candidate, "level_1"))
                level_2 = normalize_name(_required_text(candidate, "level_2"))
                candidates.append(
                    {
                        "level_1": level_1,
                        "level_2": level_2,
                        "confidence": _parse_confidence(_text(candidate, "confidence"), default=0.0),
                    }
                )
        rows.append(
            {
                "record_index": _parse_record_index(result),
                "thinking_process": _text(result, "thinking_process"),
                "candidates": candidates,
            }
        )
    return rows


def parse_final_xml(text: str) -> List[Dict[str, Any]]:
    root = _parse_xml(text)
    rows: List[Dict[str, Any]] = []
    for result in _result_nodes(root):
        rows.append(
            {
                "record_index": _parse_record_index(result),
                "thinking_process": _text(result, "thinking_process"),
                "selected_level_1": normalize_name(_text(result, "selected_level_1")),
                "selected_level_2": normalize_name(_text(result, "selected_level_2")),
                "mapping_justification": _required_text(result, "mapping_justification"),
                "confidence": _parse_confidence(_required_text(result, "confidence")),
            }
        )
    return rows


def classification_index(
    classification_options: Mapping[str, Mapping[str, Sequence[str]]]
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for level_1, level_2_map in classification_options.items():
        canonical_l1 = normalize_name(level_1)
        for level_2, level_3_values in level_2_map.items():
            canonical_l2 = normalize_name(level_2)
            index[(normalize_name(canonical_l1), normalize_name(canonical_l2))] = {
                "level_1": canonical_l1,
                "level_2": canonical_l2,
                "inline_features": [normalize_name(item) for item in level_3_values],
            }
    return index


def enrich_candidate_pool(
    candidates: Sequence[Mapping[str, Any]],
    classification_options: Mapping[str, Mapping[str, Sequence[str]]],
) -> List[Dict[str, Any]]:
    index = classification_index(classification_options)
    enriched: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for candidate in candidates:
        key = (normalize_name(candidate.get("level_1")), normalize_name(candidate.get("level_2")))
        if key not in index:
            raise ContractError(f"candidate {key[0]}/{key[1]} not in classification_options")
        if key in seen:
            continue
        seen.add(key)
        item = dict(index[key])
        item["confidence"] = _parse_confidence(candidate.get("confidence"), default=0.0)
        enriched.append(item)
    return enriched


def _candidate_key(row: Mapping[str, Any]) -> Tuple[str, str]:
    return (normalize_name(row.get("level_1")), normalize_name(row.get("level_2")))


def validate_final_selection(
    final_row: Mapping[str, Any],
    candidate_pool: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    selected_l1 = normalize_name(final_row.get("selected_level_1"))
    selected_l2 = normalize_name(final_row.get("selected_level_2"))
    normalized = dict(final_row)
    normalized["selected_level_1"] = selected_l1
    normalized["selected_level_2"] = selected_l2
    normalized["confidence"] = _parse_confidence(final_row.get("confidence"))

    if not selected_l1 and not selected_l2:
        return normalized
    candidate_map = {_candidate_key(candidate): candidate for candidate in candidate_pool}
    key = (selected_l1, selected_l2)
    if key not in candidate_map:
        raise ContractError(f"final selection {selected_l1}/{selected_l2} not in candidate pool")
    canonical = candidate_map[key]
    normalized["selected_level_1"] = normalize_name(canonical.get("level_1"))
    normalized["selected_level_2"] = normalize_name(canonical.get("level_2"))
    return normalized


def derive_final_status(
    candidate_pool: Sequence[Mapping[str, Any]],
    final_row: Mapping[str, Any] | None,
    review_threshold: float,
) -> Dict[str, Any]:
    if not candidate_pool:
        return {"status": "unresolved", "needs_review": True, "reason": "topk_empty"}
    if final_row is None:
        return {"status": "unresolved", "needs_review": True, "reason": "final_missing"}
    selected_l1 = normalize_name(final_row.get("selected_level_1"))
    selected_l2 = normalize_name(final_row.get("selected_level_2"))
    if not selected_l1 or not selected_l2:
        return {"status": "unresolved", "needs_review": True, "reason": "final_unresolved"}
    confidence = _parse_confidence(final_row.get("confidence"))
    if confidence < review_threshold:
        return {"status": "low_confidence", "needs_review": True, "reason": "below_threshold"}
    return {"status": "classified", "needs_review": False, "reason": "ok"}


def _similarity(row: Mapping[str, Any]) -> float:
    try:
        return float(row.get("similarity", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _rag_key(row: Mapping[str, Any]) -> Tuple[str, str]:
    return (normalize_name(row.get("level_1")), normalize_name(row.get("level_2")))


def select_rag_for_stages(
    rag_pool: Sequence[Mapping[str, Any]],
    candidate_pool: Sequence[Mapping[str, Any]],
    topk_k: int = 3,
    final_k: int = 5,
) -> Dict[str, List[Dict[str, Any]]]:
    sorted_pool = sorted((dict(item) for item in rag_pool), key=_similarity, reverse=True)
    topk_rag = [copy.deepcopy(item) for item in sorted_pool[:topk_k]]

    candidate_keys = {_candidate_key(candidate) for candidate in candidate_pool}
    in_pool: List[Dict[str, Any]] = []
    out_pool: List[Dict[str, Any]] = []
    for item in sorted_pool:
        row = copy.deepcopy(item)
        if _rag_key(row) in candidate_keys:
            row["out_of_candidate_pool_reference"] = False
            in_pool.append(row)
        else:
            row["out_of_candidate_pool_reference"] = True
            out_pool.append(row)

    final_rag = (in_pool + out_pool)[:final_k]
    return {"topk_rag": topk_rag, "final_rag": final_rag}

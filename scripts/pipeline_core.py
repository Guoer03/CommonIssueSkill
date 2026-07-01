#!/usr/bin/env python3
"""Core contract helpers for the issue classification pipeline."""

from __future__ import annotations

import re
import unicodedata
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


def validate_taxonomy_selection(
    final_row: Mapping[str, Any],
    classification_options: Mapping[str, Mapping[str, Sequence[str]]],
) -> Dict[str, Any]:
    selected_l1 = normalize_name(final_row.get("selected_level_1"))
    selected_l2 = normalize_name(final_row.get("selected_level_2"))
    normalized = dict(final_row)
    normalized["selected_level_1"] = selected_l1
    normalized["selected_level_2"] = selected_l2
    normalized["confidence"] = _parse_confidence(final_row.get("confidence"))

    if not selected_l1 and not selected_l2:
        return normalized
    if not selected_l1 or not selected_l2:
        raise ContractError("selected_level_1 and selected_level_2 must both be present or both be empty")

    index = classification_index(classification_options)
    key = (selected_l1, selected_l2)
    if key not in index:
        raise ContractError(f"final selection {selected_l1}/{selected_l2} not in classification_options")
    canonical = index[key]
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

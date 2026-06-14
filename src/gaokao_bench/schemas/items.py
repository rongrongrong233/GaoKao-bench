from __future__ import annotations
from typing import Any


REQUIRED_TOP_LEVEL_KEYS = {
    "id",
    "year",
    "country",
    "paper",
    "subject",
    "question_number",
    "score",
    "question_type",
    "question",
    "answer",
    "grading",
    "source",
    "extraction",
}

QUALITY_EXTRACTION_METHODS = {"visual_two_layer_v1", "manual_transcription_v1"}


def validate_item(item: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = REQUIRED_TOP_LEVEL_KEYS - set(item)
    if missing:
        errors.append(f"missing top-level keys: {', '.join(sorted(missing))}")

    question = item.get("question")
    if not isinstance(question, dict):
        errors.append("question must be an object")
    else:
        if question.get("format") not in {"text", "html", "markdown"}:
            errors.append("question.format must be text, html, or markdown")
        if not isinstance(question.get("content"), str) or not question.get("content", "").strip():
            errors.append("question.content must be a non-empty string")
        if not isinstance(question.get("assets", []), list):
            errors.append("question.assets must be a list")

    answer = item.get("answer")
    if not isinstance(answer, dict):
        errors.append("answer must be an object")
    elif "standard" not in answer:
        errors.append("answer.standard is required, use null when not extracted yet")
    elif "acceptable" in answer and not isinstance(answer["acceptable"], list):
        errors.append("answer.acceptable must be a list when present")

    grading = item.get("grading")
    if not isinstance(grading, dict):
        errors.append("grading must be an object")
    else:
        if not grading.get("method"):
            errors.append("grading.method is required")
        if grading.get("answer_check") not in {"result_only", "solution_with_reasoning", None}:
            errors.append("grading.answer_check must be result_only or solution_with_reasoning when present")
        if not isinstance(grading.get("max_score"), (int, float)):
            errors.append("grading.max_score must be numeric")

    errors.extend(_validate_no_absolute_local_paths(item))

    return errors


def validate_quality(item: dict[str, Any], *, require_visual_extraction: bool, eval_ready: bool) -> list[str]:
    errors: list[str] = []
    question = item.get("question", {}) if isinstance(item.get("question"), dict) else {}
    answer = item.get("answer", {}) if isinstance(item.get("answer"), dict) else {}
    grading = item.get("grading", {}) if isinstance(item.get("grading"), dict) else {}
    extraction = item.get("extraction", {}) if isinstance(item.get("extraction"), dict) else {}

    if require_visual_extraction:
        if extraction.get("method") not in QUALITY_EXTRACTION_METHODS:
            methods = ", ".join(sorted(QUALITY_EXTRACTION_METHODS))
            errors.append(f"extraction.method must be one of: {methods}")

    if eval_ready:
        if not isinstance(item.get("score"), (int, float)) or item.get("score", 0) <= 0:
            errors.append("score must be positive for eval-ready data")
        if not isinstance(grading.get("max_score"), (int, float)) or grading.get("max_score", 0) <= 0:
            errors.append("grading.max_score must be positive for eval-ready data")
        standard = answer.get("standard")
        if not isinstance(standard, str) or not standard.strip():
            errors.append("answer.standard must be extracted for eval-ready data")
        if extraction.get("requires_manual_review") is True:
            errors.append("extraction.requires_manual_review must be false for eval-ready data")
        content = question.get("content")
        if isinstance(content, str) and "data-uncertain=\"true\"" in content:
            errors.append("question.content contains uncertain visual transcription")

    return errors


def _validate_no_absolute_local_paths(value: Any, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            errors.extend(_validate_no_absolute_local_paths(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_validate_no_absolute_local_paths(child, f"{path}[{index}]"))
    elif isinstance(value, str):
        if value.startswith("/") or value.startswith("file://") or "/Users/" in value:
            errors.append(f"{path} contains a local absolute path")
    return errors

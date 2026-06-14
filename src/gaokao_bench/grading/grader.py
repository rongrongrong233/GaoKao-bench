from __future__ import annotations

import re
from typing import Any


def normalize_choice(answer: str | None) -> str:
    if not answer:
        return ""
    letters = re.findall(r"[A-D]", answer.upper())
    return "".join(sorted(dict.fromkeys(letters)))


def normalize_text_answer(answer: str | None) -> str:
    if answer is None:
        return ""
    text = str(answer).strip().lower()
    replacements = {
        " ": "",
        "\n": "",
        "，": ",",
        "。": "",
        "；": ";",
        "（": "(",
        "）": ")",
        "＋": "+",
        "－": "-",
        "±": "+/-",
        "或": "/",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def grade_run(item: dict[str, Any], run: dict[str, Any], judge_grader: Any | None = None) -> dict[str, Any]:
    standard = item.get("answer", {}).get("standard")
    acceptable = item.get("answer", {}).get("acceptable") or [standard]
    max_score = item.get("grading", {}).get("max_score") or item.get("score") or 0
    extracted = run.get("extracted_answer")
    method = item.get("grading", {}).get("method")
    q_type = item.get("question_type")

    base = {
        "item_id": item["id"],
        "model": run.get("model_config", {}).get("model"),
        "grading_method": method,
        "standard_answer": standard,
        "extracted_answer": extracted,
        "max_score": max_score,
    }

    if standard in (None, ""):
        return {
            **base,
            "score": None,
            "verdict": "ungraded",
            "rationale": "No standard answer is available yet.",
        }

    if method == "exact_match":
        if q_type in {"single_choice", "multiple_choice"}:
            matched = any(normalize_choice(extracted) == normalize_choice(str(candidate)) for candidate in acceptable)
            rationale = "Choice answer exact-match after normalization."
        else:
            matched = any(normalize_text_answer(extracted) == normalize_text_answer(str(candidate)) for candidate in acceptable)
            rationale = "Text answer exact-match after normalization."
        score = max_score if matched else 0
        return {
            **base,
            "score": score,
            "verdict": "correct" if score == max_score else "incorrect",
            "rationale": rationale,
        }

    if method == "partial_choice":
        model_set = set(normalize_choice(extracted))
        standard_set = set(normalize_choice(str(standard)))
        if model_set == standard_set:
            score = max_score
            verdict = "correct"
        elif model_set and model_set < standard_set:
            score = max_score / 2
            verdict = "partially_correct"
        else:
            score = 0
            verdict = "incorrect"
        return {
            **base,
            "score": score,
            "verdict": verdict,
            "rationale": "Multiple-choice partial rule: exact set full, proper subset half, otherwise zero.",
        }

    if judge_grader is None:
        return {
            **base,
            "score": None,
            "verdict": "needs_judge_model",
            "rationale": "This item requires judge-model or manual grading with the official analysis.",
            "judge_inputs": {
                "answer_check": item.get("grading", {}).get("answer_check"),
                "official_analysis": item.get("answer", {}).get("analysis"),
            },
        }

    judge_result = judge_grader.grade(item, run)
    return {
        **base,
        "grading_method": "judge_model",
        **judge_result,
    }

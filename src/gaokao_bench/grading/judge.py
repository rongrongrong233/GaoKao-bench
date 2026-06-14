from __future__ import annotations

import json
import re
from typing import Any

from gaokao_bench.evaluation.runner import OpenAICompatibleClient, _message_text, config_to_dict


JUDGE_SYSTEM_PROMPT = (
    "你是高考测评裁判员。请根据题目、标准答案/解析和模型答案判分。"
    "必须只输出一个 JSON 对象，不要输出 Markdown，不要输出推理过程。"
    "JSON 字段必须为 score、verdict、rationale；rationale 不超过 80 个汉字。"
)


class JudgeModelGrader:
    def __init__(self, config: Any):
        self.config = config
        self.client = OpenAICompatibleClient(config)

    def grade(self, item: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        max_score = item.get("grading", {}).get("max_score") or item.get("score") or 0
        prompt = {
            "instruction": "请给模型答案打分，只返回 JSON：score, verdict, rationale。score 必须在 0 到 max_score 之间。",
            "verdict_values": ["correct", "partially_correct", "incorrect", "ungradable"],
            "max_score": max_score,
            "question": item.get("question", {}),
            "standard_answer": item.get("answer", {}).get("standard"),
            "acceptable_answers": item.get("answer", {}).get("acceptable") or [],
            "official_analysis": item.get("answer", {}).get("analysis"),
            "model_extracted_answer": run.get("extracted_answer"),
            "model_final_response": run.get("final_response_text"),
            "grading": item.get("grading", {}),
        }
        response = self.client.chat(
            [
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ]
        )
        text = _message_text(response)
        parsed = self._parse_json(text)
        score = parsed.get("score")
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = None
        if score is not None:
            score = max(0.0, min(float(max_score), score))
        verdict = parsed.get("verdict") or ("ungradable" if score is None else "partially_correct")
        return {
            "score": score,
            "verdict": verdict,
            "rationale": parsed.get("rationale") or text,
            "judge_model_config": config_to_dict(self.config),
            "judge_raw_response": response,
        }

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        text = text.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}

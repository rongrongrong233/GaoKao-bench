from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, is_dataclass
from typing import Any


SYSTEM_PROMPT = (
    "Solve the problem carefully. The final response must be placed in exactly one <answer></answer> tag. "
    "For single_choice and multiple_choice questions, output only the option letter(s). "
    "For fill_blank questions, output only the filled content. "
    "For short_answer and solution questions, include the necessary explanation, proof, or calculation steps."
)
CHECK_PROMPT = "Review your previous response. Output the final response for this question only, using exactly one <answer></answer> tag."


def extract_answer_tag(text: str) -> str | None:
    match = re.search(r"<answer>(.*?)</answer>", text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return match.group(1).strip()


def config_to_dict(config: Any) -> dict[str, Any]:
    if is_dataclass(config):
        data = asdict(config)
    elif isinstance(config, dict):
        data = dict(config)
    else:
        data = dict(vars(config))
    data.pop("root", None)
    return data


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith(("/v1", "/v3")):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


class OpenAICompatibleClient:
    def __init__(self, config: Any):
        self.config = config
        self.config_dict = config_to_dict(config)
        self.api_key = os.environ.get(config.api_key_env)
        if not self.api_key:
            raise RuntimeError(f"Missing API key env: {config.api_key_env}")

    def chat(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if getattr(self.config, "reasoning_effort", None):
            body["reasoning_effort"] = self.config.reasoning_effort
        if getattr(self.config, "reasoning", None):
            body["reasoning"] = self.config.reasoning
        body.update(getattr(self.config, "extra_body", {}) or {})

        request = urllib.request.Request(
            _endpoint(self.config.base_url),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return {
                    "ok": True,
                    "status": response.status,
                    "latency_seconds": round(time.time() - started, 3),
                    "payload": payload,
                }
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            return {
                "ok": False,
                "status": exc.code,
                "latency_seconds": round(time.time() - started, 3),
                "error": error_body,
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": None,
                "latency_seconds": round(time.time() - started, 3),
                "error": repr(exc),
            }


def _message_text(response: dict[str, Any]) -> str:
    if not response.get("ok"):
        return ""
    choices = response.get("payload", {}).get("choices", [])
    if not choices:
        return ""
    message = choices[0].get("message", {})
    content = message.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return str(choices[0].get("finish_reason") or "")
    return json.dumps(content, ensure_ascii=False)


def _assistant_message(response: dict[str, Any], fallback_content: str) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": fallback_content}
    if not response.get("ok"):
        return message
    choices = response.get("payload", {}).get("choices", [])
    if not choices:
        return message
    raw_message = choices[0].get("message", {})
    for key in ("reasoning", "reasoning_details"):
        if key in raw_message:
            message[key] = raw_message[key]
    return message


def run_item(item: dict[str, Any], config: Any) -> dict[str, Any]:
    client = OpenAICompatibleClient(config)
    question_type = item.get("question_type", "unknown")
    question_content = (
        f"Answer question {item['question_number']} only. Question type: {question_type}. "
        "If the text below includes shared context, neighboring questions, or surrounding material, "
        "use it only as context and do not answer any other question.\n\n"
        f"{item['question']['content']}"
    )
    first_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question_content},
    ]
    first_response = client.chat(first_messages)
    first_text = _message_text(first_response)

    final_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question_content},
        _assistant_message(first_response, first_text),
        {"role": "user", "content": CHECK_PROMPT},
    ]
    final_response = client.chat(final_messages)
    final_text = _message_text(final_response)

    return {
        "item_id": item["id"],
        "model_config": config_to_dict(config),
        "messages": {
            "system": SYSTEM_PROMPT,
            "first_user": question_content,
            "second_user": CHECK_PROMPT,
        },
        "first_response_text": first_text,
        "final_response_text": final_text,
        "extracted_answer": extract_answer_tag(final_text),
        "raw_responses": {
            "first": first_response,
            "final": final_response,
        },
    }

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModelConfig:
    name: str
    provider: str
    model: str
    base_url: str
    api_key_env: str
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout_seconds: int = 120
    reasoning_effort: str | None = None
    reasoning: dict[str, Any] | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


def load_model_configs(path: Path) -> list[ModelConfig]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("models", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("model config must be a list or an object with a 'models' list")
    return [ModelConfig(**row) for row in rows]

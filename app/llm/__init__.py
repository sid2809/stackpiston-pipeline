"""Switchable AI provider. Set LLM_PROVIDER (anthropic | openai) and LLM_MODEL in Railway."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass


class LLMError(RuntimeError):
    pass


class Truncated(LLMError):
    """The model hit its output limit, so the JSON is incomplete."""


@dataclass
class LLMResult:
    data: dict
    text: str
    input_tokens: int
    output_tokens: int
    model: str


def parse_json(text: str) -> dict:
    """Pull one JSON object out of a reply, tolerating ``` fences or stray prose."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise LLMError("Reply contained no JSON object.")
    try:
        d = json.loads(t[start:end + 1])
    except ValueError as e:
        raise LLMError(f"Reply was not valid JSON: {e}")
    if not isinstance(d, dict):
        raise LLMError("Reply JSON was not an object.")
    return d


def get_client():
    provider = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not model:
        raise LLMError("Missing setting LLM_MODEL. Add it in Railway > Variables.")
    if provider == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient(model)
    if provider == "openai":
        from .openai_client import OpenAIClient
        return OpenAIClient(model)
    raise LLMError(f"LLM_PROVIDER must be anthropic or openai, not '{provider}'.")

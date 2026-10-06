from __future__ import annotations

import os

import anthropic

from . import LLMError, LLMResult, Truncated, parse_json


class AnthropicClient:
    def __init__(self, model: str):
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise LLMError("Missing setting ANTHROPIC_API_KEY. Add it in Railway > Variables.")
        self.model = model
        self.c = anthropic.Anthropic(api_key=key, max_retries=3, timeout=600)

    def complete_json(self, system: str, user: str, max_tokens: int = 16000,
                      temperature: float | None = None) -> LLMResult:
        try:
            return self._complete(system, user, max_tokens, temperature)
        except LLMError as e:
            # Some models only accept their default temperature: retry once without it.
            if temperature is not None and "temperature" in str(e).lower():
                return self._complete(system, user, max_tokens, None)
            raise

    def _complete(self, system: str, user: str, max_tokens: int, temperature: float | None) -> LLMResult:
        try:
            kw = {"temperature": temperature} if temperature is not None else {}
            r = self.c.messages.create(model=self.model, max_tokens=max_tokens, system=system,
                                       messages=[{"role": "user", "content": user}], **kw)
        except anthropic.AuthenticationError:
            raise LLMError("Anthropic rejected the API key. Re-copy ANTHROPIC_API_KEY into Railway.")
        except anthropic.PermissionDeniedError as e:
            raise LLMError(f"Anthropic permission denied (key scope or workspace): {e}")
        except anthropic.NotFoundError:
            raise LLMError(f"Model '{self.model}' not found. Check LLM_MODEL in Railway.")
        except anthropic.RateLimitError:
            raise LLMError("Anthropic rate limit or spend limit reached.")
        except anthropic.BadRequestError as e:
            msg = str(e)
            if "credit balance" in msg.lower():
                raise LLMError("Anthropic credit balance is too low. Add credits in Console > Billing.")
            raise LLMError(f"Anthropic rejected the request: {msg[:300]}")
        except anthropic.APIConnectionError:
            raise LLMError("Could not reach Anthropic (network).")
        except anthropic.APIError as e:
            raise LLMError(f"Anthropic error: {str(e)[:300]}")
        text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        if r.stop_reason == "max_tokens":
            raise Truncated(f"Reply was cut off at {max_tokens} output tokens.")
        return LLMResult(parse_json(text), text, r.usage.input_tokens, r.usage.output_tokens, r.model)

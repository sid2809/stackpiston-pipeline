from __future__ import annotations

import os

import openai

from . import LLMError, LLMResult, Truncated, parse_json


class OpenAIClient:
    """Uses plain JSON mode; our own validator checks the structure (strict mode can't take our schema)."""

    def __init__(self, model: str):
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise LLMError("Missing setting OPENAI_API_KEY. Add it in Railway > Variables.")
        self.model = model
        self.c = openai.OpenAI(api_key=key, max_retries=3, timeout=600)

    def complete_json(self, system: str, user: str, max_tokens: int = 16000) -> LLMResult:
        try:
            r = self.c.chat.completions.create(
                model=self.model, max_completion_tokens=max_tokens,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        except openai.AuthenticationError:
            raise LLMError("OpenAI rejected the API key. Re-copy OPENAI_API_KEY into Railway.")
        except openai.PermissionDeniedError as e:
            raise LLMError(f"OpenAI permission denied (key, project or region): {str(e)[:300]}")
        except openai.NotFoundError:
            raise LLMError(f"Model '{self.model}' not found. Check LLM_MODEL in Railway.")
        except openai.RateLimitError:
            raise LLMError("OpenAI rate limit or quota reached.")
        except openai.BadRequestError as e:
            raise LLMError(f"OpenAI rejected the request: {str(e)[:300]}")
        except openai.APIConnectionError:
            raise LLMError("Could not reach OpenAI (network).")
        except openai.APIError as e:
            raise LLMError(f"OpenAI error: {str(e)[:300]}")
        choice = r.choices[0]
        if choice.finish_reason == "length":
            raise Truncated(f"Reply was cut off at {max_tokens} output tokens.")
        text = choice.message.content or ""
        return LLMResult(parse_json(text), text, r.usage.prompt_tokens, r.usage.completion_tokens, r.model)

"""Guard against passing arguments the installed SDKs don't accept (this crashed on Railway once)."""
import inspect
from types import SimpleNamespace

import anthropic
import openai

from app.llm.anthropic_client import AnthropicClient
from app.llm.openai_client import OpenAIClient


def _binding_stub(real, reply):
    sig = inspect.signature(real)

    def create(**kwargs):
        sig.bind(**kwargs)  # raises TypeError on any unknown argument
        return reply
    return create


def test_anthropic_call_matches_installed_sdk(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    c = AnthropicClient("claude-test")
    reply = SimpleNamespace(content=[SimpleNamespace(type="text", text='{"ok": true}')], stop_reason="end_turn",
                            usage=SimpleNamespace(input_tokens=1, output_tokens=1), model="claude-test")
    real = anthropic.Anthropic(api_key="x").messages.create
    monkeypatch.setattr(c.c.messages, "create", _binding_stub(real, reply))
    assert c.complete_json("s", "u", 100, temperature=0).data == {"ok": True}


def test_openai_call_matches_installed_sdk(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    c = OpenAIClient("gpt-test")
    reply = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"ok": true}'))],
                            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1), model="gpt-test")
    real = openai.OpenAI(api_key="x").chat.completions.create
    monkeypatch.setattr(c.c.chat.completions, "create", _binding_stub(real, reply))
    assert c.complete_json("s", "u", 100, temperature=0).data == {"ok": True}

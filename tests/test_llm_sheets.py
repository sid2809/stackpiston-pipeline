import pytest

from app.llm import LLMError, parse_json
from app.sheets import norm


def test_parse_plain_and_fenced_json():
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": 1} hope that helps') == {"a": 1}


def test_parse_rejects_non_json():
    with pytest.raises(LLMError):
        parse_json("no json here")
    with pytest.raises(LLMError):
        parse_json('{"a": 1')


def test_header_match_ignores_case_and_spaces():
    assert norm("REWRITE ") == norm("Rewrite")
    assert norm("JV doc /  JV page URL") == norm("JV doc / JV page URL")

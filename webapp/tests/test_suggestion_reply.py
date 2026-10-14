"""How a bid-suggestion reply is read. Made-up replies only - no database,
no Anthropic call.

13-14 Oct: Chumbak 607168's reply was cut off mid-JSON (thinking + answer went
past max_tokens=12000). The parser salvaged part of it, the rest was backfilled
at confidence 0.70, and the campaign's rows were never auto-accepted."""
import inspect
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ondemand_engine as oe  # noqa: E402


def reply(text_, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text_)])


ROW = {"campaign_id": "607168", "campaign_name": "C", "targeting": "tote_bag", "action": "DECREASE_CPM",
       "cpm_change": 10, "confidence": 0.82, "explanation": 'DATA: 7d ROAS 2.1 || said "tote bag"',
       "alternative_keywords": [], "current_cpm": 543, "campaign_budget": 2000}


def test_structured_reply_is_read_as_is():
    rows = oe._suggestions_from_reply(reply(json.dumps({"suggestions": [ROW]})), structured=True)
    assert rows == [ROW]


def test_cut_off_reply_saves_nothing():
    half = json.dumps({"suggestions": [ROW, ROW]})[:150]
    for structured in (True, False):
        with pytest.raises(ValueError, match="cut off"):
            oe._suggestions_from_reply(reply(half, stop="max_tokens"), structured)


def test_refusal_saves_nothing():
    with pytest.raises(ValueError, match="declined"):
        oe._suggestions_from_reply(reply("", stop="refusal"), structured=True)


def test_older_models_keep_the_text_parser():
    assert oe._suggestions_from_reply(reply("[" + json.dumps(ROW) + "]"), structured=False) == [ROW]


def test_auto_and_manual_models_get_structured_outputs():
    assert oe._structured_outputs(oe.DECISION_MODEL_AUTO)          # claude-sonnet-5-5
    assert oe._structured_outputs("claude-haiku-4-5-20251001")      # shared model
    assert not oe._structured_outputs("claude-3-5-sonnet-20241022")
    assert not oe._structured_outputs(None)


def test_schema_is_one_the_api_accepts():
    item = oe.SUGGESTION_SCHEMA["properties"]["suggestions"]["items"]
    for obj in (oe.SUGGESTION_SCHEMA, item):
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    text_ = json.dumps(oe.SUGGESTION_SCHEMA)
    for bad in ("maxItems", "minItems", "minimum", "maximum", "maxLength", "minLength"):
        assert bad not in text_
    # every field the save step reads from the model's answer is in the schema
    for f in ("targeting", "action", "cpm_change", "confidence", "explanation", "alternative_keywords"):
        assert f in item["properties"]


def test_suggestion_call_has_room_and_streams():
    src = inspect.getsource(oe.generate_ondemand_suggestions)
    assert "max_tokens=SUGGESTION_MAX_TOKENS" in src and "messages.stream(" in src
    assert "max_tokens=12000" not in src
    assert oe.SUGGESTION_MAX_TOKENS >= 32000

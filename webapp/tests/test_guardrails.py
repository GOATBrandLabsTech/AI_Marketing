"""
Safety-rule tests for the Blinkit decision engine.

These run in CI on every push and pull request. They use made-up rows only:
no database, no Anthropic call, no Blinkit call. Each test pins one rule that
protects real money, so a change that quietly breaks it turns the check red
before it can go live.
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ondemand_engine as oe  # noqa: E402


def _row(action="INCREASE_CPM", pct=10, cpm=300.0, floor=200, pos=5, top_days=0,
         active_days=7, rule_action=None, **extra):
    row = {
        "campaign_id": "999", "campaign_name": "CI test campaign", "targeting": "test_keyword",
        "action": action, "cpm_change": pct, "cpm_change_pct": pct, "confidence": 0.8,
        "current_cpm": cpm, "cpm_floor": floor, "campaign_budget": 1000,
        "most_viewed_position": pos, "top_slot_days_7d": top_days, "active_days_7d": active_days,
        "rule_action": rule_action or action, "rule_tag": "CI", "tier": 1,
        "explanation": "LLM -> test", "alternative_keywords": [],
    }
    row.update(extra)
    return row


def _clean(row, tolerance_pct=20):
    rows = oe._build_clean_rows([row], "Voylla", tolerance_pct=tolerance_pct)
    assert len(rows) == 1
    return rows[0]


# ── step size ───────────────────────────────────────────────────────────────

def test_ai_chosen_step_is_used():
    out = _clean(_row("DECREASE_CPM", pct=15, cpm=300))
    assert out["action"] == "DECREASE_CPM"
    assert out["cpm_change"] == 45


def test_raise_capped_at_15_percent():
    out = _clean(_row("INCREASE_CPM", pct=30, cpm=300))
    assert out["cpm_change"] == 45   # 15% of 300


def test_cut_capped_at_20_percent():
    out = _clean(_row("DECREASE_CPM", pct=40, cpm=300))
    assert out["cpm_change"] == 60   # 20% of 300


def test_single_move_never_above_rupee_cap():
    out = _clean(_row("DECREASE_CPM", pct=20, cpm=500))
    assert out["cpm_change"] == 80   # 20% of 500 is 100, capped at Rs80


def test_missing_step_defaults_to_10_percent():
    row = _row("INCREASE_CPM", cpm=300)
    row.pop("cpm_change_pct")
    assert _clean(row)["cpm_change"] == 30


def test_campaign_tolerance_still_applies():
    out = _clean(_row("DECREASE_CPM", pct=20, cpm=300), tolerance_pct=10)
    assert out["cpm_change"] == 30


# ── floor and position-1 ────────────────────────────────────────────────────

def test_cut_never_goes_below_floor():
    out = _clean(_row("DECREASE_CPM", pct=20, cpm=210, floor=200))
    assert out["action"] == "DECREASE_CPM"
    assert 210 - out["cpm_change"] >= 200


def test_no_cut_when_already_at_floor():
    out = _clean(_row("DECREASE_CPM", pct=10, cpm=200, floor=200))
    assert out["action"] == "NO_CHANGE"
    assert out["cpm_change"] == 0


def test_no_raise_at_position_1():
    out = _clean(_row("INCREASE_CPM", pct=10, cpm=300, pos=1))
    assert out["action"] == "NO_CHANGE"


def test_no_raise_when_top_slot_most_days():
    out = _clean(_row("INCREASE_CPM", pct=10, cpm=300, pos=5, top_days=4, active_days=7))
    assert out["action"] == "NO_CHANGE"


def test_raise_allowed_when_rarely_top():
    out = _clean(_row("INCREASE_CPM", pct=10, cpm=300, pos=8, top_days=1, active_days=7))
    assert out["action"] == "INCREASE_CPM"
    assert out["cpm_change"] == 30


# ── learning inputs ─────────────────────────────────────────────────────────

def test_attribution_curve_is_never_decreasing():
    out = oe._isotonic_nondecreasing([1.0, 1.2, 1.1, 1.3, 1.25], [1, 1, 1, 1, 1])
    assert all(b >= a for a, b in zip(out, out[1:]))


def test_bid_response_shows_each_bid_level():
    days = pd.date_range("2026-09-01", periods=12, freq="D")
    daily = pd.DataFrame({
        "report_date": days, "Targeting Value": "test keyword",
        "spend": [20.0] * 12, "total_sales": [100.0] * 6 + [0.0] * 6,
        "impressions": [100] * 12, "total_units": [1] * 6 + [0] * 6,
        "most_viewed_position": [1.0] * 12,
    })
    # one suggestion row per day; CPM 200 for the first six days, then 300
    hist = pd.DataFrame({
        "action_date": [d + pd.Timedelta(days=1) for d in days],
        "current_cpm": [200.0] * 6 + [300.0] * 6,
    })
    text = oe.build_bid_response(daily, hist, 300.0)
    assert "BID RESPONSE" in text
    assert "Rs200" in text and "Rs300" in text
    assert "ROAS 5.00 -> 0.00" in text


# ── model plumbing ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("model,expected", [
    ("claude-haiku-4-5-20251001", {"temperature": 0}),
    ("claude-sonnet-5-5", {}),
])
def test_temperature_only_sent_to_models_that_accept_it(model, expected):
    assert oe._temperature_kwargs(model) == expected


def test_cost_estimate():
    assert oe._model_cost_usd("claude-sonnet-5-5", 1_000_000, 100_000) == pytest.approx(3.0)
    assert oe._model_cost_usd("claude-haiku-4-5-20251001", 0, 0, cache_read=1_000_000) == pytest.approx(0.1)


def test_pilot_campaign_model_settings():
    assert oe.DECISION_MODEL_BY_CAMPAIGN.get("296464", "").startswith("claude-")

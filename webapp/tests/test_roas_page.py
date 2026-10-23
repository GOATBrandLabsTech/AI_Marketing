"""ROAS Impact: campaign picker, comparison line, push markers and sparklines.
Made-up rows only - no database."""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as webapp  # noqa: E402
import queries  # noqa: E402

TODAY = date.today()
CUT = TODAY - timedelta(days=5)


def day(d, spend, sales):
    return {"date": d, "spend": spend, "sales": sales, "roas": round(sales / spend, 2), "impressions": 0,
            "atc": 0, "new_users": 0, "avg_position": None}


def test_pick_campaigns_only_ai_ones_default_all():
    assert queries.pick_campaigns("", [3, 1, 2]) == [1, 2, 3]
    assert queries.pick_campaigns("2,99,abc,2", [1, 2, 3]) == [2]       # unknown ids dropped
    assert queries.pick_campaigns("99", [1, 2]) == [1, 2]               # nothing valid -> all


def test_comparison_and_pushes_land_in_the_right_bucket():
    d1, d2 = date(2026, 10, 12), date(2026, 10, 13)       # same ISO week
    trend = queries.bucket_channel_series([day(d1, 100, 400), day(d2, 100, 500)], "week")
    others = queries.bucket_channel_series([day(d1, 200, 600), day(d2, 200, 600)], "week")
    pushes = [{"date": d1, "raised": 1, "cut": 2, "paused": 0}, {"date": d2, "raised": 0, "cut": 1, "paused": 1}]
    out = queries.attach_comparison(trend, others, pushes, "week")
    assert out[0]["roas"] == 4.5 and out[0]["others_roas"] == 3.0
    assert out[0]["pushes"] == {"raised": 1, "cut": 3, "paused": 1} and out[0]["push_total"] == 5


def test_sparkline_skips_gaps_and_needs_two_points():
    assert queries.sparkline_points([None, 2.0, None]) == ""
    pts = queries.sparkline_points([1.0, None, 3.0], width=10, height=10, pad=0).split()
    assert pts == ["0.0,10.0", "10.0,0.0"]


@pytest.fixture
def client(monkeypatch):
    seen = {}
    days = [day(TODAY - timedelta(days=i), 100, 400) for i in range(20, 0, -1)]
    monkeypatch.setattr(queries, "fetch_ondemand_action_dates", lambda b: [str(CUT)])
    monkeypatch.setattr(queries, "fetch_ondemand_roas_impact", lambda *a, **k: [])
    monkeypatch.setattr(queries, "fetch_ai_managed_campaign_ids", lambda b: [111, 222, 333])

    def channel_daily(brand, days=180, campaign_ids=None, exclude_ids=None):
        seen.setdefault("calls", []).append({"ids": campaign_ids, "exclude": exclude_ids})
        return [dict(d) for d in globals_days]
    globals_days = days
    monkeypatch.setattr(queries, "fetch_channel_daily", channel_daily)
    monkeypatch.setattr(queries, "fetch_push_counts",
                        lambda b, ids, start: [{"date": CUT, "raised": 2, "cut": 1, "paused": 0}])
    monkeypatch.setattr(queries, "fetch_ai_campaign_options", lambda b, ids: [
        {"cid": "111", "name": "Tote bag", "status": "ACTIVE", "auto": True},
        {"cid": "222", "name": "Sling bag", "status": "ACTIVE", "auto": True},
        {"cid": "333", "name": "Mugs", "status": "STOPPED", "auto": False}])
    monkeypatch.setattr(queries, "count_ondemand_decisions_on_date", lambda b, d: 4)

    def scoreboard(brand, ids, cut, window, **kw):
        seen["scoreboard_ids"] = ids
        return [{"cid": str(i), "name": f"C{i}", "mode": "auto", "raised": 1, "cut": 0, "paused": 0,
                 "spend_before": 100.0, "spend_after": 100.0, "roas_before": 4.0, "roas_after": 5.0,
                 "change_pct": 25} for i in ids]
    monkeypatch.setattr(queries, "fetch_campaign_scoreboard", scoreboard)
    monkeypatch.setattr(queries, "fetch_campaign_roas_daily", lambda b, ids, days=30: {"111": [1.0, 2.0, 3.0]})
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
    c.seen = seen
    return c


def test_picker_lists_ai_campaigns_and_defaults_to_all(client):
    html = client.get("/roas?brand=Chumbak&scope=ai").get_data(as_text=True)
    assert "Campaigns: all 3" in html and 'value="333"' in html and "Only Auto" in html
    assert client.seen["scoreboard_ids"] == [111, 222, 333]
    assert "<polyline" in html                       # sparkline for 111
    assert "Other campaigns over the same days" in html


def test_selection_scopes_chart_and_scoreboard(client):
    html = client.get("/roas?brand=Chumbak&scope=ai&campaigns=222&campaigns=111").get_data(as_text=True)
    assert "2 of 3 AI-managed campaigns" in html
    assert client.seen["scoreboard_ids"] == [111, 222]
    first = client.seen["calls"][0]
    assert first["ids"] == [111, 222]                 # chart = the chosen campaigns
    assert client.seen["calls"][1]["exclude"] == [111, 222, 333]   # comparison = every non-AI campaign
    # the selection survives the cutover / window / granularity form
    assert html.count('name="campaigns" value="111"') >= 2


def test_whole_channel_has_no_picker(client):
    html = client.get("/roas?brand=Chumbak&scope=all").get_data(as_text=True)
    assert "campaignPicker" not in html and "Whole channel" in html


def test_before_after_has_ctr_and_cpm():
    d0 = date(2026, 10, 1)
    rows = [dict(day(d0 + timedelta(days=i), 100, 400), impressions=10000, atc=50) for i in range(4)]
    ba = queries.channel_before_after(rows, d0 + timedelta(days=2), window_days=2)
    assert ba["before"]["impressions"] == 20000
    assert ba["before"]["ctr"] == 0.5          # 100 ATC / 20,000 impressions = 0.5%
    assert ba["before"]["cpm"] == 10.0         # Rs 200 per 20,000 impressions = Rs 10 per 1,000


def test_comparison_carries_other_campaigns_totals():
    d1 = date(2026, 10, 12)
    out = queries.attach_comparison(queries.bucket_channel_series([day(d1, 100, 400)], "day"),
                                    queries.bucket_channel_series([dict(day(d1, 300, 900), impressions=7, atc=2)], "day"),
                                    [], "day")
    assert out[0]["others"] == {"spend": 300, "sales": 900, "impressions": 7, "atc": 2, "units": None}


def test_page_is_called_ads_performance_with_metric_switch(client):
    html = client.get("/roas?brand=Chumbak&scope=ai").get_data(as_text=True)
    assert "<h1" in html and "Ads Performance" in html and "ROAS Impact" not in html
    assert 'data-metric="roas"' in html


def test_date_range_presets():
    today = date(2026, 10, 14)
    assert queries.resolve_range("this_month", None, None, today)[1:] == (date(2026, 10, 1), today)
    assert queries.resolve_range("last_month", None, None, today)[1:] == (date(2026, 9, 1), date(2026, 9, 30))
    # last N days ends yesterday (today has almost no data yet)
    assert queries.resolve_range("15", None, None, today)[1:] == (date(2026, 9, 29), date(2026, 10, 13))
    assert queries.resolve_range("custom", "2026-10-10", "2026-10-01", today)[1:] == (date(2026, 10, 1), date(2026, 10, 10))
    assert queries.resolve_range("custom", "bad", None, today)[0] == "30"
    assert queries.resolve_range(None, None, None, today)[0] == "30"


def test_card_totals_and_change_vs_previous_period():
    d0 = date(2026, 10, 1)
    rows = [dict(day(d0 + timedelta(days=i), 100, 300 + 100 * (i >= 2)), impressions=1000, atc=10 + 10 * (i >= 2), units=2)
            for i in range(4)]
    prev = queries.period_totals(rows, d0, d0 + timedelta(days=1))
    cur = queries.period_totals(rows, d0 + timedelta(days=2), d0 + timedelta(days=3))
    assert cur["roas"] == 4.0 and prev["roas"] == 3.0 and cur["units"] == 4 and cur["ctr"] == 2.0
    ch = queries.compare_totals(cur, prev)
    assert ch["roas"] == 33.3 and ch["spend"] == 0.0 and ch["atc"] == 100.0
    assert queries.compare_totals(cur, queries.period_totals([], d0, d0))["roas"] is None


def test_metric_cards_with_checkboxes_and_range_picker(client):
    html = client.get("/roas?brand=Chumbak&scope=ai&range=this_month").get_data(as_text=True)
    for key in ("spend", "impressions", "atc", "units", "sales", "roas", "ctr", "cpm"):
        assert f'type="checkbox" data-metric="{key}"' in html
    assert 'name="range"' in html and 'value="this_month" selected' in html
    assert "Quantity sold" in html and "Budget spent" in html

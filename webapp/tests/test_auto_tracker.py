"""Auto Tracker: blocks never mix before/after days, ROAS is total sales over
total spend, other campaigns are kept apart, and the page renders."""
import os
import sys
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as webapp  # noqa: E402
import auto_tracker as t  # noqa: E402

START = date(2026, 10, 12)


def day(d, spend, sales, cid="1"):
    return {"campaign_id": cid, "campaign_name": f"C{cid}", "d": d, "spend": spend, "sales": sales, "impressions": 10}


def test_week_blocks_start_on_the_auto_date():
    daily = [day(START + timedelta(days=i), 100, 300) for i in range(-28, 10)]
    blocks, base = t.build_blocks(daily, START, START + timedelta(days=9), "week")
    after = [b for b in blocks if b["after"]]
    assert after[0]["start"] == START and after[0]["label"] == "Week 1"
    assert all(b["end"] < START for b in blocks if not b["after"])
    assert after[1]["partial"] and after[1]["days"] == 3
    assert base["days"] == 28 and base["roas"] == 3.0


def test_roas_is_total_sales_over_total_spend_not_average_of_days():
    daily = [day(START - timedelta(days=2), 1000, 1000), day(START - timedelta(days=1), 10, 100)]
    _, base = t.build_blocks(daily, START, START, "day")
    assert base["roas"] == round(1100 / 1010, 2)   # not (1 + 10) / 2


def test_after_block_compared_per_day_against_before():
    daily = [day(START - timedelta(days=i), 100, 400) for i in range(1, 15)] + [day(START, 100, 500)]
    blocks, _ = t.build_blocks(daily, START, START, "day")
    first_after = [b for b in blocks if b["after"]][0]
    assert first_after["roas_vs_base"] == 25 and first_after["spend_vs_base"] == 0


def test_campaign_rows_use_each_campaigns_own_start():
    own = START - timedelta(days=4)
    daily = ([day(own - timedelta(days=i), 100, 200, "7") for i in range(1, 15)]
             + [day(own + timedelta(days=i), 100, 400, "7") for i in range(0, 5)])
    rows = t.campaign_rows(daily, {"7": own}, START, START)
    assert rows[0]["start"] == own and rows[0]["roas_before"] == 2.0 and rows[0]["roas_after"] == 4.0


def test_default_start_is_the_most_common_switch_date():
    assert t.default_start({"1": START, "2": START, "3": date(2026, 10, 8)}) == START


@pytest.fixture
def client(monkeypatch):
    auto = {"1": START}
    rows = ([day(START - timedelta(days=i), 100, 400, "1") for i in range(1, 100)]
            + [day(START - timedelta(days=i), 100, 200, "2") for i in range(1, 100)]
            + [day(START, 100, 500, "1"), day(START, 100, 200, "2")])
    monkeypatch.setattr(t, "fetch_auto_campaigns", lambda brand: auto)
    monkeypatch.setattr(t, "fetch_campaign_daily", lambda brand, start: [r for r in rows if r["d"] >= start])
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
    return c


@pytest.mark.parametrize("g", ["day", "week", "month"])
def test_page_renders_each_granularity(client, g, monkeypatch):
    real = t.tracker
    monkeypatch.setattr(t, "tracker", lambda brand, granularity, start: real(brand, granularity, start,
                                                                             today=START + timedelta(days=1)))
    html = client.get(f"/auto-tracker?brand=Chumbak&granularity={g}").get_data(as_text=True)
    assert "Auto Tracker" in html and "Other campaigns" in html and "C1" in html
    assert "+25%" in html   # Auto ROAS 5.0 vs 4.0 before

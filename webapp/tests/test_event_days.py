"""BAU vs event days: event periods are left out of the default view, kept
alone in the event view, and spikes are suggested as events."""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import event_days as ev  # noqa: E402

D0 = date(2026, 8, 1)
DAILY = [{"date": D0 + timedelta(days=i), "spend": 100.0, "sales": 500.0} for i in range(40)]
RAKHI = [{"start_date": date(2026, 8, 20), "end_date": date(2026, 8, 22), "name": "Raksha Bandhan"}]


def test_dates_cover_the_whole_event():
    assert ev.dates_of(RAKHI) == {date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 22)}


def test_modes_split_the_days():
    days = ev.dates_of(RAKHI)
    assert len(ev.apply_mode(DAILY, days, "bau")) == 37
    assert [r["date"] for r in ev.apply_mode(DAILY, days, "event")] == sorted(days)
    assert len(ev.apply_mode(DAILY, days, "all")) == 40
    assert ev.clean_mode("nonsense") == "bau" and ev.clean_mode("event") == "event"


def test_spikes_are_suggested_unless_already_an_event():
    daily = [dict(r) for r in DAILY]
    for r in daily:
        if date(2026, 8, 25) <= r["date"] <= date(2026, 8, 27):
            r["sales"] = 2500.0                              # 5x normal
    [s] = ev.suggest_events(daily, set())
    assert (s["start_date"], s["end_date"], s["days"], s["peak"]) == (date(2026, 8, 25), date(2026, 8, 27), 3, 5.0)
    known = ev.dates_of([{"start_date": date(2026, 8, 25), "end_date": date(2026, 8, 27)}])
    assert ev.suggest_events(daily, known) == []


def test_needs_two_weeks_of_history_before_judging():
    daily = [{"date": D0 + timedelta(days=i), "sales": 100.0 * (i + 1)} for i in range(10)]
    assert ev.suggest_events(daily, set()) == []


def test_bad_events_are_refused():
    import pytest
    with pytest.raises(ValueError):
        ev.add_event(None, "Voylla", "", date(2026, 8, 1), date(2026, 8, 2))
    with pytest.raises(ValueError):
        ev.add_event(None, "Voylla", "x", date(2026, 8, 5), date(2026, 8, 1))
    with pytest.raises(ValueError):
        ev.add_event(None, "Voylla", "x", date(2026, 8, 1), date(2026, 10, 1))


def test_page_defaults_to_bau_and_says_what_was_left_out(monkeypatch):
    import app as webapp
    import queries
    import db
    monkeypatch.setattr(db, "get_engine", lambda: None)
    monkeypatch.setattr(ev, "list_events", lambda engine, brand: RAKHI)
    today = date.today()
    daily = [{"date": today - timedelta(days=i), "spend": 100.0, "sales": 500.0, "impressions": 1000, "atc": 10,
              "units": 5, "new_users": 1, "avg_position": 3.0} for i in range(1, 60)]
    seen = {}
    monkeypatch.setattr(queries, "fetch_ondemand_action_dates", lambda b: [str(today - timedelta(days=10))])
    monkeypatch.setattr(queries, "fetch_ondemand_roas_impact", lambda *a, **k: [])
    monkeypatch.setattr(queries, "fetch_ai_managed_campaign_ids", lambda b: [])
    monkeypatch.setattr(queries, "fetch_channel_daily", lambda *a, **k: daily)
    monkeypatch.setattr(queries, "fetch_push_counts", lambda *a, **k: {})
    monkeypatch.setattr(queries, "fetch_ai_campaign_options", lambda *a, **k: [])
    monkeypatch.setattr(queries, "count_ondemand_decisions_on_date", lambda *a: 0)
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
    html = c.get("/roas?brand=Voylla").get_data(as_text=True)
    assert "Showing BAU (normal) days only" in html and "Raksha Bandhan" in html
    assert "Showing all days" in c.get("/roas?brand=Voylla&days=all").get_data(as_text=True)

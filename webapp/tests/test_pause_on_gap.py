"""Pause-on-gap checkbox: shows what the scheduler notebook will do (any window
on, or blank, = the campaign pauses outside its windows) and the switch sets
every window of the campaign."""
import os
import sys
from datetime import date, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as webapp  # noqa: E402
import queries  # noqa: E402


def entry(flag):
    return {"entry_id": "e1", "campaign_id": "163795", "start_date": date(2026, 10, 21), "end_date": date(2026, 10, 25),
            "start_time": time(9, 0), "end_time": time(23, 59, 59), "budget": 4000, "status": "PENDING",
            "pause_on_gap": flag}


def test_rule_matches_the_scheduler_notebook():
    assert queries.campaign_pauses_on_gap([entry(False), entry(True)]) is True
    assert queries.campaign_pauses_on_gap([entry(None)]) is True      # blank counts as on in the notebook
    assert queries.campaign_pauses_on_gap([entry(False)]) is False
    assert queries.campaign_pauses_on_gap([]) is False


def _client():
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
    return c


def test_list_reports_the_campaign_setting(monkeypatch):
    monkeypatch.setattr(queries, "fetch_schedule_entries", lambda cid: [entry(True)])
    body = _client().get("/api/schedule/163795").get_json()
    assert body["pause_on_gap"] is True and body["entries"][0]["end_hour"] == 24


def test_switch_sets_every_window_and_rejects_non_booleans(monkeypatch):
    calls = []
    monkeypatch.setattr(queries, "set_pause_on_gap", lambda cid, v: calls.append((cid, v)) or 3)
    c = _client()
    r = c.post("/api/schedule/163795/pause_on_gap", json={"value": False})
    assert r.status_code == 200 and r.get_json()["windows"] == 3 and calls == [("163795", False)]
    assert c.post("/api/schedule/163795/pause_on_gap", json={"value": "yes"}).status_code == 400
    assert len(calls) == 1

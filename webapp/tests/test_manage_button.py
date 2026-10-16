"""A campaign name with an apostrophe (Men's Gifting) must not break the
Manage button's click handler."""
import html
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as webapp  # noqa: E402
import queries  # noqa: E402

ROW = {"campaign_id": 163795, "campaign_name": "Men's Gifting (Cuffling)", "brand": "Voylla", "budget": 4000.0,
       "last_status": "ACTIVE", "mode": "semi_auto", "ondemand_managed": True, "window_count": 4,
       "next_start": "2026-10-11", "last_end": "2026-10-18"}


def test_manage_handler_survives_an_apostrophe(monkeypatch):
    monkeypatch.setattr(queries, "fetch_campaign_status", lambda brand: [dict(ROW)])
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
    page = c.get("/campaigns?brand=Voylla").get_data(as_text=True)
    attr = re.search(r"onclick='(openSchedule\([^']*\))'", page)
    assert attr, "Manage button not found"
    js = html.unescape(attr.group(1))          # what the browser hands to JavaScript
    args = json.loads("[" + js[len("openSchedule("):-1] + "]")   # the values JavaScript receives
    assert args == ["163795", "Men's Gifting (Cuffling)", "Voylla"]

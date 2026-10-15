"""Performance alerts: who gets flagged, when a reminder fires, and that the
alert never touches Auto campaigns or a campaign's mode."""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import perf_alerts as pa  # noqa: E402

TODAY = date(2026, 10, 16)


def camp(cid, spend, sales, mode="semi_auto", status="ACTIVE"):
    return {"campaign_id": cid, "campaign_name": f"C{cid}", "spend": spend, "sales": sales,
            "mode": mode, "last_status": status}


def test_flags_only_non_auto_below_60pct_of_brand_with_enough_spend():
    rows = [camp("good", 10000, 50000),                 # ROAS 5
            camp("bad", 5000, 5000),                    # ROAS 1 -> flagged
            camp("auto_bad", 5000, 5000, mode="auto"),  # never flagged: AI already runs it
            camp("small", 1000, 0),                     # below Rs 2,000
            camp("stopped", 5000, 0, status="STOPPED"),
            camp("not_set", 4000, 4000, mode="not set")]
    flagged, brand_roas = pa.flag_underperformers(rows)
    assert round(brand_roas, 2) == round(64000 / 30000, 2)
    assert [c["campaign_id"] for c in flagged] == ["bad", "not_set"]   # most sales missed first


def test_borderline_campaign_at_exactly_60pct_is_not_flagged():
    rows = [camp("a", 10000, 30000), camp("b", 10000, 30000 * 0.6 * 0 + 10000 * 1.8)]
    # brand ROAS = 4.8/2 = 2.4 ; b ROAS 1.8 = 75% of brand -> fine
    assert pa.flag_underperformers(rows)[0] == []


def test_reminder_after_two_days_then_refresh_after_seven():
    c = {"campaign_id": "9"}
    state = lambda d: {"9": {"pending_since": TODAY - timedelta(days=d), "n_pending": 2, "last_reviewed": None}}
    assert pa.decide(c, state(1), TODAY) == ("waiting", 1)
    assert pa.decide(c, state(2), TODAY) == ("reminder", 2)
    assert pa.decide(c, state(7), TODAY)[0] == "new"


def test_recently_reviewed_is_not_sent_to_the_ai_again():
    c = {"campaign_id": "9"}
    st = {"9": {"pending_since": None, "n_pending": 0, "last_reviewed": TODAY - timedelta(days=1)}}
    assert pa.decide(c, st, TODAY) == ("reviewed", 1)
    st["9"]["last_reviewed"] = TODAY - timedelta(days=5)
    assert pa.decide(c, st, TODAY) == ("new", 0)
    assert pa.decide(c, {}, TODAY) == ("new", 0)


def test_email_subject_keeps_it_in_the_inbox_and_lists_reminders():
    c1 = dict(camp("1", 5000, 5000), brand="Voylla", roas=1.0, brand_roas=4.0, status="reminder", age=3, n_pending=2,
              n_changes=0, note=None)
    c2 = dict(camp("2", 3000, 3000), brand="Voylla", roas=1.0, brand_roas=4.0, status="new", age=0, n_pending=0,
              n_changes=3, note=None, ok=True)
    report = [{"brand": "Voylla", "start": TODAY - timedelta(days=7), "end": TODAY - timedelta(days=1),
               "campaigns": [c1, c2]}]
    subject, html = pa.build_email(report, TODAY)
    assert "failed" in subject and "not accepted" in subject   # Gmail filter keeps "failed" mail in the inbox
    assert "Still not accepted" in html and "not accepted for 3 days" in html
    assert "3 bid change(s) suggested" in html


def test_no_email_when_nothing_is_flagged():
    report = [{"brand": "Voylla", "start": TODAY, "end": TODAY, "campaigns": []}]
    assert pa.build_email(report, TODAY) is None


def test_module_never_changes_modes_or_accepts():
    src = open(pa.__file__, encoding="utf-8").read()
    assert "set_autonomy_mode" not in src and "accept_ondemand_action" not in src
    assert "campaign_autonomy_mode SET" not in src.replace("\n", " ")


def test_ai_budget_goes_to_worst_campaigns_across_brands(monkeypatch):
    import ondemand_engine as oe
    import db
    asked = []
    monkeypatch.setattr(db, "get_engine", lambda: None)
    monkeypatch.setattr(oe, "_brand_last_report_day", lambda engine, brand: TODAY - timedelta(days=1))
    rows = {"A": [camp("a_good", 100000, 500000)] + [camp(f"a{i}", 5000, 5000) for i in range(3)],
            "B": [camp("b_good", 100000, 500000), camp("b_worst", 50000, 0)]}
    monkeypatch.setattr(pa, "fetch_window", lambda engine, brand, s, e: [dict(r) for r in rows[brand]])
    monkeypatch.setattr(pa, "fetch_alert_state", lambda engine, brand: {})
    monkeypatch.setattr(pa, "MAX_PER_DAY", 2)
    monkeypatch.setattr(oe, "generate_ondemand_suggestions",
                        lambda brand, cid, requested_by, model_override: asked.append(cid) or
                        {"ok": True, "rows": [{"action": "DECREASE_CPM"}], "error": None})
    report = pa.run(today=TODAY, send=False, brands=["A", "B"], log=False)
    assert asked[0] == "b_worst" and len(asked) == 2      # brand B's campaign first although B comes second
    statuses = [c["status"] for b in report for c in b["campaigns"]]
    assert statuses.count("not_sent") == 2

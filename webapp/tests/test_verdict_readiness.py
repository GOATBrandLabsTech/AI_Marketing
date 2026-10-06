"""When a pushed bid change gets its verdict on the ROAS Impact page.
Made-up rows only - no database."""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import queries  # noqa: E402


def _row(day, action="DECREASE_CPM", pushed=True, kw="jhumke", verdict="WORSENED"):
    return {"campaign_id": "296464", "targeting": kw, "action_date": date(2026, 10, day),
            "action": action, "was_implemented": pushed, "verdict": verdict}


def test_change_waits_10_days_for_its_verdict():
    r = _row(3)
    queries.annotate_verdict_readiness([r], today=date(2026, 10, 12))
    assert r["status"] == "waiting" and r["verdict"] is None
    assert r["verdict_on"] == date(2026, 10, 13)


def test_change_is_judged_once_settled():
    r = _row(3)
    queries.annotate_verdict_readiness([r], today=date(2026, 10, 13))
    assert r["status"] == "judged" and r["verdict"] == "WORSENED"


def test_hold_and_unpushed_rows_are_never_judged():
    hold = _row(1, action="NO_CHANGE", pushed=False)
    proposed = _row(1, pushed=False)
    queries.annotate_verdict_readiness([hold, proposed], today=date(2026, 10, 30))
    assert hold["status"] == "hold" and hold["verdict"] is None
    assert proposed["status"] == "not_pushed"


def test_second_change_inside_the_window_is_flagged():
    first, second = _row(1), _row(3)
    queries.annotate_verdict_readiness([first, second], today=date(2026, 10, 30))
    assert first["changed_again_on"] == date(2026, 10, 3)
    assert second["changed_again_on"] is None

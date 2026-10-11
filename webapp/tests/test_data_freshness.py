"""The scheduled run must not decide on stale report data. Made-up dates only."""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ondemand_engine as oe  # noqa: E402

TODAY = date(2026, 10, 11)


def test_data_through_yesterday_is_fine():
    assert oe.data_freshness_problem(date(2026, 10, 10), TODAY) is None


def test_data_a_day_old_is_refused():
    msg = oe.data_freshness_problem(date(2026, 10, 9), TODAY)
    assert msg and "stale" in msg and "2026-10-09" in msg


def test_no_rows_is_refused():
    assert "no report rows" in oe.data_freshness_problem(None, TODAY)

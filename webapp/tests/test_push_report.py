"""The push alert email shows each bid's old and new value."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import push_report as pr  # noqa: E402


def test_old_and_new_bid_with_change():
    lines = pr.change_lines({"tote_bag": 543, "totes": 552}, {"tote_bag": 617, "totes": 502.0})
    assert lines == ["  tote_bag: 617 -> 543 (-12%, cut)", "  totes: 502 -> 552 (+10%, raised)"]


def test_missing_old_bid_and_pauses():
    lines = pr.change_lines({"gift_mug": 238}, {"shoulder_bag": 410}, paused_keywords=["shoulder_bag"])
    assert lines == ["  gift_mug: ? -> 238 (old bid not found)", "  shoulder_bag: PAUSED (was 410)"]

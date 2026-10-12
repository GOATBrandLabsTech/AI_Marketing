"""Taking a SKU out of a campaign: what may be queued and what gets sent.
Made-up rows only."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sku_actions as sa  # noqa: E402


ROWS = [{"sku": "VJ1", "pid": "111", "campaign_ids": "900,901"}, {"sku": "VJ2", "pid": "222", "campaign_ids": ""}]


def test_only_live_campaign_product_pairs_can_be_queued():
    ok, rejected = sa.valid_requests(
        [{"campaign_id": "900", "pid": "111"}, {"campaign_id": "999", "pid": "111"}, {"campaign_id": "900", "pid": "222"}], ROWS)
    assert ok == [{"campaign_id": "900", "pid": "111", "sku": "VJ1"}]
    assert len(rejected) == 2


def test_removal_leaves_other_products():
    kept, err = sa.products_after_removal([111, 222, 333], ["222"])
    assert kept == [111, 333] and err is None


def test_never_leave_a_campaign_with_no_products():
    kept, err = sa.products_after_removal([111], ["111"])
    assert kept == [] and err.startswith("refused")

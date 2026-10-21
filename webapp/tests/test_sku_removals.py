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
    assert [(o["campaign_id"], o["pid"], o["sku"]) for o in ok] == [("900", "111", "VJ1")]
    assert len(rejected) == 2


def test_reason_is_the_products_own_state_not_the_page_view():
    rows = [{"sku": "NR1", "pid": "5", "campaign_ids": "680727", "flags": [], "stock": 158, "units_30d": 703},
            {"sku": "BR1", "pid": "6", "campaign_ids": "436753", "flags": ["advertised_oos"], "stock": 0, "units_30d": 0}]
    labels = {"advertised_oos": "Advertised, out of stock"}
    ok, _ = sa.valid_requests([{"campaign_id": "680727", "pid": "5"}, {"campaign_id": "436753", "pid": "6"}], rows, labels)
    assert ok[0]["reason"] == "No flag - stock 158, 703 sold in 30 days"
    assert ok[1]["reason"] == "Advertised, out of stock - stock 0, 0 sold in 30 days"


def test_removal_leaves_other_products():
    kept, err = sa.products_after_removal([111, 222, 333], ["222"])
    assert kept == [111, 333] and err is None


def test_never_leave_a_campaign_with_no_products():
    kept, err = sa.products_after_removal([111], ["111"])
    assert kept == [] and err.startswith("refused")

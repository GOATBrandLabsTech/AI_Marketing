"""SKU Health and SKU detail pages render, with the database swapped for
made-up rows."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as webapp  # noqa: E402
import db  # noqa: E402
import sku_actions  # noqa: E402
import sku_intel  # noqa: E402

ROW = {"sku": "VJ1", "name": "Test jhumka", "pid": "111", "item_id": "222", "sub_category": "Earrings",
       "category": "Jewellery", "stock": 0, "units_7d": 3, "units_30d": 30, "campaigns": 2, "campaign_ids": "900,901",
       "warehouses_with_stock": 0, "sp_asked": 300, "sp_live": 300, "mrp": 600, "listing_status": "In Stock",
       "lp": 200, "cost_price": 100, "offer_units_7d": 0, "brand_fund_7d": 0, "days_of_cover": 0.0,
       "profit_per_unit": 100.0}
ROW["flags"] = sku_intel.sku_flags(ROW)
DETAIL = {"daily_sales": [], "city_sales": [{"city": "Delhi", "units_7d": 3, "units_30d": 30}],
          "stock_by_warehouse": [], "price_history": [], "offer_history": [], "city_master": {},
          "campaigns": [{"campaign_id": "900", "campaign_name": "Earrings", "mode": "semi_auto", "status": "ACTIVE",
                         "region_type": "CITY", "region_ids": "1", "n_products": 3, "spend": 0, "sales": 0}]}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(db, "get_engine", lambda: None)
    monkeypatch.setattr(sku_intel, "sku_snapshot", lambda engine, brand=None: [dict(ROW)])
    monkeypatch.setattr(sku_intel, "sku_detail", lambda engine, row, brand=None: DETAIL)
    monkeypatch.setattr(sku_intel, "campaign_names", lambda engine, ids, brand=None: {"900": "Earrings"})
    monkeypatch.setattr(sku_actions, "open_removals",
                        lambda engine, brand: {("901", "111"): {"id": 5, "status": "pending", "push_note": None}})
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
    return c


def test_oos_view_lists_campaigns_with_remove_controls(client):
    html = client.get("/sku-health?flag=advertised_oos").get_data(as_text=True)
    assert "Earrings" in html and 'data-cid="900"' in html and "Remove selected from campaigns" in html
    assert "Removal pending" in html and "cancelRemoval(5)" in html
    assert "Item ID 222" in html and "Product ID 111" in html


def test_search_by_blinkit_item_id(client):
    assert "Test jhumka" in client.get("/sku-health?q=222").get_data(as_text=True)
    assert "Test jhumka" not in client.get("/sku-health?q=999").get_data(as_text=True)


def test_warehouse_total_flags_a_mismatch_with_the_summary(client, monkeypatch):
    d = dict(DETAIL, stock_by_warehouse=[{"warehouse": "A", "city": "Delhi", "stock": 3}])
    monkeypatch.setattr(sku_intel, "sku_detail", lambda engine, row, brand=None: d)
    html = client.get("/sku-health/sku/VJ1").get_data(as_text=True)
    assert "summary says 0" in html


def test_city_sales_total_matches_summary(client):
    # DETAIL: Delhi 3 (7d) / 30 (30d); ROW: units_7d 3, units_30d 30
    html = client.get("/sku-health/sku/VJ1").get_data(as_text=True)
    assert "Total (1 cities)" in html and "summary says" not in html.split("Sales by city")[1]


def test_sku_detail_page_shows_five_reports(client):
    html = client.get("/sku-health/sku/VJ1").get_data(as_text=True)
    for dept in ("Sales", "Inventory", "Price &amp; Offer", "Marketing", "Product"):
        assert dept in html
    assert "Ask the boss" in html

"""Sales breakdown: city and category filters, ranking, city stock from the
warehouses that serve the city, and the chat tool being wired in."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import breakdown as bd  # noqa: E402
import chat  # noqa: E402

SALES = [
    {"item_id": "1", "name": "Hoop Studs", "city": "Kolkata", "units": 50, "mrp_value": 15000},
    {"item_id": "1", "name": "Hoop Studs", "city": "Mumbai", "units": 70, "mrp_value": 21000},
    {"item_id": "2", "name": "Jhumka", "city": "Kolkata", "units": 80, "mrp_value": 40000},
    {"item_id": "3", "name": "Silver Ring", "city": "Kolkata", "units": 200, "mrp_value": 60000},
]
CATS = [{"item_id": "1", "category": "Earrings", "product_type": "Studs", "tier": "A.Hero"},
        {"item_id": "2", "category": "Earrings", "product_type": "Jhumkas"},
        {"item_id": "3", "category": "Rings", "product_type": "Rings"}]
STOCK = [{"item_id": "1", "warehouse": "W-East", "stock": 30, "cities": "kolkata|howrah"},
         {"item_id": "1", "warehouse": "W-West", "stock": 100, "cities": "mumbai"},
         {"item_id": "2", "warehouse": "W-East", "stock": 5, "cities": "kolkata|howrah"}]
PRICES = [{"item_id": "2", "selling_price": 499, "mrp": 999, "in_stock": "In Stock"}]


def test_top_earrings_in_kolkata():
    r = bd.aggregate(SALES, CATS, STOCK, PRICES, by="product", city="kolkata", category="earrings")
    assert [x["item_id"] for x in r["rows"]] == ["2", "1"]          # ranked by units, ring left out
    jhumka, studs = r["rows"]
    assert jhumka["units"] == 80 and jhumka["live_price"] == 499 and jhumka["stock_in_city"] == 5
    assert studs["units"] == 50 and studs["stock_in_city"] == 30 and studs["stock_total"] == 130
    assert r["units_total"] == 130 and jhumka["share_pct"] == pytest.approx(61.5)


def test_category_matches_product_type_and_singular():
    r = bd.aggregate(SALES, CATS, STOCK, PRICES, by="product", category="stud")
    assert [x["item_id"] for x in r["rows"]] == ["1"] and r["rows"][0]["units"] == 120


def test_by_category_and_by_city():
    cat = bd.aggregate(SALES, CATS, STOCK, PRICES, by="category")
    assert {x["category"]: x["units"] for x in cat["rows"]} == {"Rings": 200, "Earrings": 200}
    city = bd.aggregate(SALES, CATS, STOCK, PRICES, by="city", category="earrings")
    rows = {x["city"]: x for x in city["rows"]}
    assert rows["Kolkata"]["units"] == 130 and rows["Kolkata"]["stock_in_city"] == 35
    assert rows["Mumbai"]["stock_in_city"] == 100


def test_unknown_grouping_is_refused():
    with pytest.raises(ValueError):
        bd.aggregate(SALES, CATS, STOCK, PRICES, by="warehouse")


def test_chat_has_the_tool():
    assert "get_sales_breakdown" in chat.TOOL_FUNCTIONS
    assert any(t["name"] == "get_sales_breakdown" for t in chat.TOOLS)


def test_tier_grouping_and_filter():
    r = bd.aggregate(SALES, CATS, STOCK, PRICES, by="tier")
    assert {x["tier"]: x["units"] for x in r["rows"]} == {"A.Hero": 120, "No tier": 280}
    hero = bd.aggregate(SALES, CATS, STOCK, PRICES, by="product", category="hero")
    assert [x["item_id"] for x in hero["rows"]] == ["1"]

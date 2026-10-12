"""SKU health flags - made-up rows only, no database."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sku_intel as si  # noqa: E402


def row(**kw):
    base = {"stock": 100, "units_30d": 30, "campaigns": 1, "sp_asked": 300, "sp_live": 300,
            "listing_status": "In Stock", "lp": 200, "cost_price": 100}
    base.update(kw)
    return base


def test_healthy_sku_has_no_flags():
    # 60 units at 1 a day = 60 days of cover: advertised, priced as asked, profitable
    assert si.sku_flags(row(stock=60)) == []


def test_advertised_with_no_stock():
    assert "advertised_oos" in si.sku_flags(row(stock=0))


def test_advertised_and_running_out():
    assert "advertised_low_cover" in si.sku_flags(row(stock=5, units_30d=30))


def test_price_gap_only_judged_on_in_stock_listings():
    assert "price_below_asked" in si.sku_flags(row(stock=60, sp_live=270))
    assert "price_above_asked" in si.sku_flags(row(stock=60, sp_live=330))
    assert not any(f.startswith("price_") for f in si.sku_flags(row(stock=60, sp_live=499, listing_status="Out of Stock")))


def test_profit_and_stock_health():
    assert "below_cost" in si.sku_flags(row(stock=60, lp=90, cost_price=100))
    assert "dead_stock" in si.sku_flags(row(units_30d=0, campaigns=0))
    assert "overstock" in si.sku_flags(row(stock=1000, units_30d=30))
    assert si.days_of_cover(100, 30) == 100.0

"""SKU department agents and the boss's safety rules - made-up rows only:
no database, no Anthropic call."""
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sku_agents as sa  # noqa: E402
import sku_intel as si  # noqa: E402


def row(**kw):
    base = {"sku": "VJ1", "name": "Test jhumka", "pid": "111", "item_id": "222", "sub_category": "Earrings",
            "stock": 60, "units_7d": 7, "units_30d": 30, "campaigns": 1, "campaign_ids": "900",
            "warehouses_with_stock": 2, "sp_asked": 300, "sp_live": 300, "mrp": 600,
            "listing_status": "In Stock", "lp": 200, "cost_price": 100, "offer_units_7d": 0, "brand_fund_7d": 0}
    base.update(kw)
    base["profit_per_unit"] = (base["lp"] - base["cost_price"]) if base.get("lp") and base.get("cost_price") else None
    base["flags"] = si.sku_flags(base)
    return base


def detail(**kw):
    base = {"daily_sales": [{"date": f"d{i}", "units": 1} for i in range(30)],
            "city_sales": [{"city": "Delhi", "units_7d": 5, "units_30d": 20}, {"city": "Bangalore", "units_7d": 2, "units_30d": 10}],
            "stock_by_warehouse": [{"warehouse": "DEL-1", "city": "Delhi", "stock": 40},
                                   {"warehouse": "BLR-1", "city": "Bengaluru", "stock": 20}],
            "price_history": [], "offer_history": [], "city_master": {"1": "Delhi", "2": "Mumbai"},
            "campaigns": [{"campaign_id": "900", "campaign_name": "Earrings city", "mode": "auto", "status": "ACTIVE",
                           "region_type": "CITY", "region_ids": "1,2", "n_products": 4, "spend": 1000, "sales": 3000}]}
    base.update(kw)
    return base


def test_healthy_sku_reports_ok():
    reports = sa.department_reports(row(), detail(), [row()])
    assert [r["department"] for r in reports] == ["Sales", "Inventory", "Price & Offer", "Marketing", "Product"]
    assert {r["department"]: r["status"] for r in reports}["Sales"] == "ok"
    assert sa.inventory_agent(row(), detail())["status"] == "ok"


def test_sales_agent_sees_a_drop_and_no_sales():
    assert sa.sales_agent(row(units_7d=1, units_30d=30), detail())["status"] == "watch"
    assert sa.sales_agent(row(units_7d=0, units_30d=0), detail(city_sales=[]))["status"] == "problem"


def test_inventory_agent_finds_cities_selling_without_stock():
    # Bangalore sales match the Bengaluru warehouse (alias); Pune has none
    d = detail(city_sales=[{"city": "Bangalore", "units_30d": 5}, {"city": "Pune", "units_30d": 3}])
    assert sa.inventory_agent(row(), d)["facts"]["cities_selling_without_stock"] == ["Pune"]
    # a warehouse we cannot place might serve Pune - don't claim it
    d["stock_by_warehouse"].append({"warehouse": "X-9", "city": None, "stock": 5})
    assert sa.inventory_agent(row(), d)["facts"]["cities_selling_without_stock"] == []


def test_inventory_agent_out_of_stock_and_low_cover():
    assert sa.inventory_agent(row(stock=0), detail())["status"] == "problem"
    assert sa.inventory_agent(row(stock=5), detail())["status"] == "problem"


def test_price_agent_profit_and_offer():
    rep = sa.price_agent(row(offer_units_7d=10, brand_fund_7d=500), detail())
    assert rep["facts"]["profit_per_unit"] == 100 and rep["facts"]["brand_fund_per_unit"] == 50
    assert sa.price_agent(row(lp=90), detail())["status"] == "problem"


def test_marketing_agent_flags_targeted_city_without_stock_and_oos_ads():
    rep = sa.marketing_agent(row(), detail())
    assert rep["facts"]["cities_targeted_without_stock"] == ["Mumbai"]
    assert rep["facts"]["campaigns"][0]["roas"] == 3.0
    assert sa.marketing_agent(row(stock=0), detail())["status"] == "problem"


def test_product_agent_ranks_within_sub_category():
    peers = [row(sku=f"P{i}", units_30d=u) for i, u in enumerate([100, 50, 10, 5, 1])]
    assert sa.product_agent(peers[0], peers)["facts"]["role"] == "hero"
    assert sa.product_agent(peers[4], peers)["facts"]["role"] == "tail"
    dead = row(units_30d=0, units_7d=0)
    assert sa.product_agent(dead, peers + [dead])["facts"]["role"] == "dead"


def test_no_extra_ad_spend_without_stock():
    for r in (row(stock=0), row(stock=5)):  # none, and under a week of cover
        assert {"raise_bid", "add_to_campaign"} <= set(sa.blocked_actions(r, "increase_sales"))
        assert sa.enforce({"action": "raise_bid"}, r, "increase_sales")["action"] == "no_action"


def test_no_extra_ad_spend_below_cost_unless_clearing_stock():
    r = row(lp=90)
    assert sa.enforce({"action": "raise_bid"}, r, "increase_sales")["action"] == "no_action"
    assert sa.enforce({"action": "raise_bid"}, r, "clear_stock")["action"] == "raise_bid"


def test_never_mark_a_selling_sku_dead_and_unknown_actions_refused():
    assert sa.enforce({"action": "mark_dead"}, row(), "clear_stock")["action"] == "no_action"
    assert sa.enforce({"action": "mark_dead"}, row(units_30d=0, units_7d=0), "clear_stock")["action"] == "mark_dead"
    out = sa.enforce({"action": "delete_campaign"}, row(), "increase_sales")
    assert out["action"] == "no_action" and "not an allowed action" in out["overruled"]


class FakeClient:
    def __init__(self, reply):
        self.reply, self.calls = reply, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)],
                               usage=SimpleNamespace(input_tokens=1500, output_tokens=200))


def test_boss_is_one_small_call_and_its_answer_is_checked():
    r, d = row(stock=0), detail()
    reports = sa.department_reports(r, d, [r])
    client = FakeClient('Sure: {"action": "raise_bid", "summary": "push it", "reasons": ["x"], "confidence": 0.8}')
    rec = sa.ask_boss(r, reports, "increase_sales", client)
    assert len(client.calls) == 1 and client.calls[0]["model"] == sa.BOSS_MODEL
    assert rec["action"] == "no_action" and "no stock" in rec["overruled"]
    assert len(client.calls[0]["messages"][0]["content"]) < 8000  # keep the prompt small


def test_boss_unreadable_reply_is_no_action():
    rec = sa.ask_boss(row(), [], "protect_profit", FakeClient("I think you should raise it"))
    assert rec["action"] == "no_action"


def test_boss_prompt_is_json_with_goal():
    body = json.loads(sa.boss_prompt("avoid_stockout", row(), []))
    assert body["goal"] == sa.GOALS["avoid_stockout"]


def test_find_skus_prefers_exact_code():
    rows = [row(sku="VJ1"), row(sku="VJ10")]
    assert [r["sku"] for r in si.find_skus(rows, "vj1")] == ["VJ1"]
    assert len(si.find_skus(rows, "VJ")) == 2

"""Breakdown results reach the chat as a real table (built from the tool's
rows, not retyped by the model), saved with the message and shown again on reload."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as webapp  # noqa: E402
import chat  # noqa: E402

RESULT = {"brand": "Voylla", "by": "product", "city": "Kolkata", "category": "earrings", "period": "last 30 days to yesterday",
          "note": "Units = Blinkit sales.",
          "rows": [{"item_id": "1", "product_code": "VMJAI41653", "name": "Nayantara Drop Earrings", "product_type": "Danglers",
                    "tier": "A.Hero", "units": 728, "mrp_value": 254072, "share_pct": 25.0, "stock_in_city": 89,
                    "stock_total": 4425, "live_price": 169.0}]}


def test_table_is_built_from_the_tool_rows():
    t = chat.table_from_result("get_sales_breakdown", {"chart": True}, RESULT)
    keys = [c["key"] for c in t["columns"]]
    assert keys[:3] == ["rank", "product_code", "name"] and "stock_in_city" in keys and "live_price" in keys
    assert t["rows"][0]["mrp_value"] == 254072 and t["rows"][0]["rank"] == 1
    assert t["show_chart"] is True and t["chart"]["value"] == "units"
    assert "Kolkata" in t["title"] and "earrings" in t["title"]
    assert chat.table_from_result("get_notes", {}, RESULT) is None
    assert chat.table_from_result("get_sales_breakdown", {}, {"rows": []}) is None


def test_saved_reply_shows_its_table_again(monkeypatch):
    t = chat.table_from_result("get_sales_breakdown", {}, RESULT)
    msgs = [{"role": "user", "content": "top earrings in kolkata", "tool_calls": None},
            {"role": "assistant", "content": "Nayantara leads.", "tool_calls": json.dumps([{"tool": "get_sales_breakdown", "table": t}])}]
    monkeypatch.setattr(chat, "list_threads", lambda brand: [{"thread_id": "t1", "title": "x"}])
    monkeypatch.setattr(chat, "get_messages", lambda tid: msgs)
    monkeypatch.setattr(webapp.queries, "fetch_notes", lambda *a, **k: [])
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
    html = c.get("/chat?brand=Voylla&thread=t1").get_data(as_text=True)
    assert 'class="chat-tables"' in html and "VMJAI41653" in html
    assert "Download CSV" in html and "copyText" in html and "barChart" in html


def test_markdown_tables_are_ruled_out_in_the_prompt():
    assert "NEVER write a" in chat.SYSTEM_PROMPT and "markdown table" in chat.SYSTEM_PROMPT


def test_mrp_value_is_not_multiplied_by_quantity_again():
    import breakdown
    assert "SUM(COALESCE(mrp, 0)) AS mrp_value" in breakdown.SALES_SQL
    assert "qty_sold * COALESCE(mrp" not in breakdown.SALES_SQL

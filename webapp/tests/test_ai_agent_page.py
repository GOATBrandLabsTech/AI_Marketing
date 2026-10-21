"""AI Agent page: bid suggestions, new keywords and chat under one header;
Before/After retired (old links go to Ads Performance)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as webapp  # noqa: E402
import chat  # noqa: E402
import keyword_ideas  # noqa: E402


def _client(monkeypatch):
    import db
    monkeypatch.setattr(db, "get_engine", lambda: None)
    idea = {"id": 1, "status": "pending", "keyword": "oxidised bangles", "campaign_id": "900", "campaign_name": "Bangles",
            "product_id": "p1", "product_name": "Bangle", "searches": 5000, "growth_7d": 10.0, "growth_28d": 50.0,
            "trend": "Rising", "ai_reason": "fits", "suggested_cpm": 250, "blinkit_cpm_range": None, "push_note": None}
    monkeypatch.setattr(keyword_ideas, "list_ideas", lambda engine, brand: [idea, dict(idea, id=2, keyword="oxidised set")])
    monkeypatch.setattr(chat, "list_threads", lambda brand: [])
    monkeypatch.setattr(webapp.queries, "fetch_notes", lambda *a, **k: [])
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
    return c


def test_keywords_and_chat_share_the_agent_tabs(monkeypatch):
    c = _client(monkeypatch)
    for path in ("/ondemand?brand=Voylla&view=keywords", "/chat?brand=Voylla"):
        html = c.get(path).get_data(as_text=True)
        assert "AI Agent" in html and "Bid suggestions" in html and "New keywords" in html and ">Chat" in html.replace("\n", "").replace(" ", "")
    kw = c.get("/ondemand?brand=Voylla&view=keywords").get_data(as_text=True)
    assert 'aria-current="page"' in kw and ">2</span>" in kw          # 2 ideas waiting, shown on the tab


def test_chat_tab_hidden_for_brand_limited_accounts(monkeypatch):
    c = _client(monkeypatch)
    monkeypatch.setenv("PORTAL_ALL_BRANDS_HEADER", "X-Test-All-Brands")
    limited = c.get("/ondemand?brand=Voylla&view=keywords", headers={"X-Test-All-Brands": "false"}).get_data(as_text=True)
    full = c.get("/ondemand?brand=Voylla&view=keywords", headers={"X-Test-All-Brands": "true"}).get_data(as_text=True)
    assert "/chat?" not in limited and "/chat?" in full


def test_before_after_is_retired(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/impact?brand=Chumbak")
    assert r.status_code == 302 and "/roas" in r.headers["Location"]
    nav = c.get("/ondemand?brand=Voylla&view=keywords").get_data(as_text=True)
    assert "Before / After" not in nav and "On-Demand AI" not in nav and "Agent Chat" not in nav

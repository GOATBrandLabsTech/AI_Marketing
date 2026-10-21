"""New keyword ideas: what is offered, what is filtered out, and that nothing
is added without a person. Made-up rows only."""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import keyword_ideas as ki  # noqa: E402

TODAY = date(2026, 10, 21)
CAMPS = {"p1": [{"campaign_id": "900", "campaign_name": "Bangles", "spend": 1000, "sales": 5000},
                {"campaign_id": "901", "campaign_name": "Old", "spend": 1000, "sales": 1000}],
         "p2": [{"campaign_id": "902", "campaign_name": "Rings", "spend": 500, "sales": 2000}]}


def kw(k, searches=5000, pid="p1", brand_kw=False, s7=2500, s28=1000, **extra):
    return dict({"kw": k, "pid": pid, "searches": searches, "is_brand_keyword": brand_kw, "searches_7d_ago": s7,
                 "searches_28d_ago": s28, "first_seen": date(2026, 7, 29), "snapshot_date": TODAY,
                 "suggested_min": 250, "suggested_max": 300}, **extra)


def build(rows, targeted=(), stock=None):
    return ki.build_candidates(rows, set(targeted), CAMPS, stock or {}, "Voylla", today=TODAY)


def test_untargeted_rising_keyword_goes_to_best_campaign():
    [i] = build([kw("oxidised bangles")])
    assert i["campaign_id"] == "900" and i["trend"] == "Rising fast" and i["growth_7d"] == 100.0
    assert i["suggested_cpm"] == 250


def test_filters_targeted_other_brands_no_stock_no_campaign():
    rows = [kw("oxidised bangles"), kw("giva", brand_kw=True), kw("voylla earrings", brand_kw=True),
            kw("ring set", pid="p2"), kw("anklet", pid="nowhere")]
    out = {i["keyword"] for i in build(rows, targeted={"oxidised bangles"}, stock={"p2": 0})}
    assert out == {"voylla earrings"}       # own brand kept; giva, out of stock, no campaign dropped


def test_finished_occasion_and_broad_one_word_are_skipped():
    rows = [kw("kanjak gift"), kw("navratri necklace"), kw("pink", searches=148860), kw("karwa chauth set")]
    assert {i["keyword"] for i in build(rows)} == {"karwa chauth set"}     # Karwa Chauth is 29 Oct
    assert ki.occasion_over("karwa chauth set", date(2026, 10, 28)) is True


def test_start_cpm_between_floor_and_cap():
    assert ki.start_cpm({"suggested_min": 120}) == 200
    assert ki.start_cpm({"suggested_min": 900}) == 400
    assert ki.start_cpm({"boost_min": 310}) == 310


def test_trend_labels():
    assert ki.trend_label(5, 60, date(2026, 7, 29), TODAY) == "Rising"
    assert ki.trend_label(-30, 10, date(2026, 7, 29), TODAY) == "Falling - past its peak"
    assert ki.trend_label(None, None, date(2026, 10, 15), TODAY) == "New"


def test_caps_per_campaign_and_brand():
    ideas = [{"campaign_id": "900", "keyword": f"k{n}"} for n in range(9)] + [{"campaign_id": "902", "keyword": "r"}]
    kept = ki.cap_per_campaign(ideas, per_campaign=5, per_brand=25)
    assert sum(1 for i in kept if i["campaign_id"] == "900") == 5 and len(kept) == 6


def test_ai_reply_parsing_is_defensive():
    v = ki.parse_ai_verdicts('Sure: [{"i": 0, "relevant": true, "reason": "fits"}, {"i": 1, "relevant": false}]')
    assert v[0]["relevant"] is True and v[1]["relevant"] is False
    assert ki.parse_ai_verdicts("no json here") == {} and ki.parse_ai_verdicts("[not json]") == {}


def test_ai_failure_offers_nothing(monkeypatch):
    import anthropic

    class Boom:
        def __init__(self, **k):
            raise RuntimeError("down")
    monkeypatch.setattr(anthropic, "Anthropic", Boom)
    monkeypatch.setattr("db.get_anthropic_config", lambda: {"api_key": "x", "model": "m"})
    assert ki.ai_check("Voylla", build([kw("oxidised bangles")])) == []


def test_finder_never_accepts_or_pushes():
    src = open(ki.__file__, encoding="utf-8").read()
    find_part = src[src.index("def find_for_brand"):src.index("def list_ideas")]
    assert "accepted" not in find_part and "requests." not in src


def _client(monkeypatch, ideas):
    import app as webapp
    import db
    monkeypatch.setattr(db, "get_engine", lambda: None)
    monkeypatch.setattr(ki, "list_ideas", lambda engine, brand: ideas)
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
    return c


IDEA = {"id": 7, "status": "pending", "keyword": "oxidised bangles", "campaign_id": "900", "campaign_name": "Bangles",
        "product_id": "p1", "product_name": "Oxidised Bangle Set", "searches": 50895, "growth_7d": 12.8, "growth_28d": 537.4,
        "trend": "Rising", "ai_reason": "fits", "suggested_cpm": 266, "blinkit_cpm_range": "266-290",
        "push_note": None}


def test_keywords_tab_lists_pending_ideas(monkeypatch):
    html = _client(monkeypatch, [IDEA]).get("/ondemand?brand=Voylla&view=keywords").get_data(as_text=True)
    assert "oxidised bangles" in html and "50,895" in html and "Accept" in html and "+537%" in html


def test_decide_endpoint_validates_and_passes_the_bid(monkeypatch):
    calls = []
    monkeypatch.setattr(ki, "decide", lambda engine, i, accept, cpm=None, by=None: calls.append((i, accept, cpm)) or True)
    c = _client(monkeypatch, [])
    assert c.post("/api/keyword-ideas/7/decide", json={"accept": True, "cpm": "300"}).status_code == 200
    assert calls == [(7, True, 300.0)]
    assert c.post("/api/keyword-ideas/7/decide", json={"accept": "yes"}).status_code == 400
    assert c.post("/api/keyword-ideas/7/decide", json={"accept": True, "cpm": "abc"}).status_code == 400

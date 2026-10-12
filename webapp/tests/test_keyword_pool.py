"""The replacement-keyword pool sent with every AI call stays small and relevant."""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ondemand_engine as oe  # noqa: E402

ROW = {"suggested_value": "sling_bag", "total_searches": 10, "weighted_score": None, "is_brand": False}


def test_pool_uses_the_campaigns_own_products_and_is_capped(monkeypatch):
    seen = []

    def fake_read_sql(query, engine, params=None):
        seen.append((str(query), params))
        return pd.DataFrame([ROW])

    monkeypatch.setattr(oe.pd, "read_sql", fake_read_sql)
    assert oe._fetch_keyword_pool(object(), "Chumbak", "607078") == [ROW]
    sql, params = seen[0]
    assert "Blinkit_CampaignWise_ProductID" in sql
    assert params == {"brand": "Chumbak", "cid": "607078", "limit": 300}


def test_pool_falls_back_to_the_brand_never_other_brands(monkeypatch):
    seen = []

    def fake_read_sql(query, engine, params=None):
        seen.append(params)
        return pd.DataFrame() if "cid" in params else pd.DataFrame([ROW])

    monkeypatch.setattr(oe.pd, "read_sql", fake_read_sql)
    assert oe._fetch_keyword_pool(object(), "Chumbak", "999") == [ROW]
    assert seen[-1] == {"brand": "Chumbak", "limit": 300}

"""The scheduled-job notebooks in jobs/ must stay safe to commit and must run
the repo's own engine code, not a hand-synced copy."""
import json
import os

import pytest

JOBS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "jobs")
NOTEBOOKS = ["Blinkit_Ondemand_Daily_Suggestions.ipynb", "Blinkit_Ondemand_Bid_Push.ipynb"]


def _load(name):
    with open(os.path.join(JOBS, name), encoding="utf-8") as f:
        return json.load(f)


def _code(nb):
    return "\n".join("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code")


@pytest.mark.parametrize("name", NOTEBOOKS)
def test_notebook_has_no_saved_output(name):
    # outputs can carry live data and tokens, and a run's output must never
    # dirty the job checkout that gets reset to main before every run
    for c in _load(name)["cells"]:
        if c["cell_type"] == "code":
            assert c.get("outputs") == [], f"{name} has saved output - clear it before committing"


@pytest.mark.parametrize("name", NOTEBOOKS)
def test_notebook_uses_repo_engine_code(name):
    code = _code(_load(name))
    assert 'os.path.join(os.getcwd(), "..", "webapp")' in code
    assert "PYTHON_SCRIPTS_DIR" not in code


def test_daily_job_scope_is_the_auto_mode_setting():
    # the panel's Auto switch is the one place that decides what runs daily
    code = _code(_load(NOTEBOOKS[0]))
    assert "oe.run_auto_campaigns()" in code
    assert "SCOPE = [" not in code


def test_push_uses_only_the_newest_campaign_snapshot():
    # 12 Oct: an old PAN_INDIA row with no cities was picked and Blinkit
    # rejected 4 campaigns ("City Ids: String should have at least 1 character")
    code = _code(_load(NOTEBOOKS[1]))
    assert "created_at >= (SELECT MAX(created_at)" in code and "INTERVAL '1 hour'" in code
    assert "never send a city campaign with an empty city list" in code


def test_push_fetches_city_master_read_only_after_login():
    code = _code(_load(NOTEBOOKS[1]))
    assert "blinkit_config.fetch_and_store_city_master(engine, token, cookies)" in code
    # comes after the login check, inside its own try so it can never stop a push
    assert code.index("fetch_and_store_city_master") > code.index("if not token:")


def test_push_applies_sku_removals_safely():
    code = _code(_load(NOTEBOOKS[1]))
    # removals were switched live by Amit on 21 Oct after a checked dry run;
    # the switch itself must stay so they can be paused again
    assert "REMOVALS_DRY_RUN = False" in code or "REMOVALS_DRY_RUN = True" in code
    assert "[REMOVALS DRY RUN]" in code
    assert "sku_actions.products_after_removal(" in code
    assert "sku_actions.mark_removals(engine, removal_ids" in code


def test_push_email_shows_old_and_new_bid():
    code = _code(_load(NOTEBOOKS[1]))
    assert "push_report.change_lines(" in code and "old_cpm_by_kw" in code
    assert 'f"  {kw}: -> {cpm}"' not in code


def test_push_sends_all_india_campaigns_with_city_ids_minus_one():
    """Blinkit refuses city_ids "" (21 Oct: all 11 all-India campaigns failed);
    all-India must go out as "-1", and a CITY campaign with no cities is still refused."""
    code = _code(_load(NOTEBOOKS[1]))
    assert 'meta["region_type"] == "PAN_INDIA"' in code and 'meta["region_ids"] = "-1"' in code
    assert "never send a city campaign with an empty city list" in code


def test_push_adds_accepted_keywords_behind_their_own_switch():
    code = _code(_load(NOTEBOOKS[1]))
    assert "ADDITIONS_DRY_RUN = True" in code or "ADDITIONS_DRY_RUN = False" in code
    assert "[ADDITIONS DRY RUN]" in code
    assert "keyword_ideas.additions_to_apply(engine, brand, campaign_id" in code
    assert "keyword_ideas.mark_additions(engine, addition_ids, new_status, note)" in code
    assert "ok, note, removal_ids, addition_ids = push_campaign(" in code

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


def test_daily_job_stays_on_the_pilot_scope():
    # widening the scope is a deliberate decision, never a side effect
    assert 'SCOPE = [("Voylla", ["296464"])]' in _code(_load(NOTEBOOKS[0]))

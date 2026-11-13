""""Force rerun of AI reports": a forced run never reuses a stored analysis (run: python tests/test_force_rerun.py).
No database or model: the lookups are stubbed."""
import importlib.util
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("stockscan_api_force", ROOT / "api" / "index.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)

DAY = date(2026, 3, 10)
SKILL = {"id": "warren-buffett", "version": "2.1"}


def _stub(force: bool):
    calls = {"find": 0, "reuse": 0}
    saved = api.decision_force_rerun, api.decision_model, api.find_reusable_decision, api.reuse_decision
    api.decision_force_rerun = lambda decision_id: force
    api.decision_model = lambda decision_id: None

    def find(*a, **k):
        calls["find"] += 1
        return {"decision_id": 5, "backtest_id": 1, "finished_at": None}

    def reuse(decision_id, source, log):
        calls["reuse"] += 1
        return {"decision_id": decision_id, "reused_from_id": source["decision_id"]}

    api.find_reusable_decision, api.reuse_decision = find, reuse
    return calls, saved


def _restore(saved):
    api.decision_force_rerun, api.decision_model, api.find_reusable_decision, api.reuse_decision = saved


def test_normal_run_reuses_a_stored_analysis():
    calls, saved = _stub(force=False)
    try:
        r = api.try_reuse_decision(9, SKILL, "AAPL", DAY, api.RunLog("x", "y"))
    finally:
        _restore(saved)
    assert r == {"decision_id": 9, "reused_from_id": 5} and calls == {"find": 1, "reuse": 1}


def test_forced_run_never_reuses_and_logs_once():
    calls, saved = _stub(force=True)
    log = api.RunLog("x", "y")
    try:
        assert api.try_reuse_decision(9, SKILL, "AAPL", DAY, log) is None
        assert api.try_reuse_decision(9, SKILL, "AAPL", DAY, log) is None  # portfolio path checks twice
    finally:
        _restore(saved)
    assert calls == {"find": 0, "reuse": 0}
    assert sum("Forced rerun" in e["message"] for e in log.entries) == 1


def test_forced_portfolio_ignores_decisions_from_earlier_runs():
    saved = api._passess_latest, api._history_latest
    api._passess_latest = lambda cur, job_id, symbol, skill_id: None
    api._history_latest = lambda cur, job, day, symbol, skill_id, log=None: {"sim_date": date(2026, 3, 9), "history": True}
    try:
        job = {"job_id": 1, "config": {"force_rerun": True}}
        assert api._latest_known(None, job, DAY, "AAPL", "warren-buffett") is None
        job["config"]["force_rerun"] = False
        assert api._latest_known(None, job, DAY, "AAPL", "warren-buffett")["history"] is True
    finally:
        api._passess_latest, api._history_latest = saved


def test_requests_accept_the_flag():
    assert api.BacktestRequest(skill="warren-buffett", symbol="AAPL").force_rerun is False
    assert api.BacktestRequest(skill="warren-buffett", symbol="AAPL", force_rerun=True).force_rerun is True
    assert api.PortfolioRequest(force_rerun=True).model_dump(mode="json")["force_rerun"] is True


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

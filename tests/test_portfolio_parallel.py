"""Parallel agent batches and reuse of stored decisions from earlier runs (run: python -m pytest tests).
No database or model is used: the SQL helpers and the agent runner are stubbed."""
import importlib.util
import threading
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("stockscan_api_parallel", ROOT / "api" / "index.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)

DAY = date(2026, 3, 10)
CFG = {"analysis_validity_days": 30, "skip_threshold_pct": 20, "rerun_proximity_pct": 5, "max_buys_per_stock": 3, "buy_amount": 10000,
       "parallel_calls": 5}


def _job(**over):
    return {"job_id": 1, "config": {**CFG, **over}, "symbols": ["AAA", "BBB", "CCC", "DDD"], "cash": 1_000_000,
            "backtests": {k: 1 for k in api.PORTFOLIO_SKILLS}, "ai_calls": 0}


def _row(sim_date, bp, rec="BUY", history=False):
    return {"assessment_id": None if history else 7, "decision_id": 99, "backtest_id": 5, "sim_date": sim_date, "recommendation": rec,
            "buy_price": bp, "stop_price": None, "target_price": None, "history": history}


def _with(own, hist, fn):
    saved = api._passess_latest, api._history_latest
    api._passess_latest = lambda cur, job_id, symbol, skill_id: own
    api._history_latest = lambda cur, job, day, symbol, skill_id: hist
    try:
        return fn()
    finally:
        api._passess_latest, api._history_latest = saved


def test_stored_decision_from_an_earlier_run_avoids_a_fresh_run():
    kind, latest, age, _ = _with(None, _row(date(2026, 3, 2), 100.0, history=True),
                                 lambda: api._freshness(None, _job(), DAY, "AAA", "warren-buffett", 90.0))
    assert kind == "cached" and latest["history"] and age == 8
    assert api._src(latest) == {"source_decision_id": 99, "source_date": date(2026, 3, 2)}


def test_newer_of_own_assessment_and_history_wins():
    own, hist = _row(date(2026, 3, 1), 100.0), _row(date(2026, 3, 5), 50.0, history=True)
    latest = _with(own, hist, lambda: api._latest_known(None, _job(), DAY, "AAA", "warren-buffett"))
    assert latest is hist
    latest = _with(_row(date(2026, 3, 5), 100.0), hist, lambda: api._latest_known(None, _job(), DAY, "AAA", "warren-buffett"))
    assert not latest.get("history"), "same date: this backtest's own assessment is preferred"


def test_freshness_rules_unchanged_without_history():
    f = lambda own, price: _with(own, None, lambda: api._freshness(None, _job(), DAY, "AAA", "warren-buffett", price))[0]
    assert f(None, 10.0) == "run"
    assert f(_row(date(2026, 1, 1), 100.0), 90.0) == "run"          # older than 30 days
    assert f(_row(DAY, 100.0), 90.0) == "today"
    assert f(_row(date(2026, 3, 1), None), 90.0) == "no_buy_price"
    assert f(_row(date(2026, 3, 1), 100.0), 130.0) == "skip"         # 30% above the buy price
    assert f(_row(date(2026, 3, 1), 100.0), 103.0) == "rerun"        # within 5%
    assert f(_row(date(2026, 3, 1), 100.0), 80.0) == "cached"


def test_history_query_is_point_in_time_and_matches_skill_version_and_model():
    seen = {}

    class Cur:
        def execute(self, sql, params):
            seen["sql"], seen["params"] = sql, params

        def fetchone(self):
            return None

    saved = api.get_skill, api.backtest_model
    api.get_skill = lambda skill_id: {"id": skill_id, "version": "2.1"}
    api.backtest_model = lambda bt: "claude-haiku-5-5"
    try:
        assert api._history_latest(Cur(), _job(), DAY, "AAA", "warren-buffett") is None
    finally:
        api.get_skill, api.backtest_model = saved
    assert "d.as_of_date <= %s AND d.as_of_date >= %s" in seen["sql"] and "reused_from_id IS NULL" in seen["sql"]
    assert seen["params"] == ("warren-buffett", "AAA", DAY, date(2026, 2, 8), "2.1", "claude-haiku-5-5")


def test_parallel_batch_runs_at_the_same_time_and_keeps_the_model_context():
    running, peak, models, lock = [0], [0], [], threading.Lock()

    def fake_run(job, day, symbol, skill_id, phase, mode, price, log):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            models.append(api._MODEL_OVERRIDE.get())
        time.sleep(0.2)
        with lock:
            running[0] -= 1
        if symbol == "CCC":
            raise RuntimeError("model timeout")
        return {"symbol": symbol, "is_buy": symbol == "AAA"}

    saved = api._run_portfolio_agent
    api._run_portfolio_agent = fake_run
    log = api.RunLog("x", "y")
    try:
        with api.use_model("claude-haiku-5-5"):
            t0 = time.monotonic()
            a = api._run_agents_parallel(_job(), DAY, "warren-buffett", [("AAA", "run", 1.0), ("BBB", "run", 1.0), ("CCC", "rerun", 1.0)], log)
            elapsed = time.monotonic() - t0
    finally:
        api._run_portfolio_agent = saved
    assert a == {"symbol": "AAA", "is_buy": True}
    assert peak[0] == 3 and elapsed < 0.5, (peak, elapsed)
    assert models == ["claude-haiku-5-5"] * 3
    assert any("CCC: parallel Buffett run failed" in e["message"] and e["level"] == "warn" for e in log.entries)


def test_stage_batch_picks_later_stocks_that_need_the_same_agent():
    bars = {s: {"bar": {"open": 1}, "prev_close": 10.0} for s in "AAA BBB CCC DDD".split()}
    bars["CCC"] = {"bar": None, "prev_close": 10.0}                  # did not trade
    today = {("DDD", "warren-buffett"): {"is_buy": True}}
    saved = api._todays_buy_row, api._freshness
    api._todays_buy_row = lambda cur, job_id, day, s, sk: today.get((s, sk))
    api._freshness = lambda cur, job, day, s, sk, price: ("run", None, None, None)
    try:
        assert api._stage_batch(None, _job(), DAY, 0, "buffett", bars, {}, 4) == [("BBB", "run", 10.0)]
        assert api._stage_batch(None, _job(), DAY, 0, "dimon", bars, {}, 4) == [("DDD", "run", 10.0)]
        assert api._stage_batch(None, _job(), DAY, 0, "buffett", bars, {}, 0) == []
        assert api._stage_batch(None, {**_job(), "cash": 5}, DAY, 0, "buffett", bars, {}, 4) == []
    finally:
        api._todays_buy_row, api._freshness = saved


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

"""A stored decision whose report file is not in file storage is never reused: the agent runs again
(run: python tests/test_report_missing.py). Storage and database are stubbed: no network."""
import importlib.util
import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("stockscan_api_report_missing", ROOT / "api" / "index.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)

DAY = date(2026, 3, 10)
PDF = "reports/warren-buffett/TRI/2026/2026-03-02-7.pdf"
HTML = PDF[:-4] + ".html"


class _Container:
    def __init__(self, names):
        self.names = set(names)
        self.checks = 0

    def get_blob_client(self, name):
        outer = self

        class C:
            def exists(self):
                outer.checks += 1
                return name in outer.names
        return C()


def _azure(names):
    """Use Azure with a fake container holding `names`; returns (container, restore)."""
    saved_env = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    saved = api._azure_container
    container = _Container(names)
    os.environ["AZURE_STORAGE_CONNECTION_STRING"] = "x"
    api._azure_container = lambda: container
    api._FILES_PRESENT.clear()

    def restore():
        api._azure_container = saved
        api._FILES_PRESENT.clear()
        if saved_env is None:
            os.environ.pop("AZURE_STORAGE_CONNECTION_STRING", None)
        else:
            os.environ["AZURE_STORAGE_CONNECTION_STRING"] = saved_env
    return container, restore


def test_report_files_present_rules():
    container, restore = _azure({PDF, HTML})
    try:
        assert api.report_files_present(PDF, HTML) == (True, "")
        assert api.report_files_present(PDF, None)[0] is True          # older decisions without an HTML report
        present, why = api.report_files_present(None, None)
        assert not present and "no report file" in why
        container.names.discard(HTML)
        api._FILES_PRESENT.clear()  # confirmed-present files are cached per server process
        present, why = api.report_files_present(PDF, HTML)
        assert not present and HTML in why and "Azure Blob Storage" in why
        checks = container.checks
        api.report_files_present(PDF, None)
        assert container.checks == checks, "a file confirmed present is not checked again"
    finally:
        restore()


def test_unreachable_or_unconfigured_storage_counts_as_present():
    saved_env = os.environ.pop("AZURE_STORAGE_CONNECTION_STRING", None)
    saved_tokens = {k: os.environ.pop(k, None) for k in ("BLOB_READ_WRITE_TOKEN", "VERCEL_BLOB_READ_WRITE_TOKEN")}
    try:
        assert api.report_files_present(None, None)[0] is True       # no storage configured
    finally:
        for k, v in {**saved_tokens, "AZURE_STORAGE_CONNECTION_STRING": saved_env}.items():
            if v is not None:
                os.environ[k] = v
    _, restore = _azure(set())
    saved = api._azure_container

    def boom():
        raise RuntimeError("network down")
    api._azure_container = boom
    try:
        present, why = api.report_files_present(PDF, HTML)
        assert present and "could not be checked" in why
    finally:
        api._azure_container = saved
        restore()


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=()):
        pass

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def test_same_date_reuse_skips_a_decision_without_its_report():
    _, restore = _azure({"reports/older.pdf"})
    saved = api.db_connect
    rows = [(12, 3, None, PDF, HTML), (8, 2, None, "reports/older.pdf", None)]
    api.db_connect = lambda: type("Conn", (), {"cursor": lambda self: _Cur(rows), "close": lambda self: None})()
    log = api.RunLog("x", "y")
    try:
        src = api.find_reusable_decision({"id": "warren-buffett", "version": "1"}, "TRI", DAY, exclude_id=20, log=log)
    finally:
        api.db_connect = saved
        restore()
    assert src["decision_id"] == 8, src
    assert any("#12" in e["message"] and "not reused" in e["message"] for e in log.entries)


def test_portfolio_history_and_own_assessment_need_their_report():
    _, restore = _azure(set())
    saved = api.get_skill, api.backtest_model, api._passess_latest, api._history_latest
    api.get_skill = lambda s: {"id": s, "version": "1"}
    api.backtest_model = lambda bt: "m"
    job = {"job_id": 1, "config": {"analysis_validity_days": 30}, "backtests": {"warren-buffett": 1}}
    hist_row = (5, 2, date(2026, 3, 2), "BUY", 10.0, 12.0, 8.0, 15.0, PDF, HTML)
    try:
        log = api.RunLog("x", "y")
        assert api._history_latest(_Cur([hist_row]), job, DAY, "TRI", "warren-buffett", log) is None
        assert any("stored decision #5" in e["message"] for e in log.entries)

        own = {"assessment_id": 3, "sim_date": date(2026, 3, 5), "recommendation": "BUY", "current_price": 10.0, "buy_price": 12.0,
               "stop_price": 8.0, "target_price": 15.0, "decision_id": 9, "mode": "run"}
        api._passess_latest = lambda cur, job_id, symbol, skill_id: dict(own)
        api._history_latest = lambda cur, job, day, symbol, skill_id, log=None: None
        kind, latest, age, _ = api._freshness(_Cur([(PDF, HTML)]), job, DAY, "TRI", "warren-buffett", 11.0, log)
        assert kind == "run" and "missing" in latest["report_missing"] and age == 5
    finally:
        api.get_skill, api.backtest_model, api._passess_latest, api._history_latest = saved
        restore()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

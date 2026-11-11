"""Universe readiness follows the calendar, and price catch-ups never hide an empty Yahoo response.
Run: python tests/test_universe_catchup.py (or python -m pytest tests). No database or network is used."""
import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("stockscan_api", ROOT / "api" / "index.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


def test_last_closed_session():
    utc = lambda *a: datetime(*a, tzinfo=timezone.utc)  # noqa: E731
    # Wed 2026-11-11 15:00 New York (20:00 UTC): market still open -> Tuesday
    assert api.last_closed_session(utc(2026, 11, 11, 20, 0)) == date(2026, 11, 10)
    # Wed 17:00 New York (22:00 UTC): closed -> Wednesday
    assert api.last_closed_session(utc(2026, 11, 11, 22, 0)) == date(2026, 11, 11)
    # Saturday and Monday morning -> the Friday before
    assert api.last_closed_session(utc(2026, 11, 14, 18, 0)) == date(2026, 11, 13)
    assert api.last_closed_session(utc(2026, 11, 16, 14, 0)) == date(2026, 11, 13)


def test_ready_needs_current_prices():
    exp = date(2026, 11, 10)
    assert api.universe_item_state(0, None, None, exp) == "missing"
    # TECH matches the stored prices, but the prices stop weeks ago: that is stale, not ready (the AAPL case)
    assert api.universe_item_state(500, date(2026, 10, 20), date(2026, 10, 20), exp) == "stale"
    assert api.universe_item_state(500, exp, date(2026, 11, 7), exp) == "stale"
    assert api.universe_item_state(500, exp, exp, exp) == "ready"


class _Cur:
    def __init__(self, count, last):
        self.r = (count, last)

    def execute(self, *a):
        pass

    def fetchone(self):
        return self.r


class _Conn:
    def __init__(self, count, last):
        self.c = _Cur(count, last)

    def cursor(self):
        return self.c

    def close(self):
        pass


def _run_delta(last, expected, fetched):
    """update_prices_delta with the database, Yahoo and the clock stubbed. Returns (result or exception, saved rows)."""
    saved = []
    orig = (api.db_connect, api.fetch_history, api.save_prices, api.last_closed_session)
    api.db_connect = lambda: _Conn(400, last)
    api.save_prices = lambda s, rows: saved.extend(rows)
    api.last_closed_session = lambda now=None: expected

    def fake_fetch(symbol, start=None):
        assert start == last, "the catch-up re-reads the last stored day"
        if not fetched:
            raise api.HTTPException(status_code=404, detail="no rows")
        return [api.PriceRow(trade_date=d, open=1.0, high=1.0, low=1.0, close=1.0, adj_close=1.0, volume=0) for d in fetched]

    api.fetch_history = fake_fetch
    try:
        return api.update_prices_delta("AAPL"), saved
    except api.HTTPException as e:
        return e, saved
    finally:
        api.db_connect, api.fetch_history, api.save_prices, api.last_closed_session = orig


def test_catchup_adds_new_days_and_refreshes_last():
    res, saved = _run_delta(date(2026, 11, 5), date(2026, 11, 10), [date(2026, 11, d) for d in (5, 6, 9, 10)])
    assert res == {"action": "delta", "added": 3, "last_date": "2026-11-10"}
    assert saved[0].trade_date == date(2026, 11, 5)  # the last stored bar is rewritten with the final close


def test_current_symbol_is_not_fetched():
    res, saved = _run_delta(date(2026, 11, 10), date(2026, 11, 10), None)
    assert res["action"] == "skipped" and not saved


def test_empty_yahoo_response_fails_when_far_behind():
    res, _ = _run_delta(date(2026, 10, 20), date(2026, 11, 10), None)
    assert isinstance(res, api.HTTPException) and res.status_code == 502 and "21 days behind" in res.detail


def test_empty_yahoo_response_over_a_holiday_is_skipped():
    res, _ = _run_delta(date(2026, 11, 25), date(2026, 11, 26), None)  # Thanksgiving: no bar for the 26th
    assert res["action"] == "skipped"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

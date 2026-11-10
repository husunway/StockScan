"""Server-side checks of the portfolio backtest field rules (run: python -m pytest tests, or python tests/test_portfolio_fields.py).
The form runs the same cases in tests/portfolio-fields.test.mjs; both read data/portfolio_fields.json."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("stockscan_api", ROOT / "api" / "index.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)
FIELDS = json.loads((ROOT / "data" / "portfolio_fields.json").read_text(encoding="utf-8"))["fields"]
BY_KEY = {f["key"]: f for f in FIELDS}


def test_every_default_passes_server_validation():
    values, errors = api.validate_portfolio_config(api.PortfolioRequest().model_dump())
    assert errors == {}, errors
    for f in FIELDS:
        assert values[f["key"]] == f["default"]


def test_whole_dollar_amounts_and_decimal_percentages_accepted():
    for raw, want in (("10000000", 10_000_000), ("10,000,000", 10_000_000), ("12345678", 12_345_678), (5000, 5000), ("$5,000", 5000)):
        for key in ("starting_capital", "buy_amount"):
            v, err = api.validate_portfolio_field(BY_KEY[key], raw)
            assert err is None and v == want, (key, raw, err)
    for raw, want in (("7.5", 7.5), (12.5, 12.5), ("10", 10.0), ("0.25%", 0.25)):
        v, err = api.validate_portfolio_field(BY_KEY["take_profit_pct"], raw)
        assert err is None and v == want, (raw, err)


def test_invalid_input_gets_the_same_messages_as_the_form():
    cap = BY_KEY["starting_capital"]
    assert api.validate_portfolio_field(cap, "")[1] == "Starting capital is required."
    assert "must be a whole dollar amount" in api.validate_portfolio_field(cap, "abc")[1]
    assert api.validate_portfolio_field(cap, -5)[1] == "Starting capital must be a whole dollar amount between $1,000 and $10,000,000,000."
    assert "without decimals" in api.validate_portfolio_field(cap, "10000.5")[1]
    assert api.validate_portfolio_field(BY_KEY["take_profit_pct"], 0)[1] == "Take profit % must be a number between 0.1% and 1,000%."
    assert api.validate_portfolio_field(BY_KEY["held_reassess_days"], 0)[1] == \
        "Held reassess must be a whole number between 1 trading day and 60 trading days."
    _, errors = api.validate_portfolio_config({**api.PortfolioRequest().model_dump(), "buy_amount": None})
    assert list(errors) == ["buy_amount"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

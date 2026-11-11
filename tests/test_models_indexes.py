"""Model selection from the environment and the index registry.
Run: python tests/test_models_indexes.py (or python -m pytest tests)."""
import csv
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("stockscan_api", ROOT / "api" / "index.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return old


def test_available_models_default_first_and_deduplicated():
    old = _env(DEFAULT_AI_MODEL="claude-opus-5-5", AI_MODELS="claude-sonnet-5-5, claude-opus-5-5;claude-haiku-5-5\nclaude-fable-5-1")
    try:
        assert api.available_models() == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]
        assert api.resolve_model(None) == "claude-opus-5-5"
        assert api.resolve_model(" claude-haiku-5-5 ") == "claude-haiku-5-5"
        try:
            api.resolve_model("gpt-x")
            raise AssertionError("an unconfigured model must be rejected")
        except api.HTTPException as e:
            assert e.status_code == 422 and "not configured" in e.detail
    finally:
        _env(**old)


def test_default_when_nothing_configured():
    old = _env(DEFAULT_AI_MODEL=None, AI_MODELS=None)
    try:
        assert api.available_models() == [api.DEFAULT_CLAUDE_MODEL]
        assert api.requested_model() == api.DEFAULT_CLAUDE_MODEL
    finally:
        _env(**old)


def test_use_model_scopes_the_override():
    old = _env(DEFAULT_AI_MODEL="claude-opus-5-5", AI_MODELS="claude-sonnet-5-5")
    try:
        assert api.requested_model() == "claude-opus-5-5"
        with api.use_model("claude-sonnet-5-5"):
            assert api.requested_model() == "claude-sonnet-5-5"
            with api.use_model(None):  # runs from before model selection fall back to the default
                assert api.requested_model() == "claude-opus-5-5"
            assert api.requested_model() == "claude-sonnet-5-5"
        assert api.requested_model() == "claude-opus-5-5"
    finally:
        _env(**old)


def test_index_registry():
    assert api.index_spec(None)["key"] == "sp500"
    assert api.index_spec("NASDAQ100")["table"] == "dbo.NASDAQ100_CONSTITUENTS"
    try:
        api.index_spec("dow30")
        raise AssertionError("unknown index must be rejected")
    except api.HTTPException as e:
        assert e.status_code == 422
    # Both constituents tables are created, with distinct constraint names.
    assert "dbo.SP500_CONSTITUENTS" in api.ENSURE_UNIVERSE_SQL and "dbo.NASDAQ100_CONSTITUENTS" in api.ENSURE_UNIVERSE_SQL
    assert "DF_sp500_active" in api.ENSURE_UNIVERSE_SQL and "DF_ndx100_active" in api.ENSURE_UNIVERSE_SQL


def test_seed_files_exist_and_are_unique():
    for key, spec in api.INDEXES.items():
        assert spec["seed"].is_file(), key
        with open(spec["seed"], newline="", encoding="utf-8") as f:
            syms = [api.yahoo_symbol(r["symbol"]) for r in csv.DictReader(f)]
        assert len(syms) == len(set(syms)), key
    with open(api.INDEXES["nasdaq100"]["seed"], newline="", encoding="utf-8") as f:
        assert 95 <= sum(1 for _ in csv.DictReader(f)) <= 105


def test_requests_accept_index_and_model():
    assert api.ScanRequest(index="nasdaq100", model="x").index == "nasdaq100"
    assert api.PortfolioRequest().index == "sp500"
    assert api.UniverseLoadRequest(index="nasdaq100").index == "nasdaq100"
    try:
        api.ScanRequest(index="dow30")
        raise AssertionError("pattern must reject unknown index")
    except Exception as e:  # pydantic ValidationError
        assert "index" in str(e)
    # Extra keys do not disturb the shared field validation.
    values, errors = api.validate_portfolio_config({**api.PortfolioRequest().model_dump(), "index": "nasdaq100", "model": "m"})
    assert not errors


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

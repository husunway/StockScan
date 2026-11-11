"""Verdicts read back from CHAR(4) columns are space-padded ('BUY '); they must still count as BUY.
Run: python tests/test_verdicts.py (or python -m pytest tests)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("stockscan_api", ROOT / "api" / "index.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


def test_norm_rec():
    assert api.norm_rec("BUY ") == "BUY"
    assert api.norm_rec(" hold") == "HOLD"
    assert api.norm_rec("") is None and api.norm_rec(None) is None


def test_padded_buy_counts_as_buy():
    # The reported case: cached Buffett BUY, buy at/below 128.00, current price 110.51.
    assert api._agent_is_buy("BUY ", 128.0, 110.51)
    assert api._agent_is_buy("BUY", 128.0, 128.0)
    assert not api._agent_is_buy("BUY ", 128.0, 128.01)
    assert not api._agent_is_buy("HOLD", 128.0, 110.51)


def test_decision_rows_are_normalized():
    row = [None] * len(api.DECISION_COLUMNS)
    row[api.DECISION_COLUMNS.index("recommendation")] = "BUY "
    assert api._decision_dict(row)["recommendation"] == "BUY"
    item = [None] * len(api.SCAN_ITEM_COLUMNS)
    for k in ("murphy_rec", "buffett_rec", "dimon_rec"):
        item[api.SCAN_ITEM_COLUMNS.index(k)] = "BUY "
    d = api._scan_item_dict(item)
    assert (d["murphy_rec"], d["buffett_rec"], d["dimon_rec"]) == ("BUY", "BUY", "BUY")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

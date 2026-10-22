import os
import platform
from datetime import date
from decimal import Decimal

import base64
import hashlib
import hmac
import json
import re
import time
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv

    load_dotenv(".env.local")  # local dev only; Vercel injects env vars directly
except ImportError:
    pass

app = FastAPI(
    title="StockScan API",
    docs_url="/api/py/docs",
    openapi_url="/api/py/openapi.json",
)

# ---------------------------------------------------------------------------
# Session check - verifies the cookie issued by app/api/auth/login (see lib/session.ts)
# so the Python function is protected even if reached without going through Next.js.
# ---------------------------------------------------------------------------
SESSION_COOKIE = "stockscan_session"
PUBLIC_PATHS = {"/api/py/health"}


def verify_session(token: str | None, secret: str | None) -> str | None:
    if not token or not secret or "." not in token:
        return None
    payload, sig = token.split(".", 1)
    expected = (
        base64.urlsafe_b64encode(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
        .rstrip(b"=")
        .decode()
    )
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, TypeError):
        return None
    exp = data.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None
    return str(data.get("u", ""))


@app.middleware("http")
async def require_session(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/py/") and path not in PUBLIC_PATHS:
        if not verify_session(request.cookies.get(SESSION_COOKIE), os.environ.get("AUTH_SECRET")):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return await call_next(request)


TABLE = "dbo.stock_prices"
HISTORY_PERIOD = "2y"
INSERT_CHUNK = 200  # rows per INSERT; keeps params well under SQL Server's 2100 limit


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class LoadRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)


class PriceRow(BaseModel):
    trade_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adj_close: float | None
    volume: int | None


class LoadResponse(BaseModel):
    symbol: str
    rows_saved: int
    start_date: date
    end_date: date
    table: str
    preview: list[PriceRow]


# ---------------------------------------------------------------------------
# Yahoo Finance
# ---------------------------------------------------------------------------
def normalize_symbol(raw: str) -> str:
    symbol = raw.strip().upper()
    if not symbol or not symbol.replace(".", "").replace("-", "").replace("^", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid ticker symbol")
    return symbol


def fetch_history(symbol: str, start: "date | None" = None) -> list[PriceRow]:
    """Daily bars from Yahoo Finance: the default two-year window, or from `start` onward for delta loads."""
    import yfinance as yf

    kwargs = {"start": start.isoformat()} if start else {"period": HISTORY_PERIOD}
    df = yf.Ticker(symbol).history(interval="1d", auto_adjust=False, actions=False, **kwargs)
    if df is None or df.empty:
        raise HTTPException(
            status_code=404, detail=f"No price history found on Yahoo Finance for {symbol}"
        )

    def num(v):
        return None if v is None or v != v else float(v)  # NaN check

    rows: list[PriceRow] = []
    for ts, r in df.iterrows():
        close = num(r.get("Close"))
        if close is None:
            continue
        vol = r.get("Volume")
        rows.append(
            PriceRow(
                trade_date=ts.date(),
                open=num(r.get("Open")),
                high=num(r.get("High")),
                low=num(r.get("Low")),
                close=close,
                adj_close=num(r.get("Adj Close")),
                volume=None if vol is None or vol != vol else int(vol),
            )
        )
    if not rows:
        raise HTTPException(status_code=404, detail=f"No usable price rows for {symbol}")
    return rows


# ---------------------------------------------------------------------------
# Azure SQL
# ---------------------------------------------------------------------------
def db_connect():
    import pymssql

    cfg = {k: os.environ.get(k) for k in
           ("AZURE_SQL_SERVER", "AZURE_SQL_DATABASE", "AZURE_SQL_USER", "AZURE_SQL_PASSWORD")}
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Database not configured. Missing environment variables: {', '.join(missing)}",
        )
    try:
        return pymssql.connect(
            server=cfg["AZURE_SQL_SERVER"],
            database=cfg["AZURE_SQL_DATABASE"],
            user=cfg["AZURE_SQL_USER"],
            password=cfg["AZURE_SQL_PASSWORD"],
            tds_version="7.4",
            login_timeout=15,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not connect to Azure SQL: {e}") from e


ENSURE_TABLE_SQL = f"""
IF OBJECT_ID('{TABLE}', 'U') IS NULL
CREATE TABLE {TABLE} (
    symbol      NVARCHAR(20)   NOT NULL,
    trade_date  DATE           NOT NULL,
    [open]      DECIMAL(18, 4) NULL,
    high        DECIMAL(18, 4) NULL,
    low         DECIMAL(18, 4) NULL,
    [close]     DECIMAL(18, 4) NULL,
    adj_close   DECIMAL(18, 4) NULL,
    volume      BIGINT         NULL,
    loaded_at   DATETIME2      NOT NULL CONSTRAINT DF_stock_prices_loaded_at DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_stock_prices PRIMARY KEY (symbol, trade_date)
);
"""


def save_prices(symbol: str, rows: list[PriceRow]) -> None:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(ENSURE_TABLE_SQL)

        # Replace the symbol's rows for the fetched date range so reloads are idempotent.
        cur.execute(
            f"DELETE FROM {TABLE} WHERE symbol = %s AND trade_date BETWEEN %s AND %s",
            (symbol, rows[0].trade_date, rows[-1].trade_date),
        )

        row_sql = "(%s, %s, %s, %s, %s, %s, %s, %s)"
        for i in range(0, len(rows), INSERT_CHUNK):
            chunk = rows[i : i + INSERT_CHUNK]
            params: list = []
            for r in chunk:
                params.extend(
                    [symbol, r.trade_date, r.open, r.high, r.low, r.close, r.adj_close, r.volume]
                )
            cur.execute(
                f"INSERT INTO {TABLE} (symbol, trade_date, [open], high, low, [close], adj_close, volume) "
                f"VALUES {', '.join([row_sql] * len(chunk))}",
                tuple(params),
            )
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save prices: {e}") from e
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/api/py/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
MA_PERIODS = (5, 10, 20, 30, 50, 200)
CROSS_PAIRS = ((5, 20), (20, 50), (50, 200))
RS_WINDOWS = {"1m": 21, "3m": 63, "6m": 126, "1y": 252}


def table_exists(cur) -> bool:
    cur.execute(f"SELECT OBJECT_ID('{TABLE}', 'U')")
    return cur.fetchone()[0] is not None


def load_prices_df(cur, symbol: str):
    import pandas as pd

    cur.execute(
        f"SELECT trade_date, [open], high, low, [close], adj_close, volume FROM {TABLE} "
        f"WHERE symbol = %s ORDER BY trade_date",
        (symbol,),
    )
    rows = cur.fetchall()
    if not rows:
        raise HTTPException(
            status_code=404, detail=f"No prices stored for {symbol}. Load it on the Load Prices page first."
        )
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "adj_close", "volume"])
    for c in ("open", "high", "low", "close", "adj_close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    df["date"] = df["date"].astype(str)
    return df


def num(x, nd=4):
    """float or None for NaN/inf - FastAPI cannot serialize NaN."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v or v in (float("inf"), float("-inf")) else round(v, nd)


def points(dates, series, nd=4):
    out = []
    for d, raw in zip(dates, series):
        v = num(raw, nd)
        if v is not None:
            out.append({"time": d, "value": v})
    return out


def compute_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return 100 - 100 / (1 + avg_gain / avg_loss)


def compute_levels(df, lookback=250, window=5, tolerance=0.01):
    """Pivot highs/lows clustered into support and resistance levels."""
    import numpy as np

    recent = df.tail(lookback).reset_index(drop=True)
    high, low, dates = recent["high"].values, recent["low"].values, recent["date"].values
    pivots = []  # (level, date)
    for i in range(window, len(recent) - window):
        if high[i] == high[i - window : i + window + 1].max():
            pivots.append((high[i], dates[i]))
        if low[i] == low[i - window : i + window + 1].min():
            pivots.append((low[i], dates[i]))
    if not pivots:
        return [], []

    last_close = float(df["close"].iloc[-1])
    band = last_close * tolerance
    clusters = []
    for level, d in sorted(pivots):
        if clusters and level - clusters[-1]["levels"][-1] <= band:
            clusters[-1]["levels"].append(level)
            clusters[-1]["last_date"] = max(clusters[-1]["last_date"], d)
        else:
            clusters.append({"levels": [level], "last_date": d})

    levels = [
        {"level": num(np.mean(c["levels"]), 2), "touches": len(c["levels"]), "last_date": str(c["last_date"])}
        for c in clusters
    ]
    support = [l for l in levels if l["level"] < last_close]
    resistance = [l for l in levels if l["level"] > last_close]
    # Strongest first, nearest to price as tie-break; keep the three closest of the strongest.
    support = sorted(support, key=lambda l: (-l["touches"], last_close - l["level"]))[:3]
    resistance = sorted(resistance, key=lambda l: (-l["touches"], l["level"] - last_close))[:3]
    for l in support:
        l["type"] = "support"
    for l in resistance:
        l["type"] = "resistance"
    return sorted(support, key=lambda l: -l["level"]), sorted(resistance, key=lambda l: l["level"])


def compute_relative_strength(df, bench_df, bench_symbol: str):
    import pandas as pd

    merged = pd.merge(
        df[["date", "close"]], bench_df[["date", "close"]], on="date", suffixes=("", "_bench")
    )
    if len(merged) < 2:
        return None
    ratio = merged["close"] / merged["close_bench"]
    line = ratio / ratio.iloc[0] * 100
    line_ma50 = line.rolling(50).mean()

    windows = {}
    for label, n in RS_WINDOWS.items():
        if len(merged) > n:
            stock = merged["close"].iloc[-1] / merged["close"].iloc[-1 - n] - 1
            bench = merged["close_bench"].iloc[-1] / merged["close_bench"].iloc[-1 - n] - 1
            windows[label] = {
                "stock": num(stock * 100, 2),
                "benchmark": num(bench * 100, 2),
                "excess": num((stock - bench) * 100, 2),
            }

    last, ma = line.iloc[-1], line_ma50.iloc[-1]
    state = "n/a" if ma != ma else ("outperforming" if last > ma else "underperforming")
    return {
        "benchmark": bench_symbol,
        "line": points(merged["date"], line, 2),
        "line_ma50": points(merged["date"], line_ma50, 2),
        "windows": windows,
        "state": state,
        "rs_20d_change": num((line.iloc[-1] / line.iloc[-21] - 1) * 100, 2) if len(line) > 21 else None,
    }


def compute_analysis(symbol: str, df, benchmarks: dict):
    import numpy as np

    dates = df["date"].tolist()
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    out: dict = {"symbol": symbol, "rows": len(df), "start_date": dates[0], "end_date": dates[-1]}

    out["candles"] = [
        {"time": d, "open": num(o), "high": num(h), "low": num(l), "close": num(c)}
        for d, o, h, l, c in zip(dates, df["open"], high, low, close)
        if num(c) is not None
    ]

    # Moving averages and crossovers
    mas = {p: close.rolling(p).mean() for p in MA_PERIODS}
    out["moving_averages"] = {str(p): points(dates, mas[p]) for p in MA_PERIODS}
    crossovers = []
    for fast, slow in CROSS_PAIRS:
        sign = np.sign(mas[fast] - mas[slow])
        prev = sign.shift(1)
        hits = (sign != prev) & sign.notna() & prev.notna() & (sign != 0)
        for i in np.flatnonzero(hits.values):
            crossovers.append(
                {
                    "date": dates[i],
                    "pair": f"MA{fast}/MA{slow}",
                    "type": "bullish" if sign.iloc[i] > 0 else "bearish",
                    "price": num(close.iloc[i], 2),
                }
            )
    crossovers.sort(key=lambda e: e["date"])
    out["crossovers"] = crossovers

    # RSI
    rsi = compute_rsi(close)
    out["rsi"] = points(dates, rsi, 2)

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd.iloc[:25] = np.nan
    signal = macd.ewm(span=9, adjust=False).mean()
    signal.iloc[:33] = np.nan
    hist = macd - signal
    out["macd"] = {
        "macd": points(dates, macd),
        "signal": points(dates, signal),
        "histogram": points(dates, hist),
    }

    # Bollinger Bands
    mid = mas[20]
    std = close.rolling(20).std(ddof=0)
    upper, lower = mid + 2 * std, mid - 2 * std
    pct_b = (close - lower) / (upper - lower)
    bandwidth = (upper - lower) / mid * 100
    out["bollinger"] = {
        "upper": points(dates, upper),
        "middle": points(dates, mid),
        "lower": points(dates, lower),
        "pct_b": points(dates, pct_b, 3),
        "bandwidth": points(dates, bandwidth, 2),
    }

    # Volume
    vol_ma20 = vol.rolling(20).mean()
    vol_ratio = vol / vol_ma20
    ret = close.pct_change() * 100
    out["volume"] = [
        {"time": d, "value": num(v, 0), "up": bool(c >= o), "ratio": num(r, 2)}
        for d, v, c, o, r in zip(dates, vol, close, df["open"], vol_ratio)
        if num(v) is not None
    ]
    out["volume_ma20"] = points(dates, vol_ma20, 0)
    confirmations = []
    for i in range(max(1, len(df) - 60), len(df)):
        r, vr = ret.iloc[i], vol_ratio.iloc[i]
        if r != r or vr != vr or abs(r) < 2:
            continue
        if vr >= 1.5:
            confirmations.append({"date": dates[i], "direction": "up" if r > 0 else "down",
                                  "confirmed": True, "return_pct": num(r, 2), "volume_ratio": num(vr, 2)})
        elif vr < 0.8:
            confirmations.append({"date": dates[i], "direction": "up" if r > 0 else "down",
                                  "confirmed": False, "return_pct": num(r, 2), "volume_ratio": num(vr, 2)})
    out["volume_confirmations"] = confirmations
    if len(vol_ma20.dropna()) > 20:
        change = vol_ma20.iloc[-1] / vol_ma20.iloc[-21] - 1
        vol_trend = "rising" if change > 0.10 else "falling" if change < -0.10 else "flat"
    else:
        change, vol_trend = None, "n/a"

    # Support / resistance
    support, resistance = compute_levels(df)
    out["levels"] = support + resistance

    # Relative strength
    out["relative_strength"] = {
        key: compute_relative_strength(df, b["df"], b["symbol"]) for key, b in benchmarks.items()
    }

    # Signals summary
    last, prev = close.iloc[-1], close.iloc[-2] if len(close) > 1 else np.nan
    m20, m50, m200 = mas[20].iloc[-1], mas[50].iloc[-1], mas[200].iloc[-1]
    if all(v == v for v in (m20, m50, m200)):
        alignment = "bullish" if last > m20 > m50 > m200 else "bearish" if last < m20 < m50 < m200 else "mixed"
    else:
        alignment = "mixed"
    rsi_last = rsi.iloc[-1]
    bb_last_pct = pct_b.iloc[-1]
    bw_min_6m = bandwidth.tail(126).min()
    out["signals"] = {
        "last_close": num(last, 2),
        "change": num(last - prev, 2),
        "change_pct": num((last / prev - 1) * 100, 2),
        "ma": {
            "alignment": alignment,
            "above": {str(p): bool(last > mas[p].iloc[-1]) if mas[p].iloc[-1] == mas[p].iloc[-1] else None
                      for p in MA_PERIODS},
            "last_crossover": crossovers[-1] if crossovers else None,
        },
        "rsi": {
            "value": num(rsi_last, 1),
            "state": "n/a" if rsi_last != rsi_last else
                     "overbought" if rsi_last >= 70 else "oversold" if rsi_last <= 30 else "neutral",
        },
        "macd": {
            "macd": num(macd.iloc[-1]),
            "signal": num(signal.iloc[-1]),
            "histogram": num(hist.iloc[-1]),
            "state": "bullish" if macd.iloc[-1] > signal.iloc[-1] else "bearish",
            "momentum": "rising" if hist.iloc[-1] > hist.iloc[-2] else "falling",
        },
        "bollinger": {
            "pct_b": num(bb_last_pct, 2),
            "bandwidth": num(bandwidth.iloc[-1], 2),
            "state": "n/a" if bb_last_pct != bb_last_pct else
                     "above upper" if bb_last_pct > 1 else "below lower" if bb_last_pct < 0 else "inside",
            "squeeze": bool(bandwidth.iloc[-1] <= bw_min_6m * 1.05) if bw_min_6m == bw_min_6m else False,
        },
        "volume": {
            "trend": vol_trend,
            "ma20_change_pct": num(change * 100, 1) if change is not None else None,
            "last_ratio": num(vol_ratio.iloc[-1], 2),
            "last_confirmation": confirmations[-1] if confirmations else None,
        },
    }

    # Table view of the latest rows (accessibility / non-chart access to the same numbers)
    tail = df.tail(10).index
    out["table"] = [
        {
            "date": dates[i], "close": num(close.iloc[i], 2),
            "ma20": num(mas[20].iloc[i], 2), "ma50": num(mas[50].iloc[i], 2), "ma200": num(mas[200].iloc[i], 2),
            "rsi": num(rsi.iloc[i], 1), "macd": num(macd.iloc[i], 3), "histogram": num(hist.iloc[i], 3),
            "pct_b": num(pct_b.iloc[i], 2), "volume_ratio": num(vol_ratio.iloc[i], 2),
        }
        for i in reversed(tail)
    ]
    return out


@app.get("/api/py/symbols")
def list_symbols():
    conn = db_connect()
    try:
        cur = conn.cursor()
        if not table_exists(cur):
            return {"symbols": []}
        cur.execute(
            f"SELECT symbol, COUNT(*), MIN(trade_date), MAX(trade_date) FROM {TABLE} "
            f"GROUP BY symbol ORDER BY symbol"
        )
        return {
            "symbols": [
                {"symbol": r[0], "rows": r[1], "start_date": str(r[2]), "end_date": str(r[3])}
                for r in cur.fetchall()
            ]
        }
    finally:
        conn.close()


@app.get("/api/py/analysis/single")
def single_stock_analysis(symbol: str, index: str | None = None, sector: str | None = None):
    symbol = normalize_symbol(symbol)
    conn = db_connect()
    try:
        cur = conn.cursor()
        if not table_exists(cur):
            raise HTTPException(status_code=404, detail="No prices have been loaded yet.")
        df = load_prices_df(cur, symbol)
        benchmarks = {}
        for key, raw in (("index", index), ("sector", sector)):
            if raw and raw.strip():
                bench = normalize_symbol(raw)
                if bench != symbol:
                    benchmarks[key] = {"symbol": bench, "df": load_prices_df(cur, bench)}
    finally:
        conn.close()
    if len(df) < 30:
        raise HTTPException(status_code=422, detail=f"Only {len(df)} rows stored for {symbol}; need at least 30.")
    return compute_analysis(symbol, df, benchmarks)


# ---------------------------------------------------------------------------
# AI agent: runs a skill file (skills/<id>/SKILL.md) against gathered stock data with Claude
# ---------------------------------------------------------------------------
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def parse_skill(path: Path) -> dict:
    """SKILL.md = frontmatter (simple `key: value` lines between --- fences) + markdown instructions."""
    text = path.read_text(encoding="utf-8")
    meta, body = {}, text
    if text.startswith("---"):
        head, _, body = text[3:].partition("\n---")
        for line in head.strip().splitlines():
            if ":" in line and not line.startswith((" ", "#")):
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip().strip('"').strip("'")
    skill_id = path.parent.name
    # Optional data-structure.json beside SKILL.md: documents the input and holds the output schema.
    ds_path = path.parent / "data-structure.json"
    data_structure = None
    if ds_path.is_file():
        try:
            data_structure = json.loads(ds_path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise HTTPException(status_code=500, detail=f"{ds_path.name} for skill '{skill_id}' is not valid JSON: {e}") from e
    return {
        "id": skill_id,
        "name": meta.get("name", skill_id),
        "description": meta.get("description", ""),
        "version": meta.get("version", ""),
        "inputs": meta.get("inputs", "symbol"),
        "output": meta.get("output", "stock-verdict"),
        "data_source": meta.get("data_source", "fundamentals"),
        "path": f"skills/{skill_id}/SKILL.md",
        "data_structure_path": f"skills/{skill_id}/data-structure.json" if data_structure else None,
        "output_schema": (data_structure or {}).get("output"),
        "instructions": body.strip(),
        "content": text,
        "data_structure": data_structure,
    }


def list_skills() -> list[dict]:
    if not SKILLS_DIR.is_dir():
        return []
    return [parse_skill(p) for p in sorted(SKILLS_DIR.glob("*/SKILL.md"))]


def get_skill(skill_id: str) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,59}", skill_id):
        raise HTTPException(status_code=400, detail="Invalid skill id")
    path = SKILLS_DIR / skill_id / "SKILL.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found (looked in {SKILLS_DIR})")
    return parse_skill(path)

STOCK_VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "recommendation": {"type": "string", "enum": ["BUY", "HOLD", "SELL"]},
        "conviction": {"type": "string", "enum": ["high", "medium", "low"]},
        "headline": {"type": "string", "description": "One-sentence conclusion stating the recommendation and the main reason."},
        "currency": {"type": "string"},
        "current_price": {"type": "number"},
        "intrinsic_value_low": {"type": "number"},
        "intrinsic_value_base": {"type": "number"},
        "intrinsic_value_high": {"type": "number"},
        "margin_of_safety_pct": {"type": "number", "description": "(intrinsic_value_base - current_price) / intrinsic_value_base * 100"},
        "required_margin_of_safety_pct": {"type": "number"},
        "buy_below_price": {"type": "number"},
        "target_price": {"type": ["number", "null"]},
        "stop_price": {"type": ["number", "null"]},
        "target_rationale": {"type": "string"},
        "stop_rationale": {"type": "string"},
        "time_horizon": {"type": "string"},
        "summary": {"type": "string", "description": "Two to four paragraphs supporting the decision."},
        "checklist": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "criterion": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["pass", "fail", "unclear"]},
                    "evidence": {"type": "string"},
                },
                "required": ["criterion", "verdict", "evidence"],
            },
        },
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"title": {"type": "string"}, "content": {"type": "string"}},
                "required": ["title", "content"],
            },
        },
        "key_metrics": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                    "assessment": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["name", "value", "assessment", "source"],
            },
        },
        "valuation": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "method": {"type": "string"},
                "owner_earnings": {"type": "string"},
                "growth_rate_pct_years_1_5": {"type": "number"},
                "growth_rate_pct_years_6_10": {"type": "number"},
                "discount_rate_pct": {"type": "number"},
                "terminal_growth_pct": {"type": "number"},
                "notes": {"type": "string"},
            },
            "required": [
                "method", "owner_earnings", "growth_rate_pct_years_1_5", "growth_rate_pct_years_6_10",
                "discount_rate_pct", "terminal_growth_pct", "notes",
            ],
        },
        "risks": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "data_gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "recommendation", "conviction", "headline", "currency", "current_price",
        "intrinsic_value_low", "intrinsic_value_base", "intrinsic_value_high",
        "margin_of_safety_pct", "required_margin_of_safety_pct", "buy_below_price",
        "target_price", "stop_price", "target_rationale", "stop_rationale", "time_horizon",
        "summary", "checklist", "sections", "key_metrics", "valuation", "risks", "assumptions", "data_gaps",
    ],
}

INFO_FIELDS = (
    "longName", "sector", "industry", "country", "currency", "financialCurrency", "marketCap",
    "enterpriseValue", "currentPrice", "trailingPE", "forwardPE", "priceToBook", "enterpriseToEbitda",
    "trailingEps", "forwardEps", "returnOnEquity", "returnOnAssets", "debtToEquity", "currentRatio",
    "totalDebt", "totalCash", "freeCashflow", "operatingCashflow", "totalRevenue", "revenueGrowth",
    "earningsGrowth", "grossMargins", "operatingMargins", "profitMargins", "dividendYield", "payoutRatio",
    "sharesOutstanding", "bookValue", "beta", "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
    "heldPercentInsiders", "longBusinessSummary",
)
STATEMENT_ROWS = {
    "income": ("Total Revenue", "Gross Profit", "Operating Income", "Net Income", "Diluted EPS", "Interest Expense"),
    "balance": ("Total Debt", "Long Term Debt", "Stockholders Equity", "Cash And Cash Equivalents",
                "Total Assets", "Ordinary Shares Number"),
    "cashflow": ("Operating Cash Flow", "Capital Expenditure", "Free Cash Flow", "Depreciation And Amortization",
                 "Repurchase Of Capital Stock", "Cash Dividends Paid"),
}


def _statement_rows(df, rows):
    """Pick selected rows from a yfinance annual statement; columns are fiscal year-ends."""
    out = {}
    if df is None or getattr(df, "empty", True):
        return out
    for row in rows:
        if row in df.index:
            series = df.loc[row]
            out[row] = {str(getattr(col, "year", col)): num(v, 2) for col, v in series.items()}
    return out


def gather_fundamentals(symbol: str, info_fields=INFO_FIELDS, statement_rows=STATEMENT_ROWS) -> dict:
    """Fundamentals and annual statements from Yahoo Finance (via yfinance). Missing data is omitted, never guessed."""
    import yfinance as yf

    t = yf.Ticker(symbol)
    try:
        info = t.info or {}
    except Exception:  # noqa: BLE001
        info = {}
    fundamentals = {k: info[k] for k in info_fields if info.get(k) is not None}
    statements = {}
    for key, attr in (("income", "income_stmt"), ("balance", "balance_sheet"), ("cashflow", "cashflow")):
        try:
            statements[key] = _statement_rows(getattr(t, attr), statement_rows.get(key, ()))
        except Exception:  # noqa: BLE001
            statements[key] = {}
    return {"fundamentals": fundamentals, "annual_statements": statements}


def skill_data_requirements(skill: dict | None):
    """Which fundamentals fields and statement rows to gather: taken from the skill's data-structure.json
    input schema when present, otherwise the defaults."""
    try:
        props = skill["data_structure"]["input"]["properties"]
        info_fields = tuple(props["fundamentals"]["properties"])
        statement_rows = {k: tuple(v["properties"]) for k, v in props["annual_statements"]["properties"].items()}
        if info_fields and statement_rows:
            return info_fields, statement_rows
    except (KeyError, TypeError):
        pass
    return INFO_FIELDS, STATEMENT_ROWS


def price_snapshot(df) -> dict:
    close = df["close"]
    last = float(close.iloc[-1])
    year = df.tail(252)
    ma200 = close.rolling(200).mean().iloc[-1]
    returns = close.pct_change().dropna()
    peak = year["close"].cummax()
    return {
        "as_of": df["date"].iloc[-1],
        "last_close": num(last, 2),
        "high_52w": num(year["high"].max(), 2),
        "low_52w": num(year["low"].min(), 2),
        "ma200": num(ma200, 2),
        "pct_vs_ma200": num((last / ma200 - 1) * 100, 1) if ma200 == ma200 else None,
        "return_1y_pct": num((last / float(year["close"].iloc[0]) - 1) * 100, 1) if len(year) > 1 else None,
        "annualized_volatility_pct": num(returns.tail(252).std() * (252 ** 0.5) * 100, 1) if len(returns) > 20 else None,
        "max_drawdown_1y_pct": num(((year["close"] / peak) - 1).min() * 100, 1),
        "source": "Azure SQL dbo.stock_prices (loaded from Yahoo Finance)",
    }


def load_price_history_any(symbol: str):
    """Stored prices if present; otherwise a direct Yahoo Finance download."""
    import pandas as pd

    conn = db_connect()
    try:
        cur = conn.cursor()
        if table_exists(cur):
            try:
                return load_prices_df(cur, symbol), "database"
            except HTTPException:
                pass
    finally:
        conn.close()
    rows = fetch_history(symbol)
    df = pd.DataFrame([r.model_dump() for r in rows])
    df["date"] = df["trade_date"].astype(str)
    return df, "yahoo"


class AgentRunRequest(BaseModel):
    skill: str = Field(..., min_length=1, max_length=60)
    symbol: str = Field(..., min_length=1, max_length=20)


def validate_verdict(a: dict) -> list[str]:
    """Hard rules: BUY needs target and stop prices; SELL needs a target price. Soft checks become warnings."""
    rec = a.get("recommendation")
    if rec == "BUY" and (a.get("target_price") is None or a.get("stop_price") is None):
        raise HTTPException(status_code=502, detail="The model returned a BUY without both a target price and a stop price.")
    if rec == "SELL" and a.get("target_price") is None:
        raise HTTPException(status_code=502, detail="The model returned a SELL without a target price.")
    warnings = []
    price = a.get("current_price")
    if price is None:
        return warnings
    if rec == "BUY":
        if a["stop_price"] >= price:
            warnings.append("Stop price is not below the current price.")
        if a["target_price"] <= price:
            warnings.append("Target price is not above the current price.")
        if a.get("buy_below_price") is not None and price > a["buy_below_price"]:
            warnings.append("Current price is above the buy-below price, which is inconsistent with a BUY.")
    elif rec == "SELL":
        if a["target_price"] >= price:
            warnings.append("Target price for a SELL is not below the current price.")
        if a.get("stop_price") is not None:
            warnings.append("A stop price was given for a SELL recommendation.")
    if isinstance(a.get("stress_test"), list) and len(a["stress_test"]) < 3:
        warnings.append(f"Only {len(a['stress_test'])} stress-test scenario(s) returned; the skill requires base, recession and severe.")
    return warnings


def gather_stock_data(symbol: str, skill: dict | None = None, log: "RunLog | None" = None) -> dict:
    """Current-date payload for a single run, read from the symbol's stored data bundle (no live Yahoo call
    unless the bundle is missing or older than BUNDLE_MAX_AGE_HOURS)."""
    import pandas as pd
    from datetime import date as _date

    info_fields, statement_rows = skill_data_requirements(skill)
    bundle = load_symbol_bundle(symbol, log=log)
    df = pd.DataFrame(bundle["prices"])
    df["date"] = df["trade_date"].astype(str)
    for c in ("open", "high", "low", "close", "adj_close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    fundamentals = {k: v for k, v in bundle.get("quote_summary", {}).items() if k in info_fields}
    annual = bundle["statements"].get("A", {})
    annual_rows = {
        stmt: {item: {pe[:4]: v for pe, v in annual.get(stmt, {}).get(item, {}).items()}
               for item in rows if item in annual.get(stmt, {})}
        for stmt, rows in statement_rows.items()
    }
    data = {
        "symbol": symbol,
        "analysis_date": _date.today().isoformat(),
        "price": {**price_snapshot(df), "source": "database"},
        "fundamentals": fundamentals,
        "annual_statements": annual_rows,
        "source_note": f"Data bundle fetched {bundle['fetched_at'][:19]} UTC from Yahoo Finance via yfinance "
                       f"(stored copy; nothing fetched live for this run). Values as reported by Yahoo; statement "
                       f"figures in the company's financial currency.",
    }
    if not data["fundamentals"]:
        raise HTTPException(status_code=404, detail=f"The stored data for {symbol} has no quote-summary fundamentals.")
    return data


def claude_provider() -> str | None:
    """'foundry' when Microsoft Foundry credentials are set, 'anthropic' for the Claude API, None if neither."""
    if os.environ.get("ANTHROPIC_FOUNDRY_API_KEY") and (
        os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE") or os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL")
    ):
        return "foundry"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def require_claude() -> str:
    provider = claude_provider()
    if not provider:
        raise HTTPException(
            status_code=500,
            detail="No Claude provider configured. Set ANTHROPIC_FOUNDRY_API_KEY + ANTHROPIC_FOUNDRY_RESOURCE "
                   "(Microsoft Foundry) or ANTHROPIC_API_KEY (Claude API).",
        )
    return provider


def make_claude_client(provider: str):
    import anthropic

    if provider == "foundry":
        # Reads ANTHROPIC_FOUNDRY_API_KEY and ANTHROPIC_FOUNDRY_RESOURCE / ANTHROPIC_FOUNDRY_BASE_URL itself.
        return anthropic.AnthropicFoundry(timeout=240.0, max_retries=1)
    return anthropic.Anthropic(timeout=240.0, max_retries=1)


CLAUDE_TIMEOUT_S = 240.0
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def claude_effort() -> str:
    e = os.environ.get("ANTHROPIC_EFFORT", "medium").strip().lower()
    return e if e in EFFORT_LEVELS else "medium"


HELLO_PROMPT = "Reply with one short sentence: say hello, state which model you are, and confirm you are ready to analyze a stock."


def claude_hello(log: "RunLog | None" = None) -> dict:
    """Tiny round-trip to the configured model so a misconfiguration shows up before a long run starts."""
    import anthropic

    provider = require_claude()
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_CLAUDE_MODEL)
    client = make_claude_client(provider)
    if log:
        log.step(f"AI hello: sending a one-line test prompt to {provider} ({client.base_url}, model '{model}') ...")
    t0 = time.monotonic()
    try:
        r = client.messages.create(
            model=model, max_tokens=80, output_config={"effort": "low"},
            messages=[{"role": "user", "content": HELLO_PROMPT}],
        )
    except anthropic.AuthenticationError as e:
        raise HTTPException(status_code=502, detail=f"AI hello failed: the {provider} API key was rejected.") from e
    except anthropic.APIStatusError as e:
        raise HTTPException(status_code=502, detail=f"AI hello failed: {provider} returned {e.status_code}: {e.message[:300]}") from e
    except anthropic.APIConnectionError as e:
        cause = f"{type(e.__cause__).__name__}: {e.__cause__}" if e.__cause__ else str(e)
        raise HTTPException(status_code=502, detail=f"AI hello failed: could not reach {client.base_url} ({cause[:200]})") from e
    elapsed = time.monotonic() - t0
    text = " ".join(b.text for b in r.content if b.type == "text").strip()
    result = {"provider": provider, "endpoint": str(client.base_url), "model": r.model, "reply": text,
              "latency_s": round(elapsed, 2), "stop_reason": r.stop_reason,
              "input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens}
    if log:
        log.step(f"AI hello OK in {elapsed:.1f} s - {r.model} replied: \"{text[:300]}\" "
                 f"({r.usage.input_tokens} in / {r.usage.output_tokens} out tokens, stop_reason {r.stop_reason})")
    if r.stop_reason == "refusal":
        raise HTTPException(status_code=502, detail="AI hello: the model refused the test prompt; check the deployment's safety settings.")
    return result


@app.post("/api/py/agent/hello")
def agent_hello():
    """Manual AI connectivity test (used by the Test AI button)."""
    log = RunLog("x", "y")
    result = claude_hello(log)
    return {**result, "log": log.entries}


def run_skill_with_claude(skill: dict, data: dict, log: "RunLog | None" = None):
    """One structured-output call, streamed so progress can be logged while the model generates.
    Skill instructions are the (cached) system prompt; the gathered data is the user turn."""
    import anthropic

    provider = require_claude()
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_CLAUDE_MODEL)
    effort = claude_effort()
    schema = skill.get("output_schema") or STOCK_VERDICT_SCHEMA  # skill's data-structure.json wins
    client = make_claude_client(provider)
    user_text = (f"Apply the skill to {data['symbol']} using only the data below and return the JSON. "
                 "In key_metrics.source and within sections, cite the specific statement and period each "
                 "figure came from (for example 'annual_statements.cashflow.Free Cash Flow 2025', "
                 "'fundamentals.returnOnEquity' or 'price.ma200').\n\n"
                 + json.dumps(data, default=str, sort_keys=True))
    request = dict(
        model=model,
        max_tokens=16000,
        system=[{"type": "text", "text": skill["instructions"], "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": user_text}],
    )

    def say(msg, level="info"):
        if log:
            log.step(msg, level)

    say(f"Request to {provider} at {client.base_url}: model {model}, effort {effort}, max_tokens 16000, "
        f"system {len(skill['instructions']):,} chars (cached), data {len(user_text):,} chars "
        f"(~{(len(skill['instructions']) + len(user_text)) // 4:,} input tokens), JSON schema enforced, "
        f"timeout {CLAUDE_TIMEOUT_S:.0f} s, 1 retry, streaming"
        + (" - Vercel function limit 300 s (vercel.json maxDuration)" if os.environ.get("VERCEL") else "")
        + (" - WARNING: effort high can think for 60+ s before answering; set ANTHROPIC_EFFORT=medium if steps time out"
           if effort in ("high", "xhigh", "max") and os.environ.get("VERCEL") else ""))

    t0 = time.monotonic()
    last_report = t0
    first_token_at = None
    text_chars = 0
    thinking_events = 0
    events = 0
    grammar_fallback = False

    def open_stream(req):
        if provider == "foundry":
            # Foundry has no server-side refusal fallback; plain Messages API.
            return client.messages.stream(**req)
        return client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **req)

    try:
        try:
            manager = open_stream(request)
            manager.__enter__()
        except anthropic.BadRequestError as e:
            if "grammar" not in str(e.message).lower():
                raise
            # The JSON-schema grammar is too large for structured outputs: ask for the JSON in text instead.
            grammar_fallback = True
            say(f"Structured-output grammar rejected by the API ({str(e.message)[:120]}); retrying without the grammar - "
                "the schema is sent in the prompt and the JSON is parsed from the reply", "warn")
            fallback_request = dict(request)
            fallback_request["output_config"] = {"effort": effort}
            fallback_request["messages"] = [{"role": "user", "content": user_text + "\n\nReturn ONLY a single JSON object (no prose, "
                                             "no code fences) that conforms exactly to this JSON Schema:\n" + json.dumps(schema)}]
            manager = open_stream(fallback_request)
            manager.__enter__()
        with manager as stream:
            for event in stream:
                events += 1
                et = getattr(event, "type", "")
                if et == "message_start":
                    u = event.message.usage
                    say(f"Connected after {time.monotonic() - t0:.1f} s: {u.input_tokens:,} input tokens "
                        f"(cache read {getattr(u, 'cache_read_input_tokens', 0) or 0:,}, cache write "
                        f"{getattr(u, 'cache_creation_input_tokens', 0) or 0:,}); model generating ...")
                elif et == "content_block_delta":
                    d = event.delta
                    dt = getattr(d, "type", "")
                    if dt == "text_delta":
                        text_chars += len(d.text)
                    elif dt == "thinking_delta":
                        thinking_events += 1
                    if first_token_at is None:
                        first_token_at = time.monotonic() - t0
                        say(f"First output after {first_token_at:.1f} s ({'thinking' if dt == 'thinking_delta' else 'answer text'})")
                    if time.monotonic() - last_report >= 15:
                        last_report = time.monotonic()
                        say(f"Still generating: {time.monotonic() - t0:.0f} s elapsed, ~{text_chars // 4:,} answer tokens so far"
                            + (f", {thinking_events:,} thinking chunks" if thinking_events else ""))
                elif et == "content_block_start" and getattr(getattr(event, "content_block", None), "type", "") == "thinking":
                    say(f"Model is thinking (effort {effort}) ...")
            response = stream.get_final_message()
        u = response.usage
        say(f"Completed in {time.monotonic() - t0:.1f} s: stop_reason {response.stop_reason}, {u.input_tokens:,} input / "
            f"{u.output_tokens:,} output tokens, {events} stream events, ~{text_chars:,} chars of JSON")
    except anthropic.AuthenticationError as e:
        raise HTTPException(status_code=502, detail="Anthropic API key was rejected.") from e
    except anthropic.RateLimitError as e:
        raise HTTPException(status_code=503, detail="Anthropic API rate limit reached; try again shortly.") from e
    except anthropic.APIStatusError as e:
        raise HTTPException(status_code=502, detail=f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APITimeoutError as e:
        raise HTTPException(
            status_code=504,
            detail=f"Claude did not finish within {CLAUDE_TIMEOUT_S:.0f} s after {time.monotonic() - t0:.0f} s "
                   f"(~{text_chars:,} chars received). Lower ANTHROPIC_EFFORT (medium/low) or use a faster model.",
        ) from e
    except anthropic.APIConnectionError as e:
        cause = f"{type(e.__cause__).__name__}: {e.__cause__}" if e.__cause__ else str(e)
        raise HTTPException(
            status_code=502,
            detail=f"Could not connect to Claude via {provider} at {client.base_url} ({cause[:300]}) after "
                   f"{time.monotonic() - t0:.0f} s. Check the resource name / base URL, DNS and any corporate proxy or firewall.",
        ) from e

    if response.stop_reason == "refusal":
        why = getattr(response.stop_details, "explanation", None) or "The model declined to analyze this request."
        raise HTTPException(status_code=502, detail=why)
    if response.stop_reason == "max_tokens":
        raise HTTPException(status_code=502, detail="The analysis was cut off (max_tokens); please retry.")
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        analysis = json.loads(text)
    except ValueError:
        # Fallback replies may wrap the object in prose or fences: take the outermost {...}.
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise HTTPException(status_code=502, detail="The model did not return valid JSON.")
        try:
            analysis = json.loads(text[start:end + 1])
        except ValueError as e:
            raise HTTPException(status_code=502, detail=f"The model did not return valid JSON ({e}).") from e
    if grammar_fallback:
        missing = [k for k in schema.get("required", []) if k not in analysis]
        if missing:
            raise HTTPException(status_code=502, detail=f"The model's JSON is missing required fields: {', '.join(missing[:8])}")
        say(f"Parsed JSON from the reply; all {len(schema.get('required', []))} required fields present")
    return analysis, response


@app.get("/api/py/agent/skills")
def agent_skills():
    keys = ("id", "name", "description", "version", "inputs", "output", "data_source", "path", "data_structure_path")
    return {"skills": [{k: s[k] for k in keys} for s in list_skills()], "directory": str(SKILLS_DIR)}


@app.get("/api/py/agent/skills/{skill_id}")
def agent_skill(skill_id: str):
    return get_skill(skill_id)


@app.post("/api/py/agent/run")
def agent_run(req: AgentRunRequest):
    require_claude()
    skill = get_skill(req.skill.strip().lower())
    symbol = normalize_symbol(req.symbol)
    log = RunLog(DECISIONS_TABLE, "decision_id")  # not persisted for single runs; returned with the result
    claude_hello(log)
    if skill.get("data_source") == "tech":
        ensure_technicals(symbol, log)
        data, _refs = load_tech_bundle(symbol, date.today(), skill)
        log.step(f"Technical data as of today ({data['today']['trade_date']}) read from TECH_ tables")
    else:
        data = gather_stock_data(symbol, skill, log=log)
    analysis, response = run_skill_with_claude(skill, data, log)
    archive_run_json(f"runs/{skill['id']}/{symbol}/{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')}", data, analysis, log)
    usage = response.usage
    return {
        "log": log.entries,
        "skill": {k: skill[k] for k in ("id", "name", "version", "path", "data_structure_path")},
        "symbol": symbol,
        "analysis": analysis,
        "warnings": validate_verdict(analysis),
        "data_used": data,
        "model": response.model,
        "provider": claude_provider(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
        },
    }


# ---------------------------------------------------------------------------
# Agent backtests: run a skill as of every Monday of a year, seeing only data available then.
# One row per Monday in dbo.agent_decisions; the page drives /step calls until none are pending.
# ---------------------------------------------------------------------------
BACKTESTS_TABLE = "dbo.agent_backtests"
DECISIONS_TABLE = "dbo.agent_decisions"
ANNUAL_LAG_DAYS, QUARTERLY_LAG_DAYS = 90, 45
PROFILE_FIELDS = ("longName", "sector", "industry", "country", "currency", "financialCurrency", "longBusinessSummary")

ENSURE_BACKTEST_TABLES_SQL = f"""
IF OBJECT_ID('{BACKTESTS_TABLE}', 'U') IS NULL
CREATE TABLE {BACKTESTS_TABLE} (
    backtest_id   INT IDENTITY(1,1) PRIMARY KEY,
    skill_id      NVARCHAR(60)  NOT NULL,
    skill_version NVARCHAR(20)  NULL,
    symbol        NVARCHAR(20)  NOT NULL,
    [year]        INT           NOT NULL,
    raw_data      NVARCHAR(MAX) NOT NULL,
    log_json      NVARCHAR(MAX) NULL,
    created_at    DATETIME2     NOT NULL CONSTRAINT DF_agent_backtests_created DEFAULT SYSUTCDATETIME()
);
IF OBJECT_ID('{DECISIONS_TABLE}', 'U') IS NULL
CREATE TABLE {DECISIONS_TABLE} (
    decision_id          INT IDENTITY(1,1) PRIMARY KEY,
    backtest_id          INT            NOT NULL,
    skill_id             NVARCHAR(60)   NOT NULL,
    symbol               NVARCHAR(20)   NOT NULL,
    as_of_date           DATE           NOT NULL,
    status               NVARCHAR(10)   NOT NULL,
    recommendation       CHAR(4)        NULL,
    conviction           NVARCHAR(10)   NULL,
    current_price        DECIMAL(18, 4) NULL,
    buy_below_price      DECIMAL(18, 4) NULL,
    target_price         DECIMAL(18, 4) NULL,
    stop_price           DECIMAL(18, 4) NULL,
    intrinsic_value_low  DECIMAL(18, 4) NULL,
    intrinsic_value_base DECIMAL(18, 4) NULL,
    intrinsic_value_high DECIMAL(18, 4) NULL,
    margin_of_safety_pct DECIMAL(9, 2)  NULL,
    headline             NVARCHAR(500)  NULL,
    summary              NVARCHAR(MAX)  NULL,
    input_json           NVARCHAR(MAX)  NULL,
    output_json          NVARCHAR(MAX)  NULL,
    warnings_json        NVARCHAR(MAX)  NULL,
    model                NVARCHAR(60)   NULL,
    input_tokens         INT            NULL,
    output_tokens        INT            NULL,
    error                NVARCHAR(1000) NULL,
    started_at           DATETIME2      NULL,
    finished_at          DATETIME2      NULL,
    references_json      NVARCHAR(MAX)  NULL,
    report_pathname      NVARCHAR(400)  NULL,
    report_url           NVARCHAR(1000) NULL,
    log_json             NVARCHAR(MAX)  NULL,
    report_html_pathname NVARCHAR(400)  NULL,
    CONSTRAINT UQ_agent_decisions UNIQUE (backtest_id, as_of_date)
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_agent_decisions_symbol')
CREATE INDEX IX_agent_decisions_symbol ON {DECISIONS_TABLE} (symbol, skill_id, as_of_date);
IF COL_LENGTH('{DECISIONS_TABLE}', 'references_json') IS NULL ALTER TABLE {DECISIONS_TABLE} ADD references_json NVARCHAR(MAX) NULL;
IF COL_LENGTH('{DECISIONS_TABLE}', 'report_pathname') IS NULL ALTER TABLE {DECISIONS_TABLE} ADD report_pathname NVARCHAR(400) NULL;
IF COL_LENGTH('{DECISIONS_TABLE}', 'report_url') IS NULL ALTER TABLE {DECISIONS_TABLE} ADD report_url NVARCHAR(1000) NULL;
IF COL_LENGTH('{DECISIONS_TABLE}', 'log_json') IS NULL ALTER TABLE {DECISIONS_TABLE} ADD log_json NVARCHAR(MAX) NULL;
IF COL_LENGTH('{BACKTESTS_TABLE}', 'log_json') IS NULL ALTER TABLE {BACKTESTS_TABLE} ADD log_json NVARCHAR(MAX) NULL;
IF COL_LENGTH('{DECISIONS_TABLE}', 'report_html_pathname') IS NULL ALTER TABLE {DECISIONS_TABLE} ADD report_html_pathname NVARCHAR(400) NULL;
IF COL_LENGTH('{BACKTESTS_TABLE}', 'status') IS NULL ALTER TABLE {BACKTESTS_TABLE} ADD status NVARCHAR(10) NOT NULL CONSTRAINT DF_agent_backtests_status DEFAULT 'active';
"""
DECISION_STATUSES = ("pending", "running", "done", "failed", "cancelled")

DECISION_COLUMNS = (
    "decision_id", "backtest_id", "skill_id", "symbol", "as_of_date", "status", "recommendation", "conviction",
    "current_price", "buy_below_price", "target_price", "stop_price", "intrinsic_value_low", "intrinsic_value_base",
    "intrinsic_value_high", "margin_of_safety_pct", "headline", "summary", "warnings_json", "model",
    "input_tokens", "output_tokens", "error", "started_at", "finished_at",
    "references_json", "report_pathname", "report_url", "log_json", "report_html_pathname",
)
STATEMENT_NAMES = {"income": "Income statement", "balance": "Balance sheet", "cashflow": "Cash flow statement"}


def ensure_backtest_tables(cur) -> None:
    cur.execute(ENSURE_BACKTEST_TABLES_SQL)


# ---------------------------------------------------------------------------
# Diagnostics: what the server is configured with (never secret values), and a per-run log
# that is written to the database at each phase so the page can show progress live.
# ---------------------------------------------------------------------------
def _set(name: str) -> str:
    v = os.environ.get(name)
    return f"{name} (set, {len(v)} chars)" if v else f"{name} (missing)"


def env_diagnostics() -> dict:
    provider = claude_provider()
    if provider == "foundry":
        base = os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL") or \
            f"https://{os.environ.get('ANTHROPIC_FOUNDRY_RESOURCE')}.services.ai.azure.com/anthropic/"
        claude = {"provider": "Microsoft Foundry", "endpoint": base, "model": os.environ.get("ANTHROPIC_MODEL", DEFAULT_CLAUDE_MODEL),
                  "effort": claude_effort(), "credentials": _set("ANTHROPIC_FOUNDRY_API_KEY"),
                  "variables": [_set("ANTHROPIC_FOUNDRY_API_KEY"), _set("ANTHROPIC_FOUNDRY_RESOURCE"),
                                _set("ANTHROPIC_FOUNDRY_BASE_URL"), _set("ANTHROPIC_MODEL"), _set("ANTHROPIC_EFFORT")]}
    elif provider == "anthropic":
        claude = {"provider": "Anthropic API", "endpoint": "https://api.anthropic.com/", "model": os.environ.get("ANTHROPIC_MODEL", DEFAULT_CLAUDE_MODEL),
                  "effort": claude_effort(), "credentials": _set("ANTHROPIC_API_KEY"),
                  "variables": [_set("ANTHROPIC_API_KEY"), _set("ANTHROPIC_MODEL"), _set("ANTHROPIC_EFFORT")]}
    else:
        claude = {"provider": "not configured", "endpoint": None, "model": None, "credentials": None,
                  "variables": [_set("ANTHROPIC_FOUNDRY_API_KEY"), _set("ANTHROPIC_FOUNDRY_RESOURCE"), _set("ANTHROPIC_API_KEY")]}
    return {
        "claude": claude,
        "database": {
            "server": os.environ.get("AZURE_SQL_SERVER") or None,
            "database": os.environ.get("AZURE_SQL_DATABASE") or None,
            "user": os.environ.get("AZURE_SQL_USER") or None,
            "variables": [_set("AZURE_SQL_SERVER"), _set("AZURE_SQL_DATABASE"), _set("AZURE_SQL_USER"), _set("AZURE_SQL_PASSWORD")],
        },
        "blob": {"configured": blob_configured(), "access": blob_access(),
                 "variables": [_set("BLOB_READ_WRITE_TOKEN"), _set("BLOB_ACCESS")]},
        "skills_dir": str(SKILLS_DIR),
        "vercel": bool(os.environ.get("VERCEL")),
        "python": platform.python_version(),
    }


class RunLog:
    """Timestamped entries persisted to <table>.log_json so a polling page can show progress during a run."""

    def __init__(self, table: str, id_column: str, row_id: int | None = None):
        self.table, self.id_column, self.row_id = table, id_column, row_id
        self.entries: list[dict] = []
        self._t0 = time.monotonic()

    def add(self, message: str, level: str = "info") -> None:
        self.entries.append({"t": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                             "elapsed_s": round(time.monotonic() - self._t0, 2), "level": level, "message": message})

    def save(self, cur=None) -> None:
        """Write the log; opens a short connection when no cursor is supplied (e.g. around the model call)."""
        if self.row_id is None:
            return
        sql = f"UPDATE {self.table} SET log_json = %s WHERE {self.id_column} = %s"
        params = (json.dumps(self.entries), self.row_id)
        if cur is not None:
            cur.execute(sql, params)
            return
        try:
            conn = db_connect()
            try:
                conn.cursor().execute(sql, params)
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - logging must never break the run
            pass

    def step(self, message: str, level: str = "info", cur=None) -> None:
        self.add(message, level)
        self.save(cur)


def describe_db() -> str:
    return f"{os.environ.get('AZURE_SQL_SERVER')}/{os.environ.get('AZURE_SQL_DATABASE')} as {os.environ.get('AZURE_SQL_USER')}"


def describe_claude() -> str:
    c = env_diagnostics()["claude"]
    return f"{c['provider']} at {c['endpoint']} with model '{c['model']}' using {c['credentials']}"


def mondays(year: int, until: date) -> list[date]:
    d = date(year, 1, 1)
    d += timedelta(days=(7 - d.weekday()) % 7)  # first Monday
    out = []
    while d.year == year and d <= until:
        out.append(d)
        d += timedelta(days=7)
    return out


def _frame_to_dict(df) -> dict:
    """yfinance statement frame -> {line_item: {period_end_iso: value}}, keeping every row."""
    out = {}
    if df is None or getattr(df, "empty", True):
        return out
    for row in df.index:
        vals = {}
        for col, v in df.loc[row].items():
            key = col.date().isoformat() if hasattr(col, "date") else str(col)[:10]
            vals[key] = num(v, 4)
        out[str(row)] = vals
    return out


def fetch_backtest_raw(symbol: str) -> dict:
    """One Yahoo pull per backtest: profile text, annual + quarterly statements (all rows), earnings dates."""
    import yfinance as yf

    t = yf.Ticker(symbol)
    try:
        info = t.info or {}
    except Exception:  # noqa: BLE001
        info = {}
    profile = {k: info[k] for k in PROFILE_FIELDS if info.get(k) is not None}
    statements = {}
    for period, prefix in (("A", ""), ("Q", "quarterly_")):
        statements[period] = {}
        for key, attr in (("income", "income_stmt"), ("balance", "balance_sheet"), ("cashflow", "cashflow")):
            try:
                statements[period][key] = _frame_to_dict(getattr(t, prefix + attr))
            except Exception:  # noqa: BLE001
                statements[period][key] = {}
    earnings_dates: list[str] = []
    try:
        ed = t.get_earnings_dates(limit=24)
        if ed is not None and not ed.empty:
            earnings_dates = sorted({str(ts)[:10] for ts in ed.index})
    except Exception:  # noqa: BLE001
        pass
    return {
        "symbol": symbol,
        "profile": profile,
        "statements": statements,
        "earnings_dates": earnings_dates,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def available_from(period_end: date, period_type: str, earnings_dates: list[str]) -> date:
    """Date a fiscal period became public: the first earnings release after period end, else a fixed lag."""
    lag = ANNUAL_LAG_DAYS if period_type == "A" else QUARTERLY_LAG_DAYS
    for s in earnings_dates:
        d = date.fromisoformat(s)
        if period_end < d <= period_end + timedelta(days=lag + 60):
            return d
    return period_end + timedelta(days=lag)


def visible_statements(raw: dict, as_of: date, period_type: str) -> dict:
    """{stmt: {line_item: {period_end: value}}} restricted to periods reported before as_of."""
    out: dict = {}
    for stmt, rows in raw["statements"].get(period_type, {}).items():
        out[stmt] = {}
        for item, series in rows.items():
            kept = {}
            for pe, v in series.items():
                try:
                    pe_d = date.fromisoformat(pe)
                except ValueError:
                    continue
                if available_from(pe_d, period_type, raw.get("earnings_dates", [])) < as_of:
                    kept[pe] = v
            if kept:
                out[stmt][item] = kept
    return out


def _latest(series: dict):
    for pe in sorted(series, reverse=True):
        if series[pe] is not None:
            return pe, series[pe]
    return None, None


def _ttm(series: dict):
    vals = [series[pe] for pe in sorted(series, reverse=True)[:4]]
    return sum(vals) if len(vals) == 4 and all(v is not None for v in vals) else None


def derive_fundamentals(raw: dict, annual: dict, quarterly: dict, price: float | None):
    """Rebuild quote-summary style fields from statements visible at the time (Yahoo's live ratios can't be rewound)."""
    f = dict(raw.get("profile", {}))

    def flow(stmt, item):  # income / cash-flow items: trailing four quarters, else latest fiscal year
        v = _ttm(quarterly.get(stmt, {}).get(item, {}))
        if v is not None:
            return v, "ttm"
        pe, v = _latest(annual.get(stmt, {}).get(item, {}))
        return (v, f"fy{pe[:4]}") if v is not None else (None, None)

    def stock(stmt, item):  # balance-sheet items: most recent period from either source
        cands = [c for c in (_latest(src.get(stmt, {}).get(item, {})) for src in (quarterly, annual)) if c[0]]
        return max(cands)[1] if cands else None

    shares = stock("balance", "Ordinary Shares Number")
    equity = stock("balance", "Stockholders Equity")
    debt = stock("balance", "Total Debt")
    cash = stock("balance", "Cash And Cash Equivalents")
    cur_assets, cur_liab = stock("balance", "Current Assets"), stock("balance", "Current Liabilities")
    revenue, basis = flow("income", "Total Revenue")
    gross, _ = flow("income", "Gross Profit")
    op_inc, _ = flow("income", "Operating Income")
    net_inc, _ = flow("income", "Net Income")
    ocf, _ = flow("cashflow", "Operating Cash Flow")
    fcf, _ = flow("cashflow", "Free Cash Flow")
    dividends, _ = flow("cashflow", "Cash Dividends Paid")

    if price:
        f["currentPrice"] = round(price, 4)
    if shares:
        f["sharesOutstanding"] = shares
        if price:
            f["marketCap"] = round(price * shares, 2)
    if net_inc is not None and shares:
        eps = net_inc / shares
        f["trailingEps"] = round(eps, 4)
        if price and eps > 0:
            f["trailingPE"] = round(price / eps, 2)
    if equity and shares:
        bv = equity / shares
        f["bookValue"] = round(bv, 4)
        if price and bv > 0:
            f["priceToBook"] = round(price / bv, 3)
    if net_inc is not None and equity:
        f["returnOnEquity"] = round(net_inc / equity, 4)
    if revenue:
        f["totalRevenue"] = revenue
        for key, val in (("grossMargins", gross), ("operatingMargins", op_inc), ("profitMargins", net_inc)):
            if val is not None:
                f[key] = round(val / revenue, 4)
    if debt is not None:
        f["totalDebt"] = debt
        if equity:
            f["debtToEquity"] = round(debt / equity * 100, 2)
    if cash is not None:
        f["totalCash"] = cash
    if cur_assets and cur_liab:
        f["currentRatio"] = round(cur_assets / cur_liab, 3)
    if ocf is not None:
        f["operatingCashflow"] = ocf
    if fcf is not None:
        f["freeCashflow"] = fcf
    if dividends and net_inc and net_inc > 0:
        f["payoutRatio"] = round(-dividends / net_inc, 4)
        if shares and price:
            f["dividendYield"] = round(-dividends / shares / price, 4)

    basis_text = "trailing four quarters" if basis == "ttm" else f"latest visible fiscal year ({basis})" if basis else "no statements"
    note = (f"Fundamentals (market cap, EPS, P/E, book value, ROE, margins, debt/equity, cash flows, payout) are DERIVED "
            f"from statements visible at the analysis date using {basis_text}; dividendYield is a fraction. "
            f"Yahoo's live quote-summary ratios were not used because they cannot be rewound to a past date.")
    return f, note


def build_references(raw: dict, hist, annual: dict, quarterly: dict) -> list[dict]:
    """Every report and data set the agent was given for this date, with provenance."""
    refs = [{
        "type": "prices",
        "title": "Daily price history",
        "detail": f"{hist['date'].iloc[0]} to {hist['date'].iloc[-1]} ({len(hist)} trading days) from dbo.stock_prices, "
                  f"originally Yahoo Finance",
        "used_for": "current price, 52-week range, 200-day average, volatility, drawdown",
    }]
    for period_type, label, src in (("A", "Annual", annual), ("Q", "Quarterly", quarterly)):
        periods = sorted({pe for s in src.values() for ser in s.values() for pe in ser}, reverse=True)
        for pe in periods:
            stmts = [STATEMENT_NAMES[s] for s in ("income", "balance", "cashflow")
                     if any(pe in ser for ser in src.get(s, {}).values())]
            items = sum(1 for s in src.values() for ser in s.values() if pe in ser)
            reported = available_from(date.fromisoformat(pe), period_type, raw.get("earnings_dates", [])).isoformat()
            refs.append({
                "type": "statement",
                "title": f"{label} financial report, period ending {pe}",
                "detail": f"{', '.join(stmts)}; {items} line items; reported {reported}; Yahoo Finance",
                "used_for": "trailing-twelve-month figures and trend" if period_type == "Q" else "annual statement rows and multi-year trend",
            })
    if raw.get("profile"):
        refs.append({
            "type": "profile",
            "title": "Company profile",
            "detail": f"Name, sector, industry, country, currencies and business description from the Yahoo Finance "
                      f"quote summary (fetched {raw.get('fetched_at', '')[:10]}); descriptive text only",
            "used_for": "circle of competence, moat and competitive position",
        })
    refs.append({
        "type": "derived",
        "title": "Derived fundamentals",
        "detail": "Market cap, EPS, P/E, book value, ROE, margins, debt/equity, current ratio, cash flows and payout "
                  "computed from the statements above and the last price before the analysis date",
        "used_for": "valuation and financial-strength tests",
    })
    return refs


def point_in_time_payload(raw: dict, prices_df, as_of: date, skill: dict) -> dict:
    return point_in_time_bundle(raw, prices_df, as_of, skill)[0]


def point_in_time_bundle(raw: dict, prices_df, as_of: date, skill: dict) -> tuple[dict, list[dict]]:
    """(payload for the model, references list) built only from data available before as_of."""
    info_fields, statement_rows = skill_data_requirements(skill)
    hist = prices_df[prices_df["date"] < as_of.isoformat()]
    if len(hist) < 30:
        raise HTTPException(status_code=422, detail=f"Fewer than 30 price rows before {as_of}; load more history first.")
    price = float(hist["close"].iloc[-1])
    annual = visible_statements(raw, as_of, "A")
    quarterly = visible_statements(raw, as_of, "Q")
    references = build_references(raw, hist, annual, quarterly)
    fundamentals, note = derive_fundamentals(raw, annual, quarterly, price)
    fundamentals = {k: v for k, v in fundamentals.items() if k in info_fields}
    annual_rows = {
        stmt: {
            item: {pe[:4]: v for pe, v in annual.get(stmt, {}).get(item, {}).items()}
            for item in rows if item in annual.get(stmt, {})
        }
        for stmt, rows in statement_rows.items()
    }
    years = sorted({pe[:4] for s in annual.values() for ser in s.values() for pe in ser})
    quarters = sorted({pe for s in quarterly.values() for ser in s.values() for pe in ser})
    return ({
        "symbol": raw["symbol"],
        "analysis_date": as_of.isoformat(),
        "price": {**price_snapshot(hist), "source": "database"},
        "fundamentals": fundamentals,
        "annual_statements": annual_rows,
        "source_note": (
            f"POINT-IN-TIME BACKTEST. Today is {as_of.isoformat()}. Prices run through {hist['date'].iloc[-1]}. "
            f"Annual statements visible: fiscal years {', '.join(years) if years else 'none'}; quarterly periods visible: "
            f"{', '.join(quarters) if quarters else 'none'} (a period is visible only once reported - actual earnings-release "
            f"dates where known, otherwise {ANNUAL_LAG_DAYS}-day annual / {QUARTERLY_LAG_DAYS}-day quarterly lags). {note} "
            f"You have no knowledge of anything after {as_of.isoformat()}; do not use it."
        ),
    }, references)


# ---------------------------------------------------------------------------
# Decision report PDF (fpdf2, core Helvetica -> Latin-1 text) and Vercel Blob storage
# ---------------------------------------------------------------------------
_LATIN1_MAP = {
    "–": "-", "—": "-", "−": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
    "…": "...", "→": "->", "←": "<-", "•": "-", " ": " ", "≤": "<=", "≥": ">=",
    "≈": "~", "✓": "yes", "✗": "no", "σ": "sigma", "‰": " per mille",
}


def _latin1(text) -> str:
    s = "" if text is None else str(text)
    s = "".join(_LATIN1_MAP.get(ch, ch) for ch in s)
    return s.encode("latin-1", "replace").decode("latin-1")


def build_decision_pdf(skill: dict, symbol: str, as_of: str, analysis: dict, references: list[dict], meta: dict) -> bytes:
    """Key conclusion first, then the full analysis, then the data and reports referenced."""
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    W = pdf.epw
    ccy = analysis.get("currency", "")

    def money(v):
        return "-" if v is None else f"{v:,.2f} {ccy}"

    def pct(v):
        return "-" if v is None else f"{v:+.1f}%"

    def heading(text, size=12, top=3):
        pdf.ln(top)
        pdf.set_font("Helvetica", "B", size)
        pdf.multi_cell(W, 6, _latin1(text))
        pdf.ln(1)

    def para(text, size=10):
        pdf.set_font("Helvetica", "", size)
        for chunk in str(text or "").split("\n\n"):
            if chunk.strip():
                pdf.multi_cell(W, 5, _latin1(chunk.strip()))
                pdf.ln(1.5)

    def bullets(items):
        pdf.set_font("Helvetica", "", 10)
        for it in items or []:
            pdf.multi_cell(W, 5, _latin1(f"- {it}"))
        pdf.ln(1)

    def table(headers, rows, widths):
        if not rows:
            return
        pdf.set_font("Helvetica", "", 9)
        with pdf.table(col_widths=widths, text_align="LEFT", line_height=4.5) as tbl:
            hdr = tbl.row()
            for h in headers:
                hdr.cell(_latin1(h))
            for r in rows:
                row = tbl.row()
                for c in r:
                    row.cell(_latin1(c))
        pdf.ln(2)

    # Title block
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(W, 8, _latin1(f"{skill['name']} analysis: {symbol} as of {as_of}"))
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90)
    pdf.multi_cell(W, 5, _latin1(
        f"Point-in-time backtest. Skill {skill['id']} v{skill.get('version') or '-'}; model {meta.get('model', '')}; "
        f"generated {meta.get('generated_at', '')} UTC."))
    pdf.set_text_color(0)
    pdf.ln(3)

    # Key conclusion
    rec = analysis.get("recommendation", "")
    fill = {"BUY": (222, 245, 222), "SELL": (250, 225, 225)}.get(rec, (240, 240, 240))
    pdf.set_fill_color(*fill)
    pdf.set_font("Helvetica", "B", 13)
    pdf.multi_cell(W, 8, _latin1(f"{rec}: {analysis.get('headline', '')}"), fill=True,
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    key_rows = [
        ("Current price", money(analysis.get("current_price"))),
        ("Buy below", money(analysis.get("buy_below_price"))),
        ("Target price", money(analysis.get("target_price"))),
        ("Stop price", money(analysis.get("stop_price"))),
        ("Intrinsic value (low / base / high)",
         f"{money(analysis.get('intrinsic_value_low'))} / {money(analysis.get('intrinsic_value_base'))} / {money(analysis.get('intrinsic_value_high'))}"),
        ("Margin of safety", f"{pct(analysis.get('margin_of_safety_pct'))} (required {analysis.get('required_margin_of_safety_pct', '-')}%)"),
        ("Conviction", str(analysis.get("conviction", "-"))),
        ("Time horizon", str(analysis.get("time_horizon", "-"))),
    ]
    table(("Key figure", "Value"), key_rows, (45, 55))
    if analysis.get("target_rationale"):
        para(f"Target: {analysis['target_rationale']}", 9)
    if analysis.get("stop_rationale"):
        para(f"Stop: {analysis['stop_rationale']}", 9)
    if meta.get("warnings"):
        bullets([f"Warning: {w}" for w in meta["warnings"]])

    heading("Summary and justification")
    para(analysis.get("summary", ""))

    if analysis.get("balance_sheet"):
        bs = analysis["balance_sheet"]
        heading(f"Balance sheet: {bs.get('grade', '')}")
        table(("Aspect", "Assessment"), [(k.replace("_", " "), v) for k, v in bs.items() if k != "grade"], (30, 70))
    if analysis.get("stress_test"):
        heading("Stress test")
        table(("Scenario", "Assumptions", "Earnings", "Price", "Survives"),
              [(s.get("scenario", ""), s.get("assumptions", ""), pct(s.get("earnings_impact_pct")),
                pct(s.get("price_impact_pct")), "yes" if s.get("survives_without_capital_raise") else "no")
               for s in analysis["stress_test"]], (16, 52, 12, 10, 10))

    # Skill-specific blocks (technical skills): objects as key/value tables, lists of objects as tables.
    for key in ("trend_assessment", "trade_plan"):
        block = analysis.get(key)
        if isinstance(block, dict) and block:
            heading(key.replace("_", " ").capitalize())
            table(("Item", "Value"), [(k.replace("_", " "), "-" if v is None else str(v)) for k, v in block.items()], (35, 65))
    for key in ("key_levels", "indicator_readings", "patterns_identified"):
        items = analysis.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            cols = list(items[0].keys())
            heading(key.replace("_", " ").capitalize())
            table(tuple(c.replace("_", " ") for c in cols),
                  [tuple("-" if r.get(c) is None else str(r.get(c)) for c in cols) for r in items],
                  tuple([max(8, 100 // len(cols))] * len(cols)))

    heading("Checklist")
    table(("Criterion", "Verdict", "Evidence"),
          [(c.get("criterion", ""), c.get("verdict", ""), c.get("evidence", "")) for c in analysis.get("checklist", [])],
          (28, 14, 58))

    for s in analysis.get("sections", []):
        heading(s.get("title", ""))
        para(s.get("content", ""))

    val = analysis.get("valuation") or {}
    if val:
        heading("Valuation inputs")
        table(("Input", "Value"), [(k.replace("_", " "), "-" if v is None else str(v)) for k, v in val.items()], (35, 65))

    heading("Key metrics")
    table(("Metric", "Value", "Assessment", "Source"),
          [(m.get("name", ""), m.get("value", ""), m.get("assessment", ""), m.get("source", ""))
           for m in analysis.get("key_metrics", [])], (22, 16, 38, 24))

    for title, key in (("Risks", "risks"), ("Assumptions (model judgment, not data)", "assumptions"), ("Data gaps", "data_gaps")):
        if analysis.get(key):
            heading(title)
            bullets(analysis[key])

    heading("Data and reports referenced")
    table(("Source", "Detail", "Used for"),
          [(r.get("title", ""), r.get("detail", ""), r.get("used_for", "")) for r in references], (28, 48, 24))

    pdf.ln(2)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(90)
    pdf.multi_cell(W, 4, _latin1(
        "Model-generated analysis from third-party data reconstructed as of the analysis date. The model's training "
        "knowledge cannot be rewound to that date. Informational only; not investment advice. Verify before use."))
    pdf.set_text_color(0)
    return bytes(pdf.output())


def blob_access() -> str:
    return os.environ.get("BLOB_ACCESS", "private").strip().lower() or "private"


def blob_configured() -> bool:
    return bool(os.environ.get("BLOB_READ_WRITE_TOKEN") or os.environ.get("VERCEL_BLOB_READ_WRITE_TOKEN"))


def upload_report(pathname: str, pdf_bytes: bytes) -> dict:
    from vercel.blob.sync import put_blob

    r = put_blob(pathname, pdf_bytes, access=blob_access(), content_type="application/pdf", allow_overwrite=True)
    return {"pathname": r.pathname, "url": r.url}


def download_report(pathname: str) -> bytes:
    from vercel.blob.sync import get_blob

    return get_blob(pathname, access=blob_access()).body


def report_pathname(skill_id: str, symbol: str, as_of: str, decision_id: int) -> str:
    safe_symbol = re.sub(r"[^A-Za-z0-9.-]", "_", symbol)
    return f"reports/{skill_id}/{safe_symbol}/{as_of[:4]}/{as_of}-{decision_id}.pdf"


# ---------------------------------------------------------------------------
# Symbol data bundle: everything the skills need, downloaded once per symbol and stored in Vercel Blob
# (data/<SYMBOL>/latest.json + a dated copy) and in dbo.symbol_data. AI runs read the bundle, never Yahoo.
# ---------------------------------------------------------------------------
SYMBOL_DATA_TABLE = "dbo.symbol_data"
BUNDLE_MAX_AGE_HOURS = 24

ENSURE_SYMBOL_DATA_SQL = f"""
IF OBJECT_ID('{SYMBOL_DATA_TABLE}', 'U') IS NULL
CREATE TABLE {SYMBOL_DATA_TABLE} (
    symbol        NVARCHAR(20)   NOT NULL PRIMARY KEY,
    fetched_at    DATETIME2      NOT NULL,
    bundle_json   NVARCHAR(MAX)  NOT NULL,
    data_pathname NVARCHAR(400)  NULL,
    summary_json  NVARCHAR(MAX)  NULL
);
IF COL_LENGTH('{BACKTESTS_TABLE}', 'data_pathname') IS NULL ALTER TABLE {BACKTESTS_TABLE} ADD data_pathname NVARCHAR(400) NULL;
"""


def all_info_fields() -> tuple:
    """Union of the default quote-summary fields and every skill's documented fundamentals fields."""
    fields = set(INFO_FIELDS)
    for s in list_skills():
        try:
            fields |= set(s["data_structure"]["input"]["properties"]["fundamentals"]["properties"])
        except (KeyError, TypeError):
            pass
    return tuple(sorted(fields))


def bundle_summary(bundle: dict) -> dict:
    a = bundle["statements"].get("A", {})
    q = bundle["statements"].get("Q", {})
    a_periods = sorted({pe for s in a.values() for ser in s.values() for pe in ser})
    q_periods = sorted({pe for s in q.values() for ser in s.values() for pe in ser})
    prices = bundle.get("prices", [])
    return {
        "symbol": bundle["symbol"],
        "fetched_at": bundle["fetched_at"],
        "profile": bool(bundle.get("profile")),
        "quote_summary_fields": len(bundle.get("quote_summary", {})),
        "annual_periods": a_periods,
        "quarterly_periods": q_periods,
        "annual_line_items": sum(len(rows) for rows in a.values()),
        "quarterly_line_items": sum(len(rows) for rows in q.values()),
        "earnings_dates": len(bundle.get("earnings_dates", [])),
        "price_rows": len(prices),
        "price_range": [prices[0]["trade_date"], prices[-1]["trade_date"]] if prices else None,
        "data_pathname": bundle.get("data_pathname"),
        "data_files": bundle.get("data_files", []),
    }


PRICES_MIN_ROWS = 250        # about one trading year
PRICES_MAX_STALE_DAYS = 7    # latest stored close must be at most this old


def stored_prices(symbol: str) -> list[dict] | None:
    """Stored daily prices as plain dicts when they are sufficient for a run; None means download."""
    conn = db_connect()
    try:
        cur = conn.cursor()
        if not table_exists(cur):
            return None
        cur.execute(f"SELECT COUNT(*), MAX(trade_date) FROM {TABLE} WHERE symbol = %s", (symbol,))
        count, last = cur.fetchone()
        if count < PRICES_MIN_ROWS or last is None or (date.today() - last).days > PRICES_MAX_STALE_DAYS:
            return None
        df = load_prices_df(cur, symbol)
    finally:
        conn.close()
    return [
        {"trade_date": r.date, "open": num(r.open), "high": num(r.high), "low": num(r.low), "close": num(r.close),
         "adj_close": num(r.adj_close), "volume": None if r.volume != r.volume else int(r.volume)}
        for r in df.itertuples(index=False)
    ]


def build_symbol_bundle(symbol: str, log: "RunLog | None" = None) -> dict:
    """Download prices (stored in dbo.stock_prices too), quote summary, annual + quarterly statements and
    earnings dates for one symbol."""
    import yfinance as yf

    def say(msg):
        if log:
            log.step(msg)

    # Prices: reuse what is already stored unless it is thin or stale; download only then.
    t0 = time.monotonic()
    prices = stored_prices(symbol)
    if prices:
        say(f"Prices: {len(prices)} daily rows already stored ({prices[0]['trade_date']} to {prices[-1]['trade_date']}); not downloaded again")
    else:
        rows = fetch_history(symbol)
        save_prices(symbol, rows)
        prices = [r.model_dump(mode="json") for r in rows]
        say(f"Prices: {len(prices)} daily rows ({prices[0]['trade_date']} to {prices[-1]['trade_date']}) downloaded and stored in {time.monotonic() - t0:.1f} s")

    t0 = time.monotonic()
    raw = fetch_backtest_raw(symbol)  # profile, statements A/Q (every line item), earnings dates
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception:  # noqa: BLE001
        info = {}
    quote_summary = {k: info[k] for k in all_info_fields() if info.get(k) is not None}
    bundle = {
        **raw,
        "quote_summary": quote_summary,
        "prices": prices,
        "source": "Yahoo Finance via yfinance",
    }
    s = bundle_summary(bundle)
    say(f"Reports: {len(s['annual_periods'])} annual and {len(s['quarterly_periods'])} quarterly periods "
        f"({s['annual_line_items']} + {s['quarterly_line_items']} line items), {s['earnings_dates']} earnings dates, "
        f"{s['quote_summary_fields']} quote-summary fields, profile {'found' if s['profile'] else 'missing'} - "
        f"{time.monotonic() - t0:.1f} s")
    if not raw["statements"]["A"].get("income"):
        raise HTTPException(status_code=404, detail=f"Yahoo Finance returned no financial statements for {symbol}.")
    return bundle


def store_symbol_bundle(bundle: dict, log: "RunLog | None" = None) -> dict:
    """Blob first (when configured), then dbo.symbol_data. Returns the summary."""
    symbol = bundle["symbol"]
    stamp = bundle["fetched_at"][:19].replace(":", "-")
    if blob_configured():
        from vercel.blob.sync import put_blob

        def put(name: str, obj) -> int:
            body = json.dumps(obj, indent=1).encode()
            r = put_blob(name, body, access=blob_access(), content_type="application/json", allow_overwrite=True)
            return len(body)

        base = f"data/{symbol}"
        # One document per data set, browsable in the Blob store ...
        parts: dict[str, object] = {
            "profile.json": bundle.get("profile", {}),
            "quote-summary.json": bundle.get("quote_summary", {}),
            "earnings-dates.json": bundle.get("earnings_dates", []),
            "prices.json": bundle.get("prices", []),
        }
        for period, folder in (("A", "annual"), ("Q", "quarterly")):
            for stmt, fname in (("income", "income-statement"), ("balance", "balance-sheet"), ("cashflow", "cash-flow-statement")):
                parts[f"statements/{folder}/{fname}.json"] = bundle["statements"].get(period, {}).get(stmt, {})
        files = []
        for name, obj in parts.items():
            size = put(f"{base}/{name}", obj)
            files.append({"path": f"{base}/{name}", "bytes": size})
        # ... plus the complete bundle the runs read, a dated snapshot, and a manifest.
        full = json.dumps(bundle).encode()
        for name in (f"{base}/{stamp}.json", f"{base}/latest.json"):
            r = put_blob(name, full, access=blob_access(), content_type="application/json", allow_overwrite=True)
            bundle["data_pathname"] = r.pathname
        files.append({"path": f"{base}/latest.json", "bytes": len(full)})
        files.append({"path": f"{base}/{stamp}.json", "bytes": len(full)})
        manifest = {"symbol": symbol, "fetched_at": bundle["fetched_at"], "source": bundle.get("source"), "files": files}
        put(f"{base}/index.json", manifest)
        bundle["data_files"] = [f["path"] for f in files] + [f"{base}/index.json"]
        if log:
            log.step(f"Data stored in Vercel Blob under {base}/: {len(bundle['data_files'])} files - profile, quote summary, "
                     f"earnings dates, prices, 3 annual + 3 quarterly statements, latest.json ({len(full) / 1024:.0f} KB), "
                     f"snapshot {stamp}.json, index.json")
    elif log:
        log.step("Blob storage not configured; data bundle kept in dbo.symbol_data only", "warn")
    summary = bundle_summary(bundle)
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(ENSURE_SYMBOL_DATA_SQL)
        cur.execute(
            f"MERGE {SYMBOL_DATA_TABLE} AS t USING (SELECT %s AS symbol) AS s ON t.symbol = s.symbol "
            f"WHEN MATCHED THEN UPDATE SET fetched_at = %s, bundle_json = %s, data_pathname = %s, summary_json = %s "
            f"WHEN NOT MATCHED THEN INSERT (symbol, fetched_at, bundle_json, data_pathname, summary_json) "
            f"VALUES (%s, %s, %s, %s, %s);",
            (symbol, bundle["fetched_at"], json.dumps(bundle), bundle.get("data_pathname"), json.dumps(summary),
             symbol, bundle["fetched_at"], json.dumps(bundle), bundle.get("data_pathname"), json.dumps(summary)),
        )
        conn.commit()
    finally:
        conn.close()
    if log:
        log.step(f"Data bundle saved in {SYMBOL_DATA_TABLE}")
    return summary


def load_symbol_bundle(symbol: str, max_age_hours: float = BUNDLE_MAX_AGE_HOURS, refresh: bool = False,
                       log: "RunLog | None" = None) -> dict:
    """Stored bundle if fresh enough, otherwise download and store a new one."""
    if not refresh:
        conn = db_connect()
        try:
            cur = conn.cursor()
            cur.execute(ENSURE_SYMBOL_DATA_SQL)
            conn.commit()
            cur.execute(f"SELECT fetched_at, bundle_json FROM {SYMBOL_DATA_TABLE} WHERE symbol = %s", (symbol,))
            row = cur.fetchone()
        finally:
            conn.close()
        if row:
            age_h = (datetime.now(timezone.utc) - row[0].replace(tzinfo=timezone.utc)).total_seconds() / 3600
            if age_h <= max_age_hours:
                if log:
                    log.step(f"Using stored data bundle for {symbol} fetched {age_h:.1f} h ago (no live download)")
                return json.loads(row[1])
            if log:
                log.step(f"Stored data bundle for {symbol} is {age_h:.0f} h old; refreshing")
        elif log:
            log.step(f"No stored data bundle for {symbol}; downloading")
    bundle = build_symbol_bundle(symbol, log)
    store_symbol_bundle(bundle, log)
    return bundle


def archive_run_json(prefix: str, payload: dict | None, output: dict | None, log: "RunLog | None" = None) -> None:
    """Keep each run's exact model input and output in Blob next to its report (best effort)."""
    if not blob_configured():
        return
    try:
        from vercel.blob.sync import put_blob

        for name, obj in ((f"{prefix}-input.json", payload), (f"{prefix}-output.json", output)):
            if obj is not None:
                put_blob(name, json.dumps(obj).encode(), access=blob_access(), content_type="application/json", allow_overwrite=True)
        if log:
            log.add(f"Run input/output archived in Vercel Blob: {prefix}-input.json, {prefix}-output.json")
    except Exception as e:  # noqa: BLE001
        if log:
            log.add(f"Could not archive run JSON in Blob: {type(e).__name__}: {str(e)[:150]}", "warn")


class PrepareRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    refresh: bool = False


@app.get("/api/py/data/{symbol}")
def data_status(symbol: str):
    symbol = normalize_symbol(symbol)
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(ENSURE_SYMBOL_DATA_SQL)
        conn.commit()
        cur.execute(f"SELECT fetched_at, summary_json, data_pathname FROM {SYMBOL_DATA_TABLE} WHERE symbol = %s", (symbol,))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {"symbol": symbol, "available": False}
    age_h = (datetime.now(timezone.utc) - row[0].replace(tzinfo=timezone.utc)).total_seconds() / 3600
    return {"symbol": symbol, "available": True, "age_hours": round(age_h, 1), "fresh": age_h <= BUNDLE_MAX_AGE_HOURS,
            "data_pathname": row[2], **(json.loads(row[1]) if row[1] else {})}


@app.post("/api/py/data/prepare")
def data_prepare(req: PrepareRequest):
    """Pre-download everything for a symbol and store it (Blob + SQL)."""
    symbol = normalize_symbol(req.symbol)
    log = RunLog(SYMBOL_DATA_TABLE, "symbol")  # row_id None: entries are returned, not persisted
    bundle = load_symbol_bundle(symbol, refresh=req.refresh, log=log)
    return {**bundle_summary(bundle), "log": log.entries}


class BacktestRequest(BaseModel):
    skill: str = Field(..., min_length=1, max_length=60)
    symbol: str = Field(..., min_length=1, max_length=20)
    year: int = Field(2026, ge=2000, le=2100)
    weeks: int = Field(10, ge=1, le=52, description="How many Mondays to run, counting from January 1")
    refresh_data: bool = Field(False, description="Re-download the symbol's data bundle even if a fresh one is stored")


def _decision_dict(row) -> dict:
    d = dict(zip(DECISION_COLUMNS, row))
    for k in ("current_price", "buy_below_price", "target_price", "stop_price", "intrinsic_value_low",
              "intrinsic_value_base", "intrinsic_value_high", "margin_of_safety_pct"):
        d[k] = num(d[k], 4)
    for k in ("as_of_date", "started_at", "finished_at"):
        d[k] = d[k].isoformat() if d[k] is not None else None
    d["warnings"] = json.loads(d.pop("warnings_json") or "[]")
    d["references"] = json.loads(d.pop("references_json") or "[]")
    d["log"] = json.loads(d.pop("log_json") or "[]")
    d["report_stored"] = bool(d.get("report_pathname"))
    d["report_html_stored"] = bool(d.get("report_html_pathname"))
    return d


def _decision_counts(cur, backtest_id: int) -> dict:
    cur.execute(f"SELECT status, COUNT(*) FROM {DECISIONS_TABLE} WHERE backtest_id = %s GROUP BY status", (backtest_id,))
    counts = {s: 0 for s in DECISION_STATUSES}
    for status, n in cur.fetchall():
        counts[status] = n
    counts["total"] = sum(counts.values())
    return counts


def _backtest_meta(cur, backtest_id: int) -> dict:
    cur.execute(
        f"SELECT backtest_id, skill_id, skill_version, symbol, [year], created_at, log_json, status FROM {BACKTESTS_TABLE} "
        f"WHERE backtest_id = %s",
        (backtest_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return {"backtest_id": row[0], "skill_id": row[1], "skill_version": row[2], "symbol": row[3], "year": row[4],
            "created_at": row[5].isoformat(), "log": json.loads(row[6] or "[]"), "status": row[7] or "active",
            "counts": _decision_counts(cur, backtest_id)}


def _backtest_status(cur, backtest_id: int) -> str:
    cur.execute(f"SELECT status FROM {BACKTESTS_TABLE} WHERE backtest_id = %s", (backtest_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return row[0] or "active"


def _decisions(cur, backtest_id: int) -> list[dict]:
    cur.execute(
        f"SELECT {', '.join(DECISION_COLUMNS)} FROM {DECISIONS_TABLE} WHERE backtest_id = %s ORDER BY as_of_date",
        (backtest_id,),
    )
    return [_decision_dict(r) for r in cur.fetchall()]


@app.get("/api/py/agent/backtests")
def list_backtests(symbol: str | None = None, skill: str | None = None):
    conn = db_connect()
    try:
        cur = conn.cursor()
        ensure_backtest_tables(cur)
        conn.commit()
        where, params = [], []
        if symbol:
            where.append("b.symbol = %s"); params.append(normalize_symbol(symbol))
        if skill:
            where.append("b.skill_id = %s"); params.append(skill.strip().lower())
        cur.execute(
            f"SELECT b.backtest_id, b.skill_id, b.symbol, b.[year], b.created_at, "
            f"SUM(CASE WHEN d.status = 'done' THEN 1 ELSE 0 END), SUM(CASE WHEN d.status = 'failed' THEN 1 ELSE 0 END), COUNT(d.decision_id) "
            f"FROM {BACKTESTS_TABLE} b LEFT JOIN {DECISIONS_TABLE} d ON d.backtest_id = b.backtest_id "
            + (f"WHERE {' AND '.join(where)} " if where else "")
            + "GROUP BY b.backtest_id, b.skill_id, b.symbol, b.[year], b.created_at ORDER BY b.created_at DESC",
            tuple(params),
        )
        return {"backtests": [
            {"backtest_id": r[0], "skill_id": r[1], "symbol": r[2], "year": r[3], "created_at": r[4].isoformat(),
             "done": r[5] or 0, "failed": r[6] or 0, "total": r[7]}
            for r in cur.fetchall()
        ]}
    finally:
        conn.close()


@app.get("/api/py/agent/diagnostics")
def agent_diagnostics():
    """Configuration the backtest will use (no secret values) plus a live database connectivity check."""
    info = env_diagnostics()
    try:
        t0 = time.monotonic()
        conn = db_connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT @@VERSION")
            version = str(cur.fetchone()[0]).split("\n")[0]
            cur.execute(f"SELECT COUNT(DISTINCT symbol), COUNT(*) FROM {TABLE}") if table_exists(cur) else None
            symbols, rows = cur.fetchone() if table_exists(cur) else (0, 0)
        finally:
            conn.close()
        info["database"].update({"connected": True, "latency_ms": round((time.monotonic() - t0) * 1000),
                                 "version": version, "symbols_loaded": symbols, "price_rows": rows})
    except HTTPException as e:
        info["database"].update({"connected": False, "error": str(e.detail)})
    info["skills"] = [s["id"] for s in list_skills()]
    return info


@app.get("/api/py/agent/runs")
def list_runs(limit: int = 200):
    """Every backtest run with its agent, timing, Monday range, counts, overall status and failure reasons."""
    limit = max(1, min(limit, 1000))
    conn = db_connect()
    try:
        cur = conn.cursor()
        ensure_backtest_tables(cur)
        conn.commit()
        cur.execute(
            f"SELECT TOP {limit} b.backtest_id, b.skill_id, b.skill_version, b.symbol, b.[year], b.created_at, b.status, "
            f"MIN(d.as_of_date), MAX(d.as_of_date), MIN(d.started_at), MAX(d.finished_at), COUNT(d.decision_id), "
            f"SUM(CASE WHEN d.status = 'done' THEN 1 ELSE 0 END), SUM(CASE WHEN d.status = 'failed' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN d.status = 'cancelled' THEN 1 ELSE 0 END), SUM(CASE WHEN d.status = 'pending' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN d.status = 'running' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN d.recommendation = 'BUY' THEN 1 ELSE 0 END), SUM(CASE WHEN d.recommendation = 'HOLD' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN d.recommendation = 'SELL' THEN 1 ELSE 0 END), SUM(ISNULL(d.input_tokens, 0)), SUM(ISNULL(d.output_tokens, 0)) "
            f"FROM {BACKTESTS_TABLE} b LEFT JOIN {DECISIONS_TABLE} d ON d.backtest_id = b.backtest_id "
            f"GROUP BY b.backtest_id, b.skill_id, b.skill_version, b.symbol, b.[year], b.created_at, b.status "
            f"ORDER BY b.created_at DESC"
        )
        rows = cur.fetchall()
        ids = [r[0] for r in rows]
        failures: dict[int, list] = {}
        if ids:
            cur.execute(
                f"SELECT backtest_id, as_of_date, status, error, decision_id FROM {DECISIONS_TABLE} "
                f"WHERE backtest_id IN ({', '.join('%s' for _ in ids)}) AND error IS NOT NULL ORDER BY as_of_date",
                tuple(ids),
            )
            for bid, as_of, status, error, did in cur.fetchall():
                failures.setdefault(bid, []).append({"as_of_date": as_of.isoformat(), "status": status, "error": error, "decision_id": did})
    finally:
        conn.close()

    runs = []
    for r in rows:
        (bid, skill_id, version, symbol, year, created, bstatus, first, last, started, finished, total,
         done, failed, cancelled, pending, running, buy, hold, sell, tin, tout) = r
        done, failed, cancelled, pending, running = (x or 0 for x in (done, failed, cancelled, pending, running))
        if (bstatus or "active") == "stopped":
            overall = "stopped"
        elif failed:
            overall = "failed"
        elif pending or running:
            overall = "in progress"
        elif total and done == total:
            overall = "success"
        else:
            overall = "partial"
        duration = (finished - started).total_seconds() if started and finished else None
        runs.append({
            "backtest_id": bid, "skill_id": skill_id, "skill_version": version, "symbol": symbol, "year": year,
            "created_at": created.isoformat(), "started_at": started.isoformat() if started else None,
            "finished_at": finished.isoformat() if finished else None, "duration_s": round(duration) if duration is not None else None,
            "first_monday": first.isoformat() if first else None, "last_monday": last.isoformat() if last else None,
            "status": overall, "backtest_status": bstatus or "active",
            "counts": {"total": total, "done": done, "failed": failed, "cancelled": cancelled, "pending": pending, "running": running},
            "verdicts": {"BUY": buy or 0, "HOLD": hold or 0, "SELL": sell or 0},
            "tokens": {"input": tin or 0, "output": tout or 0},
            "failures": failures.get(bid, []),
        })
    return {"runs": runs}


@app.post("/api/py/agent/backtest")
def create_backtest(req: BacktestRequest):
    require_claude()
    log = RunLog(BACKTESTS_TABLE, "backtest_id")
    log.add(f"Claude: {describe_claude()}")
    claude_hello(log)  # first AI call of the run: prove the model answers before any data work
    log.add(f"Database: {describe_db()} ({_set('AZURE_SQL_PASSWORD')})")
    log.add(f"Blob storage: {'configured, access=' + blob_access() if blob_configured() else 'not configured - PDFs will be generated on demand'}")
    skill = get_skill(req.skill.strip().lower())
    log.add(f"Skill loaded: {skill['path']} v{skill.get('version') or '-'} ({len(skill['instructions'])} chars)")
    symbol = normalize_symbol(req.symbol)
    dates = mondays(req.year, date.today())[: req.weeks]
    if not dates:
        raise HTTPException(status_code=422, detail=f"No Mondays in {req.year} on or before today.")
    log.add(f"Schedule: {len(dates)} Mondays from {dates[0]} to {dates[-1]}")

    if skill.get("data_source") == "tech":
        # Technical skills read the TECH_ tables only; make sure they exist and are current before the run.
        st = ensure_technicals(symbol, log)
        raw = {"symbol": symbol, "data_source": "tech", "computed_at": st["computed_at"], "fetched_at": st["computed_at"],
               "first_trade_date": st["first_trade_date"], "last_trade_date": st["last_trade_date"], "counts": st["counts"]}
        data_pathname = None
        log.add(f"Data for the run: TECH_ tables for {symbol} - {st['price_rows']} trading days {st['first_trade_date']} to "
                f"{st['last_trade_date']}; " + ", ".join(f"{k} {v}" for k, v in st["counts"].items()))
    else:
        # All documents and data come from the symbol's stored bundle (downloaded once, kept in Blob + SQL).
        bundle = load_symbol_bundle(symbol, refresh=req.refresh_data, log=log)
        raw = {k: bundle[k] for k in ("symbol", "profile", "statements", "earnings_dates", "fetched_at")}
        data_pathname = bundle.get("data_pathname")
        s = bundle_summary(bundle)
        log.add(f"Data for the run: prices {s['price_range'][0]} to {s['price_range'][1]} ({s['price_rows']} rows), "
                f"annual periods {', '.join(s['annual_periods'])}, quarterly periods {len(s['quarterly_periods'])}, "
                f"bundle {data_pathname or 'in dbo.symbol_data'}")

    conn = db_connect()
    try:
        cur = conn.cursor()
        ensure_backtest_tables(cur)
        cur.execute(ENSURE_SYMBOL_DATA_SQL)
        cur.execute(
            f"INSERT INTO {BACKTESTS_TABLE} (skill_id, skill_version, symbol, [year], raw_data, data_pathname) "
            f"OUTPUT INSERTED.backtest_id VALUES (%s, %s, %s, %s, %s, %s)",
            (skill["id"], skill["version"], symbol, req.year, json.dumps(raw), data_pathname),
        )
        backtest_id = cur.fetchone()[0]
        log.row_id = backtest_id
        log.add(f"Backtest #{backtest_id} created with {len(dates)} pending decisions")
        log.save(cur)
        cur.executemany(
            f"INSERT INTO {DECISIONS_TABLE} (backtest_id, skill_id, symbol, as_of_date, status) VALUES (%s, %s, %s, %s, 'pending')",
            [(backtest_id, skill["id"], symbol, d) for d in dates],
        )
        conn.commit()
        return {**_backtest_meta(cur, backtest_id), "decisions": _decisions(cur, backtest_id)}
    finally:
        conn.close()


@app.get("/api/py/agent/backtest/{backtest_id}")
def get_backtest(backtest_id: int):
    conn = db_connect()
    try:
        cur = conn.cursor()
        ensure_backtest_tables(cur)
        conn.commit()
        return {**_backtest_meta(cur, backtest_id), "decisions": _decisions(cur, backtest_id)}
    finally:
        conn.close()


@app.post("/api/py/agent/backtest/{backtest_id}/stop")
def stop_backtest(backtest_id: int):
    """Stop completely: no further Monday is claimed (by any tab), pending Mondays are cancelled, and a Monday
    stuck in 'running' for over 3 minutes (e.g. its request was cut off) is marked failed."""
    conn = db_connect()
    try:
        cur = conn.cursor()
        ensure_backtest_tables(cur)
        _backtest_status(cur, backtest_id)
        cur.execute(f"UPDATE {BACKTESTS_TABLE} SET status = 'stopped' WHERE backtest_id = %s", (backtest_id,))
        cur.execute(
            f"UPDATE {DECISIONS_TABLE} SET status = 'cancelled', error = 'Stopped by user' "
            f"WHERE backtest_id = %s AND status = 'pending'",
            (backtest_id,),
        )
        cancelled = cur.rowcount
        cur.execute(
            f"UPDATE {DECISIONS_TABLE} SET status = 'failed', error = 'Stopped by user while running', finished_at = SYSUTCDATETIME() "
            f"WHERE backtest_id = %s AND status = 'running' AND started_at < DATEADD(minute, -3, SYSUTCDATETIME())",
            (backtest_id,),
        )
        conn.commit()
        meta = _backtest_meta(cur, backtest_id)
        log = RunLog(BACKTESTS_TABLE, "backtest_id", backtest_id)
        log.entries = meta["log"]
        log.step(f"Stopped by user: {cancelled} pending Monday(s) cancelled", "warn", cur=cur)
        conn.commit()
        return {**_backtest_meta(cur, backtest_id), "decisions": _decisions(cur, backtest_id)}
    finally:
        conn.close()


@app.post("/api/py/agent/backtest/{backtest_id}/resume")
def resume_backtest(backtest_id: int):
    """Reactivate a stopped backtest: cancelled Mondays become pending again."""
    conn = db_connect()
    try:
        cur = conn.cursor()
        ensure_backtest_tables(cur)
        _backtest_status(cur, backtest_id)
        cur.execute(f"UPDATE {BACKTESTS_TABLE} SET status = 'active' WHERE backtest_id = %s", (backtest_id,))
        cur.execute(
            f"UPDATE {DECISIONS_TABLE} SET status = 'pending', error = NULL WHERE backtest_id = %s AND status = 'cancelled'",
            (backtest_id,),
        )
        restored = cur.rowcount
        conn.commit()
        meta = _backtest_meta(cur, backtest_id)
        log = RunLog(BACKTESTS_TABLE, "backtest_id", backtest_id)
        log.entries = meta["log"]
        log.step(f"Resumed by user: {restored} Monday(s) back to pending", cur=cur)
        conn.commit()
        return {**_backtest_meta(cur, backtest_id), "decisions": _decisions(cur, backtest_id)}
    finally:
        conn.close()


@app.get("/api/py/agent/backtest/{backtest_id}/decision/{decision_id}/report.pdf")
def decision_report(backtest_id: int, decision_id: int):
    """The stored PDF from Vercel Blob; regenerated from the stored output if the blob is missing."""
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT skill_id, symbol, as_of_date, output_json, references_json, warnings_json, model, finished_at, "
            f"report_pathname FROM {DECISIONS_TABLE} WHERE backtest_id = %s AND decision_id = %s",
            (backtest_id, decision_id),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Decision not found")
    skill_id, symbol, as_of, output_json, references_json, warnings_json, model, finished_at, pathname = row
    if not output_json:
        raise HTTPException(status_code=404, detail="This decision has no completed analysis.")

    pdf_bytes = None
    if pathname and blob_configured():
        try:
            pdf_bytes = download_report(pathname)
        except Exception:  # noqa: BLE001 - fall back to regenerating
            pdf_bytes = None
    if pdf_bytes is None:
        pdf_bytes = build_decision_pdf(
            get_skill(skill_id), symbol, as_of.isoformat(), json.loads(output_json),
            json.loads(references_json or "[]"),
            {"model": model, "generated_at": finished_at.isoformat() if finished_at else "", "warnings": json.loads(warnings_json or "[]")},
        )
    filename = f"{symbol}-{as_of.isoformat()}-{skill_id}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "private, max-age=300"})


@app.get("/api/py/agent/backtest/{backtest_id}/decision/{decision_id}")
def get_decision(backtest_id: int, decision_id: int):
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {', '.join(DECISION_COLUMNS)}, input_json, output_json FROM {DECISIONS_TABLE} "
            f"WHERE backtest_id = %s AND decision_id = %s",
            (backtest_id, decision_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Decision not found")
        d = _decision_dict(row[: len(DECISION_COLUMNS)])
        d["input"] = json.loads(row[-2]) if row[-2] else None
        d["output"] = json.loads(row[-1]) if row[-1] else None
        return d
    finally:
        conn.close()


@app.post("/api/py/agent/backtest/{backtest_id}/step")
def backtest_step(backtest_id: int, retry_failed: bool = False):
    """Run the next pending Monday (one Claude call per request so it fits a serverless time limit)."""
    log = RunLog(DECISIONS_TABLE, "decision_id")
    log.add(f"Claude: {describe_claude()}")
    log.add(f"Database: connecting to {describe_db()}")
    # Phase 1: claim a decision and load what it needs, then release the connection for the model call.
    t0 = time.monotonic()
    conn = db_connect()
    try:
        cur = conn.cursor()
        log.add(f"Database connected in {round((time.monotonic() - t0) * 1000)} ms")
        ensure_backtest_tables(cur)
        cur.execute(f"SELECT skill_id, symbol, raw_data FROM {BACKTESTS_TABLE} WHERE backtest_id = %s", (backtest_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Backtest not found")
        skill_id, symbol, raw_json = row
        if _backtest_status(cur, backtest_id) == "stopped":
            return {"done": True, "stopped": True, "decision": None, "counts": _decision_counts(cur, backtest_id)}
        statuses = "('pending', 'failed')" if retry_failed else "('pending')"
        cur.execute(
            f"SELECT TOP 1 decision_id, as_of_date FROM {DECISIONS_TABLE} "
            f"WHERE backtest_id = %s AND status IN {statuses} ORDER BY as_of_date",
            (backtest_id,),
        )
        row = cur.fetchone()
        if not row:
            return {"done": True, "decision": None, "counts": _decision_counts(cur, backtest_id)}
        decision_id, as_of = row
        log.row_id = decision_id
        log.add(f"Claimed Monday {as_of} (decision #{decision_id}) for {symbol}")
        cur.execute(
            f"UPDATE {DECISIONS_TABLE} SET status = 'running', error = NULL, started_at = SYSUTCDATETIME(), log_json = %s "
            f"WHERE decision_id = %s",
            (json.dumps(log.entries), decision_id),
        )
        conn.commit()
        prices_df = load_prices_df(cur, symbol)
        log.step(f"Loaded {len(prices_df)} stored price rows ({prices_df['date'].iloc[0]} to {prices_df['date'].iloc[-1]})", cur=cur)
        conn.commit()
    finally:
        conn.close()

    skill = get_skill(skill_id)
    raw = json.loads(raw_json)
    log.add(f"Skill: {skill['path']} v{skill.get('version') or '-'}; statements from backtest #{backtest_id} "
            f"(fetched {raw.get('fetched_at', '')[:19]})")
    result: dict = {}
    error: str | None = None
    payload = None
    references: list[dict] = []
    report: dict = {}
    html_report: dict = {}
    try:
        if skill.get("data_source") == "tech":
            payload, references = load_tech_bundle(symbol, as_of, skill)
            log.step(f"Point-in-time technical data as of {as_of} from TECH_ tables: latest bar {payload['today']['trade_date']}, "
                     f"{len(payload['daily_bars'])} daily / {len(payload['weekly_bars'])} weekly bars, trend {payload['trend']['primary_trend']}/"
                     f"{payload['trend']['intermediate_trend']}/{payload['trend']['short_trend']}, {len(payload['levels'])} levels, "
                     f"{len(payload['pivots'])} pivots, {len(payload['signals'])} signals, {len(payload['patterns'])} patterns - nothing fetched or calculated")
        else:
            payload, references = point_in_time_bundle(raw, prices_df, as_of, skill)
            n_stmt = sum(1 for r in references if r["type"] == "statement")
            log.step(f"Point-in-time data as of {as_of}: price through {payload['price']['as_of']}, {n_stmt} statement "
                     f"periods visible, {len(payload['fundamentals'])} derived fundamentals")
        log.step(f"Querying Claude ({env_diagnostics()['claude']['provider']}, model '{os.environ.get('ANTHROPIC_MODEL', DEFAULT_CLAUDE_MODEL)}') ...")
        analysis, response = run_skill_with_claude(skill, payload, log)
        warnings = validate_verdict(analysis)
        log.add(f"Verdict {analysis['recommendation']} ({analysis.get('conviction')}): target {analysis.get('target_price')}, "
                f"stop {analysis.get('stop_price')}; {len(warnings)} warning(s)")
        result = {"analysis": analysis, "warnings": warnings, "model": response.model,
                  "input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
        # Build the PDF now and store it in Vercel Blob; a storage failure is a warning, not a failed decision.
        log.step("Generating PDF report ...")
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        pdf_bytes = build_decision_pdf(skill, symbol, as_of.isoformat(), analysis, references,
                                       {"model": response.model, "generated_at": generated_at, "warnings": warnings})
        log.add(f"PDF generated: {len(pdf_bytes) / 1024:.1f} KB")
        log.step("Generating HTML report (interactive chart + evidence) ...")
        chart = load_chart_data(symbol, as_of)
        html_text = build_decision_html(skill, symbol, as_of.isoformat(), analysis, references,
                                        {"model": response.model, "generated_at": generated_at, "warnings": warnings}, chart)
        log.add(f"HTML report generated: {len(html_text) / 1024:.0f} KB, chart from {chart.get('source')} with {len(chart.get('bars', []))} bars")
        if blob_configured():
            pathname = report_pathname(skill_id, symbol, as_of.isoformat(), decision_id)
            log.step(f"Uploading PDF and HTML reports to Vercel Blob ({blob_access()}): {pathname[:-4]}.{{pdf,html}}")
            try:
                t0 = time.monotonic()
                report = upload_report(pathname, pdf_bytes)
                html_report = upload_html_report(pathname[:-4] + ".html", html_text)
                log.add(f"Stored in Vercel Blob in {time.monotonic() - t0:.1f} s")
                archive_run_json(pathname[:-4], payload, analysis, log)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"Report PDF could not be stored in Vercel Blob ({type(e).__name__}: {str(e)[:200]}); it will be generated on demand.")
                log.add(f"Blob upload failed: {type(e).__name__}: {str(e)[:200]}", "warn")
        else:
            warnings.append("BLOB_READ_WRITE_TOKEN is not configured; the report PDF is generated on demand instead of stored.")
            log.add("Blob storage not configured; PDF will be generated on demand", "warn")
    except HTTPException as e:
        error = str(e.detail)[:1000]
        log.add(f"Failed: {error}", "error")

    # Phase 2: persist the outcome.
    log.add("Saving decision to dbo.agent_decisions")
    conn = db_connect()
    try:
        cur = conn.cursor()
        if error:
            cur.execute(
                f"UPDATE {DECISIONS_TABLE} SET status = 'failed', error = %s, input_json = %s, references_json = %s, "
                f"log_json = %s, finished_at = SYSUTCDATETIME() WHERE decision_id = %s",
                (error, json.dumps(payload) if payload else None, json.dumps(references), json.dumps(log.entries), decision_id),
            )
        else:
            a = result["analysis"]
            cur.execute(
                f"UPDATE {DECISIONS_TABLE} SET status = 'done', recommendation = %s, conviction = %s, current_price = %s, "
                f"buy_below_price = %s, target_price = %s, stop_price = %s, intrinsic_value_low = %s, intrinsic_value_base = %s, "
                f"intrinsic_value_high = %s, margin_of_safety_pct = %s, headline = %s, summary = %s, input_json = %s, "
                f"output_json = %s, warnings_json = %s, model = %s, input_tokens = %s, output_tokens = %s, "
                f"references_json = %s, report_pathname = %s, report_url = %s, report_html_pathname = %s, log_json = %s, "
                f"finished_at = SYSUTCDATETIME() WHERE decision_id = %s",
                (a["recommendation"], a["conviction"], a.get("current_price"), a.get("buy_below_price"),
                 a.get("target_price"), a.get("stop_price"), a.get("intrinsic_value_low"), a.get("intrinsic_value_base"),
                 a.get("intrinsic_value_high"), a.get("margin_of_safety_pct"), (a.get("headline") or "")[:500],
                 a.get("summary"), json.dumps(payload), json.dumps(a), json.dumps(result["warnings"]),
                 result["model"], result["input_tokens"], result["output_tokens"],
                 json.dumps(references), report.get("pathname"), report.get("url"), html_report.get("pathname"),
                 json.dumps(log.entries + [{"t": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                                            "elapsed_s": round(time.monotonic() - log._t0, 2), "level": "info",
                                            "message": "Decision saved"}]),
                 decision_id),
            )
        conn.commit()
        cur.execute(f"SELECT {', '.join(DECISION_COLUMNS)} FROM {DECISIONS_TABLE} WHERE decision_id = %s", (decision_id,))
        decision = _decision_dict(cur.fetchone())
        counts = _decision_counts(cur, backtest_id)
        stopped = _backtest_status(cur, backtest_id) == "stopped"
    finally:
        conn.close()
    return {"done": stopped or (counts["pending"] == 0 and counts["running"] == 0), "stopped": stopped,
            "decision": decision, "counts": counts}


# ---------------------------------------------------------------------------
# Technical-analysis data for skills with `data_source: tech` (John Murphy).
# Everything the agent needs is computed once per symbol from stored prices and written to the
# TECH_ tables; runs read those tables only, filtered to the analysis date. Pivots, levels and
# patterns carry the date they became known, so nothing newer than "today" leaks into a run.
# ---------------------------------------------------------------------------
TECH_PARAMS = {
    "sma": [5, 10, 20, 50, 100, 200], "ema": [12, 26], "macd": [12, 26, 9], "rsi": 14, "stochastic": [14, 3],
    "bollinger": [20, 2], "atr": 14, "adx": 14, "obv_sma": 20, "volume_sma": 20, "pivot_window": 5,
    "level_cluster_pct": 1.5, "level_lookback_days": 250, "volume_spike_ratio": 1.5, "divergence_lookback": 40,
}
TECH_TABLES = ("TECH_PRICE_DAILY", "TECH_PRICE_WEEKLY", "TECH_INDICATOR_DAILY", "TECH_TREND_DAILY", "TECH_PIVOT",
               "TECH_LEVEL", "TECH_SIGNAL", "TECH_PATTERN", "TECH_LOAD")
INDICATOR_COLUMNS = (
    "sma5", "sma10", "sma20", "sma50", "sma100", "sma200", "ema12", "ema26", "macd", "macd_signal", "macd_hist", "rsi14",
    "stoch_k", "stoch_d", "bb_upper", "bb_middle", "bb_lower", "bb_pct_b", "bb_bandwidth", "atr14", "obv", "obv_sma20",
    "volume_sma20", "volume_ratio", "roc10", "adx14", "plus_di14", "minus_di14", "high_52w", "low_52w",
    "pct_from_52w_high", "pct_from_52w_low", "ret_1w", "ret_1m", "ret_3m", "ret_6m", "ret_1y", "volatility_20d",
)
TREND_COLUMNS = ("primary_trend", "intermediate_trend", "short_trend", "trend_strength", "structure", "higher_highs",
                 "higher_lows", "lower_highs", "lower_lows", "sma200_slope_pct", "sma50_slope_pct", "price_vs_sma200_pct",
                 "price_vs_sma50_pct", "days_above_sma200")

ENSURE_TECH_SQL = """
IF OBJECT_ID('dbo.TECH_PRICE_DAILY', 'U') IS NULL
CREATE TABLE dbo.TECH_PRICE_DAILY (
    symbol NVARCHAR(20) NOT NULL, trade_date DATE NOT NULL,
    [open] DECIMAL(18,4) NULL, high DECIMAL(18,4) NULL, low DECIMAL(18,4) NULL, [close] DECIMAL(18,4) NULL,
    adj_close DECIMAL(18,4) NULL, volume BIGINT NULL,
    CONSTRAINT PK_TECH_PRICE_DAILY PRIMARY KEY (symbol, trade_date));
IF OBJECT_ID('dbo.TECH_PRICE_WEEKLY', 'U') IS NULL
CREATE TABLE dbo.TECH_PRICE_WEEKLY (
    symbol NVARCHAR(20) NOT NULL, week_end DATE NOT NULL,
    [open] DECIMAL(18,4) NULL, high DECIMAL(18,4) NULL, low DECIMAL(18,4) NULL, [close] DECIMAL(18,4) NULL, volume BIGINT NULL,
    CONSTRAINT PK_TECH_PRICE_WEEKLY PRIMARY KEY (symbol, week_end));
IF OBJECT_ID('dbo.TECH_INDICATOR_DAILY', 'U') IS NULL
CREATE TABLE dbo.TECH_INDICATOR_DAILY (
    symbol NVARCHAR(20) NOT NULL, trade_date DATE NOT NULL,
    sma5 DECIMAL(18,4) NULL, sma10 DECIMAL(18,4) NULL, sma20 DECIMAL(18,4) NULL, sma50 DECIMAL(18,4) NULL,
    sma100 DECIMAL(18,4) NULL, sma200 DECIMAL(18,4) NULL, ema12 DECIMAL(18,4) NULL, ema26 DECIMAL(18,4) NULL,
    macd DECIMAL(18,4) NULL, macd_signal DECIMAL(18,4) NULL, macd_hist DECIMAL(18,4) NULL, rsi14 DECIMAL(9,2) NULL,
    stoch_k DECIMAL(9,2) NULL, stoch_d DECIMAL(9,2) NULL, bb_upper DECIMAL(18,4) NULL, bb_middle DECIMAL(18,4) NULL,
    bb_lower DECIMAL(18,4) NULL, bb_pct_b DECIMAL(9,3) NULL, bb_bandwidth DECIMAL(9,2) NULL, atr14 DECIMAL(18,4) NULL,
    obv BIGINT NULL, obv_sma20 BIGINT NULL, volume_sma20 BIGINT NULL, volume_ratio DECIMAL(9,2) NULL, roc10 DECIMAL(9,2) NULL,
    adx14 DECIMAL(9,2) NULL, plus_di14 DECIMAL(9,2) NULL, minus_di14 DECIMAL(9,2) NULL, high_52w DECIMAL(18,4) NULL,
    low_52w DECIMAL(18,4) NULL, pct_from_52w_high DECIMAL(9,2) NULL, pct_from_52w_low DECIMAL(9,2) NULL,
    ret_1w DECIMAL(9,2) NULL, ret_1m DECIMAL(9,2) NULL, ret_3m DECIMAL(9,2) NULL, ret_6m DECIMAL(9,2) NULL, ret_1y DECIMAL(9,2) NULL,
    volatility_20d DECIMAL(9,2) NULL,
    CONSTRAINT PK_TECH_INDICATOR_DAILY PRIMARY KEY (symbol, trade_date));
IF OBJECT_ID('dbo.TECH_TREND_DAILY', 'U') IS NULL
CREATE TABLE dbo.TECH_TREND_DAILY (
    symbol NVARCHAR(20) NOT NULL, trade_date DATE NOT NULL,
    primary_trend NVARCHAR(10) NOT NULL, intermediate_trend NVARCHAR(10) NOT NULL, short_trend NVARCHAR(10) NOT NULL,
    trend_strength NVARCHAR(10) NOT NULL, structure NVARCHAR(120) NOT NULL,
    higher_highs INT NOT NULL, higher_lows INT NOT NULL, lower_highs INT NOT NULL, lower_lows INT NOT NULL,
    sma200_slope_pct DECIMAL(9,2) NULL, sma50_slope_pct DECIMAL(9,2) NULL, price_vs_sma200_pct DECIMAL(9,2) NULL,
    price_vs_sma50_pct DECIMAL(9,2) NULL, days_above_sma200 INT NULL,
    CONSTRAINT PK_TECH_TREND_DAILY PRIMARY KEY (symbol, trade_date));
IF OBJECT_ID('dbo.TECH_PIVOT', 'U') IS NULL
CREATE TABLE dbo.TECH_PIVOT (
    symbol NVARCHAR(20) NOT NULL, pivot_date DATE NOT NULL, pivot_type NVARCHAR(4) NOT NULL,
    price DECIMAL(18,4) NOT NULL, confirmed_date DATE NOT NULL, window INT NOT NULL,
    CONSTRAINT PK_TECH_PIVOT PRIMARY KEY (symbol, pivot_date, pivot_type));
IF OBJECT_ID('dbo.TECH_LEVEL', 'U') IS NULL
CREATE TABLE dbo.TECH_LEVEL (
    symbol NVARCHAR(20) NOT NULL, as_of_date DATE NOT NULL, level_type NVARCHAR(12) NOT NULL, rank INT NOT NULL,
    price DECIMAL(18,4) NOT NULL, touches INT NULL, last_touch_date DATE NULL, distance_pct DECIMAL(9,2) NOT NULL,
    CONSTRAINT PK_TECH_LEVEL PRIMARY KEY (symbol, as_of_date, level_type, rank));
IF OBJECT_ID('dbo.TECH_SIGNAL', 'U') IS NULL
CREATE TABLE dbo.TECH_SIGNAL (
    symbol NVARCHAR(20) NOT NULL, signal_date DATE NOT NULL, signal_type NVARCHAR(30) NOT NULL,
    direction NVARCHAR(8) NOT NULL, description NVARCHAR(300) NOT NULL, value DECIMAL(18,4) NULL,
    CONSTRAINT PK_TECH_SIGNAL PRIMARY KEY (symbol, signal_date, signal_type));
IF OBJECT_ID('dbo.TECH_PATTERN', 'U') IS NULL
CREATE TABLE dbo.TECH_PATTERN (
    symbol NVARCHAR(20) NOT NULL, pattern_type NVARCHAR(30) NOT NULL, direction NVARCHAR(8) NOT NULL,
    start_date DATE NOT NULL, end_date DATE NOT NULL, confirmed_date DATE NOT NULL,
    neckline DECIMAL(18,4) NOT NULL, target DECIMAL(18,4) NOT NULL, description NVARCHAR(400) NOT NULL,
    CONSTRAINT PK_TECH_PATTERN PRIMARY KEY (symbol, pattern_type, end_date));
IF OBJECT_ID('dbo.TECH_LOAD', 'U') IS NULL
CREATE TABLE dbo.TECH_LOAD (
    symbol NVARCHAR(20) NOT NULL PRIMARY KEY, computed_at DATETIME2 NOT NULL, first_trade_date DATE NOT NULL,
    last_trade_date DATE NOT NULL, price_rows INT NOT NULL, parameters NVARCHAR(MAX) NOT NULL, counts NVARCHAR(MAX) NOT NULL);
"""


def _f(v, nd=4):
    return num(v, nd)


def _i(v):
    try:
        return None if v is None or v != v else int(v)
    except (TypeError, ValueError):
        return None


def _label_trend(price, ma, slope, flat_pct=0.5):
    if ma is None or ma != ma or slope is None or slope != slope:
        return "unknown"
    if price > ma and slope > flat_pct / 4:
        return "up"
    if price < ma and slope < -flat_pct / 4:
        return "down"
    return "sideways"


def compute_technicals(df) -> dict:
    """All TECH_ table contents from a daily OHLCV frame (columns date, open, high, low, close, adj_close, volume).
    Every per-day value uses only data up to that day; forward-looking facts carry a confirmation date."""
    import numpy as np
    import pandas as pd

    P = TECH_PARAMS
    df = df.reset_index(drop=True)
    dates = df["date"].astype(str).tolist()
    close, high, low, vol = df["close"].astype(float), df["high"].astype(float), df["low"].astype(float), df["volume"].astype(float)
    n = len(df)
    ind = pd.DataFrame(index=df.index)

    for p in P["sma"]:
        ind[f"sma{p}"] = close.rolling(p).mean()
    ind["ema12"] = close.ewm(span=12, adjust=False).mean()
    ind["ema26"] = close.ewm(span=26, adjust=False).mean()
    macd = ind["ema12"] - ind["ema26"]
    macd.iloc[:25] = np.nan
    signal = macd.ewm(span=9, adjust=False).mean()
    signal.iloc[:33] = np.nan
    ind["macd"], ind["macd_signal"], ind["macd_hist"] = macd, signal, macd - signal
    ind["rsi14"] = compute_rsi(close, P["rsi"])
    low14, high14 = low.rolling(14).min(), high.rolling(14).max()
    ind["stoch_k"] = (close - low14) / (high14 - low14).replace(0, np.nan) * 100
    ind["stoch_d"] = ind["stoch_k"].rolling(3).mean()
    mid, std = close.rolling(20).mean(), close.rolling(20).std(ddof=0)
    ind["bb_upper"], ind["bb_middle"], ind["bb_lower"] = mid + 2 * std, mid, mid - 2 * std
    ind["bb_pct_b"] = (close - ind["bb_lower"]) / (ind["bb_upper"] - ind["bb_lower"]).replace(0, np.nan)
    ind["bb_bandwidth"] = (ind["bb_upper"] - ind["bb_lower"]) / mid.replace(0, np.nan) * 100
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    ind["atr14"] = atr
    direction = np.sign(close.diff()).fillna(0)
    ind["obv"] = (direction * vol.fillna(0)).cumsum()
    ind["obv_sma20"] = ind["obv"].rolling(20).mean()
    ind["volume_sma20"] = vol.rolling(20).mean()
    ind["volume_ratio"] = vol / ind["volume_sma20"].replace(0, np.nan)
    ind["roc10"] = close.pct_change(10) * 100
    up_move, down_move = high.diff(), -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    atr_w = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean().replace(0, np.nan)
    ind["plus_di14"] = 100 * plus_dm.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean() / atr_w
    ind["minus_di14"] = 100 * minus_dm.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean() / atr_w
    dx = 100 * (ind["plus_di14"] - ind["minus_di14"]).abs() / (ind["plus_di14"] + ind["minus_di14"]).replace(0, np.nan)
    ind["adx14"] = dx.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    ind["high_52w"] = high.rolling(252, min_periods=20).max()
    ind["low_52w"] = low.rolling(252, min_periods=20).min()
    ind["pct_from_52w_high"] = (close / ind["high_52w"] - 1) * 100
    ind["pct_from_52w_low"] = (close / ind["low_52w"] - 1) * 100
    for key, k in (("ret_1w", 5), ("ret_1m", 21), ("ret_3m", 63), ("ret_6m", 126), ("ret_1y", 252)):
        ind[key] = close.pct_change(k) * 100
    ind["volatility_20d"] = close.pct_change().rolling(20).std() * np.sqrt(252) * 100

    # Pivots (confirmed `window` bars later)
    w = P["pivot_window"]
    pivots = []  # dicts with idx, conf_idx
    hv, lv = high.values, low.values
    for i in range(w, n - w):
        if hv[i] == hv[i - w: i + w + 1].max():
            pivots.append({"idx": i, "type": "high", "price": float(hv[i]), "conf_idx": i + w})
        if lv[i] == lv[i - w: i + w + 1].min():
            pivots.append({"idx": i, "type": "low", "price": float(lv[i]), "conf_idx": i + w})
    pivots.sort(key=lambda p: (p["idx"], p["type"]))

    # Trend per day (structure uses pivots known by that day, last 60 bars)
    sma200_slope = ind["sma200"].pct_change(20) * 100
    sma50_slope = ind["sma50"].pct_change(10) * 100
    sma20_slope = ind["sma20"].pct_change(5) * 100
    trend_rows = []
    above_run = 0
    for i in range(n):
        c = float(close.iloc[i])
        s200 = ind["sma200"].iloc[i]
        if s200 == s200:
            above_run = above_run + 1 if (c > s200 and above_run >= 0) else (above_run - 1 if (c < s200 and above_run <= 0) else (1 if c > s200 else -1))
        else:
            above_run = 0
        known = [p for p in pivots if p["conf_idx"] <= i and p["idx"] >= i - 60]
        highs = [p["price"] for p in known if p["type"] == "high"]
        lows = [p["price"] for p in known if p["type"] == "low"]
        hh = sum(1 for a, b in zip(highs, highs[1:]) if b > a)
        lh = sum(1 for a, b in zip(highs, highs[1:]) if b < a)
        hl = sum(1 for a, b in zip(lows, lows[1:]) if b > a)
        ll = sum(1 for a, b in zip(lows, lows[1:]) if b < a)
        if hh + hl > lh + ll and hh + hl >= 2:
            structure = "higher highs and higher lows (uptrend structure)"
        elif lh + ll > hh + hl and lh + ll >= 2:
            structure = "lower highs and lower lows (downtrend structure)"
        elif known:
            structure = "mixed swings (no clear structure)"
        else:
            structure = "insufficient confirmed swings"
        adx = ind["adx14"].iloc[i]
        strength = "unknown" if adx != adx else "strong" if adx >= 40 else "moderate" if adx >= 25 else "weak"
        trend_rows.append({
            "trade_date": dates[i],
            "primary_trend": _label_trend(c, s200, sma200_slope.iloc[i]),
            "intermediate_trend": _label_trend(c, ind["sma50"].iloc[i], sma50_slope.iloc[i]),
            "short_trend": _label_trend(c, ind["sma20"].iloc[i], sma20_slope.iloc[i]),
            "trend_strength": strength, "structure": structure,
            "higher_highs": hh, "higher_lows": hl, "lower_highs": lh, "lower_lows": ll,
            "sma200_slope_pct": _f(sma200_slope.iloc[i], 2), "sma50_slope_pct": _f(sma50_slope.iloc[i], 2),
            "price_vs_sma200_pct": _f((c / s200 - 1) * 100, 2) if s200 == s200 else None,
            "price_vs_sma50_pct": _f((c / ind["sma50"].iloc[i] - 1) * 100, 2) if ind["sma50"].iloc[i] == ind["sma50"].iloc[i] else None,
            "days_above_sma200": above_run if s200 == s200 else None,
        })

    # Levels as of each day: clusters of confirmed pivots in the lookback, Fibonacci of the last major swing
    tol, lookback = P["level_cluster_pct"] / 100, P["level_lookback_days"]
    level_rows = []
    for i in range(n):
        c = float(close.iloc[i])
        known = [p for p in pivots if p["conf_idx"] <= i and p["idx"] >= i - lookback]
        if not known:
            continue
        clusters = []
        for p in sorted(known, key=lambda p: p["price"]):
            if clusters and p["price"] - clusters[-1]["prices"][-1] <= c * tol:
                clusters[-1]["prices"].append(p["price"])
                clusters[-1]["last_idx"] = max(clusters[-1]["last_idx"], p["idx"])
            else:
                clusters.append({"prices": [p["price"]], "last_idx": p["idx"]})
        levels = [{"price": sum(cl["prices"]) / len(cl["prices"]), "touches": len(cl["prices"]), "last_idx": cl["last_idx"]} for cl in clusters]
        support = sorted([l for l in levels if l["price"] < c], key=lambda l: (-l["touches"], c - l["price"]))[:3]
        resistance = sorted([l for l in levels if l["price"] > c], key=lambda l: (-l["touches"], l["price"] - c))[:3]
        for kind, lst in (("support", sorted(support, key=lambda l: -l["price"])), ("resistance", sorted(resistance, key=lambda l: l["price"]))):
            for rank, l in enumerate(lst, 1):
                level_rows.append({"as_of_date": dates[i], "level_type": kind, "rank": rank, "price": _f(l["price"]),
                                   "touches": l["touches"], "last_touch_date": dates[l["last_idx"]],
                                   "distance_pct": _f((l["price"] / c - 1) * 100, 2)})
        recent = [p for p in known if p["idx"] >= i - 120]
        if recent:
            hi = max((p for p in recent if p["type"] == "high"), key=lambda p: p["price"], default=None)
            lo = min((p for p in recent if p["type"] == "low"), key=lambda p: p["price"], default=None)
            if hi and lo and hi["price"] > lo["price"]:
                rng = hi["price"] - lo["price"]
                up_swing = hi["idx"] > lo["idx"]
                for key, r in (("fib_382", 0.382), ("fib_500", 0.5), ("fib_618", 0.618)):
                    price = hi["price"] - rng * r if up_swing else lo["price"] + rng * r
                    level_rows.append({"as_of_date": dates[i], "level_type": key, "rank": 1, "price": _f(price), "touches": None,
                                       "last_touch_date": None, "distance_pct": _f((price / c - 1) * 100, 2)})
                for key, p in (("swing_high", hi), ("swing_low", lo)):
                    level_rows.append({"as_of_date": dates[i], "level_type": key, "rank": 1, "price": _f(p["price"]), "touches": None,
                                       "last_touch_date": dates[p["idx"]], "distance_pct": _f((p["price"] / c - 1) * 100, 2)})

    # Signals (events on the day they occur)
    sig = []

    def cross(a, b, i):
        """+1 if a crossed above b on day i, -1 if below, else 0 (needs both days defined)."""
        pa, pb, ca, cb = a.iloc[i - 1], b.iloc[i - 1], a.iloc[i], b.iloc[i]
        if any(v != v for v in (pa, pb, ca, cb)):
            return 0
        return 1 if pa <= pb and ca > cb else -1 if pa >= pb and ca < cb else 0

    def level_cross(s, thr, i):
        p, c_ = s.iloc[i - 1], s.iloc[i]
        if p != p or c_ != c_:
            return 0
        return 1 if p <= thr < c_ else -1 if p >= thr > c_ else 0

    prior_high_252 = high.rolling(252, min_periods=60).max().shift(1)
    prior_low_252 = low.rolling(252, min_periods=60).min().shift(1)
    last_div = {"bullish": -100, "bearish": -100}
    L = P["divergence_lookback"]
    for i in range(1, n):
        d = dates[i]
        c = float(close.iloc[i])
        x = cross(ind["sma50"], ind["sma200"], i)
        if x == 1: sig.append((d, "golden_cross", "bullish", "50-day average crossed above the 200-day average", _f(ind["sma50"].iloc[i])))
        if x == -1: sig.append((d, "death_cross", "bearish", "50-day average crossed below the 200-day average", _f(ind["sma50"].iloc[i])))
        x = cross(ind["sma20"], ind["sma50"], i)
        if x == 1: sig.append((d, "sma20_50_cross_up", "bullish", "20-day average crossed above the 50-day average", _f(ind["sma20"].iloc[i])))
        if x == -1: sig.append((d, "sma20_50_cross_down", "bearish", "20-day average crossed below the 50-day average", _f(ind["sma20"].iloc[i])))
        x = cross(close, ind["sma200"], i)
        if x == 1: sig.append((d, "price_above_sma200", "bullish", "Close crossed above the 200-day average", _f(ind["sma200"].iloc[i])))
        if x == -1: sig.append((d, "price_below_sma200", "bearish", "Close crossed below the 200-day average", _f(ind["sma200"].iloc[i])))
        x = cross(ind["macd"], ind["macd_signal"], i)
        if x == 1: sig.append((d, "macd_cross_up", "bullish", "MACD line crossed above its signal line", _f(ind["macd"].iloc[i])))
        if x == -1: sig.append((d, "macd_cross_down", "bearish", "MACD line crossed below its signal line", _f(ind["macd"].iloc[i])))
        r = ind["rsi14"]
        if level_cross(r, 70, i) == 1: sig.append((d, "rsi_overbought", "bearish", "RSI rose above 70 (overbought)", _f(r.iloc[i], 2)))
        if level_cross(r, 70, i) == -1: sig.append((d, "rsi_exit_overbought", "bearish", "RSI fell back below 70", _f(r.iloc[i], 2)))
        if level_cross(r, 30, i) == -1: sig.append((d, "rsi_oversold", "bullish", "RSI fell below 30 (oversold)", _f(r.iloc[i], 2)))
        if level_cross(r, 30, i) == 1: sig.append((d, "rsi_exit_oversold", "bullish", "RSI rose back above 30", _f(r.iloc[i], 2)))
        k = ind["stoch_k"]
        if level_cross(k, 80, i) == 1: sig.append((d, "stoch_overbought", "bearish", "Stochastic %K rose above 80", _f(k.iloc[i], 2)))
        if level_cross(k, 20, i) == -1: sig.append((d, "stoch_oversold", "bullish", "Stochastic %K fell below 20", _f(k.iloc[i], 2)))
        if cross(close, ind["bb_upper"], i) == 1: sig.append((d, "bb_breakout_up", "bullish", "Close moved above the upper Bollinger Band", _f(ind["bb_upper"].iloc[i])))
        if cross(close, ind["bb_lower"], i) == -1: sig.append((d, "bb_breakout_down", "bearish", "Close moved below the lower Bollinger Band", _f(ind["bb_lower"].iloc[i])))
        ph, pl = prior_high_252.iloc[i], prior_low_252.iloc[i]
        if ph == ph and c > ph and float(close.iloc[i - 1]) <= ph:
            sig.append((d, "high_52w_breakout", "bullish", f"Close {c:.2f} above the prior 52-week high {ph:.2f}", _f(ph)))
        if pl == pl and c < pl and float(close.iloc[i - 1]) >= pl:
            sig.append((d, "low_52w_breakdown", "bearish", f"Close {c:.2f} below the prior 52-week low {pl:.2f}", _f(pl)))
        vr, pvr = ind["volume_ratio"].iloc[i], ind["volume_ratio"].iloc[i - 1]
        if vr == vr and vr >= P["volume_spike_ratio"] and not (pvr == pvr and pvr >= P["volume_spike_ratio"]):
            up = c >= float(close.iloc[i - 1])
            sig.append((d, "volume_spike", "bullish" if up else "bearish", f"Volume {vr:.1f}x its 20-day average on an {'up' if up else 'down'} day", _f(vr, 2)))
        if i >= L and r.iloc[i] == r.iloc[i]:
            win_c, win_r = close.iloc[i - L:i], r.iloc[i - L:i]
            if c >= win_c.max() and r.iloc[i] < win_r.max() - 5 and i - last_div["bearish"] > 10:
                sig.append((d, "bearish_divergence", "bearish", f"New {L}-day high in price while RSI ({r.iloc[i]:.0f}) is below its prior peak ({win_r.max():.0f})", _f(r.iloc[i], 2)))
                last_div["bearish"] = i
            if c <= win_c.min() and r.iloc[i] > win_r.min() + 5 and i - last_div["bullish"] > 10:
                sig.append((d, "bullish_divergence", "bullish", f"New {L}-day low in price while RSI ({r.iloc[i]:.0f}) is above its prior trough ({win_r.min():.0f})", _f(r.iloc[i], 2)))
                last_div["bullish"] = i
    signal_rows = [{"signal_date": a, "signal_type": b, "direction": c_, "description": d_, "value": v} for a, b, c_, d_, v in sig]

    # Patterns from confirmed pivots, confirmed by a close beyond the neckline
    patterns = []
    cv = close.values

    def first_close_beyond(start_idx, level, below, max_bars=60):
        for j in range(start_idx, min(n, start_idx + max_bars)):
            if (below and cv[j] < level) or (not below and cv[j] > level):
                return j
        return None

    highs = [p for p in pivots if p["type"] == "high"]
    lows = [p for p in pivots if p["type"] == "low"]
    for a, b in zip(highs, highs[1:]):
        if b["idx"] - a["idx"] < 15 or abs(b["price"] - a["price"]) / a["price"] > 0.03:
            continue
        between = [p for p in lows if a["idx"] < p["idx"] < b["idx"]]
        if not between:
            continue
        neck = min(p["price"] for p in between)
        j = first_close_beyond(max(b["conf_idx"], b["idx"] + 1), neck, below=True)
        if j is None:
            continue
        height = max(a["price"], b["price"]) - neck
        patterns.append({"pattern_type": "double_top", "direction": "bearish", "start_date": dates[a["idx"]], "end_date": dates[b["idx"]],
                         "confirmed_date": dates[j], "neckline": _f(neck), "target": _f(neck - height),
                         "description": f"Two peaks at {a['price']:.2f} and {b['price']:.2f}; neckline {neck:.2f} broken on a close of {cv[j]:.2f}"})
    for a, b in zip(lows, lows[1:]):
        if b["idx"] - a["idx"] < 15 or abs(b["price"] - a["price"]) / a["price"] > 0.03:
            continue
        between = [p for p in highs if a["idx"] < p["idx"] < b["idx"]]
        if not between:
            continue
        neck = max(p["price"] for p in between)
        j = first_close_beyond(max(b["conf_idx"], b["idx"] + 1), neck, below=False)
        if j is None:
            continue
        height = neck - min(a["price"], b["price"])
        patterns.append({"pattern_type": "double_bottom", "direction": "bullish", "start_date": dates[a["idx"]], "end_date": dates[b["idx"]],
                         "confirmed_date": dates[j], "neckline": _f(neck), "target": _f(neck + height),
                         "description": f"Two troughs at {a['price']:.2f} and {b['price']:.2f}; neckline {neck:.2f} broken on a close of {cv[j]:.2f}"})
    for s1, head, s2 in zip(highs, highs[1:], highs[2:]):
        if not (head["price"] > s1["price"] and head["price"] > s2["price"]) or abs(s1["price"] - s2["price"]) / s1["price"] > 0.10:
            continue
        l1 = [p for p in lows if s1["idx"] < p["idx"] < head["idx"]]
        l2 = [p for p in lows if head["idx"] < p["idx"] < s2["idx"]]
        if not l1 or not l2:
            continue
        neck = (min(p["price"] for p in l1) + min(p["price"] for p in l2)) / 2
        j = first_close_beyond(max(s2["conf_idx"], s2["idx"] + 1), neck, below=True)
        if j is None:
            continue
        height = head["price"] - neck
        patterns.append({"pattern_type": "head_and_shoulders", "direction": "bearish", "start_date": dates[s1["idx"]], "end_date": dates[s2["idx"]],
                         "confirmed_date": dates[j], "neckline": _f(neck), "target": _f(neck - height),
                         "description": f"Shoulders {s1['price']:.2f} / {s2['price']:.2f}, head {head['price']:.2f}; neckline {neck:.2f} broken on {dates[j]}"})
    for s1, head, s2 in zip(lows, lows[1:], lows[2:]):
        if not (head["price"] < s1["price"] and head["price"] < s2["price"]) or abs(s1["price"] - s2["price"]) / s1["price"] > 0.10:
            continue
        h1 = [p for p in highs if s1["idx"] < p["idx"] < head["idx"]]
        h2 = [p for p in highs if head["idx"] < p["idx"] < s2["idx"]]
        if not h1 or not h2:
            continue
        neck = (max(p["price"] for p in h1) + max(p["price"] for p in h2)) / 2
        j = first_close_beyond(max(s2["conf_idx"], s2["idx"] + 1), neck, below=False)
        if j is None:
            continue
        height = neck - head["price"]
        patterns.append({"pattern_type": "inverse_head_and_shoulders", "direction": "bullish", "start_date": dates[s1["idx"]], "end_date": dates[s2["idx"]],
                         "confirmed_date": dates[j], "neckline": _f(neck), "target": _f(neck + height),
                         "description": f"Shoulders {s1['price']:.2f} / {s2['price']:.2f}, head {head['price']:.2f}; neckline {neck:.2f} broken on {dates[j]}"})
    seen, pattern_rows = set(), []
    for p in patterns:
        key = (p["pattern_type"], p["end_date"])
        if key not in seen:
            seen.add(key)
            pattern_rows.append(p)

    # Weekly bars
    wk = df.copy()
    wk["dt"] = pd.to_datetime(wk["date"])
    wk = wk.set_index("dt").resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
    weekly_rows = [{"week_end": idx.date().isoformat(), "open": _f(r.open), "high": _f(r.high), "low": _f(r.low), "close": _f(r.close), "volume": _i(r.volume)}
                   for idx, r in wk.iterrows()]

    daily_rows = [{"trade_date": dates[i], "open": _f(df["open"].iloc[i]), "high": _f(hv[i]), "low": _f(lv[i]), "close": _f(cv[i]),
                   "adj_close": _f(df["adj_close"].iloc[i]) if "adj_close" in df else None, "volume": _i(vol.iloc[i])} for i in range(n)]
    indicator_rows = []
    for i in range(n):
        row = {"trade_date": dates[i]}
        for col in INDICATOR_COLUMNS:
            v = ind[col].iloc[i]
            row[col] = _i(v) if col in ("obv", "obv_sma20", "volume_sma20") else _f(v, 2 if col in ("rsi14", "stoch_k", "stoch_d", "bb_bandwidth", "volume_ratio", "roc10", "adx14", "plus_di14", "minus_di14", "pct_from_52w_high", "pct_from_52w_low", "ret_1w", "ret_1m", "ret_3m", "ret_6m", "ret_1y", "volatility_20d") else 3 if col == "bb_pct_b" else 4)
        indicator_rows.append(row)
    pivot_rows = [{"pivot_date": dates[p["idx"]], "pivot_type": p["type"], "price": _f(p["price"]), "confirmed_date": dates[p["conf_idx"]], "window": w}
                  for p in pivots if p["conf_idx"] < n]
    return {"daily": daily_rows, "weekly": weekly_rows, "indicators": indicator_rows, "trend": trend_rows, "pivots": pivot_rows,
            "levels": level_rows, "signals": signal_rows, "patterns": pattern_rows}


def bulk_insert(cur, table: str, columns: tuple, rows: list[dict]) -> int:
    if not rows:
        return 0
    chunk = max(1, 2000 // len(columns))
    cols = ", ".join(f"[{c}]" for c in columns)
    ph = "(" + ", ".join(["%s"] * len(columns)) + ")"
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        cur.execute(f"INSERT INTO dbo.{table} ({cols}) VALUES " + ", ".join([ph] * len(part)),
                    tuple(r.get(c) for r in part for c in columns))
    return len(rows)


def prepare_technicals(symbol: str, log: "RunLog | None" = None) -> dict:
    """Compute every TECH_ table for a symbol from stored prices (downloading them first only if missing/stale)."""
    import pandas as pd

    def say(msg, level="info"):
        if log:
            log.step(msg, level)

    prices = stored_prices(symbol)
    if prices:
        say(f"Prices: {len(prices)} stored daily rows ({prices[0]['trade_date']} to {prices[-1]['trade_date']}); not downloaded again")
    else:
        rows = fetch_history(symbol)
        save_prices(symbol, rows)
        prices = [r.model_dump(mode="json") for r in rows]
        say(f"Prices: {len(prices)} daily rows downloaded and stored ({prices[0]['trade_date']} to {prices[-1]['trade_date']})")
    df = pd.DataFrame(prices)
    df["date"] = df["trade_date"].astype(str)
    for c in ("open", "high", "low", "close", "adj_close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    t0 = time.monotonic()
    t = compute_technicals(df)
    say(f"Computed in {time.monotonic() - t0:.1f} s: {len(t['indicators'])} indicator rows, {len(t['trend'])} trend rows, "
        f"{len(t['weekly'])} weekly bars, {len(t['pivots'])} confirmed pivots, {len(t['levels'])} level rows, "
        f"{len(t['signals'])} signals, {len(t['patterns'])} patterns")

    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(ENSURE_TECH_SQL)
        for table in TECH_TABLES:
            cur.execute(f"DELETE FROM dbo.{table} WHERE symbol = %s", (symbol,))
        for r in t["daily"] + t["weekly"] + t["indicators"] + t["trend"] + t["pivots"] + t["levels"] + t["signals"] + t["patterns"]:
            r["symbol"] = symbol
        counts = {
            "TECH_PRICE_DAILY": bulk_insert(cur, "TECH_PRICE_DAILY", ("symbol", "trade_date", "open", "high", "low", "close", "adj_close", "volume"), t["daily"]),
            "TECH_PRICE_WEEKLY": bulk_insert(cur, "TECH_PRICE_WEEKLY", ("symbol", "week_end", "open", "high", "low", "close", "volume"), t["weekly"]),
            "TECH_INDICATOR_DAILY": bulk_insert(cur, "TECH_INDICATOR_DAILY", ("symbol", "trade_date") + INDICATOR_COLUMNS, t["indicators"]),
            "TECH_TREND_DAILY": bulk_insert(cur, "TECH_TREND_DAILY", ("symbol", "trade_date") + TREND_COLUMNS, t["trend"]),
            "TECH_PIVOT": bulk_insert(cur, "TECH_PIVOT", ("symbol", "pivot_date", "pivot_type", "price", "confirmed_date", "window"), t["pivots"]),
            "TECH_LEVEL": bulk_insert(cur, "TECH_LEVEL", ("symbol", "as_of_date", "level_type", "rank", "price", "touches", "last_touch_date", "distance_pct"), t["levels"]),
            "TECH_SIGNAL": bulk_insert(cur, "TECH_SIGNAL", ("symbol", "signal_date", "signal_type", "direction", "description", "value"), t["signals"]),
            "TECH_PATTERN": bulk_insert(cur, "TECH_PATTERN", ("symbol", "pattern_type", "direction", "start_date", "end_date", "confirmed_date", "neckline", "target", "description"), t["patterns"]),
        }
        computed_at = datetime.now(timezone.utc)
        cur.execute(
            "INSERT INTO dbo.TECH_LOAD (symbol, computed_at, first_trade_date, last_trade_date, price_rows, parameters, counts) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (symbol, computed_at, t["daily"][0]["trade_date"], t["daily"][-1]["trade_date"], len(t["daily"]), json.dumps(TECH_PARAMS), json.dumps(counts)),
        )
        conn.commit()
    finally:
        conn.close()
    say(f"Stored in {len(TECH_TABLES)} TECH_ tables: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return {"symbol": symbol, "computed_at": computed_at.isoformat(), "first_trade_date": t["daily"][0]["trade_date"],
            "last_trade_date": t["daily"][-1]["trade_date"], "price_rows": len(t["daily"]), "counts": counts, "parameters": TECH_PARAMS}


def tech_status(symbol: str) -> dict:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(ENSURE_TECH_SQL)
        conn.commit()
        cur.execute("SELECT computed_at, first_trade_date, last_trade_date, price_rows, counts FROM dbo.TECH_LOAD WHERE symbol = %s", (symbol,))
        row = cur.fetchone()
        latest_price = None
        if table_exists(cur):
            cur.execute(f"SELECT MAX(trade_date) FROM {TABLE} WHERE symbol = %s", (symbol,))
            latest_price = cur.fetchone()[0]
    finally:
        conn.close()
    if not row:
        return {"symbol": symbol, "available": False, "latest_price_date": latest_price.isoformat() if latest_price else None}
    computed_at, first, last, rows, counts = row
    stale = bool(latest_price and last < latest_price)
    return {"symbol": symbol, "available": True, "fresh": not stale, "computed_at": computed_at.isoformat(),
            "first_trade_date": first.isoformat(), "last_trade_date": last.isoformat(), "price_rows": rows,
            "latest_price_date": latest_price.isoformat() if latest_price else None, "counts": json.loads(counts or "{}"),
            "tables": list(TECH_TABLES)}


def ensure_technicals(symbol: str, log: "RunLog | None" = None) -> dict:
    st = tech_status(symbol)
    if st["available"] and st["fresh"]:
        if log:
            log.step(f"Technical data for {symbol} ready in TECH_ tables (computed {st['computed_at'][:19]} UTC, prices to {st['last_trade_date']}); nothing recomputed")
        return st
    if log:
        log.step(f"Technical data for {symbol} {'is stale (prices newer than ' + st['last_trade_date'] + ')' if st['available'] else 'not found'}; computing TECH_ tables")
    prepare_technicals(symbol, log)
    return tech_status(symbol)


def _rows(cur, sql, params=(), columns=None):
    cur.execute(sql, params)
    cols = columns or [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = {}
        for k, v in zip(cols, r):
            if hasattr(v, "isoformat"):
                v = v.isoformat()
            elif isinstance(v, Decimal):
                v = float(v)
            d[k] = v
        out.append(d)
    return out


def load_tech_bundle(symbol: str, as_of: date, skill: dict | None = None) -> tuple[dict, list[dict]]:
    """Point-in-time technical payload read from the TECH_ tables only (rows dated <= as_of; pivots and
    patterns only once confirmed). Returns (payload, references)."""
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT computed_at, first_trade_date, last_trade_date FROM dbo.TECH_LOAD WHERE symbol = %s", (symbol,))
        load = cur.fetchone()
        if not load:
            raise HTTPException(status_code=422, detail=f"No technical data for {symbol}. Click 'Prepare technical data' first.")
        daily = _rows(cur, "SELECT TOP 60 trade_date, [open], high, low, [close], volume FROM dbo.TECH_PRICE_DAILY "
                           "WHERE symbol = %s AND trade_date <= %s ORDER BY trade_date DESC", (symbol, as_of))[::-1]
        if not daily:
            raise HTTPException(status_code=422, detail=f"No technical data for {symbol} on or before {as_of}.")
        today = daily[-1]
        weekly = _rows(cur, "SELECT TOP 52 week_end, [open], high, low, [close], volume FROM dbo.TECH_PRICE_WEEKLY "
                            "WHERE symbol = %s AND week_end <= %s ORDER BY week_end DESC", (symbol, as_of))[::-1]
        ind = _rows(cur, f"SELECT TOP 1 trade_date, {', '.join(INDICATOR_COLUMNS)} FROM dbo.TECH_INDICATOR_DAILY "
                         f"WHERE symbol = %s AND trade_date <= %s ORDER BY trade_date DESC", (symbol, as_of))
        hist_cols = ("trade_date", "close", "sma20", "sma50", "sma200", "rsi14", "stoch_k", "macd", "macd_signal", "macd_hist", "obv", "volume_ratio", "adx14")
        hist = _rows(cur, "SELECT TOP 30 i.trade_date, p.[close], i.sma20, i.sma50, i.sma200, i.rsi14, i.stoch_k, i.macd, i.macd_signal, "
                          "i.macd_hist, i.obv, i.volume_ratio, i.adx14 FROM dbo.TECH_INDICATOR_DAILY i JOIN dbo.TECH_PRICE_DAILY p "
                          "ON p.symbol = i.symbol AND p.trade_date = i.trade_date WHERE i.symbol = %s AND i.trade_date <= %s "
                          "ORDER BY i.trade_date DESC", (symbol, as_of), hist_cols)[::-1]
        trend = _rows(cur, f"SELECT TOP 1 trade_date, {', '.join(TREND_COLUMNS)} FROM dbo.TECH_TREND_DAILY "
                           f"WHERE symbol = %s AND trade_date <= %s ORDER BY trade_date DESC", (symbol, as_of))
        pivots = _rows(cur, "SELECT TOP 20 pivot_date, pivot_type, price, confirmed_date FROM dbo.TECH_PIVOT "
                            "WHERE symbol = %s AND confirmed_date <= %s ORDER BY pivot_date DESC", (symbol, as_of))[::-1]
        levels = _rows(cur, "SELECT level_type, price, touches, last_touch_date, distance_pct, rank FROM dbo.TECH_LEVEL "
                            "WHERE symbol = %s AND as_of_date = (SELECT MAX(as_of_date) FROM dbo.TECH_LEVEL WHERE symbol = %s AND as_of_date <= %s) "
                            "ORDER BY level_type, rank", (symbol, symbol, as_of))
        signals = _rows(cur, "SELECT signal_date, signal_type, direction, description, value FROM dbo.TECH_SIGNAL "
                             "WHERE symbol = %s AND signal_date <= %s AND signal_date >= %s ORDER BY signal_date",
                        (symbol, as_of, as_of - timedelta(days=120)))
        patterns = _rows(cur, "SELECT pattern_type, direction, start_date, end_date, confirmed_date, neckline, target, description "
                              "FROM dbo.TECH_PATTERN WHERE symbol = %s AND confirmed_date <= %s AND confirmed_date >= %s ORDER BY confirmed_date",
                         (symbol, as_of, as_of - timedelta(days=250)))
        cur.execute("SELECT MIN(trade_date), COUNT(*) FROM dbo.TECH_PRICE_DAILY WHERE symbol = %s AND trade_date <= %s", (symbol, as_of))
        first, ndays = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM dbo.TECH_PIVOT WHERE symbol = %s AND confirmed_date <= %s", (symbol, as_of))
        npiv = cur.fetchone()[0]
        profile = {}
        cur.execute(f"SELECT summary_json, bundle_json FROM {SYMBOL_DATA_TABLE} WHERE symbol = %s", (symbol,)) if table_exists(cur) else None
        row = cur.fetchone() if cur.description else None
        if row and row[1]:
            prof = json.loads(row[1]).get("profile", {})
            profile = {k: prof[k] for k in ("longName", "sector", "industry", "currency") if k in prof}
    finally:
        conn.close()

    computed_at = load[0].isoformat()
    payload = {
        "symbol": symbol,
        "analysis_date": as_of.isoformat(),
        "profile": profile,
        "today": {k: today[k] for k in ("trade_date", "open", "high", "low", "close", "volume")},
        "daily_bars": daily,
        "weekly_bars": weekly,
        "indicators": ind[0] if ind else {"trade_date": today["trade_date"]},
        "indicator_history": hist,
        "trend": trend[0] if trend else {"trade_date": today["trade_date"], "primary_trend": "unknown", "intermediate_trend": "unknown",
                                        "short_trend": "unknown", "trend_strength": "unknown", "structure": "insufficient data",
                                        "higher_highs": 0, "higher_lows": 0, "lower_highs": 0, "lower_lows": 0},
        "pivots": pivots,
        "levels": levels,
        "signals": signals,
        "patterns": patterns,
        "data_coverage": {"first_trade_date": first.isoformat() if first else today["trade_date"], "trading_days_available": ndays,
                          "computed_at": computed_at, "weeks_available": len(weekly), "pivots_known": npiv, "signals_in_window": len(signals)},
        "source_note": (f"POINT-IN-TIME TECHNICAL DATA. Today is {as_of.isoformat()}; the latest bar is {today['trade_date']}. "
                        f"Every value comes from the TECH_ tables computed on {computed_at[:19]} UTC from stored daily prices; nothing was "
                        f"fetched or calculated for this run. Only rows dated on or before today are included; swing pivots and patterns "
                        f"appear only from their confirmation date. You have no knowledge of anything after today."),
    }
    references = [
        {"type": "table", "title": "TECH_PRICE_DAILY", "detail": f"{ndays} daily bars {payload['data_coverage']['first_trade_date']} to {today['trade_date']}; last 60 supplied", "used_for": "price action, current price, volume"},
        {"type": "table", "title": "TECH_PRICE_WEEKLY", "detail": f"{len(weekly)} completed weeks supplied (<= {as_of})", "used_for": "primary trend, weekly structure"},
        {"type": "table", "title": "TECH_INDICATOR_DAILY", "detail": f"row for {payload['indicators'].get('trade_date')} plus {len(hist)}-day history", "used_for": "moving averages, MACD, RSI, stochastics, Bollinger, ATR, OBV, ADX, 52-week range, returns"},
        {"type": "table", "title": "TECH_TREND_DAILY", "detail": f"row for {payload['trend'].get('trade_date')}", "used_for": "primary / intermediate / short trend, strength, swing structure"},
        {"type": "table", "title": "TECH_PIVOT", "detail": f"{npiv} swings confirmed by {as_of}; last {len(pivots)} supplied", "used_for": "swing highs/lows, structure"},
        {"type": "table", "title": "TECH_LEVEL", "detail": f"{len(levels)} levels as of the latest trading day <= {as_of}", "used_for": "support, resistance, Fibonacci, swing extremes"},
        {"type": "table", "title": "TECH_SIGNAL", "detail": f"{len(signals)} events in the 120 days to {as_of}", "used_for": "crossovers, zone entries, breakouts, volume spikes, divergences"},
        {"type": "table", "title": "TECH_PATTERN", "detail": f"{len(patterns)} patterns confirmed in the 250 days to {as_of}", "used_for": "reversal patterns and measured-move targets"},
        {"type": "table", "title": "TECH_LOAD", "detail": f"computed {computed_at[:19]} UTC from {load[1]} to {load[2]}", "used_for": "provenance"},
    ]
    return payload, references


class TechPrepareRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)


@app.get("/api/py/tech/{symbol}")
def tech_data_status(symbol: str):
    return tech_status(normalize_symbol(symbol))


@app.post("/api/py/tech/prepare")
def tech_prepare(req: TechPrepareRequest):
    symbol = normalize_symbol(req.symbol)
    log = RunLog("x", "y")
    result = prepare_technicals(symbol, log)
    return {**result, "log": log.entries}


# ---------------------------------------------------------------------------
# Decision report, HTML version: self-contained page rendered client-side from embedded JSON.
# For technical skills it carries an interactive candlestick chart (lightweight-charts) with the
# indicators, levels, signals, patterns and the verdict lines, plus an evidence panel linked to the chart.
# ---------------------------------------------------------------------------
LIGHTWEIGHT_CHARTS_CDN = "https://unpkg.com/lightweight-charts@5.0.8/dist/lightweight-charts.standalone.production.js"
CHART_BARS = 250


def load_chart_data(symbol: str, as_of: date) -> dict:
    """Bars, indicators, levels, pivots, signals and patterns for the report chart, all dated <= as_of.
    TECH_ tables when the symbol has them, otherwise plain bars from dbo.stock_prices."""
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sys.tables WHERE name = 'TECH_LOAD'")
        has_tech = cur.fetchone()[0] > 0
        if has_tech:
            cur.execute("SELECT computed_at FROM dbo.TECH_LOAD WHERE symbol = %s", (symbol,))
            has_tech = cur.fetchone() is not None
        if has_tech:
            bars = _rows(cur, f"SELECT TOP {CHART_BARS} p.trade_date, p.[open], p.high, p.low, p.[close], p.volume, i.sma20, i.sma50, i.sma200, "
                              "i.bb_upper, i.bb_lower, i.rsi14, i.macd, i.macd_signal, i.macd_hist, i.volume_sma20, i.adx14, i.obv "
                              "FROM dbo.TECH_PRICE_DAILY p LEFT JOIN dbo.TECH_INDICATOR_DAILY i ON i.symbol = p.symbol AND i.trade_date = p.trade_date "
                              "WHERE p.symbol = %s AND p.trade_date <= %s ORDER BY p.trade_date DESC", (symbol, as_of))[::-1]
            levels = _rows(cur, "SELECT level_type, price, touches, last_touch_date, distance_pct, rank FROM dbo.TECH_LEVEL "
                                "WHERE symbol = %s AND as_of_date = (SELECT MAX(as_of_date) FROM dbo.TECH_LEVEL WHERE symbol = %s AND as_of_date <= %s) "
                                "ORDER BY level_type, rank", (symbol, symbol, as_of))
            pivots = _rows(cur, "SELECT TOP 40 pivot_date, pivot_type, price, confirmed_date FROM dbo.TECH_PIVOT "
                                "WHERE symbol = %s AND confirmed_date <= %s ORDER BY pivot_date DESC", (symbol, as_of))[::-1]
            signals = _rows(cur, "SELECT signal_date, signal_type, direction, description, value FROM dbo.TECH_SIGNAL "
                                 "WHERE symbol = %s AND signal_date <= %s AND signal_date >= %s ORDER BY signal_date",
                            (symbol, as_of, as_of - timedelta(days=365)))
            patterns = _rows(cur, "SELECT pattern_type, direction, start_date, end_date, confirmed_date, neckline, target, description "
                                  "FROM dbo.TECH_PATTERN WHERE symbol = %s AND confirmed_date <= %s AND confirmed_date >= %s ORDER BY confirmed_date",
                             (symbol, as_of, as_of - timedelta(days=365)))
            trend = _rows(cur, f"SELECT TOP 1 trade_date, {', '.join(TREND_COLUMNS)} FROM dbo.TECH_TREND_DAILY "
                               f"WHERE symbol = %s AND trade_date <= %s ORDER BY trade_date DESC", (symbol, as_of))
            return {"source": "TECH_ tables", "as_of": as_of.isoformat(), "bars": bars, "levels": levels, "pivots": pivots,
                    "signals": signals, "patterns": patterns, "trend": trend[0] if trend else None, "technical": True}
        if not table_exists(cur):
            return {"source": "none", "as_of": as_of.isoformat(), "bars": [], "technical": False}
        bars = _rows(cur, f"SELECT TOP {CHART_BARS} trade_date, [open], high, low, [close], volume FROM {TABLE} "
                          "WHERE symbol = %s AND trade_date <= %s ORDER BY trade_date DESC", (symbol, as_of))[::-1]
        return {"source": "dbo.stock_prices", "as_of": as_of.isoformat(), "bars": bars, "levels": [], "pivots": [], "signals": [],
                "patterns": [], "trend": None, "technical": False}
    finally:
        conn.close()


REPORT_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f7f8fa;--surface:#fff;--text:#111827;--muted:#6b7280;--border:#e5e7eb;--accent:#2563eb;--up:#0ca30c;--down:#d03b3b;--hold:#c98500;--warn:#fab219}
@media (prefers-color-scheme:dark){:root{--bg:#0b0f17;--surface:#131a26;--text:#e5e7eb;--muted:#9ca3af;--border:#243042;--accent:#3b82f6}}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:20px}
h1{font-size:1.4rem;letter-spacing:-.01em}h2{font-size:1.05rem;margin:22px 0 8px}h3{font-size:.9rem;margin:12px 0 6px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.meta{color:var(--muted);font-size:.85rem;margin:4px 0 14px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:14px}
.verdict{border-left:6px solid var(--border)}.verdict.BUY{border-left-color:var(--up)}.verdict.SELL{border-left-color:var(--down)}.verdict.HOLD{border-left-color:var(--hold)}
.badge{display:inline-block;padding:6px 12px;border-radius:8px;color:#fff;font-weight:800;letter-spacing:.04em;background:var(--muted);vertical-align:middle;margin-right:10px}
.BUY .badge{background:var(--up)}.SELL .badge{background:var(--down)}.HOLD .badge{background:var(--hold)}
.headline{font-size:1.15rem;font-weight:600;display:inline}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:12px}
.stat{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:10px 12px}.stat b{display:block;font-size:1.15rem}.stat span{font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.stat.target{border-color:var(--up)}.stat.stop{border-color:var(--down)}.stat.buy{border-color:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:.88rem}th,td{padding:7px 10px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}th{color:var(--muted);font-size:.78rem;font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
p.prose{max-width:860px;margin-bottom:10px}
ul{padding-left:20px}li{margin:3px 0}
.pass{color:var(--up);font-weight:600}.fail{color:var(--down);font-weight:600}.unclear{color:var(--muted);font-weight:600}
.chart-wrap{position:relative}.chart{width:100%;height:760px}
.readout{display:flex;flex-wrap:wrap;gap:4px 14px;padding:8px 10px;border-bottom:1px solid var(--border);font-size:.8rem;color:var(--muted);min-height:34px}.readout b{color:var(--text)}
.toggles{display:flex;flex-wrap:wrap;gap:12px;padding:8px 10px;font-size:.82rem;border-bottom:1px solid var(--border)}.toggles label{display:inline-flex;align-items:center;gap:5px;cursor:pointer}
.key{display:inline-block;width:14px;height:3px;border-radius:2px}
.evidence{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px}
.ev{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:10px 12px;cursor:pointer;transition:border-color .2s,background .2s}
.ev:hover{border-color:var(--accent)}.ev.active{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 10%,var(--bg))}
.ev .t{font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}.ev .v{font-weight:600}.ev .s{font-size:.78rem;color:var(--muted);font-family:ui-monospace,Consolas,monospace;word-break:break-word}
.ev.bullish{border-left:4px solid var(--up)}.ev.bearish{border-left:4px solid var(--down)}.ev.neutral{border-left:4px solid var(--border)}
.hint{font-size:.8rem;color:var(--muted)}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:.72rem;font-weight:700;color:#fff;background:var(--muted)}.pill.bullish{background:var(--up)}.pill.bearish{background:var(--down)}
.warn{color:var(--hold)}
.foot{font-size:.78rem;color:var(--muted);margin-top:20px}
@media (max-width:720px){body{padding:12px}.chart{height:620px}}
</style>
</head>
<body>
<div id="app"><p class="hint">Rendering report…</p></div>
<script id="data" type="application/json">__DATA__</script>
<script src="__CDN__"></script>
<script>
(function(){
const D = JSON.parse(document.getElementById('data').textContent);
const A = D.analysis || {}, C = D.chart || {bars:[]}, M = D.meta || {};
const app = document.getElementById('app'); app.textContent = '';
const el = (tag, attrs, ...kids) => { const e = document.createElement(tag); for (const k in attrs||{}) { if (k==='class') e.className = attrs[k]; else if (k==='style') e.style.cssText = attrs[k]; else if (k.startsWith('on')) e.addEventListener(k.slice(2), attrs[k]); else e.setAttribute(k, attrs[k]); }
  for (const kid of kids.flat()) { if (kid===null||kid===undefined) continue; e.appendChild(typeof kid==='string' ? document.createTextNode(kid) : kid); } return e; };
const ccy = A.currency || '';
const money = v => (v===null||v===undefined) ? '—' : Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}) + (ccy ? ' ' + ccy : '');
const pct = v => (v===null||v===undefined) ? '—' : (v>0?'+':'') + Number(v).toFixed(1) + '%';
const paras = t => String(t||'').split(/\n{2,}/).filter(Boolean).map(p => el('p',{class:'prose'},p));
const label = k => String(k).replace(/_/g,' ').replace(/\bpct\b/g,'%').replace(/^\w/,c=>c.toUpperCase());
const show = v => v===null||v===undefined ? '—' : typeof v==='boolean' ? (v?'yes':'no') : typeof v==='number' ? v.toLocaleString(undefined,{maximumFractionDigits:2}) : Array.isArray(v) ? v.join(', ') : String(v);
const table = (cols, rows, numCols) => el('table',{}, el('thead',{}, el('tr',{}, cols.map(c => el('th',{class:(numCols||[]).includes(c)?'num':''}, label(c))))),
  el('tbody',{}, rows.map(r => el('tr',{}, cols.map(c => el('td',{class:(numCols||[]).includes(c)?'num':''}, show(r[c])))))));

// ---- header + verdict
const rec = A.recommendation || '—';
app.append(el('h1',{}, `${D.skill.name} analysis: ${D.symbol} as of ${D.as_of}`));
app.append(el('div',{class:'meta'}, `Skill ${D.skill.id} v${D.skill.version||'-'} · model ${M.model||''} · generated ${M.generated_at||''} UTC · point-in-time: nothing after ${D.as_of} was visible to the agent`));
const price = A.current_price, up = A.target_price && price ? (A.target_price/price-1)*100 : null, dn = A.stop_price && price ? (A.stop_price/price-1)*100 : null;
const v = el('div',{class:'card verdict '+rec});
v.append(el('div',{}, el('span',{class:'badge'}, rec), el('span',{class:'headline'}, A.headline||'')));
v.append(el('div',{class:'meta',style:'margin:6px 0 0'}, `Conviction ${A.conviction||'—'} · horizon ${A.time_horizon||'—'}` + (typeof A.margin_of_safety_pct==='number' ? ` · margin of safety ${pct(A.margin_of_safety_pct)}` : '')));
const g = el('div',{class:'grid'});
g.append(el('div',{class:'stat'}, el('span',{},'Price on run date'), el('b',{}, money(price))));
if (typeof A.intrinsic_value_base==='number') g.append(el('div',{class:'stat'}, el('span',{},'Intrinsic value (base)'), el('b',{}, money(A.intrinsic_value_base)), el('small',{class:'hint'}, `range ${money(A.intrinsic_value_low)} – ${money(A.intrinsic_value_high)}`)));
g.append(el('div',{class:'stat buy'}, el('span',{},'Buy below'), el('b',{}, money(A.buy_below_price))));
g.append(el('div',{class:'stat target'}, el('span',{},'Target'), el('b',{}, money(A.target_price)), up!==null ? el('small',{class:'hint'}, pct(up)+' from price') : null));
g.append(el('div',{class:'stat stop'}, el('span',{},'Stop'), el('b',{}, money(A.stop_price)), dn!==null ? el('small',{class:'hint'}, pct(dn)+' from price') : null));
v.append(g);
if (A.target_rationale || A.stop_rationale) v.append(el('div',{class:'hint',style:'margin-top:10px'}, A.target_rationale ? el('p',{}, el('b',{},'Target: '), A.target_rationale) : null, A.stop_rationale ? el('p',{}, el('b',{},'Stop: '), A.stop_rationale) : null));
if (M.warnings && M.warnings.length) v.append(el('ul',{class:'warn',style:'margin-top:8px;list-style:none;padding:0'}, M.warnings.map(w => el('li',{}, '⚠ ' + w))));
app.append(v);

// ---- evidence model (what the agent cited), linked to chart objects
const evidence = [];
const dateRe = /\d{4}-\d{2}-\d{2}/;
(A.key_levels||[]).forEach((l,i) => evidence.push({kind:'level', title: `${label(l.type||'level')} ${money(l.price)}`, sub: l.significance||'', source: l.source||'', price: l.price, tone: /support|fib|swing_low/i.test(l.type||'')?'bullish':/resist|swing_high/i.test(l.type||'')?'bearish':'neutral', id:'lvl'+i}));
(A.indicator_readings||[]).forEach((r,i) => evidence.push({kind:'indicator', title: `${r.indicator}: ${r.value}`, sub: r.reading + (r.implication ? ' — ' + r.implication : ''), source: r.source||'', tone: /bull|oversold/.test(r.reading||'')?'bullish':/bear|overbought|diverg/.test(r.reading||'')?'bearish':'neutral', date: (String(r.source||'').match(dateRe)||[])[0], pane: /rsi|stoch/i.test(r.indicator||'')?'rsi':/macd/i.test(r.indicator||'')?'macd':/volume|obv/i.test(r.indicator||'')?'volume':'price', id:'ind'+i}));
(A.patterns_identified||[]).forEach((p,i) => evidence.push({kind:'pattern', title: `${p.pattern} (${p.status})`, sub: (p.implication||'') + (p.target ? ` · target ${money(p.target)}` : ''), source: p.source||'', tone: /bottom|inverse|bull/i.test(p.pattern||'')?'bullish':/top|head|bear/i.test(p.pattern||'')?'bearish':'neutral', price: p.target, date: (String(p.source||'').match(dateRe)||[])[0], id:'pat'+i}));
(A.key_metrics||[]).forEach((m,i) => evidence.push({kind:'metric', title: `${m.name}: ${m.value}`, sub: m.assessment||'', source: m.source||'', tone:'neutral', date: (String(m.source||'').match(dateRe)||[])[0], id:'met'+i}));

// ---- chart
let chart = null, focusFns = {};
if (C.bars && C.bars.length && window.LightweightCharts) {
  const LW = window.LightweightCharts;
  const card = el('div',{class:'card',style:'padding:0;overflow:hidden'});
  app.append(el('h2',{}, 'Price chart' + (C.technical ? ' with technical indicators' : '')), card);
  const css = getComputedStyle(document.documentElement), cv = n => css.getPropertyValue(n).trim();
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  const COL = { up:'#0ca30c', down:'#d03b3b', upSoft:'rgba(12,163,12,.45)', downSoft:'rgba(208,59,59,.45)', ma20: dark?'#199e70':'#1baf7a', ma50: dark?'#d55181':'#e87ba4', ma200:'#008300', bb:'#898781', rsi: dark?'#9085e9':'#6250d6', macd: dark?'#3987e5':'#2a78d6', sig: dark?'#d95926':'#eb6834', accent: cv('--accent') };
  const readout = el('div',{class:'readout'}); const toggles = el('div',{class:'toggles'}); const wrap = el('div',{class:'chart'});
  card.append(toggles, readout, wrap);
  const bars = C.bars.map(b => ({time:b.trade_date, open:+b.open, high:+b.high, low:+b.low, close:+b.close, volume:b.volume==null?0:+b.volume, sma20:b.sma20, sma50:b.sma50, sma200:b.sma200, bb_upper:b.bb_upper, bb_lower:b.bb_lower, rsi14:b.rsi14, macd:b.macd, macd_signal:b.macd_signal, macd_hist:b.macd_hist, volume_sma20:b.volume_sma20}));
  const byDate = new Map(bars.map(b => [b.time, b]));
  const series = pts => pts.filter(p => p.value!==null && p.value!==undefined && !Number.isNaN(+p.value)).map(p => ({time:p.time, value:+p.value}));
  const hasInd = C.technical && bars.some(b => b.sma20!=null);
  const panes = hasInd ? 4 : 2;
  const ch = LW.createChart(wrap, { autoSize:true, layout:{ background:{type:LW.ColorType.Solid, color:cv('--surface')}, textColor:cv('--muted'), panes:{separatorColor:cv('--border'), separatorHoverColor:cv('--border'), enableResize:false} },
    grid:{ vertLines:{color:cv('--border')}, horzLines:{color:cv('--border')} }, crosshair:{mode:LW.CrosshairMode.Normal}, rightPriceScale:{borderColor:cv('--border')}, timeScale:{borderColor:cv('--border'), rightOffset:6} });
  chart = ch;
  const hidden = {priceLineVisible:false, lastValueVisible:false, crosshairMarkerVisible:false};
  const candles = ch.addSeries(LW.CandlestickSeries, {upColor:COL.up, downColor:COL.down, wickUpColor:COL.up, wickDownColor:COL.down, borderVisible:false}, 0);
  candles.setData(bars.map(b => ({time:b.time, open:b.open, high:b.high, low:b.low, close:b.close})));
  const tracked = [];
  const layers = {};
  const addLine = (name, key, color, pane, opts) => { const s = ch.addSeries(LW.LineSeries, Object.assign({color, lineWidth:2}, hidden, opts||{}), pane); s.setData(series(bars.map(b => ({time:b.time, value:b[key]})))); tracked.push({key:name, s, color, fmt:v=>v.toFixed(2)}); return s; };
  if (hasInd) {
    layers.ma = [addLine('SMA20','sma20',COL.ma20,0), addLine('SMA50','sma50',COL.ma50,0), addLine('SMA200','sma200',COL.ma200,0)];
    layers.bb = [addLine('BB upper','bb_upper',COL.bb,0,{lineWidth:1,lineStyle:LW.LineStyle.Dotted}), addLine('BB lower','bb_lower',COL.bb,0,{lineWidth:1,lineStyle:LW.LineStyle.Dotted})];
  }
  const vol = ch.addSeries(LW.HistogramSeries, {priceFormat:{type:'volume'}, priceLineVisible:false, lastValueVisible:false}, 1);
  vol.setData(bars.map(b => ({time:b.time, value:b.volume, color:b.close>=b.open?COL.upSoft:COL.downSoft})));
  tracked.push({key:'Volume', s:vol, color:cv('--muted'), fmt:v=>v>=1e6?(v/1e6).toFixed(2)+'M':v>=1e3?(v/1e3).toFixed(0)+'K':String(v)});
  if (hasInd) {
    addLine('Vol MA20','volume_sma20',cv('--muted'),1,{lineWidth:1});
    const rsi = addLine('RSI','rsi14',COL.rsi,2,{autoscaleInfoProvider:()=>({priceRange:{minValue:0,maxValue:100}})});
    [70,30].forEach(p => rsi.createPriceLine({price:p, color:cv('--muted'), lineWidth:1, lineStyle:LW.LineStyle.Dashed, axisLabelVisible:false, title:String(p)}));
    const hist = ch.addSeries(LW.HistogramSeries, {priceLineVisible:false, lastValueVisible:false}, 3);
    hist.setData(series(bars.map(b => ({time:b.time, value:b.macd_hist}))).map(p => ({time:p.time, value:p.value, color:p.value>=0?COL.upSoft:COL.downSoft})));
    tracked.push({key:'Hist', s:hist, color:cv('--muted'), fmt:v=>v.toFixed(3)});
    addLine('MACD','macd',COL.macd,3); addLine('Signal','macd_signal',COL.sig,3);
  }
  ch.panes().forEach((p,i) => p.setStretchFactor([3,.8,.9,1][i] || 1));

  // Verdict lines + agent key levels + TECH levels
  const priceLines = [];
  const pl = (price, color, title, style, width) => { if (price===null||price===undefined) return null; const l = candles.createPriceLine({price:+price, color, lineWidth:width||1, lineStyle:style, axisLabelVisible:true, title}); priceLines.push(l); return l; };
  layers.verdict = [pl(A.target_price, COL.up, 'Target', LW.LineStyle.Dashed, 2), pl(A.stop_price, COL.down, 'Stop', LW.LineStyle.Dashed, 2), pl(A.buy_below_price, COL.accent, 'Buy below', LW.LineStyle.Dotted, 1)].filter(Boolean);
  const levelLines = {};
  evidence.filter(e => e.kind==='level' && e.price!=null).forEach(e => { levelLines[e.id] = pl(e.price, e.tone==='bullish'?COL.up:e.tone==='bearish'?COL.down:cv('--muted'), e.title.split(' ')[0], LW.LineStyle.Solid, 1); });
  layers.agentLevels = Object.values(levelLines);
  layers.techLevels = (C.levels||[]).filter(l => ['support','resistance'].includes(l.level_type)).map(l => pl(l.price, l.level_type==='support'?'rgba(12,163,12,.45)':'rgba(208,59,59,.45)', `${l.level_type==='support'?'S':'R'}${l.rank} ×${l.touches||1}`, LW.LineStyle.LargeDashed, 1));
  layers.fib = (C.levels||[]).filter(l => l.level_type.startsWith('fib')).map(l => pl(l.price, 'rgba(201,133,0,.6)', l.level_type.replace('fib_','Fib ').replace('382','38.2%').replace('500','50%').replace('618','61.8%'), LW.LineStyle.Dotted, 1));
  // Patterns: neckline segment + target line
  layers.patterns = [];
  (C.patterns||[]).forEach(p => { const seg = ch.addSeries(LW.LineSeries, Object.assign({color:p.direction==='bearish'?COL.down:COL.up, lineWidth:2, lineStyle:LW.LineStyle.LargeDashed}, hidden), 0);
    seg.setData([{time:p.start_date, value:+p.neckline},{time:p.confirmed_date, value:+p.neckline}]); layers.patterns.push(seg); layers.patterns.push(pl(p.target, p.direction==='bearish'?COL.down:COL.up, `${p.pattern_type.replace(/_/g,' ')} target`, LW.LineStyle.SparseDotted, 1)); });
  // Signal markers
  const SHORT = {golden_cross:'GC', death_cross:'DC', sma20_50_cross_up:'20/50↑', sma20_50_cross_down:'20/50↓', price_above_sma200:'>200', price_below_sma200:'<200', macd_cross_up:'MACD↑', macd_cross_down:'MACD↓', rsi_overbought:'RSI>70', rsi_oversold:'RSI<30', rsi_exit_overbought:'RSI<70', rsi_exit_oversold:'RSI>30', stoch_overbought:'%K>80', stoch_oversold:'%K<20', bb_breakout_up:'BB↑', bb_breakout_down:'BB↓', high_52w_breakout:'52w high', low_52w_breakdown:'52w low', volume_spike:'Vol', bullish_divergence:'Bull div', bearish_divergence:'Bear div'};
  const sigByDate = new Map();
  (C.signals||[]).forEach(s => { if (!sigByDate.has(s.signal_date)) sigByDate.set(s.signal_date, []); sigByDate.get(s.signal_date).push(s); });
  const markers = (C.signals||[]).filter(s => byDate.has(s.signal_date)).map(s => ({time:s.signal_date, position:s.direction==='bearish'?'aboveBar':'belowBar', shape:s.direction==='bearish'?'arrowDown':s.direction==='bullish'?'arrowUp':'circle', color:s.direction==='bearish'?COL.down:s.direction==='bullish'?COL.up:cv('--muted'), text:SHORT[s.signal_type]||s.signal_type, size:0.8}));
  (C.patterns||[]).forEach(p => { if (byDate.has(p.confirmed_date)) markers.push({time:p.confirmed_date, position:p.direction==='bearish'?'aboveBar':'belowBar', shape:'square', color:p.direction==='bearish'?COL.down:COL.up, text:p.pattern_type.replace(/_/g,' '), size:1}); });
  const last = bars[bars.length-1]; markers.push({time:last.time, position:'aboveBar', shape:'circle', color:COL.accent, text:'today', size:1});
  markers.sort((a,b) => a.time<b.time?-1:a.time>b.time?1:0);
  const markerApi = LW.createSeriesMarkers(candles, markers);
  layers.signals = markerApi;

  // Toggles
  const applyVis = (items, on) => items.forEach(i => { if (!i) return; if (i.applyOptions) i.applyOptions({visible:on}); });
  const tg = (name, key, color, on) => { const cb = el('input',{type:'checkbox'}); cb.checked = on!==false; cb.addEventListener('change', () => { const items = layers[key]||[]; if (key==='signals') markerApi.setMarkers(cb.checked?markers:[]); else if (['verdict','agentLevels','techLevels','fib'].includes(key)) items.forEach(l => l && l.applyOptions({lineVisible:cb.checked, axisLabelVisible:cb.checked})); else applyVis(items, cb.checked); }); toggles.append(el('label',{}, cb, el('i',{class:'key',style:'background:'+color}), name)); };
  if (hasInd) { tg('SMA 20/50/200','ma',COL.ma50); tg('Bollinger','bb',COL.bb); }
  tg('Target / stop / buy-below','verdict',COL.up); if (layers.agentLevels.length) tg("Agent's key levels",'agentLevels',cv('--muted'));
  if (layers.techLevels.length) { tg('Support / resistance (TECH)','techLevels','rgba(208,59,59,.6)'); }
  if (layers.fib.length) tg('Fibonacci','fib','rgba(201,133,0,.8)', false);
  if (markers.length) tg('Signals & patterns','signals',COL.accent);
  if (layers.fib.length) layers.fib.forEach(l => l.applyOptions({lineVisible:false, axisLabelVisible:false}));

  // Hover readout
  const setReadout = (time, items) => { readout.textContent=''; readout.append(el('b',{}, time)); items.forEach(i => readout.append(el('span',{}, i.color ? el('i',{class:'key',style:'background:'+i.color+';margin-right:4px'}) : null, i.key+' ', el('b',{}, i.value)))); const sg = sigByDate.get(time); if (sg) sg.forEach(s => readout.append(el('span',{class:'pill '+s.direction}, (SHORT[s.signal_type]||s.signal_type)+': '+s.description))); };
  const readAt = time => { const b = byDate.get(time); if (!b) return; const items = [{key:'O',value:b.open.toFixed(2)},{key:'H',value:b.high.toFixed(2)},{key:'L',value:b.low.toFixed(2)},{key:'C',value:b.close.toFixed(2),color:b.close>=b.open?COL.up:COL.down}];
    tracked.forEach(t => { const d = t.s.data().find(x => x.time===time); if (d && d.value!==undefined) items.push({key:t.key, value:t.fmt(d.value), color:t.color}); }); setReadout(time, items); };
  ch.subscribeCrosshairMove(p => { if (p.time) readAt(String(p.time)); else readAt(last.time); });
  readAt(last.time);
  ch.timeScale().fitContent();

  // Evidence focus: jump to a date and flash the related line
  const flash = line => { if (!line) return; const o = {lineWidth:4}; line.applyOptions(o); setTimeout(() => line.applyOptions({lineWidth: layers.verdict.includes(line)?2:1}), 1600); };
  const goto = date => { if (!date) return; const idx = bars.findIndex(b => b.time>=date); if (idx<0) return; ch.timeScale().setVisibleLogicalRange({from:Math.max(0,idx-40), to:Math.min(bars.length+3, idx+25)}); readAt(bars[idx].time); };
  evidence.forEach(e => { focusFns[e.id] = () => { if (e.kind==='level') flash(levelLines[e.id]); if (e.date) goto(e.date); else if (e.kind==='pattern') { const p = (C.patterns||[]).find(x => e.title.toLowerCase().includes(x.pattern_type.replace(/_/g,' '))); if (p) goto(p.confirmed_date); } else if (e.kind==='level') goto(last.time); }; });
}

// ---- evidence panel
if (evidence.length) {
  app.append(el('h2',{}, 'Evidence behind the decision'), el('p',{class:'hint'}, chart ? 'Click an item to jump the chart to its date and highlight the related line or marker.' : 'Each item names the data it came from.'));
  const grid = el('div',{class:'evidence'});
  evidence.forEach(e => { const card = el('div',{class:'ev '+e.tone, id:e.id}, el('div',{class:'t'}, e.kind), el('div',{class:'v'}, e.title), e.sub ? el('div',{}, e.sub) : null, e.source ? el('div',{class:'s'}, e.source) : null);
    card.addEventListener('click', () => { document.querySelectorAll('.ev.active').forEach(x => x.classList.remove('active')); card.classList.add('active'); if (focusFns[e.id]) focusFns[e.id](); if (chart) document.querySelector('.chart-wrap, .chart')?.scrollIntoView({behavior:'smooth', block:'nearest'}); });
    grid.append(card); });
  app.append(el('div',{class:'card'}, grid));
}

// ---- narrative + structured blocks
app.append(el('h2',{}, 'Summary and justification'), ...paras(A.summary));
if (A.trend_assessment) { const t = A.trend_assessment; app.append(el('h2',{}, 'Trend assessment'), el('div',{class:'card'}, table(['item','value'], Object.entries(t).map(([k,v]) => ({item:label(k), value:v}))))); }
if (A.trade_plan) app.append(el('h2',{}, 'Trade plan'), el('div',{class:'card'}, table(['item','value'], Object.entries(A.trade_plan).map(([k,v]) => ({item:label(k), value:v})))));
if (A.balance_sheet) app.append(el('h2',{}, 'Balance sheet: '+(A.balance_sheet.grade||'')), el('div',{class:'card'}, table(['item','value'], Object.entries(A.balance_sheet).filter(([k])=>k!=='grade').map(([k,v]) => ({item:label(k), value:v})))));
if (A.stress_test && A.stress_test.length) app.append(el('h2',{}, 'Stress test'), el('div',{class:'card'}, table(['scenario','assumptions','earnings_impact_pct','price_impact_pct','survives_without_capital_raise'], A.stress_test, ['earnings_impact_pct','price_impact_pct'])));
if (A.checklist && A.checklist.length) { app.append(el('h2',{}, 'Checklist'), el('div',{class:'card'}, el('table',{}, el('thead',{}, el('tr',{}, el('th',{},'Criterion'), el('th',{},'Verdict'), el('th',{},'Evidence'))), el('tbody',{}, A.checklist.map(c => el('tr',{}, el('td',{}, c.criterion), el('td',{class:c.verdict}, (c.verdict==='pass'?'✓ ':c.verdict==='fail'?'✗ ':'? ')+c.verdict), el('td',{}, c.evidence))))))); }
(A.sections||[]).forEach(s => app.append(el('h2',{}, s.title), ...paras(s.content)));
if (A.valuation && Object.keys(A.valuation).length) app.append(el('h2',{}, 'Valuation inputs'), el('div',{class:'card'}, table(['item','value'], Object.entries(A.valuation).map(([k,v]) => ({item:label(k), value:v})))));
if (A.key_metrics && A.key_metrics.length) app.append(el('h2',{}, 'Key metrics'), el('div',{class:'card'}, table(['name','value','assessment','source'], A.key_metrics)));
[['Risks','risks'],['Assumptions (model judgment, not data)','assumptions'],['Data gaps','data_gaps']].forEach(([t,k]) => { if (A[k] && A[k].length) app.append(el('h2',{}, t), el('ul',{}, A[k].map(x => el('li',{}, x)))); });
if (D.references && D.references.length) app.append(el('h2',{}, 'Data and reports referenced'), el('div',{class:'card'}, table(['title','detail','used_for'], D.references)));
app.append(el('p',{class:'foot'}, `Model-generated analysis from stored data reconstructed as of ${D.as_of}` + (C.source ? ` (chart: ${C.source}, ${(C.bars||[]).length} bars up to ${D.as_of})` : '') + `. The model's training knowledge cannot be rewound to that date. Informational only; not investment advice. Verify before use.`));
})();
</script>
</body>
</html>
"""


def build_decision_html(skill: dict, symbol: str, as_of: str, analysis: dict, references: list[dict], meta: dict, chart: dict | None) -> str:
    import html as _html

    data = {"skill": {"id": skill["id"], "name": skill["name"], "version": skill.get("version", "")}, "symbol": symbol, "as_of": as_of,
            "analysis": analysis, "references": references, "meta": meta, "chart": chart or {"bars": [], "technical": False}}
    payload = json.dumps(data, default=str).replace("</", "<\\/")
    title = _html.escape(f"{skill['name']} analysis: {symbol} as of {as_of}")
    return REPORT_HTML_TEMPLATE.replace("__TITLE__", title).replace("__DATA__", payload).replace("__CDN__", LIGHTWEIGHT_CHARTS_CDN)


def upload_html_report(pathname: str, html_text: str) -> dict:
    from vercel.blob.sync import put_blob

    r = put_blob(pathname, html_text.encode("utf-8"), access=blob_access(), content_type="text/html; charset=utf-8", allow_overwrite=True)
    return {"pathname": r.pathname, "url": r.url}


@app.get("/api/py/agent/backtest/{backtest_id}/decision/{decision_id}/report.html")
def decision_report_html(backtest_id: int, decision_id: int):
    """The stored HTML report from Vercel Blob; regenerated from the stored output (and point-in-time chart data) if missing."""
    conn = db_connect()
    try:
        cur = conn.cursor()
        ensure_backtest_tables(cur)
        conn.commit()
        cur.execute(
            f"SELECT skill_id, symbol, as_of_date, output_json, references_json, warnings_json, model, finished_at, report_html_pathname "
            f"FROM {DECISIONS_TABLE} WHERE backtest_id = %s AND decision_id = %s",
            (backtest_id, decision_id),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Decision not found")
    skill_id, symbol, as_of, output_json, references_json, warnings_json, model, finished_at, pathname = row
    if not output_json:
        raise HTTPException(status_code=404, detail="This decision has no completed analysis.")
    html_text = None
    if pathname and blob_configured():
        try:
            html_text = download_report(pathname).decode("utf-8")
        except Exception:  # noqa: BLE001
            html_text = None
    if html_text is None:
        html_text = build_decision_html(
            get_skill(skill_id), symbol, as_of.isoformat(), json.loads(output_json), json.loads(references_json or "[]"),
            {"model": model, "generated_at": finished_at.isoformat() if finished_at else "", "warnings": json.loads(warnings_json or "[]")},
            load_chart_data(symbol, as_of),
        )
    return Response(content=html_text, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "private, max-age=300", "X-Frame-Options": "SAMEORIGIN"})


# ---------------------------------------------------------------------------
# S&P 500 universe loader: constituents table + a job that, per symbol, loads only the missing price
# days and recomputes the TECH_ tables only when they are older than the latest stored close.
# Driven in batches by the page (one request per batch) so it fits a serverless time limit.
# ---------------------------------------------------------------------------
UNIVERSE_TABLE = "dbo.SP500_CONSTITUENTS"
UJOB_TABLE = "dbo.UNIVERSE_LOAD_JOB"
UITEM_TABLE = "dbo.UNIVERSE_LOAD_ITEM"
SP500_SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "sp500_constituents.csv"

ENSURE_UNIVERSE_SQL = f"""
IF OBJECT_ID('{UNIVERSE_TABLE}', 'U') IS NULL
CREATE TABLE {UNIVERSE_TABLE} (
    symbol NVARCHAR(20) NOT NULL PRIMARY KEY, name NVARCHAR(200) NULL, sector NVARCHAR(100) NULL,
    sub_industry NVARCHAR(150) NULL, source NVARCHAR(100) NOT NULL, active BIT NOT NULL CONSTRAINT DF_sp500_active DEFAULT 1,
    updated_at DATETIME2 NOT NULL CONSTRAINT DF_sp500_updated DEFAULT SYSUTCDATETIME());
IF OBJECT_ID('{UJOB_TABLE}', 'U') IS NULL
CREATE TABLE {UJOB_TABLE} (
    job_id INT IDENTITY(1,1) PRIMARY KEY, created_at DATETIME2 NOT NULL CONSTRAINT DF_ujob_created DEFAULT SYSUTCDATETIME(),
    status NVARCHAR(10) NOT NULL, options NVARCHAR(MAX) NULL, log_json NVARCHAR(MAX) NULL);
IF OBJECT_ID('{UITEM_TABLE}', 'U') IS NULL
CREATE TABLE {UITEM_TABLE} (
    job_id INT NOT NULL, symbol NVARCHAR(20) NOT NULL, status NVARCHAR(10) NOT NULL,
    price_action NVARCHAR(10) NULL, price_rows_added INT NULL, price_last_date DATE NULL,
    tech_action NVARCHAR(10) NULL, tech_last_date DATE NULL, message NVARCHAR(600) NULL,
    started_at DATETIME2 NULL, finished_at DATETIME2 NULL, duration_ms INT NULL,
    CONSTRAINT PK_UNIVERSE_LOAD_ITEM PRIMARY KEY (job_id, symbol));
"""


def yahoo_symbol(raw: str) -> str:
    """Wikipedia/exchange tickers use dots for share classes (BRK.B); Yahoo uses dashes (BRK-B)."""
    return raw.strip().upper().replace(".", "-")


def seed_universe_if_empty(cur) -> int:
    """The constituents table is maintained in the database; when it is empty (first use) it is seeded from
    data/sp500_constituents.csv in the repository. Returns the number of rows inserted."""
    import csv

    cur.execute(f"SELECT COUNT(*) FROM {UNIVERSE_TABLE}")
    if cur.fetchone()[0] > 0 or not SP500_SEED_FILE.is_file():
        return 0
    with open(SP500_SEED_FILE, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("symbol")]
    for i in range(0, len(rows), 100):
        part = rows[i:i + 100]
        cur.execute(
            f"INSERT INTO {UNIVERSE_TABLE} (symbol, name, sector, sub_industry, source, active) VALUES "
            + ", ".join(["(%s, %s, %s, %s, 'repo:data/sp500_constituents.csv', 1)"] * len(part)),
            tuple(v for r in part for v in (yahoo_symbol(r["symbol"]), r.get("name"), r.get("sector"), r.get("sub_industry"))),
        )
    return len(rows)


def universe_state(cur) -> list[dict]:
    """Constituents joined with what is stored for each: price coverage and TECH freshness."""
    cur.execute(ENSURE_UNIVERSE_SQL)
    cur.execute(ENSURE_TECH_SQL)
    cur.execute(ENSURE_TABLE_SQL)
    seed_universe_if_empty(cur)
    cur.execute(
        f"SELECT u.symbol, u.name, u.sector, u.sub_industry, u.source, u.updated_at, "
        f"p.cnt, p.first_date, p.last_date, t.computed_at, t.last_trade_date, t.price_rows "
        f"FROM {UNIVERSE_TABLE} u "
        f"LEFT JOIN (SELECT symbol, COUNT(*) AS cnt, MIN(trade_date) AS first_date, MAX(trade_date) AS last_date FROM {TABLE} GROUP BY symbol) p ON p.symbol = u.symbol "
        f"LEFT JOIN dbo.TECH_LOAD t ON t.symbol = u.symbol "
        f"WHERE u.active = 1 ORDER BY u.symbol"
    )
    out = []
    for r in cur.fetchall():
        symbol, name, sector, sub, source, updated, cnt, first, last, computed, tech_last, tech_rows = r
        tech_fresh = bool(tech_last and last and tech_last >= last)
        out.append({"symbol": symbol, "name": name, "sector": sector, "sub_industry": sub,
                    "price_rows": cnt or 0, "price_first": first.isoformat() if first else None, "price_last": last.isoformat() if last else None,
                    "tech_computed_at": computed.isoformat() if computed else None, "tech_last": tech_last.isoformat() if tech_last else None,
                    "tech_fresh": tech_fresh,
                    "state": "ready" if (cnt and tech_fresh) else "stale" if cnt else "missing"})
    return out


def update_prices_delta(symbol: str) -> dict:
    """Load only the trading days after the last stored close (or two years when nothing is stored)."""
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(ENSURE_TABLE_SQL)
        cur.execute(f"SELECT COUNT(*), MAX(trade_date) FROM {TABLE} WHERE symbol = %s", (symbol,))
        count, last = cur.fetchone()
    finally:
        conn.close()
    if not count:
        rows = fetch_history(symbol)
        save_prices(symbol, rows)
        return {"action": "full", "added": len(rows), "last_date": rows[-1].trade_date.isoformat()}
    start = last + timedelta(days=1)
    if start > date.today():
        return {"action": "skipped", "added": 0, "last_date": last.isoformat()}
    try:
        rows = fetch_history(symbol, start=start)
    except HTTPException as e:
        if e.status_code == 404:  # nothing new yet (weekend / holiday)
            return {"action": "skipped", "added": 0, "last_date": last.isoformat()}
        raise
    rows = [r for r in rows if r.trade_date > last]
    if not rows:
        return {"action": "skipped", "added": 0, "last_date": last.isoformat()}
    save_prices(symbol, rows)
    return {"action": "delta", "added": len(rows), "last_date": rows[-1].trade_date.isoformat()}


def _ujob_counts(cur, job_id: int) -> dict:
    cur.execute(f"SELECT status, COUNT(*) FROM {UITEM_TABLE} WHERE job_id = %s GROUP BY status", (job_id,))
    counts = {"pending": 0, "running": 0, "done": 0, "skipped": 0, "failed": 0, "cancelled": 0}
    for s, n in cur.fetchall():
        counts[s] = n
    counts["total"] = sum(counts.values())
    return counts


def _ujob(cur, job_id: int) -> dict:
    cur.execute(f"SELECT job_id, created_at, status, options, log_json FROM {UJOB_TABLE} WHERE job_id = %s", (job_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Load job not found")
    cur.execute(
        f"SELECT symbol, status, price_action, price_rows_added, price_last_date, tech_action, tech_last_date, message, "
        f"started_at, finished_at, duration_ms FROM {UITEM_TABLE} WHERE job_id = %s ORDER BY symbol", (job_id,))
    items = []
    for r in cur.fetchall():
        items.append({"symbol": r[0], "status": r[1], "price_action": r[2], "price_rows_added": r[3],
                      "price_last_date": r[4].isoformat() if r[4] else None, "tech_action": r[5],
                      "tech_last_date": r[6].isoformat() if r[6] else None, "message": r[7],
                      "started_at": r[8].isoformat() if r[8] else None, "finished_at": r[9].isoformat() if r[9] else None,
                      "duration_ms": r[10]})
    return {"job_id": row[0], "created_at": row[1].isoformat(), "status": row[2], "options": json.loads(row[3] or "{}"),
            "log": json.loads(row[4] or "[]"), "counts": _ujob_counts(cur, job_id), "items": items}


class UniverseLoadRequest(BaseModel):
    only_stale: bool = Field(True, description="Skip symbols whose prices and technical data are already current")
    batch_size: int = Field(5, ge=1, le=20)
    symbols: list[str] | None = Field(None, description="Optional subset; default is every active constituent")


@app.get("/api/py/universe")
def universe_list():
    conn = db_connect()
    try:
        cur = conn.cursor()
        items = universe_state(cur)
        conn.commit()
        cur.execute(f"SELECT MAX(updated_at), MIN(source) FROM {UNIVERSE_TABLE} WHERE active = 1")
        upd, source = cur.fetchone()
        cur.execute(f"SELECT TOP 1 job_id, status, created_at FROM {UJOB_TABLE} ORDER BY job_id DESC")
        last = cur.fetchone()
    finally:
        conn.close()
    summary = {"total": len(items), "ready": sum(i["state"] == "ready" for i in items), "stale": sum(i["state"] == "stale" for i in items),
               "missing": sum(i["state"] == "missing" for i in items)}
    return {"items": items, "summary": summary, "list_updated_at": upd.isoformat() if upd else None, "list_source": source,
            "latest_job": {"job_id": last[0], "status": last[1], "created_at": last[2].isoformat()} if last else None}


@app.post("/api/py/universe/load")
def universe_load_create(req: UniverseLoadRequest):
    conn = db_connect()
    try:
        cur = conn.cursor()
        state = universe_state(cur)
        if req.symbols:
            wanted = {normalize_symbol(s) for s in req.symbols}
            state = [s for s in state if s["symbol"] in wanted]
        if not state:
            raise HTTPException(status_code=422, detail="No constituents to load. Refresh or import the S&P 500 list first.")
        todo = [s for s in state if not (req.only_stale and s["state"] == "ready")]
        skipped = [s for s in state if req.only_stale and s["state"] == "ready"]
        log = RunLog(UJOB_TABLE, "job_id")
        log.add(f"Universe: {len(state)} active constituents; {len(todo)} need work ({sum(s['state'] == 'missing' for s in todo)} missing, "
                f"{sum(s['state'] == 'stale' for s in todo)} stale); {len(skipped)} already current")
        log.add(f"Database: {describe_db()} · batch size {req.batch_size} per request · Yahoo Finance for price deltas")
        cur.execute(f"INSERT INTO {UJOB_TABLE} (status, options, log_json) OUTPUT INSERTED.job_id VALUES ('active', %s, %s)",
                    (json.dumps(req.model_dump()), json.dumps(log.entries)))
        job_id = cur.fetchone()[0]
        rows = [(job_id, s["symbol"], "pending") for s in todo] + [(job_id, s["symbol"], "skipped") for s in skipped]
        for i in range(0, len(rows), 200):
            part = rows[i:i + 200]
            cur.execute(f"INSERT INTO {UITEM_TABLE} (job_id, symbol, status) VALUES " + ", ".join(["(%s, %s, %s)"] * len(part)),
                        tuple(v for r in part for v in r))
        if skipped:
            cur.execute(f"UPDATE {UITEM_TABLE} SET message = 'already current', price_action = 'skipped', tech_action = 'skipped' "
                        f"WHERE job_id = %s AND status = 'skipped'", (job_id,))
        conn.commit()
        return _ujob(cur, job_id)
    finally:
        conn.close()


@app.get("/api/py/universe/load/latest")
def universe_load_latest():
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(ENSURE_UNIVERSE_SQL)
        conn.commit()
        cur.execute(f"SELECT TOP 1 job_id FROM {UJOB_TABLE} ORDER BY job_id DESC")
        row = cur.fetchone()
        return _ujob(cur, row[0]) if row else None
    finally:
        conn.close()


@app.get("/api/py/universe/load/{job_id}")
def universe_load_get(job_id: int):
    conn = db_connect()
    try:
        return _ujob(conn.cursor(), job_id)
    finally:
        conn.close()


@app.post("/api/py/universe/load/{job_id}/stop")
def universe_load_stop(job_id: int):
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE {UJOB_TABLE} SET status = 'stopped' WHERE job_id = %s", (job_id,))
        cur.execute(f"UPDATE {UITEM_TABLE} SET status = 'cancelled', message = 'Stopped by user' WHERE job_id = %s AND status = 'pending'", (job_id,))
        conn.commit()
        return _ujob(cur, job_id)
    finally:
        conn.close()


@app.post("/api/py/universe/load/{job_id}/resume")
def universe_load_resume(job_id: int):
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE {UJOB_TABLE} SET status = 'active' WHERE job_id = %s", (job_id,))
        cur.execute(f"UPDATE {UITEM_TABLE} SET status = 'pending', message = NULL WHERE job_id = %s AND status IN ('cancelled', 'failed')", (job_id,))
        conn.commit()
        return _ujob(cur, job_id)
    finally:
        conn.close()


@app.post("/api/py/universe/load/{job_id}/step")
def universe_load_step(job_id: int):
    """Process the next batch of pending symbols: price delta, then TECH recompute if stale. One request per batch."""
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT status, options, log_json FROM {UJOB_TABLE} WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Load job not found")
        if row[0] == "stopped":
            return {"done": True, "stopped": True, "processed": [], "counts": _ujob_counts(cur, job_id), "log": []}
        batch_size = int(json.loads(row[1] or "{}").get("batch_size", 5))
        cur.execute(f"SELECT TOP {batch_size} symbol FROM {UITEM_TABLE} WHERE job_id = %s AND status = 'pending' ORDER BY symbol", (job_id,))
        symbols = [r[0] for r in cur.fetchall()]
        if not symbols:
            cur.execute(f"UPDATE {UJOB_TABLE} SET status = 'done' WHERE job_id = %s AND status = 'active'", (job_id,))
            conn.commit()
            return {"done": True, "processed": [], "counts": _ujob_counts(cur, job_id), "log": []}
        cur.execute(f"UPDATE {UITEM_TABLE} SET status = 'running', started_at = SYSUTCDATETIME() WHERE job_id = %s AND symbol IN ({', '.join('%s' for _ in symbols)})",
                    (job_id, *symbols))
        conn.commit()
    finally:
        conn.close()

    processed = []
    batch_log = RunLog("x", "y")
    t_batch = time.monotonic()
    for symbol in symbols:
        t0 = time.monotonic()
        item = {"symbol": symbol, "status": "done", "price_action": None, "price_rows_added": 0, "price_last_date": None,
                "tech_action": None, "tech_last_date": None, "message": None}
        try:
            p = update_prices_delta(symbol)
            item.update({"price_action": p["action"], "price_rows_added": p["added"], "price_last_date": p["last_date"]})
            st = tech_status(symbol)
            if st["available"] and st["fresh"]:
                item.update({"tech_action": "skipped", "tech_last_date": st["last_trade_date"]})
            else:
                res = prepare_technicals(symbol)
                item.update({"tech_action": "computed", "tech_last_date": res["last_trade_date"]})
            item["message"] = (f"prices {p['action']}" + (f" +{p['added']} rows" if p["added"] else "") + f" to {p['last_date']}; "
                               f"technical data {item['tech_action']} (to {item['tech_last_date']})")
            batch_log.add(f"{symbol}: {item['message']} - {time.monotonic() - t0:.1f} s")
        except HTTPException as e:
            item.update({"status": "failed", "message": str(e.detail)[:600]})
            batch_log.add(f"{symbol}: FAILED - {item['message']}", "error")
        except Exception as e:  # noqa: BLE001
            item.update({"status": "failed", "message": f"{type(e).__name__}: {str(e)[:500]}"})
            batch_log.add(f"{symbol}: FAILED - {item['message']}", "error")
        item["duration_ms"] = round((time.monotonic() - t0) * 1000)
        processed.append(item)

    conn = db_connect()
    try:
        cur = conn.cursor()
        for it in processed:
            cur.execute(
                f"UPDATE {UITEM_TABLE} SET status = %s, price_action = %s, price_rows_added = %s, price_last_date = %s, tech_action = %s, "
                f"tech_last_date = %s, message = %s, finished_at = SYSUTCDATETIME(), duration_ms = %s WHERE job_id = %s AND symbol = %s",
                (it["status"], it["price_action"], it["price_rows_added"], it["price_last_date"], it["tech_action"], it["tech_last_date"],
                 it["message"], it["duration_ms"], job_id, it["symbol"]),
            )
        cur.execute(f"SELECT log_json FROM {UJOB_TABLE} WHERE job_id = %s", (job_id,))
        existing = json.loads(cur.fetchone()[0] or "[]")
        batch_log.add(f"Batch of {len(processed)} done in {time.monotonic() - t_batch:.1f} s")
        cur.execute(f"UPDATE {UJOB_TABLE} SET log_json = %s WHERE job_id = %s", (json.dumps((existing + batch_log.entries)[-400:]), job_id))
        counts = _ujob_counts(cur, job_id)
        if counts["pending"] == 0 and counts["running"] == 0:
            cur.execute(f"UPDATE {UJOB_TABLE} SET status = 'done' WHERE job_id = %s AND status = 'active'", (job_id,))
        conn.commit()
        cur.execute(f"SELECT status FROM {UJOB_TABLE} WHERE job_id = %s", (job_id,))
        stopped = cur.fetchone()[0] == "stopped"
    finally:
        conn.close()
    return {"done": stopped or (counts["pending"] == 0 and counts["running"] == 0), "stopped": stopped, "processed": processed,
            "counts": counts, "log": batch_log.entries}


SECRET_VARS = {"AZURE_SQL_PASSWORD", "AUTH_PASSWORD", "AUTH_SECRET", "ANTHROPIC_API_KEY", "ANTHROPIC_FOUNDRY_API_KEY",
               "BLOB_READ_WRITE_TOKEN"}
CONFIG_VARS = (
    "AZURE_SQL_SERVER", "AZURE_SQL_DATABASE", "AZURE_SQL_USER", "AZURE_SQL_PASSWORD",
    "AUTH_USERNAME", "AUTH_PASSWORD", "AUTH_SECRET",
    "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
    "BLOB_READ_WRITE_TOKEN", "BLOB_ACCESS",
)


@app.get("/api/py/diagnosis")
def diagnosis():
    import sys

    variables = []
    for name in CONFIG_VARS:
        value = os.environ.get(name)
        if not value:
            shown = None
        elif name in SECRET_VARS:
            shown = f"set ({len(value)} characters, hidden)"  # never return secrets
        else:
            shown = value
        variables.append({"name": name, "set": bool(value), "value": shown})

    return {
        "variables": variables,
        "claude_provider": claude_provider() or "not configured",
        "claude_model": os.environ.get("ANTHROPIC_MODEL", DEFAULT_CLAUDE_MODEL),
        "env_local_present": os.path.isfile(".env.local"),
        "working_directory": os.getcwd(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "vercel": bool(os.environ.get("VERCEL")),
    }


@app.post("/api/py/prices/load", response_model=LoadResponse)
def load_prices(req: LoadRequest):
    symbol = normalize_symbol(req.symbol)
    rows = fetch_history(symbol)
    save_prices(symbol, rows)
    return LoadResponse(
        symbol=symbol,
        rows_saved=len(rows),
        start_date=rows[0].trade_date,
        end_date=rows[-1].trade_date,
        table=TABLE,
        preview=list(reversed(rows[-10:])),
    )

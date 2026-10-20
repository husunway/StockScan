import os
from datetime import date
from decimal import Decimal

import base64
import hashlib
import hmac
import json
import re
import time
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
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


def fetch_history(symbol: str) -> list[PriceRow]:
    import yfinance as yf

    df = yf.Ticker(symbol).history(
        period=HISTORY_PERIOD, interval="1d", auto_adjust=False, actions=False
    )
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
    return {
        "id": skill_id,
        "name": meta.get("name", skill_id),
        "description": meta.get("description", ""),
        "version": meta.get("version", ""),
        "inputs": meta.get("inputs", "symbol"),
        "output": meta.get("output", "stock-verdict"),
        "path": f"skills/{skill_id}/SKILL.md",
        "instructions": body.strip(),
        "content": text,
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


def gather_fundamentals(symbol: str) -> dict:
    """Fundamentals and annual statements from Yahoo Finance (via yfinance). Missing data is omitted, never guessed."""
    import yfinance as yf

    t = yf.Ticker(symbol)
    try:
        info = t.info or {}
    except Exception:  # noqa: BLE001
        info = {}
    fundamentals = {k: info[k] for k in INFO_FIELDS if info.get(k) is not None}
    statements = {}
    for key, attr in (("income", "income_stmt"), ("balance", "balance_sheet"), ("cashflow", "cashflow")):
        try:
            statements[key] = _statement_rows(getattr(t, attr), STATEMENT_ROWS[key])
        except Exception:  # noqa: BLE001
            statements[key] = {}
    return {"fundamentals": fundamentals, "annual_statements": statements}


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
    return warnings


def gather_stock_data(symbol: str) -> dict:
    from datetime import date as _date

    df, price_source = load_price_history_any(symbol)
    data = {
        "symbol": symbol,
        "analysis_date": _date.today().isoformat(),
        "price": {**price_snapshot(df), "source": price_source},
        **gather_fundamentals(symbol),
        "source_note": "fundamentals and annual_statements: Yahoo Finance via yfinance; values as reported by Yahoo, "
                       "statement figures in the company's financial currency.",
    }
    if not data["fundamentals"]:
        raise HTTPException(status_code=404, detail=f"Yahoo Finance returned no fundamentals for {symbol}.")
    return data


def run_skill_with_claude(skill: dict, data: dict):
    """One structured-output call: skill instructions as the (cached) system prompt, gathered data as the user turn."""
    import anthropic

    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_CLAUDE_MODEL)
    client = anthropic.Anthropic(timeout=240.0, max_retries=1)
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{"type": "text", "text": skill["instructions"], "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": STOCK_VERDICT_SCHEMA}},
            messages=[{
                "role": "user",
                "content": f"Apply the skill to {data['symbol']} using only the data below and return the JSON.\n\n"
                           + json.dumps(data, default=str, sort_keys=True),
            }],
        )
    except anthropic.AuthenticationError as e:
        raise HTTPException(status_code=502, detail="Anthropic API key was rejected.") from e
    except anthropic.RateLimitError as e:
        raise HTTPException(status_code=503, detail="Anthropic API rate limit reached; try again shortly.") from e
    except anthropic.APIStatusError as e:
        raise HTTPException(status_code=502, detail=f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise HTTPException(status_code=502, detail="Could not reach the Anthropic API.") from e

    if response.stop_reason == "refusal":
        why = getattr(response.stop_details, "explanation", None) or "The model declined to analyze this request."
        raise HTTPException(status_code=502, detail=why)
    if response.stop_reason == "max_tokens":
        raise HTTPException(status_code=502, detail="The analysis was cut off (max_tokens); please retry.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text), response
    except ValueError as e:
        raise HTTPException(status_code=502, detail="The model did not return valid JSON.") from e


@app.get("/api/py/agent/skills")
def agent_skills():
    keys = ("id", "name", "description", "version", "inputs", "output", "path")
    return {"skills": [{k: s[k] for k in keys} for s in list_skills()], "directory": str(SKILLS_DIR)}


@app.get("/api/py/agent/skills/{skill_id}")
def agent_skill(skill_id: str):
    return get_skill(skill_id)


@app.post("/api/py/agent/run")
def agent_run(req: AgentRunRequest):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured.")
    skill = get_skill(req.skill.strip().lower())
    symbol = normalize_symbol(req.symbol)
    data = gather_stock_data(symbol)
    analysis, response = run_skill_with_claude(skill, data)
    usage = response.usage
    return {
        "skill": {k: skill[k] for k in ("id", "name", "version", "path")},
        "symbol": symbol,
        "analysis": analysis,
        "warnings": validate_verdict(analysis),
        "data_used": data,
        "model": response.model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
        },
    }


SECRET_VARS = {"AZURE_SQL_PASSWORD", "AUTH_PASSWORD", "AUTH_SECRET", "ANTHROPIC_API_KEY"}
CONFIG_VARS = (
    "AZURE_SQL_SERVER", "AZURE_SQL_DATABASE", "AZURE_SQL_USER", "AZURE_SQL_PASSWORD",
    "AUTH_USERNAME", "AUTH_PASSWORD", "AUTH_SECRET",
    "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
)


@app.get("/api/py/diagnosis")
def diagnosis():
    import platform
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

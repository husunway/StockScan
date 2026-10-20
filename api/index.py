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
from datetime import date, datetime, timedelta, timezone

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
    return warnings


def gather_stock_data(symbol: str, skill: dict | None = None) -> dict:
    from datetime import date as _date

    info_fields, statement_rows = skill_data_requirements(skill)
    df, price_source = load_price_history_any(symbol)
    data = {
        "symbol": symbol,
        "analysis_date": _date.today().isoformat(),
        "price": {**price_snapshot(df), "source": price_source},
        **gather_fundamentals(symbol, info_fields, statement_rows),
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
    schema = skill.get("output_schema") or STOCK_VERDICT_SCHEMA  # skill's data-structure.json wins
    client = anthropic.Anthropic(timeout=240.0, max_retries=1)
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{"type": "text", "text": skill["instructions"], "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema}},
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
    keys = ("id", "name", "description", "version", "inputs", "output", "path", "data_structure_path")
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
    data = gather_stock_data(symbol, skill)
    analysis, response = run_skill_with_claude(skill, data)
    usage = response.usage
    return {
        "skill": {k: skill[k] for k in ("id", "name", "version", "path", "data_structure_path")},
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
    CONSTRAINT UQ_agent_decisions UNIQUE (backtest_id, as_of_date)
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_agent_decisions_symbol')
CREATE INDEX IX_agent_decisions_symbol ON {DECISIONS_TABLE} (symbol, skill_id, as_of_date);
"""

DECISION_COLUMNS = (
    "decision_id", "backtest_id", "skill_id", "symbol", "as_of_date", "status", "recommendation", "conviction",
    "current_price", "buy_below_price", "target_price", "stop_price", "intrinsic_value_low", "intrinsic_value_base",
    "intrinsic_value_high", "margin_of_safety_pct", "headline", "summary", "warnings_json", "model",
    "input_tokens", "output_tokens", "error", "started_at", "finished_at",
)


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


def point_in_time_payload(raw: dict, prices_df, as_of: date, skill: dict) -> dict:
    info_fields, statement_rows = skill_data_requirements(skill)
    hist = prices_df[prices_df["date"] < as_of.isoformat()]
    if len(hist) < 30:
        raise HTTPException(status_code=422, detail=f"Fewer than 30 price rows before {as_of}; load more history first.")
    price = float(hist["close"].iloc[-1])
    annual = visible_statements(raw, as_of, "A")
    quarterly = visible_statements(raw, as_of, "Q")
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
    return {
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
    }


class BacktestRequest(BaseModel):
    skill: str = Field(..., min_length=1, max_length=60)
    symbol: str = Field(..., min_length=1, max_length=20)
    year: int = Field(2026, ge=2000, le=2100)


def _decision_dict(row) -> dict:
    d = dict(zip(DECISION_COLUMNS, row))
    for k in ("current_price", "buy_below_price", "target_price", "stop_price", "intrinsic_value_low",
              "intrinsic_value_base", "intrinsic_value_high", "margin_of_safety_pct"):
        d[k] = num(d[k], 4)
    for k in ("as_of_date", "started_at", "finished_at"):
        d[k] = d[k].isoformat() if d[k] is not None else None
    d["warnings"] = json.loads(d.pop("warnings_json") or "[]")
    return d


def _decision_counts(cur, backtest_id: int) -> dict:
    cur.execute(f"SELECT status, COUNT(*) FROM {DECISIONS_TABLE} WHERE backtest_id = %s GROUP BY status", (backtest_id,))
    counts = {"pending": 0, "running": 0, "done": 0, "failed": 0}
    for status, n in cur.fetchall():
        counts[status] = n
    counts["total"] = sum(counts.values())
    return counts


def _backtest_meta(cur, backtest_id: int) -> dict:
    cur.execute(
        f"SELECT backtest_id, skill_id, skill_version, symbol, [year], created_at FROM {BACKTESTS_TABLE} WHERE backtest_id = %s",
        (backtest_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return {"backtest_id": row[0], "skill_id": row[1], "skill_version": row[2], "symbol": row[3], "year": row[4],
            "created_at": row[5].isoformat(), "counts": _decision_counts(cur, backtest_id)}


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
        cur.execute(ENSURE_BACKTEST_TABLES_SQL)
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


@app.post("/api/py/agent/backtest")
def create_backtest(req: BacktestRequest):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured.")
    skill = get_skill(req.skill.strip().lower())
    symbol = normalize_symbol(req.symbol)
    dates = mondays(req.year, date.today())
    if not dates:
        raise HTTPException(status_code=422, detail=f"No Mondays in {req.year} on or before today.")

    # Make sure two years of prices are stored, then pull the statements once.
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(ENSURE_TABLE_SQL)
        cur.execute(f"SELECT COUNT(*), MIN(trade_date) FROM {TABLE} WHERE symbol = %s", (symbol,))
        count, first = cur.fetchone()
    finally:
        conn.close()
    if count < 250 or (first and first > date(req.year - 1, 1, 1)):
        save_prices(symbol, fetch_history(symbol))
    raw = fetch_backtest_raw(symbol)
    if not raw["statements"]["A"].get("income"):
        raise HTTPException(status_code=404, detail=f"Yahoo Finance returned no financial statements for {symbol}.")

    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(ENSURE_BACKTEST_TABLES_SQL)
        cur.execute(
            f"INSERT INTO {BACKTESTS_TABLE} (skill_id, skill_version, symbol, [year], raw_data) "
            f"OUTPUT INSERTED.backtest_id VALUES (%s, %s, %s, %s, %s)",
            (skill["id"], skill["version"], symbol, req.year, json.dumps(raw)),
        )
        backtest_id = cur.fetchone()[0]
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
        return {**_backtest_meta(cur, backtest_id), "decisions": _decisions(cur, backtest_id)}
    finally:
        conn.close()


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
    # Phase 1: claim a decision and load what it needs, then release the connection for the model call.
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT skill_id, symbol, raw_data FROM {BACKTESTS_TABLE} WHERE backtest_id = %s", (backtest_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Backtest not found")
        skill_id, symbol, raw_json = row
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
        cur.execute(
            f"UPDATE {DECISIONS_TABLE} SET status = 'running', error = NULL, started_at = SYSUTCDATETIME() WHERE decision_id = %s",
            (decision_id,),
        )
        conn.commit()
        prices_df = load_prices_df(cur, symbol)
    finally:
        conn.close()

    skill = get_skill(skill_id)
    raw = json.loads(raw_json)
    result: dict = {}
    error: str | None = None
    payload = None
    try:
        payload = point_in_time_payload(raw, prices_df, as_of, skill)
        analysis, response = run_skill_with_claude(skill, payload)
        warnings = validate_verdict(analysis)
        result = {"analysis": analysis, "warnings": warnings, "model": response.model,
                  "input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
    except HTTPException as e:
        error = str(e.detail)[:1000]

    # Phase 2: persist the outcome.
    conn = db_connect()
    try:
        cur = conn.cursor()
        if error:
            cur.execute(
                f"UPDATE {DECISIONS_TABLE} SET status = 'failed', error = %s, input_json = %s, finished_at = SYSUTCDATETIME() "
                f"WHERE decision_id = %s",
                (error, json.dumps(payload) if payload else None, decision_id),
            )
        else:
            a = result["analysis"]
            cur.execute(
                f"UPDATE {DECISIONS_TABLE} SET status = 'done', recommendation = %s, conviction = %s, current_price = %s, "
                f"buy_below_price = %s, target_price = %s, stop_price = %s, intrinsic_value_low = %s, intrinsic_value_base = %s, "
                f"intrinsic_value_high = %s, margin_of_safety_pct = %s, headline = %s, summary = %s, input_json = %s, "
                f"output_json = %s, warnings_json = %s, model = %s, input_tokens = %s, output_tokens = %s, "
                f"finished_at = SYSUTCDATETIME() WHERE decision_id = %s",
                (a["recommendation"], a["conviction"], a.get("current_price"), a.get("buy_below_price"),
                 a.get("target_price"), a.get("stop_price"), a.get("intrinsic_value_low"), a.get("intrinsic_value_base"),
                 a.get("intrinsic_value_high"), a.get("margin_of_safety_pct"), (a.get("headline") or "")[:500],
                 a.get("summary"), json.dumps(payload), json.dumps(a), json.dumps(result["warnings"]),
                 result["model"], result["input_tokens"], result["output_tokens"], decision_id),
            )
        conn.commit()
        cur.execute(f"SELECT {', '.join(DECISION_COLUMNS)} FROM {DECISIONS_TABLE} WHERE decision_id = %s", (decision_id,))
        decision = _decision_dict(cur.fetchone())
        counts = _decision_counts(cur, backtest_id)
    finally:
        conn.close()
    return {"done": counts["pending"] == 0 and counts["running"] == 0, "decision": decision, "counts": counts}


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

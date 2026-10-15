import os
from datetime import date
from decimal import Decimal

from fastapi import FastAPI, HTTPException
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


SECRET_VARS = {"AZURE_SQL_PASSWORD"}
CONFIG_VARS = ("AZURE_SQL_SERVER", "AZURE_SQL_DATABASE", "AZURE_SQL_USER", "AZURE_SQL_PASSWORD")


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

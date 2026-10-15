from fastapi import FastAPI, HTTPException

app = FastAPI(
    title="StockScan API",
    docs_url="/api/py/docs",
    openapi_url="/api/py/openapi.json",
)


@app.get("/api/py/health")
def health():
    return {"status": "ok"}


@app.get("/api/py/stock/{ticker}")
def analyze_stock(ticker: str):
    symbol = ticker.strip().upper()
    if not symbol.isalnum() and not symbol.replace(".", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid ticker symbol")
    if len(symbol) > 10:
        raise HTTPException(status_code=400, detail="Ticker symbol too long")

    # Placeholder: wire up a market data source and analysis logic here.
    return {
        "ticker": symbol,
        "status": "not_implemented",
        "message": f"Analysis for {symbol} is not implemented yet.",
    }

# StockScan

Stock analysis website: **Next.js** frontend + **Python (FastAPI)** backend, deployed on **Vercel**.
Price history is pulled from Yahoo Finance and stored in **Azure SQL**.

## Structure

```
app/                 Next.js App Router (frontend)
  layout.tsx         Shell with side menu
  page.tsx           Dashboard
  prices/page.tsx    Load Prices page (symbol -> Yahoo Finance -> Azure SQL)
  components/        Sidebar, PriceLoader, TickerSearch, ApiStatus
api/index.py         FastAPI app -> Vercel Python serverless function
requirements.txt     Python dependencies (installed by Vercel at deploy)
next.config.mjs      Rewrites /api/py/* to FastAPI (local) or the function (Vercel)
.env.example         Environment variables the backend needs (copy to .env.local)
```

## API

| Method | Path                   | Description                                                         |
| ------ | ---------------------- | ------------------------------------------------------------------- |
| GET    | `/api/py/health`       | Liveness check                                                      |
| POST   | `/api/py/prices/load`  | `{ "symbol": "AAPL" }` → downloads 2 years of daily prices and saves |
| GET    | `/api/py/symbols`      | Symbols currently stored, with row counts and date ranges           |
| GET    | `/api/py/analysis/single?symbol=AAPL&index=SPY&sector=XLK` | Candles plus indicators (see below) |
| GET    | `/api/py/diagnosis`    | Configuration status (secret values are never returned)             |

### Single stock analysis (`/analysis/single`)

Computed in Python (pandas) from the stored daily prices:

- Moving averages 5/10/20/30/50/200 with crossover events for MA5/20, MA20/50, MA50/200
- RSI (14, Wilder smoothing) with 70/30 overbought/oversold bands
- MACD (12, 26, 9) with histogram
- Bollinger Bands (20, 2σ), %B and bandwidth (squeeze flag when bandwidth is at a 6-month low)
- Volume vs. its 20-day average, trend, and confirmation of ±2% moves (≥1.5× volume = confirmed, <0.8× = weak)
- Support and resistance from clustered pivot highs/lows over the last year (strongest three each side)
- Relative strength vs. an index and a sector benchmark: stock/benchmark ratio rebased to 100, its
  50-day average, and excess return over 1m/3m/6m/1y. Benchmarks are any symbols already loaded,
  so load e.g. `SPY` and `XLK` on the Load Prices page first.

The chart is drawn with [lightweight-charts](https://github.com/tradingview/lightweight-charts) (Apache 2.0).

Prices are saved to `dbo.stock_prices` (created automatically on first load):

| column     | type           |
| ---------- | -------------- |
| symbol     | NVARCHAR(20)   |
| trade_date | DATE           |
| open/high/low/close/adj_close | DECIMAL(18,4) |
| volume     | BIGINT         |
| loaded_at  | DATETIME2 (UTC)|

Primary key is `(symbol, trade_date)`. Reloading a symbol replaces its rows for the fetched range.

## Login

The site is behind a single-user login. The username, password and a cookie-signing secret come
from environment variables; nothing is stored in code or in the database.

- `proxy.ts` (Next.js middleware) redirects unauthenticated page requests to `/login` and
  returns 401 for `/api/*`, `/docs` and `/openapi.json`.
- `app/api/auth/login` checks the credentials (constant-time compare) and sets an HttpOnly,
  SameSite=Lax, 7-day session cookie signed with HMAC-SHA256 (`lib/session.ts`).
- `api/index.py` verifies the same cookie on every `/api/py/*` route except `/api/py/health`,
  so the Python function is protected even when reached directly.
- "Sign out" in the sidebar clears the cookie.

## Configuration

The app reads these environment variables (see `.env.example`):

```
AZURE_SQL_SERVER=your-server.database.windows.net
AZURE_SQL_DATABASE=your-database
AZURE_SQL_USER=
AZURE_SQL_PASSWORD=
AUTH_USERNAME=
AUTH_PASSWORD=
AUTH_SECRET=        # random, e.g. node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
```

Changing `AUTH_SECRET` signs everyone out. Changing the username or password takes effect on the
next login attempt (existing sessions stay valid until they expire or `AUTH_SECRET` changes).

- Locally: copy `.env.example` to `.env.local` and fill in values. `.env.local` is git-ignored.
- On Vercel: Project → Settings → Environment Variables.
- Never put these values in code or commit them.

The connection uses a SQL login (`pymssql`, TDS 7.4, encrypted). Entra ID authentication
is not wired up.

### Azure SQL firewall

Azure SQL blocks all inbound connections by default:

- Local dev: add your client IP under the server's **Networking** blade.
- Vercel: serverless functions have no fixed outbound IP by default. Options are a Vercel
  static-IP / Secure Compute add-on, or an Azure private connectivity option. Do **not**
  open the firewall to all IPs.

## Local development

Requires Node 18+ and Python 3.12+.

```bash
npm install
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
copy .env.example .env.local                      # then fill in values
npm run dev
```

- Frontend: http://localhost:3000
- API docs: http://localhost:3000/docs

## Deploy to Vercel

1. Import `husunway/StockScan` in Vercel (framework preset: Next.js) and add the environment
   variables above, or
2. Run `npx vercel` from this folder.

Vercel builds the Next.js app and deploys `api/index.py` as a Python function automatically.

## Data source note

Price data comes from Yahoo Finance through the `yfinance` library. Yahoo's terms limit this
data to personal, non-commercial use; confirm this is acceptable for your intended use.

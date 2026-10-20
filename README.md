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

### AI agent and skills (`/agent`)

The agent runs a **skill file** against a stock. Skills live in the repository:

```
skills/
  warren-buffett/
    SKILL.md             # frontmatter (name, description, version, inputs, output) + markdown instructions
    data-structure.json  # input contract (every field the agent receives) + output schema it must return
  jamie-dimon/
    SKILL.md             # fortress balance sheet, risk discipline, through-the-cycle earnings, stress test
    data-structure.json  # adds liquidity/leverage/coverage fields, governance risk scores, debt flows;
                         # output adds balance_sheet grade and a 3-scenario stress_test
```

The frontmatter describes the skill; the markdown body becomes Claude's system prompt.
`data-structure.json` does two jobs: its `input` schema **drives data gathering** (the API fetches
exactly the fundamentals fields and statement rows it lists, so each skill gets the data it needs)
and documents them (types, units, sources, minimum useful set); its `output` schema is enforced
with structured outputs (falling back to the built-in `STOCK_VERDICT_SCHEMA` if the file is absent).
The result page renders the common verdict fields and any skill-specific blocks (`valuation` keys,
`balance_sheet`, `stress_test`) generically. To add a skill, add a folder with a `SKILL.md` (and
optionally its own `data-structure.json`); to change how an existing one reasons, edit the file.
Nothing in the code needs to change. (`vercel.json` includes `skills/**` in the Python function bundle.)

| Method | Path                            | Description                                   |
| ------ | ------------------------------- | --------------------------------------------- |
| GET    | `/api/py/agent/skills`          | List skills (id, name, description, version)  |
| GET    | `/api/py/agent/skills/{id}`     | One skill including its full file content     |
| POST   | `/api/py/agent/run`             | `{ "skill": "warren-buffett", "symbol": "KO" }` |

A run:

1. Gathers data - fundamentals and three years of annual statements from Yahoo Finance (yfinance),
   plus a price snapshot (52-week range, 200-day average, 1-year return, volatility, drawdown)
   from the stored prices, or from Yahoo if the symbol is not loaded.
2. Calls Claude (`claude-opus-5-5` by default; override with `ANTHROPIC_MODEL`) with the skill's
   instructions as the system prompt and the data as the user message. The provider is **Microsoft
   Foundry** when `ANTHROPIC_FOUNDRY_API_KEY` and `ANTHROPIC_FOUNDRY_RESOURCE` (or `_BASE_URL`) are set
   (same Anthropic SDK, `AnthropicFoundry` client; structured outputs, effort and prompt caching all
   apply; the server-side refusal fallback is not available there), otherwise the Anthropic API via
   `ANTHROPIC_API_KEY`. The Diagnosis page shows which provider is active.
3. Requires a fixed JSON structure (structured outputs, `STOCK_VERDICT_SCHEMA`): recommendation
   BUY/HOLD/SELL, conviction, intrinsic value low/base/high, buy-below price, target price, stop
   price, summary, checklist, sections, key metrics with source fields, valuation inputs, risks,
   assumptions and data gaps.

Server-side rules, independent of the skill: a **BUY must carry a target price and a stop price**;
a **SELL must carry a target price**; inconsistent prices (e.g. a BUY target below the current
price) are flagged as warnings on the page.

The page shows the conclusion first (recommendation, prices, margin of safety), then the summary
and justification, then the detail. Everything the model saw is viewable ("Show data sent to the
model"). Skills instruct the model to use only supplied data and to list assumptions separately;
treat the output as analysis to verify, not advice. A refusal fallback (`fallbacks: "default"`) is
enabled so a safety decline is retried on a fallback model in the same call.

Requires `ANTHROPIC_API_KEY`. Each run uses roughly 10-20K input and 3-6K output tokens.
`vercel.json` raises the Python function's `maxDuration` to 120 s because the call takes 30-90 s.

### Weekly backtest (`/agent/backtest`)

Runs a skill as of the **first N Mondays of a year** (N = 1-52, default 10; year default 2026, never
past today) and stores each decision. The AI agent › Warren Buffett menu item opens this page with the
Buffett skill selected.

The page shows a calendar of the year: every day grey, scheduled Mondays outlined, the running one
pulsing blue, completed ones green, failed ones red. It refreshes live (polls every 4 s while anything
is pending). Clicking a completed day opens the decision page: run date, key conclusion (verdict,
conviction, price, buy-below, target, stop, intrinsic value, margin of safety), the summary, the list of
**reports and data referenced** (each financial report by period and publication date, the price
history range, the company profile, and the derived fundamentals), and the **PDF report** embedded,
with open/download links.

The PDF (key conclusion first, then the full analysis, then the references) is built with fpdf2 after
each Monday completes and stored in **Vercel Blob** at `reports/<skill>/<symbol>/<year>/<date>-<id>.pdf`
(`BLOB_READ_WRITE_TOKEN`; `BLOB_ACCESS` = `private` by default, must match the store). The browser never
reads the blob directly: `/api/py/agent/backtest/{id}/decision/{did}/report.pdf` fetches it server-side
behind the login, and regenerates the PDF from the stored output if the blob is missing or no store is
configured.

**Data bundle.** Everything a run needs is downloaded **once per symbol** - two years of daily prices,
the company profile and quote summary, every line item of the annual and quarterly statements, and the
earnings-release dates - and stored as one JSON bundle in Vercel Blob (`data/<SYMBOL>/latest.json` plus a
dated copy) and in `dbo.symbol_data`. AI runs (single runs and every backtest Monday) read the bundle;
nothing is fetched from Yahoo during a run. A bundle older than 24 h is refreshed automatically when a
run starts; the backtest page shows the bundle's contents and has *Pre-download data* / *Refresh data*
buttons (`GET /api/py/data/{symbol}`, `POST /api/py/data/prepare`). Each run's exact model input and
output JSON are archived in Blob next to its PDF (`reports/.../<date>-<id>-input.json`, `-output.json`;
single runs under `runs/<skill>/<symbol>/`).

Point-in-time rules for each Monday `D`:

- Prices: stored daily closes strictly before `D` (52-week range, 200-day average, etc. computed from them).
- Statements: Yahoo annual **and** quarterly statements, every line item, fetched once per backtest and
  stored with the backtest. A fiscal period is visible only once reported: the first earnings-release
  date after period end where Yahoo lists one, otherwise period end + 90 days (annual) / 45 days (quarterly).
- Ratios: Yahoo's live quote summary is *today's* and is **not** used. Market cap, trailing EPS and P/E,
  book value and P/B, ROE, margins, debt/equity, current ratio, cash flows and payout are derived from
  the visible statements (trailing four quarters when available) and the price on `D`. The payload's
  `source_note` says so, and tells the model it has no knowledge after `D`.
- Limitation: the model's training knowledge cannot be rewound. Treat results as a test of the
  framework on the data, not as hindsight-free.

| Method | Path | Description |
| ------ | ---- | ----------- |
| POST | `/api/py/agent/backtest` | `{skill, symbol, year, weeks}` → ensures prices are loaded, fetches statements once, creates one pending row per Monday |
| GET  | `/api/py/agent/backtest/{id}/decision/{decision_id}/report.pdf` | the stored (or regenerated) PDF report |
| POST | `/api/py/agent/backtest/{id}/step` | runs the next pending Monday (one model call); `?retry_failed=true` also picks failed rows |
| GET  | `/api/py/agent/backtest/{id}` | status, counts and all decisions |
| GET  | `/api/py/agent/backtest/{id}/decision/{decision_id}` | one decision with the full input and output JSON |
| GET  | `/api/py/agent/backtests?symbol=&skill=` | previous backtests |

Tables (created on first use): `dbo.agent_backtests` (one per run, holds the raw statements JSON) and
`dbo.agent_decisions` (one per Monday: status, recommendation, conviction, price, buy-below, target,
stop, intrinsic values, margin of safety, headline, summary, full input/output JSON, tokens, errors).
The page calls `/step` in a loop and updates as each Monday completes; it can be stopped and resumed,
and failed Mondays retried. A 2026 run is ~42 model calls (roughly US$5-8 on Opus 5.5).

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
ANTHROPIC_FOUNDRY_API_KEY=   # Claude via Microsoft Foundry (preferred when set) ...
ANTHROPIC_FOUNDRY_RESOURCE=  # ... resource name from https://<resource>.services.ai.azure.com/anthropic/
ANTHROPIC_API_KEY=           # or the Anthropic Claude API, used only when the Foundry variables are absent
ANTHROPIC_MODEL=             # optional, default claude-opus-5-5 (on Foundry: the deployed model name)
BLOB_READ_WRITE_TOKEN=  # Vercel Blob store for backtest PDFs (set automatically when the store is connected on Vercel)
BLOB_ACCESS=private     # or public; must match the store
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

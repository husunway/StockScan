"""Build the StockScan design document as HTML and PDF from one content model.

Usage: python scripts/build_design_doc.py [out_dir]   (default: docs/)
Requires fpdf2 (already in requirements.txt).
"""
from __future__ import annotations

import datetime as dt
import html
import sys
from pathlib import Path

TITLE = "StockScan - Design Document"
SUBTITLE = "AI-assisted stock analysis platform: architecture, key design concepts and major functions"
VERSION = "1.1"

# --------------------------------------------------------------------------------------------
# Content model: a list of blocks. Kinds: h1, h2, h3, p, ul, table (headers, rows), diagram, note
# --------------------------------------------------------------------------------------------
C: list = []
def h1(t): C.append(("h1", t))
def h2(t): C.append(("h2", t))
def h3(t): C.append(("h3", t))
def p(t): C.append(("p", t))
def ul(*items): C.append(("ul", list(items)))
def table(headers, rows): C.append(("table", headers, rows))
def note(t): C.append(("note", t))

h1("1. Purpose and scope")
p("StockScan is a web application for analyzing listed stocks with a set of AI 'analyst agents', each of which applies "
  "a named investor's framework (Warren Buffett, Jamie Dimon, John Murphy) to data that has been gathered and stored in "
  "advance. It supports single analyses, weekly point-in-time backtests over a year, and a funnel scan across the S&P 500. "
  "Every analysis is stored with its inputs, outputs and reports so that a decision can be audited and reproduced.")
p("This document describes the key design concepts, the architecture, the major functions and the principal data stores. "
  "Diagnostics and operational detail are out of scope; only the major database tables are covered.")
note("All agent output is model-generated analysis for a reader who makes their own decisions. It is not investment advice, "
     "and the application says so on every result page and report.")

h1("2. Key design concepts")
h2("2.1 Skills as files, not code")
p("Each analyst agent is a skill folder in the repository: skills/<id>/SKILL.md holds the investor's framework, decision "
  "rules and evidence rules as markdown with a small frontmatter (name, description, version, data_source); "
  "skills/<id>/data-structure.json is the data contract - a JSON Schema for exactly what the agent receives (input) and "
  "exactly what it must return (output). The markdown body becomes the model's system prompt; the output schema is enforced "
  "with structured outputs. Changing how an agent reasons, or adding a new agent, is a file change, not a code change.")
ul("The input schema also drives data gathering: fundamentals-based skills list the quote-summary fields and statement rows "
   "they need, and the backend fetches exactly those.",
   "Three rules are enforced by the server regardless of skill: a BUY must carry a target price and a stop price; a SELL must "
   "carry a target price; inconsistent prices (e.g. a BUY target below the current price) are flagged as warnings.")
h2("2.2 Point-in-time discipline")
p("A backtest or scan run 'as of' a date must see only what existed on that date. The system enforces this in the data, not "
  "in the prompt alone:")
ul("Prices: only closes strictly before (backtest) or on/before (scan, technical) the analysis date.",
   "Financial reports: annual and quarterly statements are visible only once reported - using the company's actual earnings-"
   "release dates where known, otherwise a 90-day (annual) / 45-day (quarterly) lag. Ratios such as P/E, ROE and margins are "
   "rebuilt from the statements visible at the time, because the data vendor's live ratios cannot be rewound.",
   "Technical data: swing pivots carry the date they became known (pivot date + confirmation window); support/resistance "
   "levels are stored per day from pivots confirmed by that day; chart patterns carry their neckline-break date.",
   "The model is told that the analysis date is 'today' and that nothing after it exists. One limitation remains and is "
   "disclosed everywhere: the model's own training knowledge cannot be rewound, so results test the framework on the data, "
   "not a hindsight-free analyst.")
h2("2.3 Precompute once, analyze offline")
p("Model runs never fetch from the internet or compute indicators. Everything an agent needs is prepared beforehand:")
ul("A per-symbol data bundle (profile, 49 quote-summary fields, every annual and quarterly statement line item, earnings "
   "dates, two years of prices) is downloaded once and kept in Azure SQL and in Vercel Blob as browsable files.",
   "For technical analysis, nine TECH_ tables are computed from stored prices: daily and weekly bars, 38 indicators, trend "
   "classification, confirmed pivots, per-day support/resistance and Fibonacci levels, signal events and detected patterns.",
   "Loading is incremental: prices are fetched only for days after the last stored close, and technical data is recomputed "
   "only when it is older than the latest close. Re-running a load over the whole index is cheap.")
h2("2.4 Resumable, batched jobs")
p("The backend runs as serverless functions with a per-request time limit, and a model call takes 30-120 seconds. Long "
  "work is therefore modelled as a job with one unit of work per request: the page creates the job (rows in a table), then "
  "calls a /step endpoint in a loop; each step claims the next pending unit atomically, runs exactly one model call (or one "
  "symbol load), and persists the result. Jobs can be stopped, resumed and retried, several browser workers can drive the "
  "same job in parallel, and the page polls the database so progress is live even when another tab is doing the work.")
h2("2.5 Evidence and provenance")
ul("Every decision stores the exact JSON the model saw, the JSON it returned, the list of reports and data sets consumed "
   "(each statement by period and publication date, the price range, the profile), token usage and a timestamped run log.",
   "Agents must cite the source table and field for every metric, level, reading and pattern, and list every judgment call "
   "under 'assumptions' and every missing input under 'data gaps'.",
   "Two reports are generated per decision: an interactive HTML report (for technical skills: a candlestick chart with "
   "indicators, levels, signals, patterns and the verdict lines, and an evidence panel that jumps the chart to each cited "
   "item) and a PDF. Both are stored in Vercel Blob next to the run's input/output JSON.")
h2("2.6 Cost control: cheapest evidence first")
p("The S&P 500 scan orders work by cost. A free technical pre-screen from the TECH_ tables (primary trend up, intermediate "
  "trend not down, optional minimum ADX) decides which (date, stock) pairs reach a model at all. John Murphy runs first; "
  "only a BUY with the close inside the entry zone proceeds to Warren Buffett; only a Buffett BUY proceeds to Jamie Dimon. "
  "A rejection at any stage settles the pair immediately, so no further model calls are spent on it.")

h1("3. Architecture")
C.append(("diagram",))
table(["Component", "Role"], [
    ["GitHub + Vercel", "Source control and hosting. The Vercel project is linked to the GitHub repository; every commit to main is built and deployed automatically (see section 8)."],
    ["Next.js 16 (React, TypeScript)", "User interface: pages, calendars, live logs, charts (lightweight-charts). A proxy enforces the single-user login and forwards /api/py/* to the Python API."],
    ["FastAPI (Python, api/index.py)", "All business logic: data gathering, point-in-time payloads, technical computation, skill loading, model calls, validation, reports, job runners."],
    ["Azure SQL", "System of record: prices, data bundles, TECH_ tables, constituents, backtests and decisions, scan and load jobs."],
    ["Vercel Blob", "Document store: per-symbol data files, per-decision HTML/PDF reports and input/output JSON archives."],
    ["Claude (Microsoft Foundry or Anthropic API)", "The analyst model. Same SDK either way; provider chosen by environment variables. Structured outputs enforce each skill's output schema."],
    ["Yahoo Finance (yfinance)", "Source of prices, quote summary, financial statements and earnings dates - used only during preparation, never during a run."],
])
p("Authentication is a single configured user: a signed, HttpOnly session cookie issued by Next.js and verified both by the "
  "Next.js proxy and by the Python API, so the API cannot be reached around the front end. All secrets come from environment "
  "variables; nothing is stored in code or the database.")

h1("4. Major functions")
h2("4.1 Load Prices and the S&P 500 universe")
p("Load Prices stores two years of daily prices for a symbol. The S&P 500 universe page processes every constituent in "
  "dbo.SP500_CONSTITUENTS as a batched job: delta price updates, then technical-data recomputation only where stale; every "
  "stock is shown as a tile coloured by state (missing / stale / ready) and, during a job, by progress.")
h2("4.2 Single stock analysis (charts)")
p("An interactive multi-pane chart from the stored prices: candlesticks with moving averages (5/10/20/30/50/200) and "
  "crossovers, Bollinger Bands, support/resistance from pivots, volume with confirmation markers, RSI, MACD and relative "
  "strength versus chosen index and sector benchmarks, with signal tiles and a table view.")
h2("4.3 AI agent: skill library and single runs")
p("The skill library lists every skill with its SKILL.md and data-structure.json. A single run applies a skill to a symbol as of "
  "today and shows the verdict, prices, checklist, sections, metrics with sources, risks, assumptions and data gaps.")
h2("4.4 Weekly backtest")
p("Runs a skill as of the first N Mondays of a year (default 10, up to 52). A calendar shows every day grey, scheduled Mondays "
  "outlined, the running one pulsing, completed ones green with the verdict letter (B/H/S). Clicking a completed day opens the "
  "decision page: run date, key conclusion, references, the embedded interactive HTML report and the PDF. The page shows the "
  "configuration in use and a live, timestamped run log for the Monday in progress (data assembled, model connected, tokens, "
  "first output, progress, verdict, reports stored).")
h2("4.5 S&P 500 scan")
p("For each Monday (or weekday) in a date range and each constituent, runs the funnel described in 2.6. The page shows the "
  "exact number of stage-1 model calls before anything is spent, a progress bar per date (stocks analyzed vs total), funnel "
  "counters, a calendar whose dates carry the number of stocks recommended by all three agents, a per-date table with each "
  "agent's verdict linked to its report, an embedded point-in-time technical chart for any stock, and the full hit list.")
h2("4.6 Run history")
p("Every run with its agent, timing, date range, counts, verdict tallies, overall status and the exact reasons for any failures, "
  "expandable to the per-Monday detail.")

h1("5. The analyst agents")
table(["Agent", "Framework (from the skill file)", "Data it uses", "Distinctive outputs"], [
    ["Warren Buffett", "Circle of competence, durable moat, management quality, financial strength, owner-earnings intrinsic value, margin of safety (>= 25%).",
     "Fundamentals bundle: quote summary and annual statements (point-in-time for backtests).", "Intrinsic value low/base/high, buy-below price, valuation inputs (growth, discount rate, terminal growth)."],
    ["Jamie Dimon", "Fortress balance sheet, risk discipline, through-the-cycle earnings, management and culture, competitive position, macro/regulatory exposure; mandatory 3-scenario stress test.",
     "Fundamentals bundle plus liquidity, leverage, coverage, governance-risk fields and debt flows.", "Balance-sheet grade, stress test (base / recession / severe), normalized-earnings valuation."],
    ["John Murphy", "Technical Analysis of the Financial Markets: Dow Theory, trend and support/resistance, moving averages, oscillators and divergences, volume confirmation, patterns with measured moves, Fibonacci, trading rules.",
     "TECH_ tables only: bars, indicators, trend, confirmed pivots, levels, signals, patterns as of the date.", "Trend assessment, key levels, indicator readings, patterns, trade plan (entry zone, stop basis, target method, reward-to-risk)."],
])
p("All three return the same core verdict: recommendation (BUY / HOLD / SELL), conviction, headline, current price, buy-below "
  "price, target price, stop price with rationales, time horizon, a summary memo, a checklist, supporting sections, key metrics "
  "with sources, risks, assumptions and data gaps.")

h1("6. Principal data stores")
table(["Table / store", "Purpose"], [
    ["dbo.stock_prices", "Daily OHLCV per symbol; the base for charts, price snapshots and technical computation."],
    ["dbo.symbol_data (+ Blob data/<SYMBOL>/)", "Per-symbol data bundle: profile, quote summary, all statement line items, earnings dates, prices; dated snapshots in Blob."],
    ["TECH_PRICE_DAILY / WEEKLY, TECH_INDICATOR_DAILY, TECH_TREND_DAILY, TECH_PIVOT, TECH_LEVEL, TECH_SIGNAL, TECH_PATTERN, TECH_LOAD",
     "Precomputed technical analysis per symbol and trading day, with confirmation dates for point-in-time reads."],
    ["dbo.SP500_CONSTITUENTS", "The index membership used by the universe loader and the scan; maintained in the database (seeded from the repository list)."],
    ["dbo.agent_backtests, dbo.agent_decisions", "One row per run and one per analysis (date, symbol, skill): verdict fields, input/output JSON, references, warnings, tokens, log, report paths."],
    ["dbo.SCAN_JOB, dbo.SCAN_ITEM", "S&P 500 scan jobs and the state of every (date, stock) pair through the funnel."],
    ["dbo.UNIVERSE_LOAD_JOB / _ITEM", "Batched index loading jobs and per-symbol outcomes."],
    ["Vercel Blob reports/", "Per-decision PDF, HTML report, and the model's input and output JSON."],
])

h1("7. Reports")
p("The HTML report is a self-contained page: verdict card with target/stop/buy-below, an evidence panel of every cited level, "
  "reading, pattern and metric with its source, the narrative and structured blocks, references and, for technical skills, the "
  "interactive chart described in 2.5. The PDF carries the key conclusion first, then the full analysis, then the data and "
  "reports referenced. Both are produced when a decision completes and regenerated on demand if the stored copy is missing.")

h1("8. Deployment and configuration")
h2("8.1 Continuous deployment from GitHub")
p("The source of truth is the GitHub repository (main branch). The Vercel project is linked to it, so every commit pushed "
  "to main triggers a Vercel build and deployment automatically: Vercel builds the Next.js application, packages api/index.py "
  "as a Python serverless function (with skills/ and data/ bundled alongside it, per vercel.json) and switches production to "
  "the new deployment when the build succeeds. A failed build leaves the previous deployment live. Preview deployments are "
  "created for other branches and pull requests. No manual deploy step exists; releasing is committing.")
ul("Code, skills (SKILL.md, data-structure.json) and the S&P 500 seed list therefore go live together with the commit that "
   "changes them.",
   "Database tables are created and migrated automatically by the Python API on first use after a deployment, so schema "
   "changes need no separate release step.",
   "Rolling back is redeploying an earlier commit from the Vercel dashboard, or reverting the commit on main.")
h2("8.2 Runtime and configuration")
ul("Hosted on Vercel: the Next.js app plus one Python serverless function (api/index.py) with a 300-second limit (Fluid "
   "Compute); long work is split into per-request steps as described in 2.4.",
   "Configuration is entirely environment variables set in the Vercel project (and in a local .env.local for development): "
   "the model provider (Microsoft Foundry resource and key, or an Anthropic API key), the model and reasoning effort (default "
   "medium), the Azure SQL login, the Vercel Blob token and the single login credentials. A change to a variable takes effect "
   "on the next deployment.",
   "Secrets are never stored in code, the repository or the database; the Diagnosis pages show only whether each variable is set.")

h1("9. Known limitations and next steps")
ul("The model's training knowledge cannot be rewound to a past date; backtests test the framework on the data, not a "
   "hindsight-free analyst.",
   "Market data comes from Yahoo Finance via yfinance, whose terms limit the data to personal, non-commercial use.",
   "Pattern detection covers double tops/bottoms and head-and-shoulders; continuation patterns and trendlines are left for the "
   "agent to infer and flag as assumptions. No intermarket or sector-relative data is supplied to the technical agent.",
   "A full S&P 500 scan is large (hundreds to thousands of model calls per date range even after pre-screening); runs are "
   "designed to be stopped, resumed and parallelized, and the page states the call count before starting.",
   "Possible next steps: scheduled nightly data refresh, a scan outcome tracker (what happened after each hit), portfolio-level "
   "views, and additional analyst skills.")

# --------------------------------------------------------------------------------------------
# Architecture diagram (shared geometry for SVG and PDF)
# --------------------------------------------------------------------------------------------
BOXES = {  # id: (x, y, w, h, label, sublabel)
    "github": (20, 130, 80, 50, "GitHub", "main branch"),
    "ui": (20, 20, 150, 50, "Browser", "Next.js UI, charts, live logs"),
    "next": (210, 20, 150, 50, "Next.js server", "login proxy, /api/py rewrite"),
    "api": (400, 20, 170, 50, "FastAPI (Python)", "api/index.py"),
    "sql": (300, 130, 150, 50, "Azure SQL", "prices, TECH_, decisions, jobs"),
    "blob": (480, 130, 150, 50, "Vercel Blob", "data files, reports, archives"),
    "claude": (660, 20, 150, 50, "Claude", "Foundry or Anthropic API"),
    "yahoo": (130, 130, 140, 50, "Yahoo Finance", "preparation only"),
    "skills": (660, 130, 150, 50, "skills/ + data/", "SKILL.md, data-structure.json"),
}
ARROWS = [("ui", "next", ""), ("next", "api", ""), ("api", "claude", "structured outputs"), ("api", "sql", ""), ("api", "blob", ""),
          ("api", "yahoo", "prepare"), ("api", "skills", "load"), ("github", "next", "commit -> Vercel deploy")]


def center(b):
    x, y, w, h, *_ = b
    return x + w / 2, y + h / 2


def svg_diagram() -> str:
    parts = ['<svg viewBox="0 0 830 200" width="100%" role="img" aria-label="Architecture diagram" style="max-width:860px;display:block;margin:12px auto">',
             '<defs><marker id="ah" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#6b7280"/></marker></defs>']
    for a, b, lbl in ARROWS:
        (x1, y1), (x2, y2) = center(BOXES[a]), center(BOXES[b])
        parts.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" stroke="#6b7280" stroke-width="1.5" marker-end="url(#ah)"/>')
        if lbl:
            parts.append(f'<text x="{(x1 + x2) / 2:.0f}" y="{(y1 + y2) / 2 - 6:.0f}" font-size="10" fill="#6b7280" text-anchor="middle">{html.escape(lbl)}</text>')
    for bid, (x, y, w, h, lab, sub) in BOXES.items():
        fill = "#2563eb" if bid in ("api",) else "#eef2ff" if bid in ("ui", "next") else "#f3f4f6"
        color = "#fff" if bid == "api" else "#111827"
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="#9ca3af"/>')
        parts.append(f'<text x="{x + w / 2}" y="{y + 21}" font-size="13" font-weight="600" fill="{color}" text-anchor="middle">{html.escape(lab)}</text>')
        parts.append(f'<text x="{x + w / 2}" y="{y + 38}" font-size="10" fill="{"#dbeafe" if bid == "api" else "#6b7280"}" text-anchor="middle">{html.escape(sub)}</text>')
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------------------------
# HTML renderer
# --------------------------------------------------------------------------------------------
CSS = """
:root{--bg:#f7f8fa;--surface:#fff;--text:#111827;--muted:#6b7280;--border:#e5e7eb;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0b0f17;--surface:#131a26;--text:#e5e7eb;--muted:#9ca3af;--border:#243042;--accent:#3b82f6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:32px 20px 60px}
header{border-bottom:1px solid var(--border);padding-bottom:16px;margin-bottom:24px}
h1.title{font-size:1.9rem;margin:0 0 6px;letter-spacing:-.01em}.sub{color:var(--muted);margin:0}.meta{color:var(--muted);font-size:.85rem;margin-top:6px}
h1{font-size:1.35rem;margin:34px 0 10px;padding-top:10px;border-top:1px solid var(--border)}h2{font-size:1.08rem;margin:20px 0 6px}h3{font-size:.95rem;margin:14px 0 4px}
p{margin:8px 0;max-width:860px}ul{margin:6px 0 10px;padding-left:22px}li{margin:4px 0}
table{width:100%;border-collapse:collapse;margin:10px 0 16px;font-size:.9rem}th,td{border-bottom:1px solid var(--border);padding:8px 10px;text-align:left;vertical-align:top}th{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
.note{background:var(--surface);border:1px solid var(--border);border-left:4px solid var(--accent);border-radius:8px;padding:10px 14px;margin:12px 0;color:var(--text)}
.toc{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 18px;margin-bottom:10px}.toc a{color:var(--accent);text-decoration:none}.toc li{margin:2px 0}
.diagram{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:8px}
footer{margin-top:40px;color:var(--muted);font-size:.8rem;border-top:1px solid var(--border);padding-top:12px}
@media print{body{background:#fff}.wrap{padding:0}h1{break-after:avoid}table{break-inside:avoid}}
"""


def slug(t: str) -> str:
    return "s-" + "".join(ch if ch.isalnum() else "-" for ch in t.lower()).strip("-")


def render_html() -> str:
    out = []
    toc = [f'<li><a href="#{slug(b[1])}">{html.escape(b[1])}</a></li>' for b in C if b[0] == "h1"]
    out.append(f'<div class="toc"><strong>Contents</strong><ol>{"".join(toc)}</ol></div>')
    in_list = False
    for b in C:
        kind = b[0]
        if kind == "h1":
            out.append(f'<h1 id="{slug(b[1])}">{html.escape(b[1])}</h1>')
        elif kind in ("h2", "h3"):
            out.append(f"<{kind}>{html.escape(b[1])}</{kind}>")
        elif kind == "p":
            out.append(f"<p>{html.escape(b[1])}</p>")
        elif kind == "note":
            out.append(f'<div class="note">{html.escape(b[1])}</div>')
        elif kind == "ul":
            out.append("<ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in b[1]) + "</ul>")
        elif kind == "table":
            out.append("<table><thead><tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in b[1]) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r) + "</tr>" for r in b[2]) + "</tbody></table>")
        elif kind == "diagram":
            out.append(f'<div class="diagram">{svg_diagram()}</div>')
    today = dt.date.today().isoformat()
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(TITLE)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<header><h1 class="title">{html.escape(TITLE)}</h1><p class="sub">{html.escape(SUBTITLE)}</p>
<div class="meta">Version {VERSION} · {today} · generated by scripts/build_design_doc.py</div></header>
{''.join(out)}
<footer>StockScan design document v{VERSION}, {today}. Describes the system as implemented in the repository at that date.</footer>
</div></body></html>
"""


# --------------------------------------------------------------------------------------------
# PDF renderer (fpdf2, core Helvetica -> Latin-1 text)
# --------------------------------------------------------------------------------------------
L1 = {"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "...", "→": "->",
      "•": "-", " ": " ", "≤": "<=", "≥": ">=", "−": "-"}


def l1(t: str) -> str:
    return "".join(L1.get(ch, ch) for ch in str(t)).encode("latin-1", "replace").decode("latin-1")


def render_pdf(path: Path) -> None:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_title(TITLE)
    pdf.add_page()
    W = pdf.epw
    today = dt.date.today().isoformat()
    pdf.set_font("Helvetica", "B", 22); pdf.multi_cell(W, 10, l1(TITLE)); pdf.ln(1)
    pdf.set_font("Helvetica", "", 12); pdf.set_text_color(90); pdf.multi_cell(W, 6, l1(SUBTITLE))
    pdf.set_font("Helvetica", "", 9); pdf.multi_cell(W, 5, l1(f"Version {VERSION} - {today} - generated by scripts/build_design_doc.py")); pdf.set_text_color(0); pdf.ln(4)
    # contents
    pdf.set_font("Helvetica", "B", 12); pdf.multi_cell(W, 6, "Contents"); pdf.set_font("Helvetica", "", 10)
    for b in C:
        if b[0] == "h1":
            pdf.multi_cell(W, 5.5, l1(b[1]))
    pdf.ln(4)

    def heading(t, size, top):
        pdf.ln(top); pdf.set_font("Helvetica", "B", size); pdf.multi_cell(W, 6.5, l1(t)); pdf.ln(0.5)

    def tbl(headers, rows):
        pdf.set_font("Helvetica", "", 8.5)
        n = len(headers)
        widths = {2: (36, 64), 3: (26, 40, 34), 4: (16, 36, 24, 24)}.get(n, tuple([100 // n] * n))
        with pdf.table(col_widths=widths, text_align="LEFT", line_height=4.4) as t:
            r = t.row()
            for h in headers:
                r.cell(l1(h))
            for row in rows:
                r = t.row()
                for c in row:
                    r.cell(l1(c))
        pdf.ln(2)

    def diagram():
        # scale the shared geometry (830x200 units) into the page width
        s = W / 830.0
        x0, y0 = pdf.l_margin, pdf.get_y() + 2
        pdf.set_draw_color(150); pdf.set_line_width(0.3)
        for a, b, lbl in ARROWS:
            (ax, ay), (bx, by) = center(BOXES[a]), center(BOXES[b])
            pdf.line(x0 + ax * s, y0 + ay * s, x0 + bx * s, y0 + by * s)
            if lbl:
                pdf.set_font("Helvetica", "", 7); pdf.set_text_color(110)
                pdf.set_xy(x0 + (ax + bx) / 2 * s - 15, y0 + (ay + by) / 2 * s - 5); pdf.cell(30, 3, l1(lbl), align="C")
        for bid, (x, y, w, h, lab, sub) in BOXES.items():
            if bid == "api":
                pdf.set_fill_color(37, 99, 235)
            elif bid in ("ui", "next"):
                pdf.set_fill_color(238, 242, 255)
            else:
                pdf.set_fill_color(243, 244, 246)
            pdf.rect(x0 + x * s, y0 + y * s, w * s, h * s, style="DF")
            pdf.set_text_color(255 if bid == "api" else 17)
            pdf.set_font("Helvetica", "B", 8.5); pdf.set_xy(x0 + x * s, y0 + y * s + 2.5); pdf.cell(w * s, 4, l1(lab), align="C")
            pdf.set_text_color(219 if bid == "api" else 110)
            pdf.set_font("Helvetica", "", 6.5); pdf.set_xy(x0 + x * s, y0 + y * s + 7); pdf.cell(w * s, 3.5, l1(sub), align="C")
        pdf.set_text_color(0); pdf.set_draw_color(0)
        pdf.set_xy(pdf.l_margin, y0 + 200 * s + 4)

    for b in C:
        kind = b[0]
        if kind == "h1":
            heading(b[1], 14, 5)
        elif kind == "h2":
            heading(b[1], 11.5, 2)
        elif kind == "h3":
            heading(b[1], 10, 1)
        elif kind == "p":
            pdf.set_font("Helvetica", "", 10); pdf.multi_cell(W, 5, l1(b[1])); pdf.ln(1.5)
        elif kind == "note":
            pdf.set_fill_color(239, 246, 255); pdf.set_font("Helvetica", "I", 9.5)
            pdf.multi_cell(W, 5, l1(b[1]), fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT); pdf.ln(2)
        elif kind == "ul":
            pdf.set_font("Helvetica", "", 10)
            for it in b[1]:
                pdf.multi_cell(W, 5, l1("- " + it)); pdf.ln(0.5)
            pdf.ln(1)
        elif kind == "table":
            tbl(b[1], b[2])
        elif kind == "diagram":
            if pdf.get_y() > pdf.h - 80:
                pdf.add_page()
            diagram()
    pdf.output(str(path))


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "docs"
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path, pdf_path = out_dir / "StockScan-Design.html", out_dir / "StockScan-Design.pdf"
    html_path.write_text(render_html(), encoding="utf-8")
    render_pdf(pdf_path)
    print(f"wrote {html_path} ({html_path.stat().st_size / 1024:.0f} KB) and {pdf_path} ({pdf_path.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()

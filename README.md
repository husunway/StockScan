# StockScan

Stock analysis website: **Next.js** frontend + **Python (FastAPI)** backend, deployed on **Vercel**.

## Structure

```
app/                 Next.js App Router (frontend)
  page.tsx           Homepage
  components/        Client components (ticker search, API status)
api/index.py         FastAPI app -> Vercel Python serverless function
requirements.txt     Python dependencies (installed by Vercel at deploy)
next.config.mjs      Rewrites /api/py/* to FastAPI (local) or the function (Vercel)
```

## Local development

Requires Node 18+ and Python 3.12+.

```bash
npm install
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
npm run dev
```

- Frontend: http://localhost:3000
- API docs: http://localhost:3000/docs

## Deploy to Vercel

1. Push this folder to a Git repo and import it in Vercel (framework preset: Next.js), or
2. Run `npx vercel` from this folder.

Vercel builds the Next.js app and deploys `api/index.py` as a Python function automatically.

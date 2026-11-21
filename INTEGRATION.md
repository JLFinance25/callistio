# Europa / Callisto: integration guide (for merging into the main site)

Europa is a **feature**: ask a question about a company's SEC filings and get an answer where every number
was checked against the SEC source before it is shown. It runs as a small HTTP service (Python, FastAPI).
The main site calls it over HTTP, so the main site can be in any language.

Written 2026-11-21. Response example below is a real reply from the running service (trimmed).

## 1. Run it (5 minutes)
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill in your own keys (never commit .env)
.venv/bin/uvicorn backend.app:app --port 8000
```
- Required in `.env`: `NICRRON_API_KEY` (all model calls go through Nicrron; `NICRRON_BASE_URL` has a default).
- Optional: `PRISMTRACE_*` (traces to PRISM; without them it runs and says tracing is off),
  `MASSIVE_API_KEY` (stock price, market data, news; without it those parts say "unavailable").
- Health check: `GET /api/health` → `{"ok": true}`. Switches on: `GET /api/status` (booleans only).
- CORS is open (`*`), so a browser front end on another address can call it directly.

## 2. The one call the main site needs
`POST /api/ask` with JSON `{"ticker": "AAPL", "question": "How much did R&D spending grow?"}`

Reply (trimmed):
```json
{
  "status": "ANSWERED",
  "answer": "Research and development expense rose to $45.8 billion in fiscal 2026 from $34.6 billion in fiscal 2025. That is an increase of $11.2 billion, or +32.5%.",
  "claims": [
    {"text": "$45.8 billion", "status": "VERIFIED",
     "source": "Apple 10-K FY2026 (SEC XBRL): Research and development expense, fiscal 2026",
     "sec_value": 45765000000.0, "fact_id": "rnd_fy2026"}
  ],
  "blocked": [],
  "company": "Apple Inc.",
  "checker": "on",
  "seconds": 2.47,
  "data_note": "Data: Apple 10-K fiscal 2026 vs fiscal 2025, public SEC filings.",
  "disclaimer": "Research aid, not investment advice.",
  "agents": [{"name": "guard_advice", "kind": "code", "model": null, "status": "ok", "note": "passed", "seconds": 0.0}],
  "models": ["anthropic/claude-haiku-5-5", "anthropic/claude-sonnet-5-5", "openai/gpt-5.4-mini"],
  "sources": [{"name": "SEC EDGAR 10-K (XBRL facts and filing text)", "status": "ok"}],
  "news": [], "unavailable": [], "prism_session_id": "ask-406ca42e6aa6"
}
```

What to show in the UI:
| Field | Show it as |
|---|---|
| `answer` | The main text. Already safe: anything unchecked was removed before this point. |
| `status` | `ANSWERED`, `I_DONT_KNOW` (honest "the filings don't say"), `DECLINED_ADVICE` ("should I buy?" is refused). |
| `claims[]` | Hover or footnote per number: `text`, `status` (`VERIFIED`), `source`. |
| `blocked[]` | Optional "removed because unverified" list (sentence `text`, `status`, `note`). Good for showing the checker works. |
| `disclaimer`, `data_note` | Small print under the answer. Keep the disclaimer visible. |
| `agents[]` | Optional "how this answer was made" steps (name, model, seconds). |
| `news[]` | Headlines (title, publisher, published_utc, url), word for word, not verified. |

Answers take about 2 to 15 seconds. Show a loading state.

Other addresses: `POST /api/chat` accepts `{"message": "..."}` or OpenAI-style `{"messages": [...]}`
(ticker optional, default AAPL; same pipeline, adds `reply`). `GET /` serves a simple test chat page.
`POST /api/compare` still returns **FAKE placeholder data** (`fake_data: true`). Do not use it in the product.

## 3. Limits to know before the demo
- Only **Apple (AAPL)**, fiscal 2025 vs 2026 10-K figures are loaded (`backend/facts.py`). Other tickers answer "I don't know".
- Market data is end-of-day (Massive), not live.
- Not investment advice: the service refuses buy/sell questions on purpose.
- Synthetic test material is labeled SYNTHETIC (`scripts/demo_synthetic_wrong_number.py`).

## 4. Files that matter
| File | What it does |
|---|---|
| `backend/app.py` | The HTTP addresses above |
| `backend/desk.py` | The agent pipeline: guard → router → specialists → writer → skeptic → number gate → notices |
| `backend/checker.py`, `backend/gate.py` | The number checker (pure code, fails closed) |
| `backend/facts.py`, `backend/data/` | SEC facts and filing text |
| `backend/llm.py` | All model calls (Nicrron) |
| `backend/tracing.py` | PRISM traces |
| `tests/` | `.venv/bin/python -m pytest -q` |

## 5. Merging options (pick one with the team)
1. **Run it as its own service** (fastest, nothing to rewrite): deploy this folder, point the main site at its
   `/api/ask`. Recommended for today.
2. **Copy the folder into the main repo** as a sub-folder and run it next to the main backend.
3. **Port the logic** into the main backend's language: only worth it after the event.

Updates: newer versions (3-model council, more checker fixes) are pushed to this same repo. The `/api/ask`
request and the fields above stay the same; new fields may be added.

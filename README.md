# What Changed (REFERENCE ONLY: the team builds in its own repo)

What's new in a company's latest SEC filing versus the one before, with every number and quote checked against the source. Built at Money Talks #3 (Nov 21, 2026). Research aid, not investment advice.

## Run it
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then fill in your own keys
uvicorn backend.app:app --reload --reload-dir backend --port 8000
```
Check: http://localhost:8000/api/health · Tests: `python -m pytest -q`

## Built with
- **Nicrron API** for every model call (`backend/llm.py`)
- **PRISM by Block Convey** for traces and testing
- Public SEC EDGAR data

## Research desk (multi-agent)
- `/api/ask` runs a research desk: code guard -> router (Claude Haiku) -> specialists in parallel (SEC filings, Massive market data, Massive second-source income statements, Massive headlines) -> writer (Claude Sonnet) -> skeptic (OpenAI) -> code number gate. Every number shown is checked against its source; missing sources are named as "unavailable", never guessed.
- Switches in `.env`: `EUROPA_PIPELINE=multi|single` (single = the old one-agent path, also the automatic fallback), `EUROPA_SKEPTIC=enforce|advisory|off`, `EUROPA_CHECKER=off` (PRISM BEFORE run only), `MASSIVE_API_KEY` (empty = market parts unavailable).
- `GET /api/status` shows the pipeline and whether Massive and PRISM are on (true/false only).

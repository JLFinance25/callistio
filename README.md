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

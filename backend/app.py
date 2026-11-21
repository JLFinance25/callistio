"""What Changed: backend API (FastAPI).

Starting point: returns FAKE data in the agreed contract shape (TEAM-PLAN.md), so the
front end can be built against it right away. Replace fake_compare / fake_ask with the
real agent. Keep the response shape the same.

Run:  uvicorn backend.app:app --reload --reload-dir backend --port 8000
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend import ask as ask_agent

app = FastAPI(title="What Changed")

# The Ninoo front end runs on a different address, so allow cross-site calls.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DISCLAIMER = "Research aid, not investment advice."


class CompareRequest(BaseModel):
    ticker: str


class AskRequest(BaseModel):
    ticker: str
    question: str


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/compare")
def compare(req: CompareRequest):
    return fake_compare(req.ticker.upper())


@app.post("/api/ask")
def ask(req: AskRequest):
    return ask_agent.answer(req.ticker, req.question)


@app.get("/")
def chat_page():
    """The simple chat page (same address as the API, so one public link serves both)."""
    return FileResponse(Path(__file__).parent / "static" / "index.html")


# ---------- FAKE DATA: replace with the real agent ----------

def fake_compare(ticker: str) -> dict:
    return {
        "fake_data": True,  # remove when real data flows
        "company": f"FAKE DATA for {ticker}",
        "cik": "0000000000",
        "filings": [
            {"form": "10-K", "period_end": "2026-09-26", "url": "https://www.sec.gov/"},
            {"form": "10-K", "period_end": "2025-09-27", "url": "https://www.sec.gov/"},
        ],
        "changes": [
            {
                "rank": 1,
                "title": "FAKE: new risk factor about a single supplier",
                "summary": "Placeholder text so the page has something to show.",
                "claims": [
                    {"text": "FAKE: Revenue was $100.0 billion", "status": "VERIFIED",
                     "source": "placeholder", "sec_value": 100000000000},
                ],
            }
        ],
        "blocked": [
            {"text": "FAKE: Revenue was $120.0 billion", "status": "MISMATCH",
             "note": "Placeholder: SEC says $100.0B"},
        ],
        "cost_usd": 0.0,
        "models": [],
        "prism_session_id": None,
        "disclaimer": DISCLAIMER,
    }


def fake_ask(ticker: str, question: str) -> dict:
    q = question.lower()
    if any(w in q for w in ("buy", "sell", "should i invest", "price target")):
        return {"answer": "I can't give personal investment advice. I can show what the filings say.",
                "claims": [], "status": "DECLINED_ADVICE", "fake_data": True}
    return {"answer": f"FAKE DATA: placeholder answer about {ticker}.", "claims": [],
            "status": "I_DONT_KNOW", "fake_data": True}

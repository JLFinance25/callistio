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

import os as _os
import warnings as _warnings
if not _os.environ.get("NICRRON_API_KEY"):
    _warnings.warn("NICRRON_API_KEY missing in .env: every question will answer \"I couldn't produce a checked answer\".")

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


@app.get("/api/status")
def status():
    """Which pipeline runs and which data sources are switched on. Booleans only, never key values."""
    import os

    from backend import massive, tracing
    return {"pipeline": os.environ.get("EUROPA_PIPELINE", "multi").strip().lower() or "multi",
            "massive": massive.available(), "prism": tracing.handler is not None,
            "nicrron": bool(os.environ.get("NICRRON_API_KEY"))}


@app.post("/api/compare")
def compare(req: CompareRequest):
    return fake_compare(req.ticker.upper())


MAX_Q = 2000
_TICKER_RX = __import__("re").compile(r"[A-Za-z.]{1,10}")


_SURR_RX = __import__("re").compile("[\ud800-\udfff]")


def _san(s) -> str:
    """Drop lone UTF-16 surrogates (they crash UTF-8 encoding in logs, tracing and model calls)."""
    return _SURR_RX.sub("", s) if isinstance(s, str) else s


def _clean_ticker(t) -> str:
    t = _san(str(t or "AAPL")).strip()
    return t.upper() if _TICKER_RX.fullmatch(t) else ""


def _bad(msg: str) -> dict:
    return {"answer": msg, "reply": msg, "response": msg, "status": "I_DONT_KNOW", "claims": [], "blocked": []}


@app.exception_handler(__import__("fastapi").exceptions.RequestValidationError)
def _validation_error(request, exc):
    """Never echo the raw bad input back (public link)."""
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=422, content={"detail": "Send JSON like {\"ticker\": \"AAPL\", \"question\": \"...\"}."})


@app.post("/api/ask")
def ask(req: AskRequest):
    ticker, q = _clean_ticker(req.ticker), _san(req.question or "").strip()
    if not ticker:
        return _bad("Please send a stock ticker of up to 10 letters, like AAPL.")
    if not q:
        return _bad("Please send a question.")
    if len(q) > MAX_Q:
        return _bad(f"Please keep questions under {MAX_Q} characters.")
    return ask_agent.answer(ticker, q)


@app.post("/api/chat")
def chat(body: dict):
    """Generic chat address for testers like PRISM "Test your AI": accepts {message}, {question}, {input},
    {prompt} or OpenAI-style {messages:[...]}; ticker optional (default AAPL). Same pipeline as /api/ask."""
    def _text(c) -> str:
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(p["text"] for p in c[:50] if isinstance(p, dict) and isinstance(p.get("text"), str))
        return ""
    text = next((body[k] for k in ("message", "question", "input", "prompt", "query")
                 if isinstance(body.get(k), str) and body[k].strip()), "")
    msgs = body.get("messages")
    if not text and isinstance(msgs, list):
        for m in reversed(msgs[-200:]):
            if isinstance(m, dict) and m.get("role", "user") == "user" and _text(m.get("content")).strip():
                text = _text(m.get("content"))
                break
    text = _san(text).strip()
    ticker = _clean_ticker(body.get("ticker"))
    if not ticker:
        return _bad("Please send a stock ticker of up to 10 letters, like AAPL.")
    if not text:
        return _bad("Please send a question.")
    if len(text) > MAX_Q:
        return _bad(f"Please keep questions under {MAX_Q} characters.")
    out = ask_agent.answer(ticker, text)
    return {**out, "reply": out.get("answer", ""), "response": out.get("answer", "")}


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
                    {"text": "SYNTHETIC (placeholder, not checked): Revenue was $100.0 billion", "status": "UNCHECKABLE",
                     "source": "placeholder", "sec_value": 100000000000},
                ],
            }
        ],
        "blocked": [
            {"text": "SYNTHETIC (placeholder): Revenue was $120.0 billion", "status": "MISMATCH",
             "note": "SYNTHETIC placeholder: SEC says $100.0B"},
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

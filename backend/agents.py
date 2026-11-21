"""Model agents for the Europa desk: router, news picker, writer, skeptic.

Every model call goes through llm.model(name). Each agent tries a short list of models in order and
never raises: a failure falls back to the next model, then to a safe code answer.
Models only choose, phrase and criticise. They never do arithmetic and never see headline text
(except the news picker, which may only return headline ids).
"""
import json
import os
import re

from langchain_core.runnables import RunnableLambda

from backend import llm

ROUTER_MODELS = [os.environ.get("MODEL_ROUTER") or llm.MODEL_FAST, "openai/gpt-5.4-mini"]
WRITER_MODELS = [os.environ.get("MODEL_WRITER") or llm.MODEL_STRONG, "openai/gpt-5.4"]
SKEPTIC_MODELS = [os.environ.get("MODEL_SKEPTIC") or "openai/gpt-5.4-mini", "openai/gpt-5-mini",
                  "mistral/mistral-large-4"]
NEWS_MODELS = [os.environ.get("MODEL_NEWS") or "openai/gpt-5.4-mini", llm.MODEL_FAST]

KNOWN = ("filings", "market", "fundamentals", "news")
VERDICTS = ("SUPPORTED", "UNSUPPORTED", "ADVICE")
FAIL_DRAFT = {"status": "I_DONT_KNOW", "answer": "I couldn't produce a checked answer to that.", "claims": []}


def _last_json(text: str, need_key: str | None = None) -> dict | None:
    """The LAST complete JSON object in the text (that has need_key, if given)."""
    dec, found, i = json.JSONDecoder(), None, 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict) and (need_key is None or need_key in obj):
            found = obj
        i = end
    return found


def call_json(models, system, human, config=None, need_key=None):
    """Try each model in order. Returns (json dict, model name) or (None, None). Never raises."""
    for name in models:
        try:
            msg = llm.model(name).invoke([("system", system), ("human", human)], config=config)
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            obj = _last_json(content, need_key)
            if obj is not None:
                return obj, name
        except Exception:  # noqa: BLE001  any model or network error -> next model
            continue
    return None, None


# ---------------------------------------------------------------- router
_KEYWORDS = [
    ("market", r"\bprice\b|\bstock\b|share price|\bclos(e|ed|ing)\b|\btrading\b|\breturns?\b|market cap|"
               r"52-week|\bvolume\b"),
    ("news", r"\bnews\b|\bheadlines?\b|\barticles?\b|\bpress\b"),
    ("fundamentals", r"second source|cross-check|cross check|\bconfirm|\bagree|\bmassive\b"),
    ("filings", r"\brevenue|\bsales\b|\bmargin|\beps\b|\bincome\b|\brisks?\b|10-k|\bfiling|\bbuybacks?\b|"
                r"\brepurchase|r&d|research and development|\bearnings\b|\bcash\b"),
]


def route_by_keywords(question: str) -> list[str]:
    q = (question or "").lower()
    picked = [name for name, rx in _KEYWORDS if re.search(rx, q)]
    return picked or ["filings"]


ROUTER_SYSTEM = """You route questions for Europa, a research assistant for financial analysts.
Pick which specialists are needed to answer the question:
- "filings": annual 10-K figures (revenue, margins, R&D, operating income, net income, EPS, cash, buybacks) and risk factors.
- "market": stock price, closing price, price returns over a period, 52-week range, market cap, trading volume.
- "fundamentals": whether a second source confirms or agrees with the reported annual figures.
- "news": recent headlines about the company.
Pick one or more. Questions about forecasts, price targets or "what will revenue be" go to "filings".

Set "advice" to true ONLY when the user asks what they should do with their money (buy, sell, hold,
get in, get out, how much to own) or wants a promise about returns. Forecasts, price targets and
"what will revenue be" are NOT advice.

Reply with JSON only:
{"specialists": ["filings"], "advice": false, "reason": "short reason"}"""


def route(inp: dict, config=None) -> dict:
    human = f"Ticker: {inp.get('ticker', '')}\nQuestion: {inp.get('question', '')}"
    obj, name = call_json(ROUTER_MODELS, ROUTER_SYSTEM, human, config, need_key="specialists")
    if obj is None or not isinstance(obj.get("specialists"), list):
        return {"specialists": route_by_keywords(inp.get("question", "")), "advice": False,
                "reason": "keyword routing (router model unavailable)", "model": None, "fallback": True}
    picked = []
    for s in obj["specialists"]:
        s = str(s).strip().lower()
        if s in KNOWN and s not in picked:
            picked.append(s)
    return {"specialists": picked or ["filings"], "advice": obj.get("advice") is True,
            "reason": str(obj.get("reason") or "")[:300], "model": name, "fallback": False}


# ---------------------------------------------------------------- news picker
NEWS_SYSTEM = """You pick the headlines most relevant to an analyst's question.
You see numbered headlines as "N1 | date | publisher | title". Return at most 3 ids, most relevant first.
Return ids only, never headline text.
Reply with JSON only: {"ids": ["N2", "N5"]}"""


def pick_news(inp: dict, config=None) -> dict:
    items = list(inp.get("items") or [])
    first3 = [it.get("id") for it in items[:3]]
    if len(items) <= 3:
        return {"ids": first3, "model": None}
    lines = [f"N{n} | {str(it.get('published_utc') or '')[:10]} | {it.get('publisher') or ''} | {it.get('title') or ''}"
             for n, it in enumerate(items, 1)]
    human = f"Question: {inp.get('question', '')}\n\nHeadlines:\n" + "\n".join(lines)
    obj, name = call_json(NEWS_MODELS, NEWS_SYSTEM, human, config, need_key="ids")
    if obj is None or not isinstance(obj.get("ids"), list):
        return {"ids": first3, "model": None}
    ids = []
    for raw in obj["ids"]:
        m = re.fullmatch(r"\s*N?(\d+)\s*", str(raw), re.I)
        if not m:
            continue
        k = int(m.group(1))
        if 1 <= k <= len(items):
            iid = items[k - 1].get("id")
            if iid not in ids:
                ids.append(iid)
        if len(ids) == 3:
            break
    return {"ids": ids or first3, "model": name}


# ---------------------------------------------------------------- writer
WRITER_SYSTEM = """You are Europa, a research assistant for financial analysts. You answer questions about one company
using ONLY the EVIDENCE SHEETS given to you (for example SEC 10-K facts, end-of-day market data, a second-source check).

Rules:
- Use only the evidence sheets. Never use outside knowledge. Never forecast, estimate or guess.
- If the evidence does not answer the question (forecasts, price targets, quarterly figures, anything not listed),
  set status to I_DONT_KNOW and say in one sentence that the data here doesn't cover it.
- No investment advice. Do not say whether to buy, sell or hold. Market numbers are past data, never a view on
  what to do.
- 2 to 4 short sentences. Do not show fact ids in the answer.
- Every number you write must be listed in claims with the fact_id it came from, and written exactly as the
  sheet shows it.
- Write time windows in words ("over the past year", "since the start of the year"), never digits, except "52-week".
- Write dates exactly as the sheet shows them (YYYY-MM-DD). Say "fiscal 2026" for fiscal years.
- Do not state counts of things.
- If a source is listed under UNAVAILABLE, do not guess that part and do not apologise at length; code adds a notice.
- Never describe news content. If headlines exist you may say that recent headlines are listed below.
- If you quote a filing, copy the words exactly and list it in claims as a quote with its filing year.

Reply with JSON only, no other text:
{"status": "ANSWERED" or "I_DONT_KNOW",
 "answer": "...",
 "claims": [{"value": "$478.8 billion", "fact_id": "revenue_fy2026"},
            {"type": "quote", "text": "exact words", "filing": "FY2026"}]}"""


def write(inp: dict, config=None) -> dict:
    from backend import ask  # lazy: ask may import desk, which imports agents

    sheets = "\n\n".join(f"EVIDENCE SHEET {n}:\n{s}" for n, s in enumerate(inp.get("sheets") or [], 1))
    unavailable = inp.get("unavailable") or []
    unav = ("UNAVAILABLE (do not guess these parts):\n" +
            "\n".join(f"- {u.get('agent')}: {u.get('reason')}" for u in unavailable)) if unavailable else ""
    news = "Recent headlines exist and are listed below your answer by code." if inp.get("has_news") else ""
    human = "\n\n".join(p for p in [
        f"Company: {inp.get('company') or inp.get('ticker')} ({inp.get('ticker')})", sheets, unav, news,
        f"Question: {inp.get('question', '')}"] if p)
    obj, name = call_json(WRITER_MODELS, WRITER_SYSTEM, human, config, need_key="answer")
    if obj is None:
        return {"draft": dict(FAIL_DRAFT, claims=[]), "model": None}
    draft = ask._parse(json.dumps(obj))
    if not isinstance(draft.get("answer"), str) or not isinstance(draft.get("claims"), list):
        return {"draft": dict(FAIL_DRAFT, claims=[]), "model": name}
    draft["claims"] = [c for c in draft["claims"] if isinstance(c, dict)]
    return {"draft": draft, "model": name}


# ---------------------------------------------------------------- skeptic
SKEPTIC_SYSTEM = """You are an adversarial reviewer from a different model family than the writer.
Try to refute each numbered sentence of the draft answer using ONLY the evidence sheets.

For each sentence give one verdict:
- "UNSUPPORTED" only if you can name the evidence line that contradicts it, or the sentence states a cause,
  a direction, a metric/year pairing or a comparison that the evidence does not support.
  A plain "larger / smaller / up / down / more than" statement IS supported when the numbers on the sheets make it true.
- "ADVICE" if it tells the reader what to do with money or predicts returns.
- Otherwise "SUPPORTED".
Sentences that only say data is unavailable, or that Europa does not know, are always SUPPORTED.

Reply with JSON only:
{"verdicts": [{"i": 1, "verdict": "SUPPORTED", "why": ""}]}"""


def review(inp: dict, config=None) -> dict:
    sentences = list(inp.get("sentences") or [])
    sheets = "\n\n".join(f"EVIDENCE SHEET {n}:\n{s}" for n, s in enumerate(inp.get("sheets") or [], 1))
    numbered = "\n".join(f"{n}. {s}" for n, s in enumerate(sentences, 1))
    human = f"Question: {inp.get('question', '')}\n\n{sheets}\n\nDRAFT SENTENCES:\n{numbered}"
    obj, name = call_json(SKEPTIC_MODELS, SKEPTIC_SYSTEM, human, config, need_key="verdicts")
    if obj is None or not isinstance(obj.get("verdicts"), list):
        return {"status": "unavailable", "verdicts": [], "model": None}
    out, seen = [], set()
    for v in obj["verdicts"]:
        if not isinstance(v, dict):
            continue
        try:
            i = int(v.get("i"))
        except (TypeError, ValueError):
            continue
        verdict = str(v.get("verdict", "")).strip().upper()
        if not 1 <= i <= len(sentences) or verdict not in VERDICTS or i in seen:
            continue
        seen.add(i)
        out.append({"i": i, "verdict": verdict, "why": str(v.get("why") or "")[:300]})
    return {"status": "ok", "verdicts": out, "model": name}


ROUTER = RunnableLambda(route, name="desk_router")
NEWS_PICKER = RunnableLambda(pick_news, name="news_picker")
WRITER = RunnableLambda(write, name="write_answer")
SKEPTIC = RunnableLambda(review, name="skeptic_review")

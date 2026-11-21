"""Europa research desk: the multi-agent pipeline behind POST /api/ask (EUROPA_PIPELINE=multi).

guard_advice -> desk_router -> specialists (parallel) -> news_picker -> council (three models, compare, merge;
EUROPA_COUNCIL=off or a council failure -> write_answer) -> skeptic_review -> number_gate -> notices. Every step is a named LangChain Runnable, so PRISM records each one.
Code has the last word: the gate checks every number, and code (not a model) writes the
"X is unavailable" sentences. ask.answer() falls back to the single-agent path if this raises.
"""
import contextvars
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from langchain_core.runnables import RunnableLambda, RunnableParallel

from backend import ask, checker, facts, tracing

LABELS = {"filings": "SEC filing data", "market": "Market data", "fundamentals": "The second-source check",
          "news": "News"}
SOURCE_NAMES = {"filings": "SEC EDGAR 10-K", "market": "Massive market data (end-of-day)",
                "fundamentals": "Massive income statements (second source)", "news": "Massive news headlines"}
WRITABLE = ("filings", "market", "fundamentals")
_TICKER_RX = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_SECOND_SOURCE_RX = re.compile(r"second (data )?source|another source|independent source|cross-?\s?check", re.I)
_OTHER_COMPANY_REASON = "the question is about a company other than the ticker's company"  # = specialists.OTHER_COMPANY_REASON
_HEADLINE_POINTER_RX = re.compile(r"\bheadlines?\b[^.]*\blisted below\b", re.I)
_PACK_DEFAULTS = {"reason": None, "source": None, "as_of": None, "facts": {}, "exclude": [], "sheet": "",
                  "allow_text": [], "allow_dates": [], "items": [], "crosscheck": [], "seconds": 0.0}


# ---------------------------------------------------------------- guard (code, no model)
def _massive():
    try:
        from backend import massive
        return massive
    except Exception:  # noqa: BLE001  module missing or broken -> treat Massive as not available
        return None


def _valid_ticker(t: str) -> bool:
    m = _massive()
    try:
        return bool(m.valid_ticker(t)) if m else bool(_TICKER_RX.match(t))
    except Exception:  # noqa: BLE001
        return bool(_TICKER_RX.match(t))


def _massive_available() -> bool:
    m = _massive()
    try:
        return bool(m and m.available())
    except Exception:  # noqa: BLE001
        return False


# Advice questions: never a bare "no". Decline in one line, then give checked facts and what to weigh.
# Fixed code text with no digits, so the number gate has nothing to cut in it.
ADVICE_OPEN = ("Here is what Apple's latest annual report (10-K) shows, and what it means for someone looking at the "
               "stock:")
ADVICE_CLOSE = ("Things to weigh before deciding: how the share price compares with these profits (valuation), how "
                "much of your money is already in one stock, whether you could hold through a drop, and the new "
                "risks listed in the filing. Europa shows what the filing says; the buy or sell decision stays "
                "with you.")
ADVICE_NO_FACTS = ("A view on a stock starts from the company's own reports. Ask about Apple's filings, for example "
                   "how revenue, profit, margins or risk factors changed, and every number in the answer is checked "
                   "against the 10-K.")
ADVICE_PROMISE = ("No one can honestly promise what a stock will do, and Europa does not make buy or sell calls. "
                  "What it can show is what the filing says, with every number checked.")
ADVICE_OTHER = ("In this demo Europa has checked filing figures for Apple (AAPL), and market data for {who} stock is "
                "not available right now. An analyst deciding on a stock would check: revenue and profit growth in "
                "the latest annual report, profit margins, cash and debt, the risk factors, and how the share price "
                "compares with earnings. Ask the same question about Apple to see how Europa answers it with checked "
                "numbers. Europa shows what the data says; the buy or sell decision stays with you.")
# Another company with market data (Massive): checked price figures in place of the fixed text.
ADVICE_MKT_OPEN = "Here is the latest market data for {name}, and what it means for someone looking at the stock:"
ADVICE_MKT_CLOSE = ("Filing figures (sales, profit, margins) are checked for Apple in this demo; for {name} this "
                    "summary uses market data. Things to weigh before deciding: the company's sales and profit growth "
                    "in its latest annual report, how the share price compares with earnings (valuation), how much "
                    "of your money is already in one stock, and whether you could hold through a drop. Europa shows "
                    "what the data says; the buy or sell decision stays with you.")
ADVICE_MKT_Q = ("How has {name} stock done recently? Give the latest closing price, the recent price returns and the "
                "52-week high and low. End with a 'What it means:' sentence and a 'Deeper read:' sentence.")
# The question the desk answers, fully checked, in place of "should I buy Apple?"
ADVICE_BRIEF_Q = ("Summarize Apple's fiscal 2026 versus fiscal 2025 in plain English: revenue growth, net income, "
                  "gross margin, diluted EPS, cash returned through buybacks, and any new risk factor. "
                  "End with a 'What it means:' sentence and a 'Deeper read:' sentence.")
_PROMISE_RX = re.compile(r"\b(guarantee[ds]?|promise[ds]?)\b", re.I)
_OTHER_NAMES = {"nvidia": "Nvidia", "tesla": "Tesla", "microsoft": "Microsoft", "amazon": "Amazon",
                "google": "Google", "alphabet": "Alphabet", "meta": "Meta", "samsung": "Samsung",
                "netflix": "Netflix", "amd": "AMD", "intel": "Intel", "anthropic": "Anthropic", "openai": "OpenAI"}
_OTHER_TICKERS = {"NVDA": "Nvidia", "TSLA": "Tesla", "MSFT": "Microsoft", "AMZN": "Amazon", "GOOGL": "Alphabet",
                  "GOOG": "Alphabet", "META": "Meta", "NFLX": "Netflix", "AMD": "AMD", "INTC": "Intel"}
_OTHER_RX = re.compile(r"\b(" + "|".join(_OTHER_NAMES) + r"|" + "|".join(t.lower() for t in _OTHER_TICKERS) + r")\b",
                       re.I)
_APPLE_RX = re.compile(r"\b(apple|aapl)\b", re.I)
# Wordings ask._ADVICE misses: "do i invest in nvidia right now", "is it a good time to buy", "worth buying".
_ADVICE_MORE = [r"\b(do|can|could|would|shall|must) (i|we)\b[^?]*\b(buy|sell|invest)\b",
                r"\b(good|bad|right|smart|best) (time|moment|idea) to (buy|sell|invest)\b",
                r"\bworth (buying|investing|owning)\b",
                r"\binvest(ing)? in (" + "|".join(_OTHER_NAMES) + r")\b"]


def _ticker_name(ticker: str):
    """Company name for a ticker: the built-in names first, then Massive's reference record (cached; the market
    specialist fetches the same record). None when unknown. Names holding a digit are skipped so code-written
    notices stay digit-free."""
    known = facts.COMPANY.get(ticker) or _OTHER_TICKERS.get(ticker)
    if known or not _valid_ticker(ticker):
        return known
    try:
        from backend import massive
        if not massive.available():
            return None
        name = str(massive.ticker_overview(ticker).get("name") or "").strip()
    except Exception:
        return None
    return name if name and not re.search(r"\d", name) and len(name) <= 60 else None


def _other_company(ticker: str, question: str):
    """The other company an advice question is about, as "Nvidia's" / "that company's"; None when it is about Apple."""
    m = _OTHER_RX.search(question)
    if m and not _APPLE_RX.search(question):
        w = m.group(1).lower()
        return (_OTHER_NAMES.get(w) or _OTHER_TICKERS.get(w.upper()) or "that company") + "'s"
    if ticker not in facts.FACTS:
        return (_OTHER_TICKERS.get(ticker) + "'s") if ticker in _OTHER_TICKERS else "that company's"
    return None


def _other_target(ticker: str, question: str):
    """(name, ticker or None) of the other company an advice question is about; None when it is about Apple."""
    m = _OTHER_RX.search(question)
    if m and not _APPLE_RX.search(question):
        w = m.group(1).lower()
        name = _OTHER_NAMES.get(w) or _OTHER_TICKERS.get(w.upper())
        tk = w.upper() if w.upper() in _OTHER_TICKERS else (
            "GOOGL" if name == "Google" else next((t for t, n in _OTHER_TICKERS.items() if n == name), None))
        return name, tk
    if ticker not in facts.FACTS:
        return _ticker_name(ticker) or (ticker if _valid_ticker(ticker) else "that company"), (
            ticker if _valid_ticker(ticker) else None)
    return None


def _advice_answer(ticker, question: str = "") -> str:
    """The fixed part of an advice reply, used when checked facts cannot be added."""
    who = _other_company(ticker, question)
    if who:
        return ADVICE_OTHER.format(who=who)
    if _PROMISE_RX.search(question):
        return ADVICE_PROMISE + " " + ADVICE_CLOSE
    return ADVICE_NO_FACTS + " " + ADVICE_CLOSE


def _guard(inp: dict) -> dict:
    q = inp["question"].lower()
    t = inp["ticker"]
    if not inp.get("_brief") and any(re.search(p, q) for p in list(ask._ADVICE) + _ADVICE_MORE):
        return {**inp, "stop": ("DECLINED_ADVICE", _advice_answer(t, inp["question"]))}
    if any(re.search(p, q) for p in ask._INVENT):
        return {**inp, "stop": ("I_DONT_KNOW", ask.INVENT_ANSWER)}
    if not _valid_ticker(t) or (t not in facts.FACTS and not _massive_available()):
        return {**inp, "stop": ("I_DONT_KNOW", f"This demo only has filings loaded for AAPL, not {t}.")}
    return inp


GUARD = RunnableLambda(_guard, name="guard_advice")


# ---------------------------------------------------------------- dependencies
@dataclass
class Deps:
    guard: Any
    router: Any
    specialists: dict
    news_picker: Any
    writer: Any
    skeptic: Any
    gate: Any
    council: Any = None  # three-model council (backend/council.py); None = single writer, as before
    council_writer: Any = None  # the writer the council stands in for; if deps.writer was swapped, no council


def default_deps() -> Deps:
    from backend import agents, gate, specialists  # lazy: built by other builders
    council_run = None
    if _council_on():  # the council module is only loaded when it is switched on
        from backend import council
        council_run = council.COUNCIL
    return Deps(guard=GUARD, router=agents.ROUTER, specialists=dict(specialists.SPECIALISTS),
                news_picker=agents.NEWS_PICKER, writer=agents.WRITER, skeptic=agents.SKEPTIC, gate=gate.GATE,
                council=council_run, council_writer=agents.WRITER)


def _council_on() -> bool:
    """EUROPA_COUNCIL=on -> the three-model council. Default (2026-11-21 review): off, the single writer."""
    return os.environ.get("EUROPA_COUNCIL", "off").strip().lower() in ("on", "1", "true", "yes")


_DEPS: contextvars.ContextVar = contextvars.ContextVar("europa_desk_deps", default=None)


# ---------------------------------------------------------------- helpers
def _unavailable_pack(agent: str, reason: str, seconds: float = 0.0) -> dict:
    return {"agent": agent, "status": "unavailable", **_PACK_DEFAULTS, "reason": reason,
            "facts": {}, "exclude": [], "allow_text": [], "allow_dates": [], "items": [], "crosscheck": [],
            "seconds": seconds}


def _normalize(agent: str, pack) -> dict:
    if not isinstance(pack, dict):
        return _unavailable_pack(agent, f"internal error in {agent}")
    out = {**_PACK_DEFAULTS, **pack, "agent": agent}
    if out.get("status") != "ok":
        out["status"] = "unavailable"
        out["reason"] = out.get("reason") or f"{agent} returned no data"
    for k in ("facts",):
        out[k] = out[k] if isinstance(out[k], dict) else {}
    for k in ("exclude", "allow_text", "allow_dates", "items", "crosscheck"):
        out[k] = list(out[k]) if isinstance(out[k], (list, tuple)) else []
    out["sheet"] = out["sheet"] if isinstance(out["sheet"], str) else ""
    return out


def _safe_specialist(agent: str, runnable):
    def run(inp: dict, config) -> dict:
        t0 = time.time()
        try:
            if runnable is None:
                raise RuntimeError("missing specialist")
            pack = runnable.invoke(inp, config=config) if hasattr(runnable, "invoke") else runnable(inp)
            return _normalize(agent, pack)
        except Exception:  # noqa: BLE001  a specialist never crashes the desk
            return _unavailable_pack(agent, f"internal error in {agent}", round(time.time() - t0, 2))
    return RunnableLambda(run, name=f"run_{agent}")


def _strip_excluded(sheet: str, exclude: set) -> str:
    if not exclude:
        return sheet
    return "\n".join(line for line in sheet.split("\n")
                     if not any(line.startswith(f"{fid} |") for fid in exclude))


def _row(name, kind, model=None, status="ok", note="", seconds=0.0) -> dict:
    return {"name": name, "kind": kind, "model": model, "status": status, "note": note,
            "seconds": round(seconds, 2)}


def _timed(runnable, inp, config):
    t0 = time.time()
    out = runnable.invoke(inp, config=config)
    return out, time.time() - t0


NOTICE_FILINGS_OTHER = ("Filing figures (sales, profit, margins) are checked for Apple in this demo; for {who} this "
                        "answer uses market data.")
NOTICE_FILINGS_OTHER_ASK = ("Filing figures (sales, profit, margins) are checked for Apple in this demo. For {who}, ask "
                            "about the share price, recent returns or the yearly range to get checked market data.")
NOTICE_NEWS_OTHER = "Europa's headlines follow the selected ticker ({ticker}), so none are listed for another company."
NOTICE_OUTAGE = "{source} did not respond just now, so that part is left out rather than guessed."
NOTICE_NOTHING = "Europa has no checked data for that question yet."


def _notices(inp: dict) -> dict:
    """Code-written sentences appended to the answer. No model is asked to admit a gap."""
    packs, claims = inp["packs"], inp["claims"]
    out = []
    asked_second = bool(_SECOND_SOURCE_RX.search(inp.get("question") or ""))
    for p in packs:
        if p["status"] != "ok":
            if p["agent"] == "fundamentals" and not asked_second:
                continue  # an extra check the user did not ask for: shown in sources/unavailable, not in the answer
            # Jayden 2026-11-21: notices say what the answer is built on, never "I can't". Fixed text, no digits.
            if p["agent"] == "filings" and inp.get("ticker") and inp["ticker"] not in facts.FACTS:
                # not an outage: filing figures are only loaded for AAPL; other tickers are answered from market data
                who = _ticker_name(inp["ticker"]) or inp["ticker"]
                has_market = any(q["agent"] == "market" and q["status"] == "ok" for q in packs)
                out.append(NOTICE_FILINGS_OTHER.format(who=who) if has_market
                           else NOTICE_FILINGS_OTHER_ASK.format(who=who))
                continue
            if p["agent"] == "news" and p.get("reason") == _OTHER_COMPANY_REASON:
                out.append(NOTICE_NEWS_OTHER.format(ticker=inp.get("ticker") or "the selected ticker"))
                continue
            # fixed text only: a raw error reason ("HTTP 503") would put an unchecked number after the gate
            out.append(NOTICE_OUTAGE.format(source=LABELS.get(p["agent"], "A data source")))
    by = {p["agent"]: p for p in packs}
    market = by.get("market")
    if market and market["status"] == "ok":
        mc = [c for c in claims if isinstance(c, dict) and c.get("provider") == "massive"]
        if mc:
            # the pack's as_of is the last trading day; a market-cap fact's as_of is only the fetch date
            as_of = market.get("as_of") or next((c.get("as_of") for c in mc if c.get("as_of")), None) or "unknown date"
            out.append(f"Market data: Massive, end-of-day, as of {as_of}.")
    exclude = {fid for p in packs for fid in p.get("exclude", [])}
    fund = by.get("fundamentals")
    cc = (fund or {}).get("crosscheck") or []
    all_agree = bool(cc) and all(isinstance(c, dict) and c.get("result") == "AGREE" for c in cc)
    if fund and fund["status"] == "ok" and not exclude:
        if all_agree:  # every compared figure matched; anything less is not "confirmed"
            out.append("The annual figures were confirmed by a second source (Massive).")
        elif asked_second:
            out.append("The second source (Massive) did not have every figure for both fiscal years, "
                       "so Europa can't say the annual figures were confirmed.")
    if exclude:
        out.append("Two sources disagreed on some figures, so Europa left those out.")
    if inp.get("council_disputed"):
        out.append("The council models disagreed on some figures, so Europa left those out.")
    body = inp["answer"].strip()
    if inp.get("news") and inp.get("writer_ran"):
        # the code notice replaces any writer sentence that only points at the headlines (delete-only, no digits)
        body = " ".join(s for s in checker.split_sentences(body)
                        if not (_HEADLINE_POINTER_RX.search(s) and not re.search(r"\d", s)))
        out.append("Recent headlines are listed below, unverified.")
    answer = " ".join(s for s in [body, *out] if s) or NOTICE_NOTHING
    return {**inp, "answer": answer, "added": len(out)}


NOTICES = RunnableLambda(_notices, name="notices")


# ---------------------------------------------------------------- pipeline
def _pipeline(inp: dict, config) -> dict:
    """Advice about Apple is declined in one line, then answered with a checked summary (the normal desk run on
    ADVICE_BRIEF_Q: skeptic and number gate as usual) and a list of things to weigh. Status stays DECLINED_ADVICE."""
    out = _pipeline_core(inp, config)
    ticker, question = inp["ticker"], inp["question"]
    if out.get("status") != "DECLINED_ADVICE" or inp.get("_brief") or _PROMISE_RX.search(question):
        return out
    other = _other_target(ticker, question)
    if other:  # another company: checked market data when there is some, else the fixed text already in `out`
        name, tk = other
        if not tk or not _massive_available():
            return out
        brief = _pipeline_core({"ticker": tk, "question": ADVICE_MKT_Q.format(name=name), "_brief": True}, config)
        if brief.get("status") != "ANSWERED" or not (brief.get("answer") or "").strip():
            return out
        return {**brief, "status": "DECLINED_ADVICE", "agents": out["agents"] + brief["agents"], "_company": name,
                "answer": ADVICE_MKT_OPEN.format(name=name) + "\n\n" + brief["answer"].strip() + "\n\n"
                          + ADVICE_MKT_CLOSE.format(name=name)}
    brief = _pipeline_core({"ticker": ticker, "question": ADVICE_BRIEF_Q, "_brief": True}, config)
    if brief.get("status") != "ANSWERED" or not (brief.get("answer") or "").strip():
        return out
    return {**brief, "status": "DECLINED_ADVICE", "agents": out["agents"] + brief["agents"],
            "answer": ADVICE_OPEN + "\n\n" + brief["answer"].strip() + "\n\n" + ADVICE_CLOSE}


def _pipeline_core(inp: dict, config) -> dict:
    deps = _DEPS.get() or default_deps()
    ticker, question = inp["ticker"], inp["question"]
    agents, models = [], []

    def used(m):
        if m and m not in models:
            models.append(m)

    base = {"pipeline": "multi", "agents": agents, "models": models, "sources": [], "unavailable": [],
            "news": [], "skeptic": {"status": "off", "model": None, "refuted": 0, "flags": []},
            "market_chosen": False, "market_as_of": None}

    # 1. guard
    g, s = _timed(deps.guard, inp, config)
    if "stop" in g:
        agents.append(_row("guard_advice", "code", status="ok", note="stopped the question", seconds=s))
        status, answer = g["stop"]
        return {**base, "status": status, "answer": answer, "claims": [], "blocked": []}
    agents.append(_row("guard_advice", "code", note="passed", seconds=s))

    # 2. router
    r, s = _timed(deps.router, {"ticker": ticker, "question": question}, config)
    used(r.get("model"))
    chosen = [n for n in r.get("specialists") or [] if n in LABELS] or ["filings"]
    agents.append(_row("desk_router", "model", r.get("model"), "ok" if not r.get("fallback") else "unavailable",
                       ("keyword fallback: " if r.get("fallback") else "picked ") + ", ".join(chosen), s))
    if r.get("advice") is True and not inp.get("_brief"):
        return {**base, "status": "DECLINED_ADVICE", "answer": _advice_answer(ticker, question), "claims": [],
                "blocked": []}

    # 3. specialists in parallel
    par = RunnableParallel({n: _safe_specialist(n, deps.specialists.get(n)) for n in chosen}).with_config(
        run_name="specialists")
    raw, s = _timed(par, {"ticker": ticker, "question": question}, config)
    packs = [_normalize(n, raw.get(n)) for n in chosen]
    for p in packs:
        agents.append(_row(f"specialist_{p['agent']}", "code", status=p["status"],
                           note=p["reason"] or (f"as of {p['as_of']}" if p.get("as_of") else ""),
                           seconds=float(p.get("seconds") or 0)))
    by = {p["agent"]: p for p in packs}
    ok_packs = [p for p in packs if p["status"] == "ok"]
    unavailable = [{"agent": p["agent"], "reason": p["reason"]} for p in packs if p["status"] != "ok"]
    sources = [{"name": p.get("source") or SOURCE_NAMES[p["agent"]], "status": p["status"],
                "as_of": p.get("as_of"), "reason": p.get("reason")} for p in packs]
    market = by.get("market")
    base.update(sources=sources, unavailable=unavailable, market_chosen=market is not None,
                market_as_of=market.get("as_of") if market and market["status"] == "ok" else None)

    # 4. news picker
    news = []
    npack = by.get("news")
    if npack and npack["status"] == "ok" and npack["items"]:
        items = npack["items"]
        picked, s = _timed(deps.news_picker, {"question": question, "items": items}, config)
        used(picked.get("model"))
        ids = set(picked.get("ids") or [])
        news = [{k: it.get(k) for k in ("title", "publisher", "published_utc", "url")}
                for it in items if it.get("id") in ids][:3]
        agents.append(_row("news_picker", "model", picked.get("model"),
                           note=f"picked {len(news)} headline(s)" if picked.get("model") else "first headlines, no model",
                           seconds=s))
    base["news"] = news

    # 5. nothing writable -> fixed answer, no writer / skeptic / gate
    writable = [p for p in ok_packs if p["agent"] in WRITABLE and p["sheet"].strip()]
    writer_ran = False
    council_exclude: list = []
    if not writable:
        if npack and npack["status"] == "ok":
            status = "ANSWERED"
            answer = (f"Here are recent headlines for {ticker}, word for word from their publishers. "
                      "Europa has not verified them.")
            dates = sorted(d for d in (str(n.get("published_utc") or "")[:10] for n in news)
                           if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d))
            if dates:  # fixed, code-built text added with no gate step in this branch; says how fresh the list is
                answer += f" The newest one is dated {dates[-1]}."
        else:
            status, answer = "I_DONT_KNOW", ""  # the notices say what is and is not covered; see NOTICE_NOTHING
        claims, blocked = [], []
    else:
        # 6. writer
        exclude = {fid for p in packs for fid in p.get("exclude", [])}
        sheets = [_strip_excluded(p["sheet"], exclude) if p["agent"] == "filings" else p["sheet"] for p in writable]
        winp = {"ticker": ticker, "company": facts.COMPANY.get(ticker), "question": question,
                "long_form": bool(inp.get("_brief")),  # the summary behind an advice question keeps the strong writer
                "sheets": sheets, "unavailable": unavailable, "has_news": bool(news)}
        w = None
        swapped = deps.council_writer is not None and deps.writer is not deps.council_writer
        if deps.council is not None and _council_on() and not swapped:
            # 6a. council: three models answer separately, code compares their numbers, one merged draft
            try:
                c = deps.council.invoke(winp, config=config)
            except Exception:  # noqa: BLE001  the council never crashes the desk
                c = None
            c = c if isinstance(c, dict) else {}
            agents.extend(r for r in (c.get("rows") or []) if isinstance(r, dict))
            if isinstance(c.get("council"), dict):
                base["council"] = c["council"]
            if c.get("ok") is True and isinstance(c.get("draft"), dict):
                for m in c.get("models") or []:
                    used(m)
                w = {"draft": c["draft"], "model": c.get("model"), "council": True}
                council_exclude = [str(x) for x in (c.get("exclude") or [])]
        if w is None:  # council off, or fewer than two members answered: the single writer, as before
            w, s = _timed(deps.writer, winp, config)
        writer_ran = True
        w = w if isinstance(w, dict) else {}
        used(w.get("model"))
        draft = w.get("draft")
        if not isinstance(draft, dict) or not isinstance(draft.get("answer"), str):  # fail closed on a bad draft
            draft = {"status": "I_DONT_KNOW", "answer": "I couldn't produce a checked answer to that.", "claims": []}
        draft = dict(draft)
        draft["claims"] = [c for c in draft["claims"] if isinstance(c, dict)] if isinstance(
            draft.get("claims"), list) else []
        if draft.get("status") not in ("ANSWERED", "I_DONT_KNOW"):
            draft["status"] = "ANSWERED"
        if not w.get("council"):
            agents.append(_row("write_answer", "model", w.get("model"), "ok" if w.get("model") else "unavailable",
                               f"draft {draft.get('status')}", s))

        if ask.checker_on():
            # 7. skeptic + gate
            mode = os.environ.get("EUROPA_SKEPTIC", "enforce").strip().lower()
            if mode not in ("enforce", "advisory", "off"):
                mode = "enforce"
            verdicts, sk = [], {"status": "off", "model": None, "refuted": 0, "flags": []}
            if mode != "off":
                sentences = checker.split_sentences(draft.get("answer", ""))
                rv, s = _timed(deps.skeptic, {"ticker": ticker, "company": facts.COMPANY.get(ticker),
                                              "question": question, "sheets": sheets, "sentences": sentences}, config)
                rv = rv if isinstance(rv, dict) else {}
                used(rv.get("model"))
                verdicts = rv.get("verdicts") if rv.get("status") == "ok" else []
                verdicts = [v for v in verdicts if isinstance(v, dict)] if isinstance(verdicts, list) else []
                if rv.get("status") != "ok" and mode == "enforce":  # fail closed: no review, no number sentences
                    verdicts = [{"i": i, "verdict": "UNSUPPORTED", "why": "skeptic unavailable"}
                                for i, t in enumerate(sentences, 1) if re.search(r"\d", str(t))]
                flags = [v for v in verdicts if v.get("verdict") != "SUPPORTED"]
                sk = {"status": ("advisory" if mode == "advisory" else "ok") if rv.get("status") == "ok"
                      else "unavailable", "model": rv.get("model"), "refuted": 0,
                      "flags": flags if mode == "advisory" else []}
                agents.append(_row("skeptic_review", "model", rv.get("model"),
                                   "ok" if rv.get("status") == "ok" else "unavailable",
                                   f"{mode}: {len(flags)} flagged of {len(sentences)}", s))
            # every pack goes to the gate: it reads facts only from ok packs, exclusions from all of them
            # figures the council disputed go in as exclusions, so code cuts any sentence that still uses one
            gate_packs = packs + ([{"agent": "council", "status": "unavailable", "exclude": council_exclude}]
                                  if council_exclude else [])
            g, s = _timed(deps.gate, {"ticker": ticker, "draft": draft, "packs": gate_packs,
                                      "verdicts": verdicts if mode == "enforce" else []}, config)
            res = g["result"]
            status, answer = res["status"], res.get("answer", "")
            claims, blocked = list(res.get("claims") or []), list(res.get("blocked") or [])
            sk["refuted"] = sum(1 for b in blocked if isinstance(b, dict) and b.get("status") == "REFUTED")
            base["skeptic"] = sk
            agents.append(_row("number_gate", "code", note=f"{len(blocked)} sentence(s) cut", seconds=s))
        else:
            # PRISM BEFORE run: model output shown unchecked, exactly like ask._pipeline
            status, answer, blocked = draft["status"], draft.get("answer", ""), []
            claims = [{"text": str(c.get("value") or c.get("text")), "status": "UNCHECKED"}
                      for c in draft["claims"] if isinstance(c, dict)]

    # 8. notices
    n, s = _timed(NOTICES, {"answer": answer, "packs": packs, "claims": claims, "news": news,
                            "writer_ran": writer_ran, "question": question, "ticker": ticker,
                            "council_disputed": bool(council_exclude) and ask.checker_on()}, config)
    agents.append(_row("notices", "code", note=f"{n['added']} notice(s)", seconds=s))
    return {**base, "status": status, "answer": n["answer"], "claims": claims, "blocked": blocked}


PIPELINE = RunnableLambda(_pipeline, name="europa_desk")


def answer(ticker: str, question: str, deps: Deps | None = None) -> dict:
    ticker = ticker.strip().upper()
    sid = f"ask-{uuid.uuid4().hex[:12]}"
    t0 = time.time()
    token = _DEPS.set(deps)
    try:
        with tracing.session(sid):
            out = PIPELINE.invoke({"ticker": ticker, "question": question.strip()},
                                  config={"callbacks": tracing.callbacks(), "run_name": "europa_desk",
                                          "metadata": {"ticker": ticker, "checker": ask.checker_on(),
                                                       "pipeline": "multi"}})
    finally:
        _DEPS.reset(token)
    market_chosen, as_of = out.pop("market_chosen", False), out.pop("market_as_of", None)
    data_note = ask.DATA_NOTE
    if market_chosen:
        data_note += f" Market data: Massive, end-of-day, as of {as_of}." if as_of else " Market data: unavailable."
    # the company the answer is about: Apple's SEC name, or the other company an advice question named
    company = out.pop("_company", None) or _ticker_name(ticker)
    return {**out, "company": company,
            "prism_session_id": sid if tracing.handler else None,
            "models": out.get("models") or [], "checker": "on" if ask.checker_on() else "off",
            "seconds": round(time.time() - t0, 2), "data_note": data_note, "disclaimer": ask.DISCLAIMER}

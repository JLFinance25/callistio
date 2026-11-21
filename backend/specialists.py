"""Four code-only specialists for the Europa research desk. No model calls.

Each takes {"ticker", "question"} and returns an evidence pack (PLAN-MULTIAGENT.md section 3).
They never raise: any failure becomes status "unavailable" with a plain-English reason.
All maths is done here in Python; every fact carries source, as_of, provider and display text.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from langchain_core.runnables import RunnableLambda

from backend import facts as sec
from backend import massive

SEC_SOURCE = "SEC EDGAR 10-K (XBRL facts and filing text)"
SEC_DATES = {"AAPL": ["2026-09-26", "2025-09-27", "2026-11-03"]}
PERIODS = {"AAPL": {"fy2026": "2026-09-26", "fy2025": "2025-09-27"}}
FIELD_MAP = [  # Massive field -> SEC fact key prefix
    ("revenue", "revenue"),
    ("gross_profit", "gross_margin"),
    ("research_development", "rnd"),
    ("operating_income", "operating_income"),
    ("consolidated_net_income_loss", "net_income"),
    ("diluted_earnings_per_share", "eps_diluted"),
]
GM_PCT_IDS = ["gross_margin_pct_fy2026", "gross_margin_pct_fy2025", "gross_margin_pct_change_pts"]
ADVICE_HEADLINE = re.compile(
    r"\b(buy(ing)?|sell(ing)?(?![- ]?off)|hold|price target|upgrade[sd]?|downgrade[sd]?|should you|"
    r"stocks? to (buy|own|watch)|millionaire|for investors|right now|no-brainer|unstoppable|"
    r"could (have )?(become|make you|turn|set you|supercharge)|set you up|passive income|(where|what) will|"
    r"will (soar|skyrocket|crash)|predictions?|best [a-z ]*stocks?)\b|\$\s?[\d,]+ (in|invested|investment)\b",
    re.IGNORECASE)
# A headline with a number claim would put an unverified number on screen, so it is dropped (dates are fine).
NUMBER_HEADLINE = re.compile(
    r"[$%\u20ac\u00a3]|\d[\d,.]*\s*-?\s*(percent|pct|million|billion|trillion|bn|mn|points?|bps|fold|x)\b",
    re.IGNORECASE)


def _mentions_company(title: str, ticker: str) -> bool:
    """True if the title names the ticker or the first word of the company name ("Apple").
    If no company name is loaded for the ticker there is nothing to match, so the headline is kept."""
    name = (sec.COMPANY.get(ticker) or "").split(" ")[0]
    if not name:
        return True
    words = [w for w in (name, ticker) if w]
    return any(re.search(rf"\b{re.escape(w)}\b", title, re.IGNORECASE) for w in words)


# ---------- helpers ----------

def _pack(agent: str, **kw) -> dict:
    p = {"agent": agent, "status": "unavailable", "reason": None, "source": "", "as_of": None,
         "facts": {}, "exclude": [], "sheet": "", "allow_text": [], "allow_dates": [], "items": [],
         "crosscheck": [], "seconds": 0.0}
    p.update(kw)
    return p


def _safe(agent: str, source: str, fn, inp: dict) -> dict:
    t0 = time.perf_counter()
    try:
        pack = fn(inp)
    except massive.Unavailable as e:
        pack = _pack(agent, source=source, reason=e.reason)
    except Exception:  # never raise out of a specialist
        pack = _pack(agent, source=source, reason=f"internal error in {agent}")
    pack["seconds"] = round(time.perf_counter() - t0, 3)
    return pack


def _ticker(inp) -> str:
    return str((inp or {}).get("ticker") or "").strip().upper()


def _q(x, places: str) -> Decimal:
    return Decimal(repr(float(x))).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def fmt_price(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}${_q(abs(v), '0.01'):,}"


def fmt_pct(v: float) -> str:
    return f"{_q(v, '0.1'):+}%"


def fmt_usd_big(v: float) -> str:
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e12:
        return f"{sign}${_q(a / 1e12, '0.01')} trillion"
    if a >= 1e9:
        return f"{sign}${_q(a / 1e9, '0.1')} billion"
    return f"{sign}${_q(a / 1e6, '0.1')} million"


def fmt_count(v: float) -> str:
    a = abs(v)
    if a >= 1e9:
        return f"{_q(a / 1e9, '0.01')} billion"
    if a >= 1e6:
        return f"{_q(a / 1e6, '0.1')} million"
    return f"{int(_q(a, '1')):,}"


def _fact(label, value, kind, source, as_of, display) -> dict:
    return {"label": label, "value": value, "kind": kind, "source": source, "as_of": as_of,
            "provider": "massive", "display": display}


def _ret(now_c: float, base_c: float) -> float | None:
    if not base_c:
        return None
    return round((now_c / base_c - 1.0) * 100.0, 4)


# ---------- filings ----------

def _filings(inp: dict) -> dict:
    t = _ticker(inp)
    if t not in sec.FACTS:
        return _pack("filings", source=SEC_SOURCE, reason=f"no SEC filings are loaded for {t} in this demo")
    return _pack("filings", status="ok", source=SEC_SOURCE, sheet=sec.fact_sheet(t),
                 allow_dates=list(SEC_DATES.get(t, [])))


def filings(inp: dict) -> dict:
    return _safe("filings", SEC_SOURCE, _filings, inp)


# ---------- market ----------

MARKET_SOURCE = "Massive market data (end-of-day)"


def _price_facts(bars: list[dict]) -> tuple[dict, str]:
    last = bars[-1]
    as_of = last["date"]
    src = f"Massive market data (end-of-day, as of {as_of})"
    rsrc = f"Calculated from Massive daily closes (split-adjusted, dividends not included, as of {as_of})"
    f: dict = {}
    for fid, key, label in [("px_close_last", "c", "Closing price, last trading day"),
                            ("px_open_last", "o", "Opening price, last trading day"),
                            ("px_high_last", "h", "Intraday high, last trading day"),
                            ("px_low_last", "l", "Intraday low, last trading day")]:
        f[fid] = _fact(label, last[key], "usd_per_share", src, as_of, fmt_price(last[key]))
    if len(bars) >= 2:
        pc = bars[-2]["c"]
        f["px_close_prior"] = _fact("Closing price, trading day before", pc, "usd_per_share", src, as_of,
                                    fmt_price(pc))
    if last["v"] >= 1_000_000:
        f["volume_last"] = _fact("Shares traded, last trading day", last["v"], "count", src, as_of,
                                 fmt_count(last["v"]))

    c = last["c"]

    def add_ret(fid, label, base):
        if base is None:
            return
        r = _ret(c, base)
        if r is not None:
            f[fid] = _fact(f"Price return, {label}", r, "pct", rsrc, as_of, fmt_pct(r))

    for fid, back, label in [("ret_1d_pct", 1, "one day"),
                             ("ret_1w_pct", 5, "one week (five trading days)"),
                             ("ret_1m_pct", 21, "one month"),
                             ("ret_3m_pct", 63, "three months")]:
        add_ret(fid, label, bars[-1 - back]["c"] if len(bars) > back else None)

    last_d = date.fromisoformat(as_of)
    prev_year = [b for b in bars if date.fromisoformat(b["date"]).year < last_d.year]
    add_ret("ret_ytd_pct", "year to date", prev_year[-1]["c"] if prev_year else None)
    year_ago = (last_d - timedelta(days=365)).isoformat()
    older = [b for b in bars if b["date"] <= year_ago]
    add_ret("ret_1y_pct", "one year", older[-1]["c"] if older else None)

    window = [b for b in bars if b["date"] > year_ago]
    if window:
        hi = max(b["h"] for b in window)
        lo = min(b["l"] for b in window)
        f["px_52w_high"] = _fact("52-week high (intraday)", hi, "usd_per_share", src, as_of, fmt_price(hi))
        f["px_52w_low"] = _fact("52-week low (intraday)", lo, "usd_per_share", src, as_of, fmt_price(lo))
    return f, as_of


def _market(inp: dict) -> dict:
    t = _ticker(inp)
    today = massive.today_et()
    facts: dict = {}
    reasons = []
    as_of = None
    try:
        bars = massive.daily_bars(t, today - timedelta(days=400), today)
        pf, as_of = _price_facts(bars)
        facts.update(pf)
    except massive.Unavailable as e:
        reasons.append(e.reason)
    if not (reasons and reasons[0] == "MASSIVE_API_KEY is not set"):
        try:
            ov = massive.ticker_overview(t)
            osrc = f"Massive market data (ticker overview, as of {today.isoformat()})"
            mc = ov.get("market_cap")
            if isinstance(mc, (int, float)) and mc > 0:
                facts["market_cap"] = _fact("Market capitalization", float(mc), "usd", osrc, today.isoformat(),
                                            fmt_usd_big(float(mc)))
            so = ov.get("weighted_shares_outstanding")
            if isinstance(so, (int, float)) and so > 0:
                facts["shares_outstanding"] = _fact("Shares outstanding (weighted)", float(so), "count", osrc,
                                                    today.isoformat(), fmt_count(float(so)))
        except massive.Unavailable as e:
            reasons.append(e.reason)
    if not facts:
        return _pack("market", source=MARKET_SOURCE,
                     reason=reasons[0] if reasons else "Massive returned no usable market data")
    as_of = as_of or today.isoformat()
    lines = [f"MARKET DATA (Massive, end-of-day, as of {as_of}; id | what | value):"]
    lines += [f"{fid} | {fa['label']} | {fa['display']}" for fid, fa in facts.items()]
    # Only the last bar's date may be written. Today's date is not allowed: on a weekend or holiday
    # "closed at $X on <today>" would be a wrong date.
    return _pack("market", status="ok", source=MARKET_SOURCE, as_of=as_of, facts=facts,
                 sheet="\n".join(lines), allow_text=["52-week"], allow_dates=[as_of])


def market(inp: dict) -> dict:
    return _safe("market", MARKET_SOURCE, _market, inp)


# ---------- fundamentals ----------

FUND_SOURCE = "Massive income statements (cross-check against SEC XBRL)"


def _num(x):
    try:
        if x is None or isinstance(x, bool):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _fundamentals(inp: dict) -> dict:
    t = _ticker(inp)
    if t not in sec.FACTS or t not in PERIODS:
        return _pack("fundamentals", source=FUND_SOURCE, reason="no SEC facts loaded to cross-check against")
    rows = massive.income_statements(t, 8)
    matched = {}
    for fy, target in PERIODS[t].items():
        td = date.fromisoformat(target)
        for row in rows:
            try:
                pe = date.fromisoformat(str(row.get("period_end", ""))[:10])
            except ValueError:
                continue
            if abs((pe - td).days) <= 7 and str(row.get("timeframe", "annual")).lower() == "annual":
                matched[fy] = row
                break
    if not matched:
        return _pack("fundamentals", source=FUND_SOURCE,
                     reason="Massive has no annual income statement for these fiscal years yet")
    cross, exclude, agree = [], [], []
    for fy in PERIODS[t]:
        row = matched.get(fy)
        for field, key in FIELD_MAP:
            fid = f"{key}_{fy}"
            sf = sec.FACTS[t].get(fid)
            if not sf:
                continue
            sv = sf["value"]
            mv = _num(row.get(field)) if row else None
            if mv is None:
                result = "NOT_COMPARED"
            else:
                tol = 0.005 if key == "eps_diluted" else max(0.0005 * abs(sv), 1e6)
                result = "AGREE" if abs(mv - sv) <= tol else "DISAGREE"
            cross.append({"fact_id": fid, "label": sf["label"], "sec_value": sv, "massive_value": mv,
                          "result": result})
            if result == "AGREE":
                agree.append(fid)
            elif result == "DISAGREE":
                ids = [fid, f"{key}_change_pct", f"{key}_change_amount"]
                if key in ("revenue", "gross_margin"):
                    ids += GM_PCT_IDS
                exclude += [i for i in ids if i not in exclude]
    sheet = ("SECOND-SOURCE CHECK (Massive income statements): confirmed by a second source: "
             f"{', '.join(agree) or 'none'}. NOT confirmed, do not use: {', '.join(exclude) or 'none'}.")
    return _pack("fundamentals", status="ok", source=FUND_SOURCE, exclude=exclude, sheet=sheet,
                 crosscheck=cross)


def fundamentals(inp: dict) -> dict:
    return _safe("fundamentals", FUND_SOURCE, _fundamentals, inp)


# ---------- news ----------

NEWS_SOURCE = "Massive news feed"


def _news(inp: dict) -> dict:
    t = _ticker(inp)
    items = []
    for r in massive.news(t, 50):  # one call; extra rows leave room after pitch-style headlines are dropped
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or "").strip()
        if not title or ADVICE_HEADLINE.search(title) or NUMBER_HEADLINE.search(title):
            continue
        if not _mentions_company(title, t):
            continue
        pub = r.get("publisher") if isinstance(r.get("publisher"), dict) else {}
        items.append({"id": str(r.get("id") or ""), "title": title, "publisher": pub.get("name") or "",
                      "published_utc": str(r.get("published_utc") or ""), "url": str(r.get("article_url") or "")})
        if len(items) >= 8:
            break
    if not items:
        return _pack("news", source=NEWS_SOURCE, reason="no recent headlines that mention the company")
    newest = items[0]["published_utc"][:10] or None
    return _pack("news", status="ok", source=NEWS_SOURCE, as_of=newest, items=items)


def news(inp: dict) -> dict:
    return _safe("news", NEWS_SOURCE, _news, inp)


FILINGS = RunnableLambda(filings, name="specialist_filings")
MARKET = RunnableLambda(market, name="specialist_market")
FUNDAMENTALS = RunnableLambda(fundamentals, name="specialist_fundamentals")
NEWS = RunnableLambda(news, name="specialist_news")
SPECIALISTS = {"filings": FILINGS, "market": MARKET, "fundamentals": FUNDAMENTALS, "news": NEWS}

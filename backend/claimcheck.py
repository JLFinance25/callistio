"""Check this claim: a pasted statement about a stock gets a verdict per number, with the source as the reason.

Plain code only. The verdict comes from the same number checker that guards every answer (backend/checker.py): a
number is a MATCH only when the checker verified it against a source fact for the metric, period, date and direction
the sentence names. Anything else is WRONG (with the source figure, when exactly one source fact fits what the
sentence names) or NOT IN SOURCE. No model is asked, and no figure is computed here: only source values are printed."""
from __future__ import annotations

import re
from datetime import date

from backend import checker, gate, specialists as sp
from backend.facts import FACTS

ASK_FOR_CLAIM = ('Paste the statement you want checked, numbers included. For example: check this: "Apple\'s revenue '
                 'was $500 billion in fiscal 2026".')
_WHO = (r"(?:someone|somebody|(?:my|a) (?:friend|broker|dad|mom|uncle|cousin|coworker|advis[eo]r)|chat ?gpt|gpt|gemini|"
        r"claude|copilot|grok|an? ai|another ai|reddit|twitter|a tweet|the news|an article|tiktok|youtube)")
_LEAD = re.compile(
    r"^\W*(?:(?:hey|hi|ok|okay|so|please|callisto)[,:]?\s+)*(?:(?:can|could|will|would) you\s+)?(?:please\s+)?(?:"
    r"(?:fact[- ]?check|check|verify)(?: (?:this|that))?(?: claim| statement)?\s*[:\-–]\s*|"
    r"(?:fact[- ]?check|check|verify) (?:this|that)(?: claim| statement)?,?\s+|"
    r"is it true(?: that)?[,:]?\s+|true or false[,:]?\s+|"
    r"i (?:heard|read|saw)(?: that| somewhere that)?[,:]?\s+|"
    + _WHO + r" (?:told me|says?|said|claims?|claimed)(?: that)?[,:]?\s+)(.+)$", re.I | re.S)
_TAIL = re.compile(r"^(.+?)[\s,.;:\-–]+(?:is (?:that|this|it) (?:right|true|correct|accurate)|true or false|"
                   r"(?:can|could) you (?:check|verify|confirm)(?: (?:that|this))?|fact[- ]?check(?: (?:that|this))?)\W*$",
                   re.I | re.S)
_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MEANS = {
    "TRUE": "What it means: every number in this statement matches the source for the measure and period it names.",
    "FALSE": "What it means: at least one number in this statement does not match the source, so do not rely on it "
             "as written.",
    "CANNOT CONFIRM": "What it means: Callisto's sources hold no figure that backs this number, so it can neither "
                      "confirm it nor correct it.",
    "NOTHING TO CHECK": "What it means: Callisto checks numbers against their source. This statement has no number "
                        "to check; words like \"best\" or \"soaring\" are opinions it does not judge.",
}
HOW = "How this was checked: plain code compared each number with the source value; no AI model judged it."
OTHER_CO = ("For this company Callisto can check market data only (price, returns, 52-week range, market value, "
            "dividends), not sales or profit.")


def _clean(text: str) -> str:
    t = " ".join(str(text or "").split()).strip()
    return t.strip("\"'“”‘’ ").rstrip("?").strip()


def wanted(q: str):
    """The statement to check when the message asks for a check and holds a number, else None."""
    s = str(q or "").strip()
    if not s or len(s) > 600:
        return None
    m = _LEAD.match(s)
    claim = _clean(m.group(1)) if m else None
    if claim:
        t = _TAIL.match(claim)   # "check this: X, is that right?"
        claim = _clean(t.group(1)) if t else claim
    else:
        t = _TAIL.match(s)       # "X. Is that right?"
        claim = _clean(t.group(1)) if t else None
    if not claim or not re.search(r"\d", claim):
        return None
    return claim


def _human(iso) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
        return f"{_DAY[d.weekday()]}, {_MON[d.month - 1]} {d.day}"
    except Exception:
        return ""


def _show(f: dict) -> str:
    """A source value as text. Uses the fact's own display when it has one; formats only, never computes."""
    if f.get("display"):
        return str(f["display"])
    v, kind = f.get("value"), f.get("kind")
    if kind == "usd":
        return sp.fmt_usd_big(v)
    if kind == "usd_per_share":
        return sp.fmt_price(v)
    if kind in ("pct", "pts"):
        return f"{sp._q(v, '0.1')}{'%' if kind == 'pct' else ' percentage points'}"
    return str(v)


def _src(f: dict) -> str:
    s = str(f.get("source") or "").replace(" (SEC XBRL)", ", SEC filing")
    when = _human(f.get("as_of")) if f.get("as_of") else ""
    return s if not when or when in s else f"{s}, {when}"


def _fits(num: dict, f: dict) -> bool:
    try:
        return bool(checker._kind_ok(num.get("kind"), f.get("kind"), num.get("dollar", True)))
    except Exception:
        return num.get("kind") == f.get("kind")


def _correction(fid_pool: dict, all_facts: dict, num: dict, ctx: dict):
    """The one source fact that is the measure, period and date the sentence names (value aside), else None."""
    hits = []
    for fid, f in fid_pool.items():
        try:
            if _fits(num, f) and not checker.problem(fid, f, None, ctx, all_facts):
                hits.append(f)
        except Exception:
            continue
    return hits[0] if len(hits) == 1 else None


def check(claim: str, ticker: str) -> dict:
    base = {"status": "ANSWERED", "kind": "claim_check", "claims": [], "checks": [], "blocked": [], "news": [],
            "ticker": ticker, "agent": "Callisto", "disclaimer": "Research aid, not investment advice."}
    try:
        from backend import desk
        name = desk._ticker_name(ticker) or ticker
        base["company"] = name
        text = _clean(claim)
        if text and text[-1] not in ".!":
            text += "."
        pack = sp.market({"ticker": ticker})
        ok = isinstance(pack, dict) and pack.get("status") == "ok"
        extra = gate._clean_facts(pack.get("facts") or {}) if ok else {}
        pool = checker.fact_pool(ticker, extra or None)
        nums = checker.find_numbers(text)
        if not nums:
            return {**base, "verdict": "NOTHING TO CHECK",
                    "answer": "Verdict: NOTHING TO CHECK.\n" + MEANS["NOTHING TO CHECK"]}
        if not pool:
            return {**base, "verdict": "CANNOT CONFIRM",
                    "answer": f"Verdict: CANNOT CONFIRM.\nCallisto has no checked data for {ticker}.\n" + HOW}
        res = checker.check(ticker, text, [], extra or None)
        claims = [c for c in res.get("claims") or [] if isinstance(c, dict)][:len(nums)]
        if len(claims) != len(nums):
            raise ValueError("checker returned a different number of claims")
        all_facts = {**FACTS.get(ticker, {}), **pool}
        checks, lines, prev_end = [], [], 0
        for num, c in zip(nums, claims):
            ctx = checker.context(text, num, prev_end)
            prev_end = ctx.get("year_end", prev_end)
            item = {"text": num["text"]}
            if c.get("status") == "VERIFIED" and c.get("fact_id") in pool:
                f = pool[c["fact_id"]]
                item.update(verdict="MATCH", source=_src(f), source_value=_show(f), as_of=f.get("as_of"),
                            reason=f"{f['label']}: {_show(f)} ({_src(f)}).")
            else:
                f = _correction(pool, all_facts, num, ctx)
                if f:
                    why = ("the source shows the opposite direction. " if "Direction" in str(c.get("note") or "") else "")
                    item.update(verdict="WRONG", source=_src(f), source_value=_show(f), as_of=f.get("as_of"),
                                reason=f"{why}The source says {f['label']}: {_show(f)} ({_src(f)}).")
                elif c.get("status") == "MISMATCH" and c.get("note"):
                    item.update(verdict="WRONG", reason=f"this number is in the source, but not as stated. {c['note']}")
                else:
                    item.update(verdict="NOT IN SOURCE",
                                reason="no figure in Callisto's sources matches this number for what the sentence says.")
            checks.append(item)
            r = item["reason"]
            lines.append(f"• {item['verdict']}: \"{num['text']}\". {r[0].upper() + r[1:]}")
        n, good = len(checks), sum(1 for x in checks if x["verdict"] == "MATCH")
        wrong = sum(1 for x in checks if x["verdict"] == "WRONG")
        one = n == 1
        if good == n:
            verdict, head = "TRUE", ("The number matches the source." if one else f"All {n} numbers match the source.")
        elif wrong:
            verdict = "FALSE"
            head = ("The number does not match the source." if one else
                    f"{n - good} of {n} numbers do not match the source; {good} matched.")
        else:
            verdict = "CANNOT CONFIRM"
            head = ("The source holds no figure that backs this number." if one else
                    f"{n - good} of {n} numbers have no figure in the source to back them; {good} matched.")
        out = [f"Verdict: {verdict}. {head}"] + lines + [MEANS[verdict]]
        if verdict == "CANNOT CONFIRM" and ticker not in FACTS:
            out.append(OTHER_CO)
        srcs = (["Apple's annual report (10-K) filed with the SEC"] if ticker in FACTS else []) + (
            [f"Massive end-of-day market data, as of {pack.get('as_of')}"] if ok and pack.get("as_of") else [])
        out.append(HOW + (" Sources: " + "; ".join(srcs) + "." if srcs else ""))
        return {**base, "verdict": verdict, "answer": "\n".join(out), "claims": claims, "checks": checks}
    except Exception:
        return {**base, "verdict": "CANNOT CONFIRM",
                "answer": "Verdict: CANNOT CONFIRM.\nCallisto could not check this statement against its sources.\n" + HOW}

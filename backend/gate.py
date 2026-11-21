"""number_gate: the final word on every answer from the multi-agent desk. Pure code, no model calls.

Order (fails closed at each step):
1. skeptic deletions (REFUTED) -> 2. advice-phrase filter (ADVICE) -> 3. checker.check() over SEC facts plus
market-data facts minus quarantined ids -> 4. naked-number sweep (UNCHECKABLE) -> 5. status.
"""
import re
from datetime import date

from langchain_core.runnables import RunnableLambda

from backend import checker

FALLBACK = "I couldn't verify that answer against my sources, so I won't show it."
IDK_FALLBACK = "The data I have doesn't answer that, so I won't guess."

ADVICE_PHRASES = [re.compile(p, re.I) for p in (
    r"\byou should\b",
    r"\bi (would )?recommend\b",
    r"\b(is|looks like) a (good|great|strong|bad) (buy|investment|time to)\b",
    r"\b(buy|sell|hold) (rating|now|the stock|shares)\b",
    r"\bworth (buying|selling)\b",
    r"\bwill (rise|fall|go up|go down|outperform)\b",
)]

# Digits that are not claims: fiscal years, years, SEC form names, risk-factor items, quarters.
GENERIC_ALLOWED = [re.compile(p, re.I) for p in (
    r"\bFY\s?\d{4}\b",
    r"\b(19|20)\d{2}\b",
    r"\b(10-K|10-Q|8-K|13-F|20-F)\b",
    r"\bItem\s+\d+[A-Z]?\b",
    r"\bQ[1-4]\b",
)]
DIGITS = re.compile(r"\d[\d,]*(?:\.\d+)?")

_VALID_KINDS = ("usd", "usd_per_share", "pct", "pts", "count")


def expand_dates(iso_dates) -> list[str]:
    """'2026-11-20' -> the ways a writer may print that date. Values that do not parse are skipped."""
    out = []
    for raw in iso_dates or ():
        try:
            d = date.fromisoformat(str(raw).strip()[:10])
        except (TypeError, ValueError):
            continue
        month, mon = d.strftime("%B"), d.strftime("%b")
        forms = [d.isoformat(), f"{month} {d.day}, {d.year}", f"{mon} {d.day}, {d.year}", f"{mon}. {d.day}, {d.year}",
                 f"{month} {d.day}", f"{mon} {d.day}", f"{d.month}/{d.day}/{d.year}",
                 f"{d.month:02d}/{d.day:02d}/{d.year}"]
        for f in forms:
            if f not in out:
                out.append(f)
    return out


def sweep(text: str, allow_text=(), allow_dates=()) -> list[str]:
    """Digit tokens in `text` that nothing covers (not a checked number, allowed literal, date, year or form name)."""
    text = text or ""
    mask = list(text)

    def blank(a, b):
        for i in range(a, b):
            mask[i] = " "

    # (a) numbers the checker understands (by the time we sweep, these are all VERIFIED)
    for n in checker.find_numbers(text):
        blank(n["start"], n["start"] + len(n["text"]))
    # (b) allowed literals and dates, longest first, case-insensitive
    literals = [str(t) for t in (allow_text or ()) if t] + expand_dates(allow_dates)
    for lit in sorted(set(literals), key=len, reverse=True):
        for m in re.finditer(re.escape(lit), text, re.I):
            blank(m.start(), m.end())
    # (c) generic patterns
    for rx in GENERIC_ALLOWED:
        for m in rx.finditer(text):
            blank(m.start(), m.end())
    return [m.group(0) for m in DIGITS.finditer("".join(mask))]


def _clean_facts(facts) -> dict:
    """Keep only well-formed facts, so one bad pack cannot crash the checker."""
    out = {}
    if not isinstance(facts, dict):
        return out
    for fid, f in facts.items():
        if not isinstance(fid, str) or not isinstance(f, dict):
            continue
        try:
            value = float(f.get("value"))
        except (TypeError, ValueError):
            continue
        if value != value or f.get("kind") not in _VALID_KINDS:  # NaN or unknown kind
            continue
        out[fid] = {**f, "value": value, "label": str(f.get("label", fid)), "source": str(f.get("source", "unknown"))}
    return out


def _as_list(x):
    if isinstance(x, (list, tuple, set)):
        return list(x)
    return []


def _final_check(ticker, draft, packs, verdicts):
    answer = draft.get("answer") or ""
    if not isinstance(answer, str):
        answer = str(answer)
    sentences = checker.split_sentences(answer)

    # (1) skeptic deletions
    refuted, drop = [], {}
    for v in _as_list(verdicts):
        if not isinstance(v, dict):
            continue
        try:
            i = int(v.get("i"))
        except (TypeError, ValueError):
            continue
        if 1 <= i <= len(sentences) and str(v.get("verdict", "")).upper() in ("UNSUPPORTED", "ADVICE"):
            drop.setdefault(i, str(v.get("why") or ""))
    remaining = []
    for idx, s in enumerate(sentences, start=1):
        if idx in drop:
            refuted.append(dict(text=s, status="REFUTED", note="Skeptic (second model): " + drop[idx]))
        else:
            remaining.append(s)

    # (2) advice-phrase filter
    advice, kept = [], []
    for s in remaining:
        if any(rx.search(s) for rx in ADVICE_PHRASES):
            advice.append(dict(text=s, status="ADVICE", note="Europa does not give investment advice."))
        else:
            kept.append(s)

    # (3) checker over SEC + extra facts from ok packs, minus quarantined ids (taken from EVERY pack)
    extra, exclude, allow_text, allow_dates = {}, set(), [], []
    for p in _as_list(packs):
        if not isinstance(p, dict):
            continue
        exclude.update(str(x) for x in _as_list(p.get("exclude")))
        if p.get("status") != "ok":
            continue
        extra.update(_clean_facts(p.get("facts")))
        allow_text += [str(x) for x in _as_list(p.get("allow_text")) if x]
        allow_dates += [str(x) for x in _as_list(p.get("allow_dates")) if x]
    model_claims = [c for c in _as_list(draft.get("claims")) if isinstance(c, dict)]
    res = checker.check(ticker, " ".join(kept), model_claims, extra, sorted(exclude))

    # (4) naked-number sweep, sentence by sentence
    quote_texts = {c.get("text") for c in model_claims if c.get("type") == "quote" and c.get("text")}
    allow_text += [c["text"] for c in res["claims"] if c.get("status") == "VERIFIED" and c.get("text") in quote_texts]
    claims, unchecked, final, seen = list(res["claims"]), [], [], set()
    for s in checker.split_sentences(res["answer"]):
        leftover = sweep(s, allow_text, allow_dates)
        if not leftover:
            final.append(s)
            continue
        unchecked.append(dict(text=s, status="UNCHECKABLE", note="Contains a number Europa cannot check against a source."))
        for tok in leftover:
            if tok not in seen:
                seen.add(tok)
                claims.append(dict(text=tok, status="UNCHECKABLE", source=None, sec_value=None,
                                   note="Europa has no source to check this number against."))

    # (5) status
    status = draft.get("status")
    if status not in ("ANSWERED", "I_DONT_KNOW"):
        status = "ANSWERED"
    out_answer = " ".join(final).strip()
    if not out_answer:
        # the writer already said it did not know: say that plainly instead of "couldn't verify"
        out_answer = IDK_FALLBACK if status == "I_DONT_KNOW" else FALLBACK
        status = "I_DONT_KNOW"
    return dict(status=status, answer=out_answer, claims=claims, blocked=refuted + advice + res["blocked"] + unchecked)


def final_check(ticker: str, draft: dict, packs: list[dict], verdicts: list[dict] | None = None) -> dict:
    """Return {status, answer, claims, blocked}. Never raises: on any internal error it shows nothing."""
    try:
        if not isinstance(draft, dict):
            raise TypeError("draft must be a dict")
        return _final_check(str(ticker or "").upper(), draft, packs, verdicts)
    except Exception:  # fail closed
        return dict(status="I_DONT_KNOW", answer=FALLBACK, claims=[], blocked=[])


def _gate(inp: dict) -> dict:
    return {**inp, "result": final_check(inp["ticker"], inp["draft"], inp.get("packs", []), inp.get("verdicts"))}


GATE = RunnableLambda(_gate, name="number_gate")

"""Number and quote checker. Fails closed: anything we can't prove is blocked.

Statuses (CHECKER-SPEC.md): VERIFIED (show) · MISMATCH / NOT_FOUND (block).
Every number written in the answer must match an SEC fact, within the rounding the writer used.
"""
import re

from backend.facts import FACTS, filing_text

_MULT = {"trillion": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
NUM = r"(\d[\d,]*(?:\.\d+)?)"
PATTERNS = [  # (regex, kind)
    (re.compile(r"\$\s?" + NUM + r"\s*(trillion|billion|million|thousand|bn|b|m|k)?\b", re.I), "usd"),
    (re.compile(NUM + r"\s*(trillion|billion|million)\b", re.I), "usd"),  # "478,834 million"
    (re.compile(NUM + r"\s*(?:percentage points|percent points|points|pts|pp)\b", re.I), "pts"),
    (re.compile(NUM + r"\s*(?:%|percent\b)", re.I), "pct"),
]


def find_numbers(text: str) -> list[dict]:
    """Every money / percent / points number in the text, with how precisely it was written."""
    found, taken = [], []
    for rx, kind in PATTERNS:
        for m in rx.finditer(text):
            if any(a <= m.start() < b for a, b in taken):
                continue
            raw = m.group(1).replace(",", "")
            decimals = len(raw.split(".")[1]) if "." in raw else 0
            mult = _MULT.get((m.group(2) or "").lower(), 1.0) if kind == "usd" and m.lastindex >= 2 else 1.0
            value = float(raw) * mult
            tol = 0.5 * (10 ** -decimals) * mult + 1e-9  # half of the last digit written
            found.append(dict(text=m.group(0).strip(), value=value, tol=tol, kind=kind, start=m.start()))
            taken.append((m.start(), m.end()))
    return sorted(found, key=lambda n: n["start"])


def _kind_ok(num_kind: str, fact_kind: str) -> bool:
    if num_kind == "usd":
        return fact_kind in ("usd", "usd_per_share")
    return num_kind == fact_kind or (num_kind == "pct" and fact_kind == "pts")


def match_fact(ticker: str, num: dict, prefer: str | None = None):
    """Return (fact_id, fact) the number matches, preferring the fact the model cited."""
    facts = FACTS.get(ticker, {})
    order = ([prefer] if prefer in facts else []) + [k for k in facts if k != prefer]
    for fid in order:
        f = facts[fid]
        if _kind_ok(num["kind"], f["kind"]) and abs(abs(f["value"]) - num["value"]) <= num["tol"]:
            return fid, f
    return None, None


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_quote(ticker: str, quote: str, year: str) -> bool:
    q = _norm(quote).strip(' ."')
    return len(q) >= 12 and q in _norm(filing_text(ticker, year))


def split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])", text.strip()) if s]


def check(ticker: str, answer: str, model_claims: list[dict]) -> dict:
    """Check the model's answer. Returns the safe answer, per-number claims, and what was blocked."""
    facts = FACTS.get(ticker, {})
    cited = {}  # number text -> fact id the model said it came from
    for c in model_claims or []:
        if c.get("fact_id"):
            cited[str(c.get("value", "")).strip()] = c["fact_id"]

    claims, blocked, bad_spans = [], [], []
    for num in find_numbers(answer):
        prefer = cited.get(num["text"])
        fid, f = match_fact(ticker, num, prefer)
        if f:
            claims.append(dict(text=num["text"], status="VERIFIED", source=f"{f['source']}: {f['label']}",
                               sec_value=f["value"], fact_id=fid))
            continue
        # The model cited a real fact but wrote the wrong number -> MISMATCH, else NOT_FOUND.
        note = "No SEC fact matches this number."
        status = "NOT_FOUND"
        if prefer in facts:
            status = "MISMATCH"
            note = f"SEC value for {facts[prefer]['label']} differs."
        claims.append(dict(text=num["text"], status=status, source=None, sec_value=None, note=note))
        bad_spans.append(num["text"])

    # Quotes the model says it took from the filing must really be there.
    for c in model_claims or []:
        if c.get("type") == "quote" and c.get("text"):
            ok = verify_quote(ticker, c["text"], c.get("filing", "FY2026"))
            claims.append(dict(text=c["text"], status="VERIFIED" if ok else "NOT_FOUND",
                               source=f"Apple 10-K {c.get('filing', 'FY2026')} text" if ok else None,
                               sec_value=None))
            if not ok:
                bad_spans.append(c["text"])

    kept = []
    for s in split_sentences(answer):
        if any(b in s for b in bad_spans):
            blocked.append(dict(text=s, status="BLOCKED", note="Contains a number or quote the SEC data does not support."))
        else:
            kept.append(s)
    return dict(answer=" ".join(kept), claims=claims, blocked=blocked)

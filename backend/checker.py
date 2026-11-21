"""Number and quote checker. Fails closed: anything we can't prove is blocked.

Statuses (CHECKER-SPEC.md): VERIFIED (show) · MISMATCH / NOT_FOUND (block).
Every number written in the answer must match an SEC fact, within the rounding the writer used.
"""
import re

from backend.facts import FACTS, filing_text

_MULT = {"trillion": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
NUM = r"(\d[\d,]*(?:\.\d+)?|(?<!\d)\.\d+)"  # ".5" is 0.5, never a bare "5"
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
            txt = m.group(0).strip()
            found.append(dict(text=txt, value=value, tol=tol, kind=kind, start=m.start(), end=m.end(), raw=raw,
                              dollar=txt.startswith("$")))
            taken.append((m.start(), m.end()))
    return sorted(found, key=lambda n: n["start"])


def _kind_ok(num_kind: str, fact_kind: str, dollar: bool = True) -> bool:
    if num_kind == "usd":
        if fact_kind == "count":  # "52.3 million shares" yes, "$52.3 million" never
            return not dollar
        return fact_kind in ("usd", "usd_per_share")
    return num_kind == fact_kind  # a percent is never a percentage-point fact (1.9% is not 1.9 points)


# ---- direction: "rose 4.8%" must not pass against a fact of -4.76%
_UP = r"rose|rise[sn]?|rising|increas\w*|up|grew|grow\w*|gain\w*|higher|climb\w*|jump\w*|more"
_DOWN = r"fell|fall\w*|decreas\w*|down|declin\w*|drop\w*|lower|less|lost|loss(?:es)?|slid|slipped|shr[au]nk|reduc\w*"
_DIR = re.compile(rf"\b(?:(?P<up>{_UP})|(?P<down>{_DOWN}))\b", re.I)
_SENT_GAP = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")
_MINUS, _PLUS = "-\u2212\u2013", "+"


def is_change_fact(fid: str) -> bool:
    """Facts that can be negative and carry a direction: year-over-year changes and price returns."""
    return "_change_" in fid or fid.startswith("ret_")


def stated_direction(text: str, num: dict, prev_end: int = 0) -> tuple[int, bool]:
    """(+1 up / -1 down / 0 not said) for one number, and whether its sentence has any 'down' word.
    A sign written on the number wins; then the nearest direction word between the previous number and
    this one; then the first word after it ("4.8% lower")."""
    start, end = num["start"], num.get("end", num["start"] + len(num["text"]))
    lo, hi = 0, len(text)
    for m in _SENT_GAP.finditer(text):
        if m.end() <= start:
            lo = m.end()
        elif m.start() >= end:
            hi = m.start()
            break
    sentence = text[lo:hi]
    any_down = any(m.group("down") for m in _DIR.finditer(sentence))
    if start > 0 and text[start - 1] in _MINUS + _PLUS and (start < 2 or not text[start - 2].isalnum()):
        return (1 if text[start - 1] in _PLUS else -1), any_down
    before = list(_DIR.finditer(text[max(lo, min(prev_end, start)):start]))
    if before:
        return (1 if before[-1].group("up") else -1), any_down
    after = re.match(r"\s*([A-Za-z]+)", text[end:hi])
    m = _DIR.fullmatch(after.group(1)) if after else None
    if m:
        return (1 if m.group("up") else -1), any_down
    return 0, any_down


def direction_ok(fid: str, fact: dict, stated: tuple[int, bool] | None) -> bool:
    if stated is None or not is_change_fact(fid) or fact["value"] == 0:
        return True
    sign, any_down = stated
    if fact["value"] > 0:
        return sign >= 0
    return sign < 0 or (sign == 0 and any_down)  # a fall must be written as a fall


def fact_pool(ticker: str, extra_facts: dict | None = None, exclude=None) -> dict:
    """SEC facts plus extra (e.g. market-data) facts, minus quarantined ids."""
    pool = {**FACTS.get(ticker, {}), **(extra_facts or {})}
    for fid in exclude or ():
        pool.pop(fid, None)
    return pool


def _value_ok(num: dict, f: dict) -> bool:
    return (_kind_ok(num["kind"], f["kind"], num.get("dollar", True))
            and abs(abs(f["value"]) - num["value"]) <= num["tol"])


def match_fact(ticker: str, num: dict, prefer: str | None = None, extra_facts: dict | None = None, exclude=None,
               only=None, stated=None):
    """Return (fact_id, fact) the number matches, preferring the fact the model cited.
    `only`: fact ids the writer cited for this number; when given, no other fact may match.
    `stated`: stated_direction() for the number; a change or return fact must agree with it."""
    facts = fact_pool(ticker, extra_facts, exclude)
    if only is not None:
        order = [k for k in only if k in facts]
    else:
        order = ([prefer] if prefer in facts else []) + [k for k in facts if k != prefer]
    for fid in order:
        f = facts[fid]
        if _value_ok(num, f) and direction_ok(fid, f, stated):
            return fid, f
    return None, None


def _claim_keys(value) -> list:
    """Keys for a claim value so "-1.9%", "\u22121.9%", "+1.9 %" and "1.9%" all map to the number "1.9%"."""
    v = str(value if value is not None else "").replace("\u2212", "-").replace("\u2013", "-")
    v = re.sub(r"\s+", " ", v).strip()
    nums = find_numbers(v)
    if nums:
        return [(n["kind"], n["value"], n["tol"]) for n in nums]
    m = re.search(r"\d[\d,]*(?:\.\d+)?|\.\d+", v)
    return [m.group(0).replace(",", "")] if m else []


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_quote(ticker: str, quote: str, year: str) -> bool:
    q = _norm(quote).strip(' ."')
    return len(q) >= 12 and q in _norm(filing_text(ticker, year))


def split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])", text.strip()) if s]


def check(ticker: str, answer: str, model_claims: list[dict], extra_facts: dict | None = None, exclude=None) -> dict:
    """Check the model's answer. Returns the safe answer, per-number claims, and what was blocked."""
    facts = fact_pool(ticker, extra_facts, exclude)
    cited = {}  # number (kind, value, rounding) -> fact ids the model said it came from
    for c in model_claims or []:
        if isinstance(c, dict) and c.get("fact_id"):
            for key in _claim_keys(c.get("value")):
                ids = cited.setdefault(key, [])
                if str(c["fact_id"]) not in ids:
                    ids.append(str(c["fact_id"]))

    claims, blocked, bad_spans, prev_end = [], [], [], 0
    for num in find_numbers(answer):
        # A number the writer cited a fact for must match THAT fact, not any fact with a close value.
        only = cited.get((num["kind"], num["value"], num["tol"])) or cited.get(num["raw"])
        prefer = next((k for k in only if k in facts), None) if only else None
        stated = stated_direction(answer, num, prev_end)
        prev_end = num["end"]
        fid, f = match_fact(ticker, num, prefer, extra_facts, exclude, only=only, stated=stated)
        if f:
            claim = dict(text=num["text"], status="VERIFIED", source=f"{f['source']}: {f['label']}",
                         sec_value=f["value"], fact_id=fid)
            for key in ("as_of", "provider"):
                if f.get(key):
                    claim[key] = f[key]
            claims.append(claim)
            continue
        # The model cited a real fact but wrote the wrong number -> MISMATCH, else NOT_FOUND.
        note = "No SEC or market-data fact matches this number." if extra_facts else "No SEC fact matches this number."
        status = "NOT_FOUND"
        wrong_way = next((k for k in (only if only is not None else facts)
                          if k in facts and _value_ok(num, facts[k])), None)
        if wrong_way:  # the value matches, but the sentence says it moved the other way (or drops the minus)
            status = "MISMATCH"
            note = f"Direction does not match the source for {facts[wrong_way]['label']}."
        elif prefer in facts:
            status = "MISMATCH"
            note = f"SEC value for {facts[prefer]['label']} differs."
        elif only:
            note = "The fact cited for this number is not one Europa has."
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

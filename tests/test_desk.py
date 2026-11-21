"""Desk orchestrator with fake agents, specialists and gate. No model, no Massive, no network."""
import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableLambda

from backend import desk

OLD_KEYS = {"answer", "claims", "status", "blocked", "company", "prism_session_id", "models", "checker",
            "seconds", "data_note", "disclaimer"}
FILINGS_SHEET = ("FACTS (id | what | value):\n"
                 "revenue_fy2026 | Net sales (revenue), fiscal 2026 | $478.8 billion\n"
                 "eps_diluted_fy2026 | Diluted earnings per share, fiscal 2026 | $8.98 per share")
# 2026-11-21 (review hole 6): the notice is fixed text; the raw reason is no longer put into the answer
MARKET_NOTICE = "Market data is unavailable right now, so I can't answer that part and I won't guess."


def pack(agent, status="ok", **kw):
    base = {"agent": agent, "status": status, "reason": None, "source": None, "as_of": None, "facts": {},
            "exclude": [], "sheet": "", "allow_text": [], "allow_dates": [], "items": [], "crosscheck": [],
            "seconds": 0.01}
    return {**base, **kw}


class Fakes:
    def __init__(self, route=None, packs=None, draft=None, verdicts=None, gate_result=None):
        self.calls = {k: [] for k in ("router", "news", "writer", "skeptic", "gate")}
        self.spec_calls = []
        route = route or {"specialists": ["filings"], "advice": False, "reason": "", "model": "m-router",
                          "fallback": False}
        packs = packs or {"filings": pack("filings", sheet=FILINGS_SHEET)}
        draft = draft or {"status": "ANSWERED", "answer": "Revenue was $478.8 billion.",
                          "claims": [{"value": "$478.8 billion", "fact_id": "revenue_fy2026"}]}
        verdicts = verdicts if verdicts is not None else [{"i": 1, "verdict": "SUPPORTED", "why": ""}]

        def rec(key, out):
            def f(inp):
                self.calls[key].append(inp)
                return out(inp) if callable(out) else out
            return f

        def spec(name):
            def f(inp):
                self.spec_calls.append(name)
                p = packs.get(name)
                if isinstance(p, Exception):
                    raise p
                return p
            return RunnableLambda(f, name=f"specialist_{name}")

        def gate(inp):
            self.calls["gate"].append(inp)
            res = gate_result or {"status": inp["draft"]["status"], "answer": inp["draft"]["answer"],
                                  "claims": [{"text": "$478.8 billion", "status": "VERIFIED"}], "blocked": []}
            return {**inp, "result": res}

        self.deps = desk.Deps(
            guard=desk.GUARD,
            router=RunnableLambda(rec("router", route), name="desk_router"),
            specialists={n: spec(n) for n in ("filings", "market", "fundamentals", "news")},
            news_picker=RunnableLambda(rec("news", lambda i: {"ids": [it["id"] for it in i["items"][:3]],
                                                              "model": None}), name="news_picker"),
            writer=RunnableLambda(rec("writer", {"draft": draft, "model": "m-writer"}), name="write_answer"),
            skeptic=RunnableLambda(rec("skeptic", {"status": "ok", "verdicts": verdicts, "model": "m-skeptic"}),
                                   name="skeptic_review"),
            gate=RunnableLambda(gate, name="number_gate"))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("EUROPA_CHECKER", raising=False)
    monkeypatch.delenv("EUROPA_SKEPTIC", raising=False)


def test_regex_advice_never_reaches_router():
    f = Fakes()
    out = desk.answer("aapl", "Should I buy Apple stock?", deps=f.deps)
    assert out["status"] == "DECLINED_ADVICE" and f.calls["router"] == [] and out["claims"] == []


def test_router_advice_declines():
    f = Fakes(route={"specialists": ["filings"], "advice": True, "reason": "", "model": "m", "fallback": False})
    out = desk.answer("AAPL", "Is now the moment to get out of Apple?", deps=f.deps)
    assert out["status"] == "DECLINED_ADVICE" and f.calls["writer"] == [] and f.spec_calls == []


def test_unknown_ticker_without_massive_stops_in_guard():
    f = Fakes()
    out = desk.answer("MSFT", "What were net sales?", deps=f.deps)
    assert out["status"] == "I_DONT_KNOW" and "not MSFT" in out["answer"] and f.calls["router"] == []


def test_market_only_unavailable_is_i_dont_know():
    f = Fakes(route={"specialists": ["market"], "advice": False, "reason": "", "model": "m", "fallback": False},
              packs={"market": pack("market", "unavailable", reason="MASSIVE_API_KEY is not set")})
    out = desk.answer("AAPL", "What was the closing price?", deps=f.deps)
    assert out["status"] == "I_DONT_KNOW" and "unavailable" in out["answer"]
    assert f.calls["writer"] == f.calls["skeptic"] == f.calls["gate"] == []
    assert out["unavailable"][0]["agent"] == "market"
    assert out["data_note"].endswith("Market data: unavailable.")


def test_filings_ok_market_unavailable():
    f = Fakes(route={"specialists": ["filings", "market"], "advice": False, "reason": "", "model": "m",
                     "fallback": False},
              packs={"filings": pack("filings", sheet=FILINGS_SHEET),
                     "market": pack("market", "unavailable", reason="MASSIVE_API_KEY is not set")})
    out = desk.answer("AAPL", "Revenue and price?", deps=f.deps)
    assert len(f.calls["writer"]) == 1 and out["status"] == "ANSWERED"
    assert out["answer"].endswith(MARKET_NOTICE)
    assert f.calls["writer"][0]["unavailable"] == [{"agent": "market", "reason": "MASSIVE_API_KEY is not set"}]
    assert [s["status"] for s in out["sources"]] == ["ok", "unavailable"]


def test_excluded_fact_removed_from_writer_sheet():
    f = Fakes(route={"specialists": ["filings", "fundamentals"], "advice": False, "reason": "", "model": "m",
                     "fallback": False},
              packs={"filings": pack("filings", sheet=FILINGS_SHEET),
                     "fundamentals": pack("fundamentals", exclude=["revenue_fy2026"], sheet="SECOND SOURCE: x")})
    out = desk.answer("AAPL", "Do the sources agree on revenue?", deps=f.deps)
    sheets = f.calls["writer"][0]["sheets"]
    assert "revenue_fy2026 |" not in sheets[0] and "eps_diluted_fy2026 |" in sheets[0]
    assert "Two sources disagreed" in out["answer"]


def test_checker_off_skips_skeptic_and_gate(monkeypatch):
    monkeypatch.setenv("EUROPA_CHECKER", "off")
    f = Fakes()
    out = desk.answer("AAPL", "What were net sales?", deps=f.deps)
    assert f.calls["skeptic"] == [] and f.calls["gate"] == []
    assert out["claims"] == [{"text": "$478.8 billion", "status": "UNCHECKED"}] and out["checker"] == "off"


def test_skeptic_advisory(monkeypatch):
    monkeypatch.setenv("EUROPA_SKEPTIC", "advisory")
    f = Fakes(verdicts=[{"i": 1, "verdict": "UNSUPPORTED", "why": "wrong year"}])
    out = desk.answer("AAPL", "What were net sales?", deps=f.deps)
    assert f.calls["gate"][0]["verdicts"] == []
    assert out["skeptic"]["status"] == "advisory" and out["skeptic"]["flags"]


def test_skeptic_enforce_passes_verdicts_and_counts_refuted():
    v = [{"i": 1, "verdict": "UNSUPPORTED", "why": "x"}]
    f = Fakes(verdicts=v, gate_result={"status": "I_DONT_KNOW", "answer": "", "claims": [],
                                       "blocked": [{"text": "Revenue was $478.8 billion.", "status": "REFUTED"}]})
    out = desk.answer("AAPL", "What were net sales?", deps=f.deps)
    assert f.calls["gate"][0]["verdicts"] == v
    assert out["skeptic"]["refuted"] == 1 and out["status"] == "I_DONT_KNOW"


def test_specialist_raises_becomes_unavailable():
    f = Fakes(route={"specialists": ["filings", "market"], "advice": False, "reason": "", "model": "m",
                     "fallback": False},
              packs={"filings": pack("filings", sheet=FILINGS_SHEET), "market": RuntimeError("boom")})
    out = desk.answer("AAPL", "Revenue and price?", deps=f.deps)
    assert out["unavailable"] == [{"agent": "market", "reason": "internal error in market"}]
    assert out["status"] == "ANSWERED"


def test_news_only_and_picker():
    items = [{"id": f"n{i}", "title": f"Title {i}", "publisher": "Pub", "published_utc": "2026-11-20",
              "url": f"https://example.com/{i}"} for i in range(5)]
    f = Fakes(route={"specialists": ["news"], "advice": False, "reason": "", "model": "m", "fallback": False},
              packs={"news": pack("news", items=items)})
    out = desk.answer("AAPL", "latest headlines", deps=f.deps)
    assert out["status"] == "ANSWERED" and "Europa has not verified them" in out["answer"]
    assert len(out["news"]) == 3 and set(out["news"][0]) == {"title", "publisher", "published_utc", "url"}
    assert f.calls["writer"] == [] and len(f.calls["news"]) == 1


def test_contract_keys():
    f = Fakes()
    out = desk.answer("AAPL", "What were net sales?", deps=f.deps)
    assert OLD_KEYS <= set(out)
    assert out["pipeline"] == "multi" and out["agents"] and out["sources"]
    assert {"market_chosen", "market_as_of"}.isdisjoint(out)
    assert out["company"] == "Apple Inc." and out["models"] == ["m-router", "m-writer", "m-skeptic"]
    names = [a["name"] for a in out["agents"]]
    assert names[:2] == ["guard_advice", "desk_router"] and "number_gate" in names


def test_callback_sees_named_steps():
    class Capture(BaseCallbackHandler):
        def __init__(self):
            self.names = []

        def on_chain_start(self, serialized, inputs, **kw):
            self.names.append(kw.get("name") or (serialized or {}).get("name"))

    cap, f = Capture(), Fakes()
    token = desk._DEPS.set(f.deps)
    try:
        desk.PIPELINE.invoke({"ticker": "AAPL", "question": "What were net sales?"},
                             config={"callbacks": [cap], "run_name": "europa_desk"})
    finally:
        desk._DEPS.reset(token)
    for n in ("europa_desk", "guard_advice", "desk_router", "specialists", "specialist_filings", "write_answer",
              "skeptic_review", "number_gate", "notices"):
        assert n in cap.names, n

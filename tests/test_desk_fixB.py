"""Non-Apple ticker: the answer says the demo covers Apple only, not 'data unavailable'."""
import pytest

from backend import desk
from tests.test_desk import Fakes, pack

NOT_APPLE = "This demo covers Apple (AAPL) only"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("EUROPA_CHECKER", raising=False)
    monkeypatch.delenv("EUROPA_SKEPTIC", raising=False)
    monkeypatch.setattr(desk, "_massive_available", lambda: True)  # so the guard lets MSFT through


def _fakes():
    return Fakes(packs={"filings": pack("filings", "unavailable", reason="no filings for MSFT")})


@pytest.mark.parametrize("t", ["MSFT", "TSLA", "NVDA"])
def test_non_apple_ticker_says_apple_only(t):
    out = desk.answer(t, "What were net sales?", deps=_fakes().deps)
    assert out["status"] == "I_DONT_KNOW"
    assert NOT_APPLE in out["answer"] and "I won't guess" in out["answer"]
    assert "unavailable right now" not in out["answer"]


def test_aapl_outage_keeps_old_message():
    out = desk.answer("AAPL", "What were net sales?", deps=_fakes().deps)
    assert out["status"] == "I_DONT_KNOW"
    assert "is unavailable right now" in out["answer"] and NOT_APPLE not in out["answer"]


def test_advice_refusal_does_not_offer_apple_filings_for_another_ticker():
    from backend import desk
    other = desk._advice_answer("TSLA", "Should I buy Tesla stock?")
    assert "Apple (AAPL)" in other and "Tesla's numbers" in other and "what Apple's filings say" not in other
    out = desk._guard({"question": "Should I buy Tesla stock?", "ticker": "TSLA"})
    assert out["stop"] == ("DECLINED_ADVICE", other)
    out = desk._guard({"question": "Should I buy Apple stock?", "ticker": "AAPL"})
    assert out["stop"][0] == "DECLINED_ADVICE" and "Apple's filings" in out["stop"][1]

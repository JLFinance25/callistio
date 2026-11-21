"""Check-this-claim: which messages count as a statement to check. Pure code, no network."""
import pytest

from backend import claimcheck as cc


@pytest.mark.parametrize("q,claim", [
    ("Check this: Apple's revenue was $500 billion in fiscal 2026", "Apple's revenue was $500 billion in fiscal 2026"),
    ("Is it true that Nvidia fell 14% on Friday?", "Nvidia fell 14% on Friday"),
    ("ChatGPT says Apple's net income was $132.2 billion in fiscal 2025, is that right?",
     "Apple's net income was $132.2 billion in fiscal 2025"),
    ("Fact check: Tesla is down 23.1% this year", "Tesla is down 23.1% this year"),
    ("I heard Microsoft pays a dividend of $3.92 per share", "Microsoft pays a dividend of $3.92 per share"),
])
def test_wanted_fires(q, claim):
    assert cc.wanted(q) == claim


@pytest.mark.parametrize("q", [
    "What happened to Nvidia yesterday?", "How has Tesla done this year?", "Does Apple pay a dividend?",
    "Should I buy Apple at $342?", "check this claim", "check this: Apple is the best company",
    "is it true that I should buy 100 shares of Apple?", "is it true that Apple will hit $500 next year?",
    "Is it true that Apple is a buy at $342.56?", "check this: sell Tesla at $400", "",
])
def test_wanted_stays_out(q):
    assert cc.wanted(q) is None


def test_other_day_blocks_a_match_on_the_wrong_weekday():
    f = {"label": "Closing price, last trading day", "as_of": "2026-11-20"}   # a Friday
    assert cc._other_day("Tesla closed at $345.82 on Thursday.", f) == "Thursday"
    assert cc._other_day("Tesla closed at $345.82 on Friday.", f) is None
    assert cc._other_day("Tesla closed at $345.82.", f) is None
    assert cc._other_day("Sales were $1 on Thursday.", {"label": "Net sales (revenue), fiscal 2026"}) is None

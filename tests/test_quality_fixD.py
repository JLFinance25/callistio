from backend import agents


def test_writer_one_measure_prior_year_same_sentence():
    w = agents.WRITER_SYSTEM
    assert "One measure per sentence" in w
    assert "SAME sentence as the headline number" in w
    assert 'Never put a number in a separate "That is up from' in w


def test_writer_last_sentence_has_no_numbers():
    w = agents.WRITER_SYSTEM
    assert "NO numbers" in w and "never a cause" in w


def test_writer_keeps_old_rules():
    w = agents.WRITER_SYSTEM
    assert "2 to 4 short sentences" in w and "No investment advice" in w


def test_skeptic_accepts_premise_rejection_but_not_advice():
    s = agents.SKEPTIC_SYSTEM
    assert "rejects a wrong premise" in s and "SUPPORTED" in s
    assert '"ADVICE" if it tells the reader' in s


def test_writer_has_the_what_it_means_and_deeper_read_rule():
    from backend import agents, desk
    assert '"What it means: ..."' in agents.WRITER_SYSTEM and '"Deeper read: ..."' in agents.WRITER_SYSTEM
    assert "What it means:" in desk.ADVICE_BRIEF_Q and "Deeper read:" in desk.ADVICE_BRIEF_Q


def test_apple_advice_reply_leads_with_data_not_a_refusal():
    """Jayden 2026-11-21: answers must not open by saying what Europa cannot do. Still no buy or sell call."""
    from backend import desk
    for t in (desk.ADVICE_OPEN, desk.ADVICE_NO_FACTS, desk.ADVICE_PROMISE):
        assert not t.lower().startswith(("i can't", "i cannot", "i don't"))
    assert "the buy or sell decision stays with you" in desk.ADVICE_CLOSE


def test_fast_writer_for_ordinary_questions_strong_writer_for_long_summaries(monkeypatch):
    from backend import agents
    monkeypatch.delenv("MODEL_WRITER", raising=False)
    monkeypatch.delenv("EUROPA_FAST_WRITER", raising=False)
    assert agents.writer_models(False) is agents.WRITER_FAST_MODELS
    assert agents.writer_models(True) is agents.WRITER_MODELS
    monkeypatch.setenv("EUROPA_FAST_WRITER", "off")
    assert agents.writer_models(False) is agents.WRITER_MODELS
    monkeypatch.setenv("EUROPA_FAST_WRITER", "on")
    monkeypatch.setenv("MODEL_WRITER", "anything")
    assert agents.writer_models(False) is agents.WRITER_MODELS

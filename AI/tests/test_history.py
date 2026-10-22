from app.chat.history import approx_tokens, build_history


def msg(role, content, status="complete"):
    return {"role": role, "content": content, "status": status}


def convo(n_pairs, size=40):
    """u0 a0 u1 a1 ... each message `size` chars -> size//4 + 4 approx tokens."""
    out = []
    for i in range(n_pairs):
        out += [msg("user", f"u{i}".ljust(size, ".")), msg("assistant", f"a{i}".ljust(size, "."))]
    return out


def test_approx_tokens_is_the_documented_estimate():
    assert approx_tokens("") == 4
    assert approx_tokens("x" * 40) == 14


def test_everything_included_when_it_fits():
    history = build_history(convo(2), budget_tokens=10_000)
    assert [m["content"][:2] for m in history.messages] == ["u0", "a0", "u1", "a1"]
    assert history.truncated is False


def test_budget_is_respected():
    messages = convo(10)  # 20 messages x 14 tokens = 280
    history = build_history(messages, budget_tokens=100)
    assert history.approx_tokens <= 100
    assert history.truncated is True
    assert len(history.messages) < len(messages)


def test_latest_messages_are_preferred_over_old_ones():
    history = build_history(convo(10), budget_tokens=100)
    kept = [m["content"][:2] for m in history.messages]
    assert kept[-1] == "a9"  # newest survives
    assert "u0" not in kept  # oldest is what got dropped
    assert kept == sorted(kept, key=lambda c: (int(c[1:]), c[0] == "a"))  # still chronological


def test_error_messages_are_skipped():
    rows = [msg("user", "q1"), msg("assistant", "broken partial", "error"), msg("user", "q2")]
    history = build_history(rows, budget_tokens=10_000)
    assert [m["content"] for m in history.messages] == ["q1", "q2"]


def test_empty_and_whitespace_messages_are_skipped():
    rows = [msg("user", "q1"), msg("assistant", ""), msg("assistant", "   \n"), msg("user", "q2")]
    assert [m["content"] for m in build_history(rows, 10_000).messages] == ["q1", "q2"]


def test_unfinished_streaming_rows_are_skipped():
    # the placeholder row for the reply we are ABOUT to generate, and a crash orphan
    rows = [msg("user", "q1"), msg("assistant", "half", "streaming"), msg("user", "q2"), msg("assistant", "", "streaming")]
    assert [m["content"] for m in build_history(rows, 10_000).messages] == ["q1", "q2"]


def test_cancelled_partial_replies_are_kept():
    rows = [msg("user", "q1"), msg("assistant", "partial answer", "cancelled"), msg("user", "q2")]
    assert [m["content"] for m in build_history(rows, 10_000).messages] == ["q1", "partial answer", "q2"]


def test_history_always_starts_with_a_user_turn():
    # u0 a0 u1 a1 u2: a budget that fits exactly the 4 newest would begin with a0
    # (an answer to a question the model can no longer see). It must be dropped.
    rows = convo(3)[:-1]
    per_message = approx_tokens("x" * 40)
    history = build_history(rows, budget_tokens=per_message * 4)
    assert [m["content"][:2] for m in history.messages] == ["u1", "a1", "u2"]
    assert history.messages[0]["role"] == "user"
    assert history.truncated is True


def test_newest_user_message_is_kept_even_if_it_alone_exceeds_the_budget():
    rows = [msg("user", "old"), msg("assistant", "reply"), msg("user", "x" * 4000)]
    history = build_history(rows, budget_tokens=50)
    assert [m["role"] for m in history.messages] == ["user"]
    assert len(history.messages[0]["content"]) == 4000


def test_output_uses_the_simple_app_format():
    history = build_history([msg("user", "hi")], 1000)
    assert history.messages == [{"role": "user", "content": "hi"}]

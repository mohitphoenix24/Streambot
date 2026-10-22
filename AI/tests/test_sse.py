import json

from app.chat.sse import sse


def test_exact_wire_format():
    assert sse("delta", {"text": "Hello"}) == 'event: delta\ndata: {"text": "Hello"}\n\n'


def test_event_ends_with_a_blank_line():
    out = sse("done", {})
    assert out.endswith("\n\n")  # TWO newlines terminate an event
    assert out.count("\n") == 3  # event line, data line, terminator


def test_payload_is_valid_json_on_one_line():
    out = sse("done", {"stop_reason": "completed", "usage": {"input_tokens": 1}, "cost": None})
    data_line = out.split("\n")[1]
    assert data_line.startswith("data: ")
    assert json.loads(data_line[len("data: "):]) == {
        "stop_reason": "completed", "usage": {"input_tokens": 1}, "cost": None,
    }


def test_newlines_inside_text_cannot_break_event_framing():
    # A raw newline in `data:` would end the line (or, doubled, the event) early.
    out = sse("delta", {"text": "line1\n\nline2"})
    assert out.count("\n\n") == 1 and out.endswith("\n\n")  # only the terminator
    assert json.loads(out.split("\n")[1][6:]) == {"text": "line1\n\nline2"}


def test_unicode_survives():
    out = sse("delta", {"text": "héllo 👋 世界"})
    assert json.loads(out.split("\n")[1][6:])["text"] == "héllo 👋 世界"

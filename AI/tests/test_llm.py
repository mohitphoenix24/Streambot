"""The OpenAI adapter, tested against a STUB of the SDK client. No network, no cost.

These tests prove OUR code maps SDK events/errors correctly. They cannot prove the
real API still emits those shapes; that is what the (rare, paid) smoke test in
scripts/smoke_openai.py is for.
"""
from types import SimpleNamespace as NS

import httpx2
import openai
import pytest

from app.config import Settings
from app.llm.client import (
    Completed, LLMError, OpenAILLM, TextDelta, clean_title, to_openai_input, translate_openai_error,
)

REQ = httpx2.Request("POST", "https://api.example/v1/responses")


def status_error(cls, status, code=None, headers=None):
    return cls("provider said something with sk-SECRET", response=httpx2.Response(status, request=REQ, headers=headers or {}), body={"code": code})


# ── stubs mimicking the bits of the SDK we touch ─────────────────────────
class StubStream:
    def __init__(self, items):
        self.items, self.closed = items, False

    def __aiter__(self):
        async def gen():
            for item in self.items:
                if isinstance(item, Exception):
                    raise item
                yield item
        return gen()

    async def close(self):
        self.closed = True


class StubResponses:
    def __init__(self, results):
        self.results, self.calls = list(results), []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def client_with(*results):
    return NS(responses=StubResponses(results))


def delta(text):
    return NS(type="response.output_text.delta", delta=text)


def completed(status="completed", reason=None, usage=(11, 7, 18), kind="response.completed", model="served-snapshot-1"):
    u = NS(input_tokens=usage[0], output_tokens=usage[1], total_tokens=usage[2]) if usage else None
    details = NS(reason=reason) if reason else None
    return NS(type=kind, response=NS(status=status, incomplete_details=details, usage=u, model=model))


@pytest.fixture
def llm_factory():
    def make(client, **overrides):
        s = Settings(
            openai_api_key="test-key", openai_model="test-model",
            max_output_tokens=321, llm_max_attempts=2, retry_base_delay_s=0.0,
            system_prompt="BE NICE", **overrides,
        )
        return OpenAILLM(s, client=client)
    return make


async def collect(llm, history=None):
    events, error = [], None
    try:
        async for e in llm.stream(history or [{"role": "user", "content": "hi"}]):
            events.append(e)
    except LLMError as exc:
        error = exc
    return events, error


# ── request construction ─────────────────────────────────────────────────
async def test_stream_request_uses_responses_api_with_expected_parameters(llm_factory):
    stream = StubStream([delta("Hi"), completed()])
    client = client_with(stream)
    llm = llm_factory(client)

    await collect(llm, [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}, {"role": "user", "content": "q2"}])

    (call,) = client.responses.calls
    assert call["model"] == "test-model"
    assert call["stream"] is True
    assert call["store"] is False  # we own history; don't keep it at the provider
    assert call["instructions"] == "BE NICE"  # system prompt goes in `instructions`
    assert call["max_output_tokens"] == 321
    assert call["input"] == [
        {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}, {"role": "user", "content": "q2"},
    ]
    assert "api_key" not in call


def test_history_conversion_isolated_in_one_function():
    assert to_openai_input([{"role": "user", "content": "x"}]) == [{"role": "user", "content": "x"}]


# ── event mapping ────────────────────────────────────────────────────────
async def test_text_deltas_and_usage_are_normalised(llm_factory):
    stream = StubStream([NS(type="response.created"), delta("Hel"), NS(type="response.output_item.added"), delta("lo"), completed()])
    events, error = await collect(llm_factory(client_with(stream)))

    assert error is None
    assert events[:2] == [TextDelta("Hel"), TextDelta("lo")]  # bookkeeping events ignored
    last = events[-1]
    assert isinstance(last, Completed) and last.stop_reason == "completed"
    assert (last.usage.input_tokens, last.usage.output_tokens, last.usage.total_tokens) == (11, 7, 18)
    assert last.served_model == "served-snapshot-1"  # provider-reported model is captured
    assert stream.closed is True  # upstream HTTP response always closed


async def test_hitting_the_output_limit_is_reported_as_max_output_tokens(llm_factory):
    stream = StubStream([delta("trunc"), completed("incomplete", "max_output_tokens", kind="response.incomplete")])
    events, _ = await collect(llm_factory(client_with(stream)))
    assert events[-1].stop_reason == "max_output_tokens"


async def test_stream_without_terminal_event_is_an_error(llm_factory):
    events, error = await collect(llm_factory(client_with(StubStream([delta("a"), delta("b")]))))
    assert [type(e) for e in events] == [TextDelta, TextDelta]
    assert error is not None  # never silently treated as complete


async def test_response_failed_event_becomes_llm_error(llm_factory):
    failed = NS(type="response.failed", response=NS(error=NS(code="invalid_prompt", message="secret detail")))
    _, error = await collect(llm_factory(client_with(StubStream([failed]))))
    assert error is not None and "secret detail" not in error.public_message


async def test_stream_is_closed_even_if_the_consumer_stops_early(llm_factory):
    stream = StubStream([delta("a"), delta("b"), delta("c"), completed()])
    llm = llm_factory(client_with(stream))
    agen = llm.stream([{"role": "user", "content": "hi"}])
    await agen.__anext__()
    await agen.aclose()  # e.g. the user pressed Stop
    assert stream.closed is True


# ── retry integration (the real OpenAILLM + the real retry loop) ─────────
async def test_connection_error_before_first_token_is_retried(llm_factory):
    client = client_with(openai.APIConnectionError(request=REQ), StubStream([delta("ok"), completed()]))
    events, error = await collect(llm_factory(client))
    assert error is None and events[0] == TextDelta("ok")
    assert len(client.responses.calls) == 2


async def test_failure_after_first_token_is_not_retried(llm_factory):
    first = StubStream([delta("Hello my"), openai.APIConnectionError(request=REQ)])
    second = StubStream([delta("Hello my name is"), completed()])
    client = client_with(first, second)

    events, error = await collect(llm_factory(client))

    assert events == [TextDelta("Hello my")]
    assert error is not None
    assert len(client.responses.calls) == 1  # the second stream was never requested
    assert first.closed is True


async def test_retries_stop_after_the_configured_attempts(llm_factory):
    client = client_with(*[status_error(openai.InternalServerError, 503)] * 5)
    _, error = await collect(llm_factory(client))
    assert error is not None and len(client.responses.calls) == 2  # LLM_MAX_ATTEMPTS=2


# ── error classification ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    "exc, retryable",
    [
        (openai.APIConnectionError(request=REQ), True),
        (openai.APITimeoutError(REQ), True),
        (httpx2.ReadError("reset"), True),
        (status_error(openai.InternalServerError, 500), True),
        (status_error(openai.InternalServerError, 503), True),
        (status_error(openai.RateLimitError, 429, code="rate_limit_exceeded"), True),
        (status_error(openai.RateLimitError, 429, code="insufficient_quota"), False),  # out of money != transient
        (status_error(openai.AuthenticationError, 401), False),
        (status_error(openai.PermissionDeniedError, 403), False),
        (status_error(openai.NotFoundError, 404), False),
        (status_error(openai.BadRequestError, 400), False),
    ],
)
def test_only_transient_failures_are_retryable(exc, retryable):
    err = translate_openai_error(exc)
    assert err.retryable is retryable


@pytest.mark.parametrize(
    "exc",
    [
        status_error(openai.AuthenticationError, 401),
        status_error(openai.RateLimitError, 429, code="insufficient_quota"),
        status_error(openai.BadRequestError, 400, code="context_length_exceeded"),
        status_error(openai.InternalServerError, 500),
        openai.APIConnectionError(request=REQ),
    ],
)
def test_public_messages_never_leak_provider_text(exc):
    assert "sk-SECRET" not in translate_openai_error(exc).public_message


def test_retry_after_header_is_parsed():
    err = translate_openai_error(status_error(openai.RateLimitError, 429, headers={"retry-after": "7"}))
    assert err.retryable and err.retry_after == 7.0


# ── non-streaming + titles ───────────────────────────────────────────────
async def test_complete_returns_text_and_usage(llm_factory):
    response = NS(status="completed", incomplete_details=None, output_text="Full answer", usage=NS(input_tokens=3, output_tokens=4, total_tokens=7))
    client = client_with(response)
    result = await llm_factory(client).complete([{"role": "user", "content": "q"}])
    assert (result.text, result.stop_reason, result.usage.total_tokens) == ("Full answer", "completed", 7)
    assert "stream" not in client.responses.calls[0]  # normal request, not streaming


async def test_title_failure_returns_none_instead_of_breaking_the_chat(llm_factory):
    llm = llm_factory(client_with(openai.APIConnectionError(request=REQ)))
    assert await llm.generate_title("hello") is None


async def test_title_uses_the_title_model_when_configured(llm_factory):
    response = NS(output_text='"Understanding RAG."')
    client = client_with(response)
    title = await llm_factory(client, openai_title_model="cheap-model").generate_title("How does RAG work?")
    assert title == "Understanding RAG"
    assert client.responses.calls[0]["model"] == "cheap-model"


@pytest.mark.parametrize("raw, expected", [
    ('"Quoted Title"', "Quoted Title"), ("Trailing period.", "Trailing period"),
    ("First line\nSecond line", "First line"), ("   ", None), ("", None),
])
def test_clean_title(raw, expected):
    assert clean_title(raw) == expected


# ── agent loop: model -> tool -> model ───────────────────────────────────
from app.llm.client import ToolCallFinished, ToolCallStarted  # noqa: E402


class FakeTools:
    """Stands in for app.tools.ToolRunner: records calls, returns a canned outcome."""

    definitions = [{"type": "function", "name": "web_search"}]
    instructions = "USE TOOLS WISELY"

    def __init__(self, error=None):
        self.calls, self.error = [], error

    async def run(self, name, arguments):
        self.calls.append((name, arguments))
        sources = () if self.error else ({"n": 1, "title": "T", "url": "https://e.com", "snippet": "s"},)
        return NS(query="q", model_text="[1] T", sources=sources, error=self.error)


def fn_call(call_id="call_1", args='{"query": "python release"}'):
    return NS(type="function_call", call_id=call_id, name="web_search", arguments=args)


def round_end(*output, usage=(10, 5, 15), status="completed", reason=None, kind="response.completed"):
    u = NS(input_tokens=usage[0], output_tokens=usage[1], total_tokens=usage[2]) if usage else None
    details = NS(reason=reason) if reason else None
    return NS(type=kind, response=NS(status=status, incomplete_details=details, usage=u, model="m", output=list(output)))


async def collect_with_tools(llm, tools):
    events, error = [], None
    try:
        async for e in llm.stream([{"role": "user", "content": "hi"}], tools):
            events.append(e)
    except LLMError as exc:
        error = exc
    return events, error


async def test_tool_call_runs_the_tool_and_continues_with_its_output(llm_factory):
    reasoning = NS(type="reasoning", id="rs_1", encrypted_content="enc")
    client = client_with(
        StubStream([round_end(reasoning, fn_call())]),
        StubStream([delta("Answer [1]"), round_end(usage=(20, 8, 28))]),
    )
    tools = FakeTools()
    events, error = await collect_with_tools(llm_factory(client), tools)

    assert error is None
    assert [type(e) for e in events] == [ToolCallStarted, ToolCallFinished, TextDelta, Completed]
    assert events[0] == ToolCallStarted("call_1", "web_search", "python release")
    assert events[1].sources[0]["url"] == "https://e.com" and events[1].error is None
    assert tools.calls == [("web_search", '{"query": "python release"}')]

    first, second = client.responses.calls
    assert first["tools"] == tools.definitions and first["include"] == ["reasoning.encrypted_content"]
    assert first["instructions"] == "BE NICE\n\nUSE TOOLS WISELY"  # guidance only when tools are on
    assert "tool_choice" not in first
    # round 2 carries: the question, the model's reasoning + call, and the tool output
    assert second["input"][0] == {"role": "user", "content": "hi"}
    assert {"type": "reasoning", "id": "rs_1", "encrypted_content": "enc"} in second["input"]
    assert second["input"][-1] == {"type": "function_call_output", "call_id": "call_1", "output": "[1] T"}


async def test_usage_is_summed_over_all_rounds(llm_factory):
    client = client_with(
        StubStream([round_end(fn_call(), usage=(10, 5, 15))]),
        StubStream([delta("x"), round_end(usage=(20, 8, 28))]),
    )
    events, _ = await collect_with_tools(llm_factory(client), FakeTools())
    done = events[-1]
    assert (done.usage.input_tokens, done.usage.output_tokens, done.usage.total_tokens) == (30, 13, 43)


async def test_usage_is_unknown_if_any_round_reported_none(llm_factory):
    client = client_with(
        StubStream([round_end(fn_call(), usage=None)]),
        StubStream([delta("x"), round_end(usage=(20, 8, 28))]),
    )
    events, _ = await collect_with_tools(llm_factory(client), FakeTools())
    assert events[-1].usage is None  # a partial sum would under-report cost


async def test_round_cap_forces_an_answer(llm_factory):
    # cap = 1 search: the 2nd request must forbid further tool calls
    client = client_with(
        StubStream([round_end(fn_call())]),
        StubStream([delta("final"), round_end()]),
    )
    events, _ = await collect_with_tools(llm_factory(client, agent_max_searches=1), FakeTools())
    first, second = client.responses.calls
    assert "tool_choice" not in first and second["tool_choice"] == "none"
    assert second["tools"] == FakeTools.definitions  # same tool list keeps the prompt prefix identical
    assert isinstance(events[-1], Completed)


async def test_even_if_the_model_calls_a_tool_at_the_cap_it_is_not_run(llm_factory):
    client = client_with(
        StubStream([round_end(fn_call())]),
        StubStream([round_end(fn_call("call_2"))]),  # disobeys tool_choice=none
    )
    tools = FakeTools()
    events, _ = await collect_with_tools(llm_factory(client, agent_max_searches=1), tools)
    assert len(tools.calls) == 1 and isinstance(events[-1], Completed)


async def test_a_retried_round_never_repeats_the_search(llm_factory):
    client = client_with(
        StubStream([round_end(fn_call())]),
        openai.APIConnectionError(request=REQ),  # round 2, attempt 1: fails before any token
        StubStream([delta("ok"), round_end()]),  # round 2, attempt 2
    )
    tools = FakeTools()
    events, error = await collect_with_tools(llm_factory(client), tools)
    assert error is None and len(tools.calls) == 1  # the tool ran once, not once per attempt
    assert len(client.responses.calls) == 3
    assert any(e == TextDelta("ok") for e in events)


async def test_tool_errors_are_passed_to_the_model_not_raised(llm_factory):
    client = client_with(
        StubStream([round_end(fn_call())]),
        StubStream([delta("sorry"), round_end()]),
    )
    events, error = await collect_with_tools(llm_factory(client), FakeTools(error="Web search is unavailable right now."))
    assert error is None
    assert events[1] == ToolCallFinished("call_1", "web_search", (), "Web search is unavailable right now.")


async def test_a_truncated_round_does_not_run_tools(llm_factory):
    client = client_with(StubStream([round_end(fn_call(), status="incomplete", reason="max_output_tokens", kind="response.incomplete")]))
    tools = FakeTools()
    events, _ = await collect_with_tools(llm_factory(client), tools)
    assert tools.calls == [] and events[-1].stop_reason == "max_output_tokens"


async def test_without_tools_the_request_is_exactly_as_before(llm_factory):
    client = client_with(StubStream([delta("hi"), completed()]))
    await collect(llm_factory(client))
    (call,) = client.responses.calls
    assert "tools" not in call and "include" not in call and "tool_choice" not in call
    assert call["instructions"] == "BE NICE"


async def test_default_cap_is_ten_searches_per_reply():
    assert Settings().agent_max_searches == 10


async def test_parallel_calls_beyond_the_cap_are_answered_but_not_run(llm_factory):
    client = client_with(
        StubStream([round_end(fn_call("c1"), fn_call("c2"), fn_call("c3"))]),
        StubStream([delta("done"), round_end()]),
    )
    tools = FakeTools()
    events, error = await collect_with_tools(llm_factory(client, agent_max_searches=2), tools)

    assert error is None and len(tools.calls) == 2  # only 2 of the 3 searches ran
    second = client.responses.calls[1]
    outputs = [i for i in second["input"] if i.get("type") == "function_call_output"]
    assert [o["call_id"] for o in outputs] == ["c1", "c2", "c3"]  # the API requires an output for every call
    assert "limit" in outputs[2]["output"]
    assert second["tool_choice"] == "none"  # cap reached -> must answer
    assert sum(isinstance(e, ToolCallStarted) for e in events) == 2

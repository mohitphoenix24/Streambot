"""The Tavily search tool and the per-reply ToolRunner. No network: `fetch` is injected."""
import json

import pytest

from app.tools.registry import ToolRunner
from app.tools.web_search import TavilySearch, ToolError


def fake_fetch(payload=None, error=None):
    calls = []

    async def fetch(query, n):
        calls.append((query, n))
        if error:
            raise error
        return payload

    fetch.calls = calls
    return fetch


RESULTS = {"results": [
    {"title": "Python 3.14 released", "url": "https://python.org/news", "content": "  Python   3.14\nis out.  "},
    {"title": "Bad scheme", "url": "javascript:alert(1)", "content": "x"},
    {"title": "", "url": "https://example.com/a", "content": "no title"},
]}


async def test_results_are_trimmed_and_non_web_urls_dropped():
    search = TavilySearch("k", max_results=5, fetch=fake_fetch(RESULTS))
    sources = await search.search("python release")
    assert [s.url for s in sources] == ["https://python.org/news", "https://example.com/a"]
    assert sources[0].snippet == "Python 3.14 is out."  # whitespace collapsed
    assert sources[1].title == "example.com"  # missing title falls back to the host


async def test_max_results_is_requested_and_enforced():
    many = {"results": [{"title": f"t{i}", "url": f"https://e.com/{i}", "content": "c"} for i in range(9)]}
    fetch = fake_fetch(many)
    sources = await TavilySearch("k", max_results=3, fetch=fetch).search("q")
    assert len(sources) == 3 and fetch.calls == [("q", 3)]


async def test_provider_failure_becomes_a_safe_tool_error():
    search = TavilySearch("k", fetch=fake_fetch(error=RuntimeError("401 key=SECRET https://api.tavily.com")))
    with pytest.raises(ToolError) as err:
        await search.search("q")
    assert "SECRET" not in err.value.public_message and "tavily" not in err.value.public_message.lower()


async def test_empty_query_is_rejected_without_a_request():
    fetch = fake_fetch(RESULTS)
    with pytest.raises(ToolError):
        await TavilySearch("k", fetch=fetch).search("   ")
    assert fetch.calls == []


async def test_runner_numbers_sources_across_searches():
    runner = ToolRunner(TavilySearch("k", fetch=fake_fetch(RESULTS)))
    first = await runner.run("web_search", json.dumps({"query": "one"}))
    second = await runner.run("web_search", json.dumps({"query": "two"}))

    assert [s["n"] for s in first.sources] == [1, 2]
    assert [s["n"] for s in second.sources] == [3, 4]  # numbering continues within the reply
    assert "[3] " in second.model_text and "https://python.org/news" in second.model_text
    assert [s["n"] for s in runner.sources] == [1, 2, 3, 4]


async def test_runner_reports_tool_errors_to_the_model_instead_of_raising():
    runner = ToolRunner(TavilySearch("k", fetch=fake_fetch(error=RuntimeError("boom"))))
    out = await runner.run("web_search", '{"query": "q"}')
    assert out.error and out.model_text.startswith("Error:") and out.sources == ()


@pytest.mark.parametrize("name, args", [("web_search", "not json"), ("web_search", "{}"), ("rm_rf", '{"query":"x"}')])
async def test_runner_survives_bad_calls(name, args):
    runner = ToolRunner(TavilySearch("k", fetch=fake_fetch(RESULTS)))
    out = await runner.run(name, args)
    assert out.error and out.sources == ()


async def test_no_results_is_not_an_error():
    runner = ToolRunner(TavilySearch("k", fetch=fake_fetch({"results": []})))
    out = await runner.run("web_search", '{"query": "q"}')
    assert out.error is None and out.model_text == "No results found."

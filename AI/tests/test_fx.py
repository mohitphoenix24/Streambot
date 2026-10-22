"""Live USD->INR display rate: fetch, cache, fallback. The fetcher is injected: no network."""
import pytest

from app.core.fx import FxRate, UsdInrProvider


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


def provider(fetch, clock=None, **kw):
    return UsdInrProvider(None, kw.pop("fallback", 96.0), fetch=fetch, clock=clock or Clock(), **kw)


def payload(inr=96.38):
    return {"result": "success", "rates": {"INR": inr}, "time_last_update_utc": "Tue, 06 Oct 2026 00:02:31 +0000"}


async def test_live_rate_is_fetched_and_reported_with_its_timestamp():
    async def fetch(): return payload(96.38)
    r = await provider(fetch).get()
    assert r == FxRate(96.38, "live", "Tue, 06 Oct 2026 00:02:31 +0000")


async def test_rate_is_cached_so_we_do_not_call_the_api_on_every_request():
    calls = []
    async def fetch(): calls.append(1); return payload()
    clock = Clock(); p = provider(fetch, clock, ttl_s=100)
    await p.get(); await p.get(); clock.t = 99; await p.get()
    assert len(calls) == 1
    clock.t = 101; await p.get()  # expired: refresh
    assert len(calls) == 2


async def test_failure_with_no_prior_rate_uses_the_fallback_without_crashing():
    async def fetch(): raise OSError("no network")
    r = await provider(fetch, fallback=96.0).get()
    assert (r.rate, r.source) == (96.0, "fallback")


async def test_failure_after_a_good_rate_keeps_the_last_known_live_rate():
    state = {"ok": True}
    async def fetch():
        if state["ok"]: return payload(95.5)
        raise OSError("down")
    clock = Clock(); p = provider(fetch, clock, ttl_s=10)
    assert (await p.get()).rate == 95.5
    state["ok"] = False; clock.t = 11
    r = await p.get()
    assert (r.rate, r.source) == (95.5, "live")  # stale but real beats a guess


async def test_failed_lookups_are_not_retried_on_every_request():
    calls = []
    async def fetch(): calls.append(1); raise OSError("down")
    clock = Clock(); p = provider(fetch, clock, retry_after_s=60)
    await p.get(); await p.get(); await p.get()
    assert len(calls) == 1
    clock.t = 61; await p.get()
    assert len(calls) == 2


@pytest.mark.parametrize("bad", [{}, {"rates": {}}, {"rates": {"INR": "abc"}}, {"rates": {"INR": 0}}, {"rates": {"INR": -5}}])
async def test_bad_payloads_fall_back_instead_of_showing_nonsense(bad):
    async def fetch(): return bad
    r = await provider(fetch, fallback=96.0).get()
    assert (r.rate, r.source) == (96.0, "fallback")


async def test_fixed_rate_never_fetches_and_zero_hides_inr():
    async def fetch(): raise AssertionError("must not fetch")
    assert await UsdInrProvider(91.5, 96.0, fetch=fetch).get() == FxRate(91.5, "fixed")
    assert await UsdInrProvider(0, 96.0, fetch=fetch).get() == FxRate(None, "off")

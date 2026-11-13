"""The model call: one request per analysis, and waits/retries when the service is rate limited
(run: python tests/test_model_call.py). A fake client stands in for Claude: no network, no cost."""
import importlib.util
import json
import types
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("stockscan_api_model_call", ROOT / "api" / "index.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)

SKILL = {"id": "warren-buffett", "instructions": "Analyze.", "output_schema": {"type": "object", "required": ["recommendation"]}}
DATA = {"symbol": "TRI"}


def _rate_limit(retry_after=None):
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    e = anthropic.RateLimitError.__new__(anthropic.RateLimitError)  # no HTTP library needed for the fake
    Exception.__init__(e, "rate limited")
    e.response, e.status_code = types.SimpleNamespace(headers=headers), 429
    return e


class _Stream:
    def __init__(self):
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True

    def __iter__(self):
        return iter([])

    def get_final_message(self):
        return types.SimpleNamespace(
            stop_reason="end_turn", usage=types.SimpleNamespace(input_tokens=10, output_tokens=5),
            content=[types.SimpleNamespace(type="text", text=json.dumps({"recommendation": "BUY"}))], model="m")


class _Client:
    """messages.stream(...) returns a manager; entering it is what sends a request (like the real SDK)."""

    def __init__(self, failures):
        self.requests = 0
        self.failures = list(failures)
        self.base_url = "https://example.invalid"
        self.messages = types.SimpleNamespace(stream=self._stream)

    def _stream(self, **req):
        client = self

        class Manager:
            def __enter__(self):
                client.requests += 1
                if client.failures:
                    raise client.failures.pop(0)
                return _Stream()

            def __exit__(self, *a):
                pass

        return Manager()


def _run(client):
    sleeps = []
    saved = api.require_claude, api.make_claude_client, api.time.sleep
    api.require_claude = lambda: "foundry"
    api.make_claude_client = lambda provider: client
    api.time.sleep = sleeps.append
    log = api.RunLog("x", "y")
    try:
        return api.run_skill_with_claude(SKILL, DATA, log), sleeps, log
    finally:
        api.require_claude, api.make_claude_client, api.time.sleep = saved


def test_one_analysis_sends_exactly_one_request():
    client = _Client([])
    (analysis, _), sleeps, _ = _run(client)
    assert analysis == {"recommendation": "BUY"}
    assert client.requests == 1, f"{client.requests} requests sent for one analysis"
    assert sleeps == []


def test_rate_limit_waits_and_retries_using_retry_after():
    client = _Client([_rate_limit(retry_after=7), _rate_limit()])
    (analysis, _), sleeps, log = _run(client)
    assert analysis == {"recommendation": "BUY"} and client.requests == 3
    assert sleeps == [7.0, 10.0]  # retry-after honoured, then the default backoff for attempt 2
    assert sum("rate limited (429)" in e["message"] for e in log.entries) == 2


def test_rate_limit_gives_up_within_the_budget_with_a_recognisable_error():
    client = _Client([_rate_limit(retry_after=30)] * 10)
    try:
        _run(client)
        raise AssertionError("expected HTTPException")
    except api.HTTPException as e:
        assert e.status_code == 503 and api.is_transient_model_error(e.detail)
    assert client.requests == 3  # 30 + 30 s waited; a third wait would pass the 60 s budget


def test_transient_errors_are_recognised():
    assert api.is_transient_model_error(api.RATE_LIMIT_DETAIL)
    assert api.is_transient_model_error(api.OVERLOADED_DETAIL)
    assert not api.is_transient_model_error("The model did not return valid JSON.")
    assert not api.is_transient_model_error(None)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

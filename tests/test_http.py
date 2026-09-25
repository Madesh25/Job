import json

import httpx
import pytest

from jobengine import http
from jobengine.safety import SafetyError, assert_fetch_allowed
from jobengine.settings import load_settings

S = load_settings("dev", {})


@pytest.fixture
def transport():
    calls = []
    responses = []

    def handler(request):
        calls.append(request)
        if responses:
            return responses.pop(0)
        return httpx.Response(200, json={"ok": True})

    http.set_transport(httpx.MockTransport(handler))
    yield calls, responses
    http.set_transport(None)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/view/123",
        "https://linkedin.com/jobs/view/123",
        "https://api.linkedin.com/v2/jobs",
    ],
)
def test_linkedin_is_always_refused(url, transport):
    calls, _ = transport
    with pytest.raises(SafetyError, match="linkedin"):
        http.get_json(url, s=S)
    permissive = S.model_copy(
        update={"allowed_hosts": S.allowed_hosts + ("www.linkedin.com", "linkedin.com",
                                                    "api.linkedin.com")}
    )
    with pytest.raises(SafetyError, match="linkedin"):
        http.get_json(url, s=permissive)
    assert calls == []


def test_host_not_on_allowlist_is_refused(transport):
    calls, _ = transport
    with pytest.raises(SafetyError, match="not on safety.allowed_hosts"):
        http.get_json("https://click.example.com/track?u=1", s=S)
    with pytest.raises(SafetyError, match="only https"):
        http.get_json("http://api.adzuna.com/v1/api/jobs/pl/search/1", s=S)
    assert calls == []


def test_assert_fetch_allowed_accepts_allowlisted_hosts():
    for host in S.allowed_hosts:
        assert_fetch_allowed(f"https://{host}/x", S)


def test_get_json_passes_params_and_headers(transport):
    calls, _ = transport
    data = http.get_json(
        "https://api.adzuna.com/v1/api/jobs/pl/search/1",
        params={"app_id": "id", "app_key": "secret"},
        headers={"Accept": "application/json"},
        s=S,
    )
    assert data == {"ok": True}
    assert calls[0].url.params["app_key"] == "secret"
    assert calls[0].headers["Accept"] == "application/json"


def test_errors_never_include_query_string(transport):
    _, responses = transport
    responses.append(httpx.Response(401, json={"message": "bad key"}))
    with pytest.raises(http.HttpError) as info:
        http.get_json(
            "https://api.adzuna.com/v1/api/jobs/pl/search/1",
            params={"app_key": "super-secret-key"},
            s=S,
        )
    message = str(info.value)
    assert "super-secret-key" not in message and "app_key" not in message
    assert message == (
        "GET https://api.adzuna.com/v1/api/jobs/pl/search/1 failed: HTTP 401 bad key"
    )
    assert info.value.status == 401


def test_429_is_retried_with_retry_after(transport, monkeypatch):
    calls, responses = transport
    waits = []
    monkeypatch.setattr(http, "_sleep", waits.append)
    responses.extend(
        [
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(429, headers={"Retry-After": "0.5"}),
            httpx.Response(200, json={"done": 1}),
        ]
    )
    assert http.post_json("https://api.notion.com/v1/pages", json={"a": 1}, s=S) == {"done": 1}
    assert waits == [2.0, 0.5]
    assert len(calls) == 3
    assert json.loads(calls[0].content) == {"a": 1}


def test_429_gives_up_after_max_retries(transport, monkeypatch):
    _, responses = transport
    monkeypatch.setattr(http, "_sleep", lambda _: None)
    responses.extend([httpx.Response(429)] * (http.MAX_429_RETRIES + 1))
    with pytest.raises(http.HttpError) as info:
        http.patch_json("https://api.notion.com/v1/pages/x", json={}, s=S)
    assert info.value.status == 429


def test_network_error_is_wrapped(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("no route", request=request)

    http.set_transport(httpx.MockTransport(boom))
    try:
        with pytest.raises(http.HttpError, match="ConnectError"):
            http.get_json("https://api.lever.co/v0/postings/acme?mode=json", s=S)
    finally:
        http.set_transport(None)


def test_http_library_request_logs_are_silenced():
    """httpx logs full URLs (with the Adzuna key) at INFO; it must stay at WARNING."""
    import logging

    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_query_string_never_reaches_logs(transport, caplog):
    import logging

    logging.getLogger().setLevel(logging.DEBUG)
    try:
        with caplog.at_level(logging.DEBUG):
            http.get_json(
                "https://api.adzuna.com/v1/api/jobs/pl/search/1",
                params={"app_key": "super-secret-key"}, s=S,
            )
    finally:
        logging.getLogger().setLevel(logging.WARNING)
    assert "super-secret-key" not in caplog.text

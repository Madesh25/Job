"""PR 10b step 1: the Lusha probe prints the answer's shape, never its values."""

import pytest

from jobengine.contacts import probe
from jobengine.contacts.providers import lusha
from jobengine.contacts.providers.base import ProviderDeps
from jobengine.settings import load_settings

ANSWER = {
    "requestId": "7f0c2b1e-0000-4000-8000-000000000000",
    "totalResults": 2,
    "data": [
        {"contactId": "c-1", "name": "Ewa Sample", "jobTitle": "DevOps Engineer",
         "hasEmails": True, "emails": [{"email": "ewa@vistula.example.com", "type": "work"}],
         "location": {"country": "Poland"}},
        {"contactId": "c-2", "name": "Jan Example"},
    ],
    "byId": {"c-1": {"x": 1}, "ewa@vistula.example.com": {"y": None}},
    "billing": {"creditsCharged": 0},
    "empty": [],
}


def test_shape_has_fields_and_types_only():
    paths = lusha.shape(ANSWER)
    assert "requestId: str" in paths
    assert "data[].emails[].email: str" in paths
    assert "data[].location.country: str" in paths
    assert "billing.creditsCharged: int" in paths
    assert "empty[]: empty" in paths
    assert "byId.<key>.x: int" in paths  # an email used as a key is not shown
    text = "\n".join(paths)
    for secret in ("Ewa", "vistula", "c-1", "7f0c2b1e", "DevOps"):
        assert secret not in text


def test_probe_request_and_output():
    seen = {}

    def request(method, url, *, params=None, headers=None, json_body=None):
        seen.update(method=method, url=url, headers=headers, body=json_body)
        return ANSWER

    s = load_settings("prod", {"DRY_RUN": "false", "LUSHA_API_KEY": "l-key"})
    count, paths = lusha.probe("Vistula Cloud", ProviderDeps(s=s, request=request,
                                                             search_titles={}))
    assert seen == {"method": "POST", "url": "https://api.lusha.com/prospecting/contact/search",
                    "headers": {"api_key": "l-key", "Content-Type": "application/json"},
                    "body": {"pages": {"page": 0, "size": 10}, "filters": {
                        "companies": {"include": {"names": ["Vistula Cloud"]}}}}}
    assert count == 2 and "data[].contactId: str" in paths


def test_probe_is_prod_only_and_needs_the_key():
    with pytest.raises(PermissionError):
        probe.run_probe("lusha", "Vistula Cloud", load_settings("dev", {"DRY_RUN": "false"}))
    s = load_settings("prod", {"DRY_RUN": "false"})
    with pytest.raises(PermissionError, match="LUSHA_API_KEY"):
        lusha.probe("Vistula Cloud", ProviderDeps(s=s, request=lambda *a, **k: {},
                                                  search_titles={}))
    assert "lusha" in probe.PROVIDERS
    assert "api.lusha.com" in s.allowed_hosts

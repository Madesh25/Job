"""--probe: one real search call to check free-plan API access. No reveal, nothing written,
counters unchanged. Refused outside prod (and in DRY_RUN): the call would spend a credit
on some plans, so it only runs where paid calls are allowed."""

from __future__ import annotations

from jobengine.contacts.providers import apollo, hunter, snov
from jobengine.contacts.providers.base import ProviderDeps, real_request
from jobengine.safety import paid_api_allowed
from jobengine.settings import Settings

PROVIDERS = {"apollo": apollo, "hunter": hunter, "snov": snov}
REFUSED = ("--probe makes a real provider call, so it only runs with APP_ENV=prod and "
           "DRY_RUN=false.")


def run_probe(provider: str, domain: str, s: Settings) -> str:
    if not paid_api_allowed(provider, s):
        raise PermissionError(REFUSED)
    module = PROVIDERS[provider]
    deps = ProviderDeps(s=s, request=real_request(s),
                        search_titles=s.contacts.get("search_titles") or {})
    count, fields = module.probe(domain, deps)
    return (f"{provider} probe for {domain}: {count} results. Fields present: "
            f"{', '.join(fields) or 'none'}. Nothing was written and no counter changed.")

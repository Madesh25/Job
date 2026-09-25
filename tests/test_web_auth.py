import pytest

from jobengine.settings import load_settings
from jobengine.web.auth import AuthError, check_scheduler_token, check_telegram_secret

URL = "https://job-engine-dev-abc.a.run.app"
SA = "job-engine-scheduler@job-search-engine-509510.iam.gserviceaccount.com"


def settings(**extra):
    return load_settings("dev", {"TELEGRAM_WEBHOOK_SECRET": "s3cretS3cretS3cretS3cretS3cret12",
                                 "SERVICE_URL": URL, "SCHEDULER_SA_EMAIL": SA, **extra})


def verifier(claims=None, fail=False):
    def verify(token):
        if fail or token != "good":
            raise ValueError("bad signature")
        return claims or {"aud": URL, "email": SA, "email_verified": True}
    return verify


def test_telegram_secret():
    s = settings()
    check_telegram_secret("s3cretS3cretS3cretS3cretS3cret12", s)
    for header in (None, "", "wrong"):
        with pytest.raises(AuthError) as exc:
            check_telegram_secret(header, s)
        assert exc.value.status == 401
    unset = load_settings("dev", {})
    with pytest.raises(AuthError):
        check_telegram_secret("", unset)


def test_scheduler_token_ok():
    claims = check_scheduler_token("Bearer good", settings(), verifier())
    assert claims["email"] == SA


@pytest.mark.parametrize("header", [None, "", "Basic good", "Bearer "])
def test_scheduler_token_missing_is_401(header):
    with pytest.raises(AuthError) as exc:
        check_scheduler_token(header, settings(), verifier())
    assert exc.value.status == 401


def test_invalid_token_is_401():
    with pytest.raises(AuthError) as exc:
        check_scheduler_token("Bearer forged", settings(), verifier())
    assert exc.value.status == 401


@pytest.mark.parametrize("claims", [
    {"aud": "https://other.a.run.app", "email": SA, "email_verified": True},
    {"aud": URL, "email": "someone@example.com", "email_verified": True},
    {"aud": URL, "email": SA, "email_verified": False},
])
def test_wrong_audience_or_caller_is_403(claims):
    with pytest.raises(AuthError) as exc:
        check_scheduler_token("Bearer good", settings(), verifier(claims))
    assert exc.value.status == 403

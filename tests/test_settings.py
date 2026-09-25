import pytest

from jobengine.settings import PROD_WRITE_IDS, load_settings

PROD_WRITE = {
    "job_opportunities": "0228ce56-475a-4b60-8d6b-fd2a59297b24",
    "contacts": "ccb541f8-9e38-43e2-8d17-5dd6b24e6ea6",
    "resume_log": "9a768923-470a-4883-9fe7-8e156b4c5e97",
    "target_companies": "064bf98d-ab49-498d-9b94-2ecc41640230",
    "config": "84f44e56-ff55-4806-9859-50b3744fc325",
    "strategy": "4097af1c-faaf-455f-9616-728301043408",
}


def test_default_env_is_local_and_dry_run():
    s = load_settings(None, {})
    assert s.app_env == "local"
    assert s.dry_run is True
    assert s.env_label == "[LOCAL]"


@pytest.mark.parametrize("value", ["False", "0", "no", "", "FALSE", "true", None])
def test_dry_run_true_unless_exact_false(value):
    environ = {} if value is None else {"DRY_RUN": value}
    assert load_settings("local", environ).dry_run is True


def test_dry_run_false_only_for_exact_false():
    assert load_settings("local", {"DRY_RUN": "false"}).dry_run is False


@pytest.mark.parametrize("env", ["production", "staging", "PROD", ""])
def test_invalid_app_env_raises(env):
    with pytest.raises(ValueError):
        load_settings(env, {})


def test_invalid_app_env_from_environ_raises():
    with pytest.raises(ValueError):
        load_settings(None, {"APP_ENV": "test"})


def test_prod_loads_prod_write_ids_and_no_forced_model():
    s = load_settings("prod", {})
    assert s.notion_write == PROD_WRITE
    assert s.force_model is None
    assert s.env_label == ""
    assert s.gmail_sender == "madeshwaran.manikam@gmail.com"


def test_prod_write_ids_constant_matches_prod_yaml():
    assert PROD_WRITE_IDS == frozenset(PROD_WRITE.values())


def test_missing_secrets_are_none_and_present_ones_are_read():
    s = load_settings("dev", {"NOTION_TOKEN": "dummy-token"})
    assert s.notion_token == "dummy-token"
    assert s.anthropic_api_key is None
    assert s.gmail_sender_token_json is None


def test_dev_and_local_force_haiku():
    for env in ("local", "dev"):
        s = load_settings(env, {})
        assert s.force_model == "claude-haiku-4-5"
        assert s.gmail_sender == "madeshwaranm02@gmail.com"

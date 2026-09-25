import json
from types import SimpleNamespace

from jobengine.gmail_auth import NOT_LOCAL, main
from jobengine.gmail_client import SENDER_SCOPES


def creds(scopes=SENDER_SCOPES):
    return SimpleNamespace(client_id="id.example", client_secret="invented", token="access",
                           refresh_token="refresh", token_uri="https://oauth2.example.com",
                           scopes=list(scopes))


def test_refused_outside_local(capsys, tmp_path):
    def flow(*args):
        raise AssertionError("no consent screen outside local")

    code = main(["--client-secret", "c.json", "--account", "m02", "--out",
                 str(tmp_path / "t.json")], {"APP_ENV": "dev"}, flow=flow)
    assert code == 2
    assert NOT_LOCAL in capsys.readouterr().err


def test_writes_authorised_user_json(tmp_path, capsys):
    out = tmp_path / ".secrets" / "gmail-m02-token.json"
    hints = []
    code = main(["--client-secret", "c.json", "--account", "m02", "--out", str(out)], {},
                flow=lambda path, hint: hints.append(hint) or creds(),
                whoami=lambda c: "madeshwaranm02@gmail.com")
    assert code == 0
    data = json.loads(out.read_text())
    assert set(data) == {"client_id", "client_secret", "refresh_token", "token_uri", "scopes"}
    assert data["scopes"] == SENDER_SCOPES
    assert hints == ["madeshwaranm02@gmail.com"]
    assert "Authorised account: madeshwaranm02@gmail.com" in capsys.readouterr().out
    assert oct(out.stat().st_mode)[-3:] == "600"


def test_missing_scope_is_refused(tmp_path):
    code = main(["--client-secret", "c.json", "--account", "main", "--out",
                 str(tmp_path / "t.json")], {},
                flow=lambda path, hint: creds(SENDER_SCOPES[1:]), whoami=lambda c: "x")
    assert code == 1
    assert not (tmp_path / "t.json").exists()

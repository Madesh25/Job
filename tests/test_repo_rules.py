import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist"
}

LONG_DASHES = ["\u2014", "\u2013"]

# Built from pieces so this file does not match its own patterns.
SECRET_PATTERNS = [
    re.compile(re.escape("sk-" + "ant-")),
    re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}"),
    re.compile(re.escape('"private' + '_key":')),
    re.compile("ya" + r"29\."),
]

GMAIL_WRITE_CALLS = ["drafts" + "()", "messages" + "().send"]
GMAIL_WRITE_ALLOWED = {"src/jobengine/safety.py", "src/jobengine/gmail_client.py"}


def repo_text_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if rel.name == ".env":
            continue
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        files.append(path)
    return files


def test_repo_has_files_to_scan():
    names = {p.relative_to(ROOT).as_posix() for p in repo_text_files()}
    assert "src/jobengine/safety.py" in names
    assert "README.md" in names


def test_no_long_dashes():
    offenders = [
        p.relative_to(ROOT).as_posix()
        for p in repo_text_files()
        if any(ch in p.read_text(encoding="utf-8") for ch in LONG_DASHES)
    ]
    assert offenders == []


def test_no_secrets():
    offenders = []
    for p in repo_text_files():
        text = p.read_text(encoding="utf-8")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{p.relative_to(ROOT).as_posix()}: {pattern.pattern}")
    assert offenders == []


def test_env_file_not_in_repo_and_example_exists():
    assert (ROOT / ".env.example").is_file()
    git = shutil.which("git")
    if git and (ROOT / ".git").exists():
        tracked = subprocess.run(
            [git, "ls-files", "--", ".env"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert tracked == ""
    else:
        assert not (ROOT / ".env").exists()


def test_gmail_write_calls_only_in_safety_or_gmail_client():
    offenders = []
    for p in repo_text_files():
        rel = p.relative_to(ROOT).as_posix()
        if not rel.startswith("src/") or rel in GMAIL_WRITE_ALLOWED:
            continue
        text = p.read_text(encoding="utf-8")
        offenders.extend(f"{rel}: {call}" for call in GMAIL_WRITE_CALLS if call in text)
    assert offenders == []


HTTP_IMPORT = re.compile(r"^\s*(?:import|from)\s+(httpx|requests|urllib\.request)\b", re.M)
HTTP_IMPORT_ALLOWED = {
    "src/jobengine/http.py",
    "src/jobengine/telegram_bot.py",
    "src/jobengine/gmail_reader.py",
}
GMAIL_READER = "src/jobengine/gmail_reader.py"
GMAIL_FORBIDDEN = [".send(", "drafts(", "modify(", "trash(", "delete(", "batchModify"]
# A URL literal pointing at linkedin. Regex patterns in config/ are allowed; src/ has none.
LINKEDIN_URL = re.compile(r"https?://(?:[\w-]+\.)*linked" + r"in\.com", re.I)


def src_files():
    return [p for p in repo_text_files() if p.relative_to(ROOT).as_posix().startswith("src/")]


def test_http_libraries_only_imported_by_allowed_modules():
    offenders = []
    for p in src_files():
        rel = p.relative_to(ROOT).as_posix()
        if rel in HTTP_IMPORT_ALLOWED:
            continue
        for match in HTTP_IMPORT.finditer(p.read_text(encoding="utf-8")):
            offenders.append(f"{rel}: {match.group(1)}")
    assert offenders == []


def test_gmail_reader_is_read_only():
    path = ROOT / GMAIL_READER
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    assert [call for call in GMAIL_FORBIDDEN if call in text] == []


def test_no_request_to_linkedin_in_src():
    offenders = [
        p.relative_to(ROOT).as_posix()
        for p in src_files()
        if LINKEDIN_URL.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


ANTHROPIC_IMPORT = re.compile(r"^\s*(?:import|from)\s+anthropic\b", re.M)


def test_anthropic_imported_only_in_llm_module():
    offenders = [
        p.relative_to(ROOT).as_posix()
        for p in src_files()
        if ANTHROPIC_IMPORT.search(p.read_text(encoding="utf-8"))
        and p.relative_to(ROOT).as_posix() != "src/jobengine/llm.py"
    ]
    assert offenders == []


GOOGLE_CLIENT_IMPORT = re.compile(r"^\s*(?:import|from)\s+googleapiclient\b", re.M)
GOOGLE_CLIENT_ALLOWED = {
    "src/jobengine/gmail_reader.py",
    "src/jobengine/drive_client.py",
    "src/jobengine/gmail_client.py",
    "src/jobengine/gmail_auth.py",
}
# Module 06: drafts only. Never send, remove, trash, edit a draft or create labels.
GMAIL_CLIENT = "src/jobengine/gmail_client.py"
GMAIL_CLIENT_FORBIDDEN = [".send(", "delete(", "trash(", "batchDelete", "drafts().update",
                          "labels().create"]
# The token helper only reads the profile of the account it authorised.
GMAIL_AUTH = "src/jobengine/gmail_auth.py"
GMAIL_AUTH_FORBIDDEN = [*GMAIL_CLIENT_FORBIDDEN, "drafts(", "modify("]
DRIVE_FORBIDDEN = [".delete(", "permissions(", "emptyTrash", ".update(", ".copy("]


def test_google_api_client_only_in_gmail_reader_and_drive_client():
    offenders = [
        p.relative_to(ROOT).as_posix()
        for p in src_files()
        if GOOGLE_CLIENT_IMPORT.search(p.read_text(encoding="utf-8"))
        and p.relative_to(ROOT).as_posix() not in GOOGLE_CLIENT_ALLOWED
    ]
    assert offenders == []


def test_gmail_client_never_sends_or_deletes():
    text = (ROOT / GMAIL_CLIENT).read_text(encoding="utf-8")
    assert [call for call in GMAIL_CLIENT_FORBIDDEN if call in text] == []
    auth = (ROOT / GMAIL_AUTH).read_text(encoding="utf-8")
    assert [call for call in GMAIL_AUTH_FORBIDDEN if call in auth] == []


def test_drive_client_never_deletes_or_shares():
    text = (ROOT / "src/jobengine/drive_client.py").read_text(encoding="utf-8")
    assert [call for call in DRIVE_FORBIDDEN if call in text] == []


def test_resume_fixtures_are_synthetic():
    folder = ROOT / "fixtures" / "resume"
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "@gmail.com" not in text, path.name
        assert "Alex Example" in text, path.name


# An f-string or format call that puts a name next to "@" would be a guessed email.
GUESSED_EMAIL = re.compile(
    r"""(f["'][^"'\n]*\{[^}]*(first|last|name)[^}]*\}[^"'\n]*@)"""
    r"""|(["'][^"'\n]*\{\}[^"'\n]*@[^"'\n]*["']\s*\.format\()""",
    re.I,
)


def test_contacts_never_build_emails_from_names():
    folder = ROOT / "src" / "jobengine" / "contacts"
    offenders = [p.relative_to(ROOT).as_posix() for p in folder.rglob("*.py")
                 if GUESSED_EMAIL.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_contact_fixtures_use_invented_addresses_only():
    email = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
    allowed = ("example.com", "example.org")
    paths = [*(ROOT / "fixtures" / "contacts").glob("*.json"),
             *(ROOT / "fixtures" / "mail").glob("*.json")]
    for path in paths:
        for domain in email.findall(path.read_text(encoding="utf-8")):
            ok = domain.endswith(allowed) or domain == "gmail.com"  # the personal-address case
            assert ok, f"{path.name}: {domain}"


def test_protected_notion_pages_are_only_linked_never_targeted():
    """Module 08: the V16 page, Build Spec and templates are edited by hand. Their IDs may
    appear in src/ only as a link (https://app.notion.com/p/...), never as a request target.
    NotionClient also refuses any write to them at runtime (check_page_write)."""
    import yaml

    pages = yaml.safe_load((ROOT / "config" / "base.yaml").read_text())["notion"]["pages"]
    ids = {pid.replace("-", "").lower() for pid in pages.values()}
    offenders = []
    for p in src_files():
        for line in p.read_text(encoding="utf-8").splitlines():
            flat = line.replace("-", "").lower()
            for pid in ids:
                if pid in flat and "app.notion.com/p/" not in line:
                    offenders.append(f"{p.relative_to(ROOT).as_posix()}: {line.strip()}")
    assert offenders == []


# ---------------------------------------------------------------- Module 09: container


def test_dockerignore_keeps_secrets_and_output_out_of_the_image():
    lines = {line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()}
    assert {".env", ".secrets", "out"} <= lines
    assert "!.env" not in lines and "!.secrets" not in lines


def test_dockerfile_bakes_in_no_secrets():
    text = (ROOT / "Dockerfile").read_text()
    assert "COPY .env" not in text and "COPY .secrets" not in text
    for line in text.splitlines():
        if line.strip().upper().startswith("ENV"):
            assert not re.search(r"TOKEN|KEY|SECRET", line, re.I), line
    assert "DRY_RUN" not in text  # it stays at its safe default (true)
    assert 'CMD ["python", "-m", "jobengine.web"]' in text

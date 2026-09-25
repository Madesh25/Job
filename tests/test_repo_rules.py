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

"""Read-only publication guard. Prints paths/rules, never matched secret values."""

import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "telegram-token": re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "cloud-access-key": re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    "api-secret-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "private-host-path": re.compile(r"[A-Z]:[\\/]+Users[\\/]+", re.I),
}
BLOCKED_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".bak",
    ".backup",
    ".dump",
    ".mp4",
    ".mov",
    ".jpg",
    ".jpeg",
    ".png",
    ".zip",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".pyc",
}
BLOCKED_PARTS = {".venv", "__pycache__", "var", "assets", "graphify-out", ".railway"}


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], stderr=subprocess.PIPE)


def forbidden_path(name):
    path = Path(name)
    return (
        (path.name.startswith(".env") and path.name != ".env.example")
        or path.suffix.lower() in BLOCKED_SUFFIXES
        or bool(set(path.parts) & BLOCKED_PARTS)
    )


def inspect_text(label, data):
    text = data.decode("utf-8", errors="replace")
    issues = [
        {"path": label, "rule": rule} for rule, pattern in PATTERNS.items() if pattern.search(text)
    ]
    # Environment assignments only. Synthetic credentials in unit tests are not live secrets.
    for line in text.splitlines():
        match = re.fullmatch(r"([A-Z_]*(?:TOKEN|SECRET|PASSWORD|API_KEY))=(.*)", line.strip())
        if match and match[2].strip() and not match[2].startswith(("REPLACE_", "YOUR_", "<")):
            issues.append({"path": label, "rule": "non-placeholder-env-secret"})
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--denylist-file", type=Path, help="Private terms, one per line, outside this repository"
    )
    args = parser.parse_args()
    if args.denylist_file is not None:
        if args.denylist_file.resolve().is_relative_to(ROOT):
            raise ValueError("The private denylist must stay outside the public repository")
        terms = args.denylist_file.read_text(encoding="utf-8").splitlines()
        terms = [term.strip() for term in terms if term.strip()]
        if terms:
            PATTERNS["private-reference"] = re.compile("|".join(map(re.escape, terms)), re.I)
    if Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve() != ROOT:
        raise RuntimeError("Expected an independent Git repository at the project root")
    names = sorted(
        set(
            git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
            .decode()
            .strip("\0")
            .split("\0")
        )
        - {""}
    )
    issues = []
    for name in names:
        path = ROOT / name
        if forbidden_path(name):
            issues.append({"path": name, "rule": "forbidden-artifact"})
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            issues.append({"path": name, "rule": "external-link"})
        elif path.is_file():
            issues.extend(inspect_text(name, path.read_bytes()))
    for entry in git("ls-files", "--stage", "-z").decode().strip("\0").split("\0"):
        if entry.startswith("120000 "):
            issues.append({"path": entry.split("\t", 1)[1], "rule": "git-symlink"})
    # Check every historical blob as well, even if a later commit removed the file.
    commits = git("rev-list", "--all").decode().splitlines()
    blobs = 0
    if commits:
        for line in git("rev-list", "--objects", "--all").decode().splitlines():
            object_id, _, name = line.partition(" ")
            if git("cat-file", "-t", object_id).strip() != b"blob":
                continue
            blobs += 1
            if forbidden_path(name):
                issues.append({"path": "history:" + name, "rule": "forbidden-artifact"})
            issues.extend(inspect_text("history:" + name, git("cat-file", "blob", object_id)))
    result = {
        "files_scanned": len(names),
        "commits_scanned": len(commits),
        "historical_blobs_scanned": blobs,
        "issues": issues,
    }
    print(json.dumps(result, indent=2))
    return bool(issues)


if __name__ == "__main__":
    raise SystemExit(main())

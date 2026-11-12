"""Build an allowlisted CLI upload directory. Does not authenticate or deploy."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "Dockerfile",
    "README.md",
    "LICENSE",
    "examples/catalog.json",
    "examples/video-profile.json",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "alembic.ini",
)


def prepare(output: Path, root: Path = ROOT) -> dict[str, str]:
    output = output.resolve()
    root = root.resolve()
    if output.exists():
        raise ValueError("Output must be a new directory; stale files are not allowed")
    sources = [root / name for name in FILES]
    sources += sorted((root / "src" / "vip").rglob("*.py"))
    sources += sorted((root / "alembic").rglob("*.py"))
    sources += sorted((root / "alembic").glob("*.mako"))
    for source in sources:
        if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError("Invalid deployment source")
    manifest = {}
    output.mkdir(parents=True)
    for source in sources:
        name = source.relative_to(root).as_posix()
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        manifest[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / "upload-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare(args.output)
    print(f"Prepared {len(manifest)} allowlisted files in {args.output.resolve()}")
    print("No upload or deployment executed.")


if __name__ == "__main__":
    main()

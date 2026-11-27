#!/usr/bin/env python3
"""Build a reproducible, metadata-scrubbed codex-orchestrator.skill archive."""

from __future__ import annotations

import hashlib
import stat
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_SRC = REPO_ROOT / "skills" / "codex-orchestrator"
DIST_DIR = REPO_ROOT / "dist"
OUT_FILE = DIST_DIR / "codex-orchestrator.skill"

EXCLUDE_NAMES = {".DS_Store", "__pycache__"}
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def main() -> None:
    if not (SKILL_SRC / "SKILL.md").exists():
        raise SystemExit(f"SKILL.md not found under {SKILL_SRC}")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        p
        for p in SKILL_SRC.rglob("*")
        if p.is_file()
        and not any(
            part in EXCLUDE_NAMES or part.endswith(".pyc")
            for part in p.relative_to(SKILL_SRC).parts
        )
    )

    with zipfile.ZipFile(OUT_FILE, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in files:
            rel = f.relative_to(SKILL_SRC)
            arcname = f"codex-orchestrator/{rel.as_posix()}"
            info = zipfile.ZipInfo(arcname, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix
            mode = 0o755 if rel.as_posix() == "scripts/run_codex.py" else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            zf.writestr(info, f.read_bytes())

    with zipfile.ZipFile(OUT_FILE, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            raise SystemExit(f"Corrupted archive entry: {bad}")

    sha256 = hashlib.sha256(OUT_FILE.read_bytes()).hexdigest()
    print(f"Built {OUT_FILE} ({OUT_FILE.stat().st_size} bytes, sha256={sha256})")


if __name__ == "__main__":
    main()

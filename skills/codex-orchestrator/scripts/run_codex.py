#!/usr/bin/env python3
"""
Worktree-isolated Codex CLI orchestrator (1 Codex session = 1 Git worktree).

Manages the full lifecycle for Gemini (Manager/Verifier) delegating work to
Codex CLI (Implementer):
  preflight -> start -> inspect -> resume -> land / discard -> gc
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = SKILL_ROOT / "resources" / "worker_report.schema.json"
DEFAULT_MODEL = os.environ.get("CODEX_ORCHESTRATOR_MODEL", "gpt-6.2-astra")
DEFAULT_REASONING_EFFORT = os.environ.get("CODEX_ORCHESTRATOR_REASONING_EFFORT", "low")
DEFAULT_SERVICE_TIER = os.environ.get("CODEX_ORCHESTRATOR_SERVICE_TIER", "fast")
REASONING_EFFORT_CHOICES = ("low", "medium", "high", "xhigh", "max", "ultra")
if DEFAULT_REASONING_EFFORT not in REASONING_EFFORT_CHOICES:
    DEFAULT_REASONING_EFFORT = "low"
CACHE_ROOT = Path(
    os.environ.get("CODEX_ORCHESTRATOR_HOME", Path.home() / ".cache" / "codex-worktrees")
).expanduser()
REGISTRY_DIR = CACHE_ROOT / "registry"
WORKTREES_DIR = CACHE_ROOT / "worktrees"
LOGS_DIR = CACHE_ROOT / "logs"

SENSITIVE_NAME_PATTERNS = (
    re.compile(r"(^|/)\.env(\..+)?$", re.IGNORECASE),
    re.compile(r"\.(pem|key|p12|pfx|jks|keystore)$", re.IGNORECASE),
    re.compile(r"(^|/)id_(rsa|ed25519|ecdsa)(\.pub)?$", re.IGNORECASE),
    re.compile(r"(^|/)(credentials|secrets?)\.(json|ya?ml|toml|ini)$", re.IGNORECASE),
)

SUSPICIOUS_DIFF_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ts_ignore_or_nocheck", re.compile(r"@ts-ignore|@ts-nocheck|@ts-expect-error")),
    ("type_ignore_or_noqa", re.compile(r"#\s*(type:\s*ignore|noqa\b)")),
    ("skipped_or_focused_test", re.compile(r"\b(skip|only)\s*\(|@pytest\.mark\.skip|@unittest\.skip")),
    ("bare_except_pass", re.compile(r"except(\s+Exception)?\s*:\s*(#.*)?$")),
    ("no_verify_flag", re.compile(r"--no-verify")),
]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def emit_json(data: dict[str, Any], exit_code: int = 0) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def fail(message: str, **extra: Any) -> None:
    payload: dict[str, Any] = {"ok": False, "error": message}
    payload.update(extra)
    emit_json(payload, exit_code=1)


def run_proc(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    stdin_text: str | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        input=stdin_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )


def git(
    repo: Path,
    args: list[str],
    *,
    check: bool = True,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = run_proc(["git", *args], cwd=repo, stdin_text=stdin_text)
    if check and proc.returncode != 0:
        fail(
            f"git {' '.join(args)} failed (exit {proc.returncode})",
            cwd=str(repo),
            stderr=proc.stderr.strip(),
            stdout=proc.stdout.strip(),
        )
    return proc


def resolve_repo_root(path_str: str) -> Path:
    p = Path(path_str).expanduser().resolve()
    if not p.exists():
        fail(f"Repository path does not exist: {p}")
    proc = run_proc(["git", "rev-parse", "--show-toplevel"], cwd=p)
    if proc.returncode != 0:
        fail(
            "Target directory is not inside a Git repository; worktree isolation requires Git.",
            path=str(p),
            stderr=proc.stderr.strip(),
        )
    return Path(proc.stdout.strip()).resolve()


def sanitize_slug(slug: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", slug.strip().lower()).strip("-.")
    if not cleaned:
        fail("Slug must contain at least one alphanumeric character.")
    return cleaned[:40]


def ensure_dirs() -> None:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def registry_path(run_id: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", run_id):
        fail(f"Invalid run_id format: {run_id}")
    return REGISTRY_DIR / f"{run_id}.json"


def save_record(record: dict[str, Any]) -> None:
    ensure_dirs()
    path = registry_path(record["run_id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_record(run_id: str) -> dict[str, Any]:
    path = registry_path(run_id)
    if not path.exists():
        fail(f"Run record not found: {run_id}", registry_dir=str(REGISTRY_DIR))
    return json.loads(path.read_text(encoding="utf-8"))


def list_all_records() -> list[dict[str, Any]]:
    if not REGISTRY_DIR.exists():
        return []
    records: list[dict[str, Any]] = []
    for p in sorted(REGISTRY_DIR.glob("*.json")):
        try:
            records.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return records


def get_dirty_files(repo: Path) -> list[str]:
    proc = git(repo, ["status", "--porcelain"])
    return [line for line in proc.stdout.splitlines() if line.strip()]


def get_submodules(repo: Path) -> list[str]:
    proc = run_proc(["git", "submodule", "status"], cwd=repo)
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def get_worktree_git_dir(wt_path: Path) -> Path | None:
    proc = run_proc(["git", "rev-parse", "--git-dir"], cwd=wt_path)
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    p = Path(raw)
    if not p.is_absolute():
        p = (wt_path / p).resolve()
    return p.resolve()


def extract_session_id_from_jsonl(jsonl_path: Path) -> str | None:
    if not jsonl_path.exists():
        return None
    for line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        for key in ("thread_id", "session_id", "conversation_id"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for nested_key in ("thread", "session", "data", "payload"):
            nested = obj.get(nested_key)
            if isinstance(nested, dict):
                for key in ("id", "thread_id", "session_id"):
                    val = nested.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
    return None


def validate_copy_dep_rel(repo_root: Path, rel_str: str) -> tuple[Path, str]:
    rel = Path(rel_str)
    if rel.is_absolute() or ".." in rel.parts:
        fail(f"Unsafe --copy-deps path (must be relative within repo): {rel_str}")
    norm_rel = rel.as_posix()
    for pat in SENSITIVE_NAME_PATTERNS:
        if pat.search(norm_rel):
            fail(
                f"Refusing to copy potentially sensitive file/directory into Codex worktree: {rel_str}",
                hint="Only copy non-secret build/dependency directories (e.g. node_modules, .venv, target).",
            )
    src = (repo_root / rel).resolve()
    try:
        src.relative_to(repo_root)
    except ValueError:
        fail(f"Dependency path escapes repository boundary: {rel_str}")
    return rel, norm_rel


def copy_dependency_dir(repo_root: Path, wt_path: Path, rel_str: str) -> dict[str, Any]:
    rel, norm_rel = validate_copy_dep_rel(repo_root, rel_str)
    src = (repo_root / rel).resolve()
    dst = (wt_path / rel).resolve()
    try:
        dst.relative_to(wt_path)
    except ValueError:
        fail(f"Dependency path escapes worktree boundary: {rel_str}")
    if not src.exists():
        return {"path": norm_rel, "status": "skipped_missing"}
    if dst.exists():
        return {"path": norm_rel, "status": "skipped_already_exists"}
    dst.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        proc = run_proc(["cp", "-cR", str(src), str(dst)])
        if proc.returncode == 0:
            return {"path": norm_rel, "status": "copied_apfs_cow"}
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)
    return {"path": norm_rel, "status": "copied_standard"}


def scan_diff_warnings(
    wt_path: Path,
    diff_text: str,
    tracked_files: list[str],
    untracked_files: list[str],
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    all_files = tracked_files + untracked_files
    test_files = [
        f for f in all_files
        if re.search(r"(^|/)(tests?|__tests__|spec)/|\.(test|spec)\.|_test\.|test_[^/]+\.py$", f, re.IGNORECASE)
    ]
    if test_files:
        warnings.append({
            "kind": "test_files_touched",
            "detail": f"Inspect carefully to ensure existing assertions were not weakened: {', '.join(test_files)}",
        })

    lock_files = [
        f for f in all_files
        if re.search(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|uv\.lock|poetry\.lock|go\.sum)$", f)
    ]
    if lock_files:
        warnings.append({
            "kind": "lockfiles_touched",
            "detail": f"Dependency lockfiles changed: {', '.join(lock_files)}",
        })

    current_file = ""
    seen_hits: set[tuple[str, str]] = set()
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        for kind, pattern in SUSPICIOUS_DIFF_PATTERNS:
            if pattern.search(added) and (kind, current_file) not in seen_hits:
                seen_hits.add((kind, current_file))
                warnings.append({
                    "kind": kind,
                    "detail": f"Added line in {current_file or '<unknown>'}: {added.strip()[:140]}",
                })

    for rel in untracked_files:
        fp = wt_path / rel
        if not fp.is_file() or fp.stat().st_size > 256_000:
            continue
        for line in fp.read_text(encoding="utf-8", errors="replace").splitlines():
            for kind, pattern in SUSPICIOUS_DIFF_PATTERNS:
                if pattern.search(line) and (kind, rel) not in seen_hits:
                    seen_hits.add((kind, rel))
                    warnings.append({
                        "kind": kind,
                        "detail": f"New untracked file {rel}: {line.strip()[:140]}",
                    })
    return warnings


def read_report_if_present(report_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not report_path.exists():
        return None, None
    raw = report_path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        return None, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"Failed to parse report JSON ({exc}): {raw[:400]}"


def check_base_drift(repo_root: Path, base_branch: str, base_sha: str) -> dict[str, Any]:
    head_proc = git(repo_root, ["rev-parse", "HEAD"])
    current_head = head_proc.stdout.strip()
    branch_proc = git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    current_branch = branch_proc.stdout.strip()
    return {
        "current_branch": current_branch,
        "current_head": current_head,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "head_drifted": current_head != base_sha,
        "branch_changed": current_branch != base_branch,
    }


def collect_worktree_changes(wt_path: Path, base_sha: str) -> dict[str, Any]:
    status_proc = git(wt_path, ["status", "--porcelain"])
    status_lines = [l for l in status_proc.stdout.splitlines() if l.strip()]

    tracked_names_proc = git(wt_path, ["diff", "--name-only", base_sha])
    tracked_files = [l.strip() for l in tracked_names_proc.stdout.splitlines() if l.strip()]

    untracked_proc = git(wt_path, ["ls-files", "--others", "--exclude-standard"])
    untracked_files = [l.strip() for l in untracked_proc.stdout.splitlines() if l.strip()]

    stat_proc = git(wt_path, ["diff", "--stat", base_sha])
    diff_proc = git(wt_path, ["diff", base_sha])

    return {
        "status_lines": status_lines,
        "tracked_files": tracked_files,
        "untracked_files": untracked_files,
        "diff_stat": stat_proc.stdout.strip(),
        "diff_text": diff_proc.stdout,
        "has_changes": bool(tracked_files or untracked_files or status_lines),
    }


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_preflight(args: argparse.Namespace) -> None:
    codex_bin = shutil.which("codex")
    codex_ver = None
    if codex_bin:
        ver_proc = run_proc(["codex", "--version"])
        codex_ver = (ver_proc.stdout or ver_proc.stderr).strip()

    repo_root = resolve_repo_root(args.repo)
    head_sha = git(repo_root, ["rev-parse", "HEAD"]).stdout.strip()
    branch = git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    dirty_files = get_dirty_files(repo_root)
    submodules = get_submodules(repo_root)
    setup_hook = repo_root / ".agents" / "worktree-setup.sh"

    active_runs = [
        {
            "run_id": r["run_id"],
            "status": r.get("status"),
            "round": r.get("round"),
            "branch": r.get("branch"),
            "worktree_path": r.get("worktree_path"),
        }
        for r in list_all_records()
        if r.get("repo_root") == str(repo_root)
        and r.get("status") not in ("landed", "abandoned", "cleaned")
    ]

    emit_json({
        "ok": True,
        "codex_binary": codex_bin,
        "codex_version": codex_ver,
        "schema_exists": SCHEMA_PATH.exists(),
        "repo_root": str(repo_root),
        "current_branch": branch,
        "head_sha": head_sha,
        "is_dirty": bool(dirty_files),
        "dirty_files": dirty_files,
        "submodules": submodules,
        "setup_hook_present": setup_hook.exists(),
        "setup_hook_path": str(setup_hook) if setup_hook.exists() else None,
        "active_runs_for_repo": active_runs,
    })


def cmd_start(args: argparse.Namespace) -> None:
    ensure_dirs()
    if not SCHEMA_PATH.exists():
        fail(f"Worker report schema missing: {SCHEMA_PATH}")
    if not args.dry_run and not shutil.which("codex"):
        fail("`codex` binary not found in PATH.")

    repo_root = resolve_repo_root(args.repo)
    brief_path = Path(args.brief).expanduser().resolve()
    if not brief_path.exists():
        fail(f"Brief file does not exist: {brief_path}")
    brief_text = brief_path.read_text(encoding="utf-8")
    if not brief_text.strip():
        fail(f"Brief file is empty: {brief_path}")

    dirty_files = get_dirty_files(repo_root)
    if dirty_files and not args.allow_dirty_base:
        fail(
            "Main working tree has uncommitted changes. A git worktree branches from a commit and will NOT include them.",
            dirty_files=dirty_files,
            action_required=(
                "If this task does NOT depend on the uncommitted changes, re-run with --allow-dirty-base. "
                "If it DOES depend on them, ask the user to create a WIP commit first."
            ),
        )

    base_ref = args.base or "HEAD"
    base_sha = git(repo_root, ["rev-parse", "--verify", f"{base_ref}^{{commit}}"]).stdout.strip()
    base_branch = git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    slug = sanitize_slug(args.slug)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    short_id = uuid.uuid4().hex[:6]
    run_id = f"{stamp}-{slug}-{short_id}"
    branch_name = f"codex/{slug}-{short_id}"

    repo_bucket = sanitize_slug(repo_root.name)
    wt_path = (WORKTREES_DIR / repo_bucket / run_id).resolve()
    log_dir = (LOGS_DIR / run_id).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    # Pre-validate copy_deps and setup hook BEFORE creating worktree so failures leave zero orphans
    copy_dep_items = [s.strip() for s in args.copy_deps.split(",") if s.strip()] if args.copy_deps else []
    for item in copy_dep_items:
        validate_copy_dep_rel(repo_root, item)

    hook_path = repo_root / ".agents" / "worktree-setup.sh"
    if args.run_setup_hook and not hook_path.exists():
        fail(f"--run-setup-hook passed, but hook does not exist: {hook_path}")

    # Save an immutable copy of the brief into the log directory
    brief_copy = log_dir / "round-1.brief.md"
    brief_copy.write_text(brief_text, encoding="utf-8")

    # Create isolated worktree and branch
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    git(repo_root, ["worktree", "add", "-b", branch_name, str(wt_path), base_sha])

    # Optional dependency copy (CoW on macOS)
    bootstrap_results: list[dict[str, Any]] = []
    for item in copy_dep_items:
        bootstrap_results.append(copy_dependency_dir(repo_root, wt_path, item))

    # Optional opt-in repo setup hook
    setup_hook_result: dict[str, Any] | None = None
    if args.run_setup_hook:
        hook_env = os.environ.copy()
        hook_env["MAIN_REPO_ROOT"] = str(repo_root)
        hook_env["WORKTREE_PATH"] = str(wt_path)
        hook_env["BASE_SHA"] = base_sha
        hook_proc = run_proc(["bash", str(hook_path)], cwd=wt_path, env=hook_env, timeout=300)
        setup_hook_result = {
            "hook": str(hook_path),
            "exit_code": hook_proc.returncode,
            "stdout_tail": hook_proc.stdout[-1000:],
            "stderr_tail": hook_proc.stderr[-1000:],
        }
        if hook_proc.returncode != 0:
            run_proc(["git", "worktree", "remove", "--force", str(wt_path)], cwd=repo_root)
            run_proc(["git", "branch", "-D", branch_name], cwd=repo_root)
            fail("Worktree setup hook failed (cleaned up temporary worktree).", setup_hook_result=setup_hook_result)

    report_path = log_dir / "round-1.report.json"
    events_path = log_dir / "round-1.events.jsonl"
    stderr_path = log_dir / "round-1.stderr.log"

    model = args.model or DEFAULT_MODEL
    reasoning_effort = args.reasoning_effort or DEFAULT_REASONING_EFFORT
    service_tier = args.service_tier if args.service_tier is not None else DEFAULT_SERVICE_TIER

    cmd = [
        "codex",
        "exec",
        "-C",
        str(wt_path),
        "-s",
        args.sandbox,
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "--output-schema",
        str(SCHEMA_PATH),
        "-o",
        str(report_path),
        "--json",
    ]
    if service_tier == "standard":
        cmd.extend(["--disable", "fast_mode"])
    elif service_tier:
        cmd.extend(["-c", f'service_tier="{service_tier}"'])
    if args.profile:
        cmd.extend(["-p", args.profile])
    if args.allow_git_dir:
        wt_git_dir = get_worktree_git_dir(wt_path)
        if wt_git_dir:
            cmd.extend(["--add-dir", str(wt_git_dir)])
    cmd.append("-")

    record: dict[str, Any] = {
        "run_id": run_id,
        "slug": slug,
        "repo_root": str(repo_root),
        "base_branch": base_branch,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "branch": branch_name,
        "worktree_path": str(wt_path),
        "log_dir": str(log_dir),
        "sandbox": args.sandbox,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "service_tier": service_tier,
        "profile": args.profile,
        "allow_git_dir": bool(args.allow_git_dir),
        "max_rounds": args.max_rounds,
        "round": 1,
        "codex_session_id": None,
        "status": "dry_run" if args.dry_run else "running",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "rounds": [],
    }
    save_record(record)

    if args.dry_run:
        record["codex_session_id"] = f"dry-run-{short_id}"
        record["status"] = "awaiting_review"
        record["rounds"].append({
            "round": 1,
            "mode": "dry_run",
            "command": cmd,
            "brief_path": str(brief_copy),
            "report_path": str(report_path),
            "exit_code": 0,
            "finished_at": utc_now(),
        })
        record["updated_at"] = utc_now()
        save_record(record)
        emit_json({
            "ok": True,
            "dry_run": True,
            "run_id": run_id,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "service_tier": service_tier,
            "worktree_path": str(wt_path),
            "branch": branch_name,
            "base_sha": base_sha,
            "command": cmd,
            "bootstrap_results": bootstrap_results,
            "setup_hook_result": setup_hook_result,
        })

    timed_out = False
    try:
        proc = run_proc(cmd, cwd=wt_path, stdin_text=brief_text, timeout=args.timeout)
        events_path.write_text(proc.stdout, encoding="utf-8")
        stderr_path.write_text(proc.stderr, encoding="utf-8")
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        events_path.write_text((exc.stdout or "") if isinstance(exc.stdout, str) else "", encoding="utf-8")
        stderr_path.write_text((exc.stderr or "") if isinstance(exc.stderr, str) else "", encoding="utf-8")

    session_id = extract_session_id_from_jsonl(events_path)
    report, report_error = read_report_if_present(report_path)
    changes = collect_worktree_changes(wt_path, base_sha)

    if timed_out:
        status = "timed_out"
    elif report and report.get("status") == "blocked":
        status = "blocked"
    elif exit_code != 0:
        status = "failed"
    else:
        status = "awaiting_review"

    record["codex_session_id"] = session_id
    record["status"] = status
    record["updated_at"] = utc_now()
    record["rounds"].append({
        "round": 1,
        "brief_path": str(brief_copy),
        "events_path": str(events_path),
        "stderr_path": str(stderr_path),
        "report_path": str(report_path),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "finished_at": utc_now(),
    })
    save_record(record)

    emit_json({
        "ok": exit_code == 0 and not timed_out,
        "run_id": run_id,
        "status": status,
        "round": 1,
        "max_rounds": args.max_rounds,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "service_tier": service_tier,
        "codex_session_id": session_id,
        "worktree_path": str(wt_path),
        "branch": branch_name,
        "base_sha": base_sha,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "bootstrap_results": bootstrap_results,
        "setup_hook_result": setup_hook_result,
        "tracked_files": changes["tracked_files"],
        "untracked_files": changes["untracked_files"],
        "diff_stat": changes["diff_stat"],
        "report": report,
        "report_error": report_error,
        "stderr_tail": stderr_path.read_text(encoding="utf-8", errors="replace")[-1200:],
    }, exit_code=0 if (exit_code == 0 and not timed_out) else 1)


def cmd_inspect(args: argparse.Namespace) -> None:
    record = load_record(args.run)
    wt_path = Path(record["worktree_path"])
    repo_root = Path(record["repo_root"])
    if not wt_path.exists():
        fail(f"Worktree directory no longer exists: {wt_path}", run_id=record["run_id"], status=record.get("status"))

    changes = collect_worktree_changes(wt_path, record["base_sha"])
    warnings = scan_diff_warnings(wt_path, changes["diff_text"], changes["tracked_files"], changes["untracked_files"])
    drift = check_base_drift(repo_root, record["base_branch"], record["base_sha"])

    latest_report = None
    latest_report_error = None
    if record.get("rounds"):
        rp = Path(record["rounds"][-1]["report_path"])
        latest_report, latest_report_error = read_report_if_present(rp)

    payload: dict[str, Any] = {
        "ok": True,
        "run_id": record["run_id"],
        "status": record.get("status"),
        "round": record.get("round"),
        "max_rounds": record.get("max_rounds"),
        "model": record.get("model"),
        "reasoning_effort": record.get("reasoning_effort"),
        "service_tier": record.get("service_tier"),
        "codex_session_id": record.get("codex_session_id"),
        "worktree_path": str(wt_path),
        "branch": record["branch"],
        "drift": drift,
        "has_changes": changes["has_changes"],
        "status_lines": changes["status_lines"],
        "tracked_files": changes["tracked_files"],
        "untracked_files": changes["untracked_files"],
        "diff_stat": changes["diff_stat"],
        "audit_warnings": warnings,
        "latest_report": latest_report,
        "latest_report_error": latest_report_error,
    }

    if args.full_diff:
        diff_text = changes["diff_text"]
        truncated = False
        if len(diff_text) > args.max_diff_chars:
            diff_text = diff_text[: args.max_diff_chars] + "\n... [DIFF TRUNCATED] ..."
            truncated = True
        payload["diff"] = diff_text
        payload["diff_truncated"] = truncated

        untracked_previews: dict[str, str] = {}
        for rel in changes["untracked_files"][:20]:
            fp = wt_path / rel
            if fp.is_file() and fp.stat().st_size <= 64_000:
                untracked_previews[rel] = fp.read_text(encoding="utf-8", errors="replace")
            else:
                untracked_previews[rel] = f"<non-text or large file: {fp.stat().st_size if fp.exists() else 'missing'} bytes>"
        payload["untracked_previews"] = untracked_previews

    emit_json(payload)


def cmd_resume(args: argparse.Namespace) -> None:
    record = load_record(args.run)
    if record.get("status") in ("landed", "abandoned", "cleaned"):
        fail(f"Cannot resume run in terminal state: {record.get('status')}", run_id=record["run_id"])

    wt_path = Path(record["worktree_path"])
    if not wt_path.exists():
        fail(f"Worktree missing for run {record['run_id']}: {wt_path}")

    session_id = record.get("codex_session_id")
    if not session_id:
        fail(
            "No codex_session_id recorded for this run; cannot resume safely.",
            run_id=record["run_id"],
            hint="Discard this worktree and start a fresh run.",
        )

    current_round = int(record.get("round", 1))
    max_rounds = int(record.get("max_rounds", 3))
    if current_round >= max_rounds and not args.force_round:
        fail(
            f"Circuit breaker tripped: run {record['run_id']} already reached max_rounds ({max_rounds}).",
            run_id=record["run_id"],
            round=current_round,
            max_rounds=max_rounds,
            action_required=(
                "Stop looping Codex. Escalate to the user with current diff & blockers, "
                "take over directly, or explicitly pass --force-round if the user approved more rounds."
            ),
        )

    feedback_path = Path(args.feedback).expanduser().resolve()
    if not feedback_path.exists():
        fail(f"Feedback file does not exist: {feedback_path}")
    feedback_text = feedback_path.read_text(encoding="utf-8")
    if not feedback_text.strip():
        fail(f"Feedback file is empty: {feedback_path}")

    if not args.dry_run and not shutil.which("codex"):
        fail("`codex` binary not found in PATH.")

    next_round = current_round + 1
    log_dir = Path(record["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    feedback_copy = log_dir / f"round-{next_round}.feedback.md"
    feedback_copy.write_text(feedback_text, encoding="utf-8")

    report_path = log_dir / f"round-{next_round}.report.json"
    events_path = log_dir / f"round-{next_round}.events.jsonl"
    stderr_path = log_dir / f"round-{next_round}.stderr.log"

    sandbox = record.get("sandbox", "workspace-write")
    model = args.model or record.get("model") or DEFAULT_MODEL
    reasoning_effort = args.reasoning_effort or record.get("reasoning_effort") or DEFAULT_REASONING_EFFORT
    service_tier = (
        args.service_tier
        if args.service_tier is not None
        else record.get("service_tier", DEFAULT_SERVICE_TIER)
    )
    cmd = [
        "codex",
        "exec",
        "resume",
        session_id,
        "-m",
        model,
        "-c",
        f'sandbox_mode="{sandbox}"',
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "--output-schema",
        str(SCHEMA_PATH),
        "-o",
        str(report_path),
        "--json",
    ]
    if service_tier == "standard":
        cmd.extend(["--disable", "fast_mode"])
    elif service_tier:
        cmd.extend(["-c", f'service_tier="{service_tier}"'])
    cmd.append("-")

    record["model"] = model
    record["reasoning_effort"] = reasoning_effort
    record["service_tier"] = service_tier
    record["round"] = next_round
    record["status"] = "dry_run" if args.dry_run else "running"
    record["updated_at"] = utc_now()
    save_record(record)

    if args.dry_run:
        record["status"] = "awaiting_review"
        record["rounds"].append({
            "round": next_round,
            "mode": "dry_run",
            "command": cmd,
            "feedback_path": str(feedback_copy),
            "report_path": str(report_path),
            "exit_code": 0,
            "finished_at": utc_now(),
        })
        record["updated_at"] = utc_now()
        save_record(record)
        emit_json({
            "ok": True,
            "dry_run": True,
            "run_id": record["run_id"],
            "round": next_round,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "service_tier": service_tier,
            "codex_session_id": session_id,
            "worktree_path": str(wt_path),
            "command": cmd,
        })

    timed_out = False
    try:
        proc = run_proc(cmd, cwd=wt_path, stdin_text=feedback_text, timeout=args.timeout)
        events_path.write_text(proc.stdout, encoding="utf-8")
        stderr_path.write_text(proc.stderr, encoding="utf-8")
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        events_path.write_text((exc.stdout or "") if isinstance(exc.stdout, str) else "", encoding="utf-8")
        stderr_path.write_text((exc.stderr or "") if isinstance(exc.stderr, str) else "", encoding="utf-8")

    new_sid = extract_session_id_from_jsonl(events_path)
    if new_sid:
        record["codex_session_id"] = new_sid

    report, report_error = read_report_if_present(report_path)
    changes = collect_worktree_changes(wt_path, record["base_sha"])

    if timed_out:
        status = "timed_out"
    elif report and report.get("status") == "blocked":
        status = "blocked"
    elif exit_code != 0:
        status = "failed"
    else:
        status = "awaiting_review"

    record["status"] = status
    record["updated_at"] = utc_now()
    record["rounds"].append({
        "round": next_round,
        "feedback_path": str(feedback_copy),
        "events_path": str(events_path),
        "stderr_path": str(stderr_path),
        "report_path": str(report_path),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "finished_at": utc_now(),
    })
    save_record(record)

    emit_json({
        "ok": exit_code == 0 and not timed_out,
        "run_id": record["run_id"],
        "status": status,
        "round": next_round,
        "max_rounds": max_rounds,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "service_tier": service_tier,
        "codex_session_id": record["codex_session_id"],
        "worktree_path": str(wt_path),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "tracked_files": changes["tracked_files"],
        "untracked_files": changes["untracked_files"],
        "diff_stat": changes["diff_stat"],
        "report": report,
        "report_error": report_error,
        "stderr_tail": stderr_path.read_text(encoding="utf-8", errors="replace")[-1200:],
    }, exit_code=0 if (exit_code == 0 and not timed_out) else 1)


def ensure_worktree_committed(wt_path: Path, base_sha: str, commit_msg: str) -> str:
    """Commit all tracked + untracked changes in the isolated worktree onto its temporary branch."""
    git(wt_path, ["add", "-A"])
    diff_cached = run_proc(["git", "diff", "--cached", "--quiet"], cwd=wt_path)
    if diff_cached.returncode == 0:
        head = git(wt_path, ["rev-parse", "HEAD"]).stdout.strip()
        if head == base_sha:
            fail("Worktree has no changes to land.")
        return head
    git(wt_path, ["commit", "-m", commit_msg])
    return git(wt_path, ["rev-parse", "HEAD"]).stdout.strip()


def cmd_land(args: argparse.Namespace) -> None:
    record = load_record(args.run)
    if record.get("status") in ("abandoned", "cleaned"):
        fail(f"Cannot land a run with status={record.get('status')}", run_id=record["run_id"])

    repo_root = Path(record["repo_root"])
    wt_path = Path(record["worktree_path"])
    if not wt_path.exists():
        fail(f"Worktree missing: {wt_path}")

    changes = collect_worktree_changes(wt_path, record["base_sha"])
    if not changes["has_changes"]:
        # Also check if worktree already has commits ahead of base_sha
        wt_head = git(wt_path, ["rev-parse", "HEAD"]).stdout.strip()
        if wt_head == record["base_sha"]:
            fail("Worktree has no changes relative to base_sha; nothing to land.", run_id=record["run_id"])

    drift = check_base_drift(repo_root, record["base_branch"], record["base_sha"])
    if (drift["head_drifted"] or drift["branch_changed"]) and not args.allow_base_drift:
        fail(
            "Target repository HEAD or branch changed since this worktree was created (base drift detected).",
            drift=drift,
            action_required=(
                "Verify you are on the intended branch, re-run with --allow-base-drift to land, "
                "and MUST re-run tests in the main repository after landing before discarding the worktree."
            ),
        )

    mode = args.mode
    landed_commit: str | None = None
    patch_file: str | None = None

    if mode == "patch":
        # Temporarily intent-to-add untracked files so git diff --binary captures everything cleanly
        added_intent = list(changes["untracked_files"])
        try:
            if added_intent:
                git(wt_path, ["add", "-N", "--", *added_intent])
            patch_proc = git(wt_path, ["diff", "--binary", record["base_sha"]])
        finally:
            if added_intent:
                run_proc(["git", "reset", "--", *added_intent], cwd=wt_path)
        patch_text = patch_proc.stdout
        if not patch_text.strip():
            fail("Generated patch is empty; nothing to apply.")
        patch_path = Path(record["log_dir"]) / "land.patch"
        patch_path.write_text(patch_text, encoding="utf-8")
        patch_file = str(patch_path)

        # Atomic dry-run check first so main working tree is never left half-patched
        check_proc = run_proc(["git", "apply", "--check", str(patch_path)], cwd=repo_root)
        if check_proc.returncode != 0:
            fail(
                "Patch does not apply cleanly to main repository working tree (no files were modified).",
                stderr=check_proc.stderr.strip(),
                patch_file=patch_file,
                hint="Inspect conflicts or commit/stash conflicting edits in main repo first.",
            )
        git(repo_root, ["apply", str(patch_path)])

    elif mode in ("squash", "cherry-pick"):
        main_dirty = get_dirty_files(repo_root)
        if main_dirty:
            fail(
                f"Main repository working tree must be clean for --mode {mode}.",
                dirty_files=main_dirty,
                hint="Use --mode patch, or commit/stash main repo edits first.",
            )
        msg = args.commit_msg or f"codex({record['slug']}): apply verified changes from {record['run_id']}"
        wt_commit = ensure_worktree_committed(wt_path, record["base_sha"], msg)

        if mode == "squash":
            merge_proc = run_proc(["git", "merge", "--squash", record["branch"]], cwd=repo_root)
            if merge_proc.returncode != 0:
                fail(
                    "git merge --squash encountered conflicts in main repository.",
                    stdout=merge_proc.stdout.strip(),
                    stderr=merge_proc.stderr.strip(),
                    hint="Resolve conflicts manually or run `git reset --merge` in main repository.",
                )
            if args.commit_msg:
                git(repo_root, ["commit", "-m", args.commit_msg])
                landed_commit = git(repo_root, ["rev-parse", "HEAD"]).stdout.strip()
        else:  # cherry-pick
            cp_proc = run_proc(["git", "cherry-pick", wt_commit], cwd=repo_root)
            if cp_proc.returncode != 0:
                fail(
                    "git cherry-pick encountered conflicts in main repository.",
                    stdout=cp_proc.stdout.strip(),
                    stderr=cp_proc.stderr.strip(),
                    hint="Resolve conflicts or run `git cherry-pick --abort` in main repository.",
                )
            landed_commit = git(repo_root, ["rev-parse", "HEAD"]).stdout.strip()

    record["status"] = "landed"
    record["landed_at"] = utc_now()
    record["land_mode"] = mode
    record["landed_commit"] = landed_commit
    record["land_base_drifted"] = bool(drift["head_drifted"])
    record["updated_at"] = utc_now()
    save_record(record)

    post_verify_required = bool(drift["head_drifted"])
    emit_json({
        "ok": True,
        "run_id": record["run_id"],
        "mode": mode,
        "landed_commit": landed_commit,
        "patch_file": patch_file,
        "base_drifted": drift["head_drifted"],
        "post_land_verification_required": post_verify_required,
        "worktree_retained": True,
        "next_step": (
            "Re-run tests in the main repository first (base drifted), then run `discard --run "
            f"{record['run_id']}` to remove the temporary worktree and branch."
            if post_verify_required
            else f"Verify main repo status if desired, then run `discard --run {record['run_id']}` to clean up the worktree."
        ),
    })


def remove_worktree_and_branch(record: dict[str, Any], force: bool = True) -> dict[str, Any]:
    repo_root = Path(record["repo_root"])
    wt_path = Path(record["worktree_path"])
    branch = record["branch"]

    wt_removed = False
    if wt_path.exists():
        rm_args = ["worktree", "remove"]
        if force:
            rm_args.append("--force")
        rm_args.append(str(wt_path))
        proc = run_proc(["git", *rm_args], cwd=repo_root)
        if proc.returncode != 0:
            fail(
                "Failed to remove git worktree.",
                worktree_path=str(wt_path),
                stderr=proc.stderr.strip(),
            )
        wt_removed = True

    run_proc(["git", "worktree", "prune"], cwd=repo_root)

    branch_deleted = False
    br_check = run_proc(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo_root)
    if br_check.returncode == 0:
        del_proc = run_proc(["git", "branch", "-D", branch], cwd=repo_root)
        branch_deleted = del_proc.returncode == 0

    return {"worktree_removed": wt_removed, "branch_deleted": branch_deleted}


def cmd_discard(args: argparse.Namespace) -> None:
    record = load_record(args.run)
    prev_status = record.get("status")
    result = remove_worktree_and_branch(record, force=True)

    record["status"] = "cleaned" if prev_status == "landed" else "abandoned"
    record["cleaned_at"] = utc_now()
    record["updated_at"] = utc_now()
    save_record(record)

    emit_json({
        "ok": True,
        "run_id": record["run_id"],
        "previous_status": prev_status,
        "status": record["status"],
        **result,
    })


def cmd_list(args: argparse.Namespace) -> None:
    repo_filter = str(resolve_repo_root(args.repo)) if args.repo else None
    rows: list[dict[str, Any]] = []
    for r in list_all_records():
        if repo_filter and r.get("repo_root") != repo_filter:
            continue
        if not args.all and r.get("status") in ("cleaned", "abandoned"):
            continue
        rows.append({
            "run_id": r["run_id"],
            "status": r.get("status"),
            "round": r.get("round"),
            "max_rounds": r.get("max_rounds"),
            "model": r.get("model"),
            "reasoning_effort": r.get("reasoning_effort"),
            "service_tier": r.get("service_tier"),
            "repo_root": r.get("repo_root"),
            "branch": r.get("branch"),
            "worktree_path": r.get("worktree_path"),
            "worktree_exists": Path(r["worktree_path"]).exists(),
            "codex_session_id": r.get("codex_session_id"),
            "updated_at": r.get("updated_at"),
        })
    emit_json({"ok": True, "count": len(rows), "runs": rows})


def cmd_gc(args: argparse.Namespace) -> None:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    cleaned: list[str] = []
    skipped: list[dict[str, str]] = []
    for r in list_all_records():
        status = r.get("status")
        if status not in ("landed", "abandoned", "cleaned"):
            continue
        ts_str = r.get("updated_at") or r.get("created_at")
        try:
            ts = dt.datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
        except Exception:
            skipped.append({"run_id": r["run_id"], "reason": "unparseable_timestamp"})
            continue
        if ts > cutoff:
            continue
        if args.dry_run:
            cleaned.append(r["run_id"])
            continue
        if Path(r["repo_root"]).exists():
            remove_worktree_and_branch(r, force=True)
        log_dir = Path(r.get("log_dir", ""))
        if log_dir.exists() and str(log_dir).startswith(str(LOGS_DIR)):
            shutil.rmtree(log_dir, ignore_errors=True)
        rp = registry_path(r["run_id"])
        if rp.exists():
            rp.unlink()
        cleaned.append(r["run_id"])

    emit_json({
        "ok": True,
        "dry_run": args.dry_run,
        "days": args.days,
        "cleaned_runs": cleaned,
        "skipped": skipped,
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage 1-session-1-worktree Codex CLI execution for Gemini orchestration."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_pre = sub.add_parser("preflight", help="Check repo git status, submodules, and active runs.")
    p_pre.add_argument("--repo", default=".", help="Path inside the target git repository.")
    p_pre.set_defaults(func=cmd_preflight)

    p_start = sub.add_parser("start", help="Create an isolated git worktree and launch a new Codex session.")
    p_start.add_argument("--repo", required=True, help="Path inside the target git repository.")
    p_start.add_argument("--slug", required=True, help="Short descriptive task slug for worktree/branch naming.")
    p_start.add_argument("--brief", required=True, help="Path to the task brief markdown file (store under /tmp).")
    p_start.add_argument("--base", default="HEAD", help="Git ref/commit to branch the worktree from (default: HEAD).")
    p_start.add_argument("--sandbox", choices=["workspace-write", "read-only"], default="workspace-write")
    p_start.add_argument("--model", default=DEFAULT_MODEL, help=f"Codex model slug (default: {DEFAULT_MODEL}).")
    p_start.add_argument(
        "--reasoning-effort",
        choices=REASONING_EFFORT_CHOICES,
        default=DEFAULT_REASONING_EFFORT,
        help=f"Codex model_reasoning_effort override (default: {DEFAULT_REASONING_EFFORT}).",
    )
    p_start.add_argument(
        "--service-tier",
        default=DEFAULT_SERVICE_TIER,
        help=f"Codex service_tier override, e.g. 'fast' or 'standard' to unset (default: {DEFAULT_SERVICE_TIER}).",
    )
    p_start.add_argument("--profile", default=None, help="Optional Codex config profile.")
    p_start.add_argument("--max-rounds", type=int, default=3, help="Circuit-breaker max rounds (default: 3).")
    p_start.add_argument("--timeout", type=int, default=600, help="Hard timeout in seconds (default: 600).")
    p_start.add_argument("--copy-deps", default=None, help="Comma-separated non-secret relative paths to CoW-copy (e.g. node_modules,.venv).")
    p_start.add_argument("--run-setup-hook", action="store_true", help="Run <repo>/.agents/worktree-setup.sh after reviewing it.")
    p_start.add_argument("--allow-dirty-base", action="store_true", help="Allow cutting worktree from HEAD even when main working tree is dirty.")
    p_start.add_argument("--allow-git-dir", action="store_true", help="Grant Codex write access ONLY to this worktree's .git/worktrees/<name> metadata dir.")
    p_start.add_argument("--dry-run", action="store_true", help="Create worktree and print exact codex command without invoking Codex.")
    p_start.set_defaults(func=cmd_start)

    p_ins = sub.add_parser("inspect", help="Audit worktree git diff, untracked files, warnings, and Codex report.")
    p_ins.add_argument("--run", required=True, help="Run ID returned by `start`.")
    p_ins.add_argument("--full-diff", action="store_true", help="Include full diff text and untracked file previews.")
    p_ins.add_argument("--max-diff-chars", type=int, default=120_000, help="Max characters for diff output.")
    p_ins.set_defaults(func=cmd_inspect)

    p_res = sub.add_parser("resume", help="Resume the bound Codex session in the exact same worktree with structured feedback.")
    p_res.add_argument("--run", required=True, help="Run ID to resume.")
    p_res.add_argument("--feedback", required=True, help="Path to feedback markdown file.")
    p_res.add_argument("--model", default=None, help="Optional model override (defaults to the run's pinned model).")
    p_res.add_argument(
        "--reasoning-effort",
        choices=REASONING_EFFORT_CHOICES,
        default=None,
        help="Optional reasoning effort override (e.g. escalate from low to medium/high on tough rework).",
    )
    p_res.add_argument(
        "--service-tier",
        default=None,
        help="Optional service_tier override (defaults to the run's pinned tier).",
    )
    p_res.add_argument("--timeout", type=int, default=600, help="Hard timeout in seconds.")
    p_res.add_argument("--force-round", action="store_true", help="Bypass circuit breaker after explicit user approval.")
    p_res.add_argument("--dry-run", action="store_true", help="Print exact resume command without invoking Codex.")
    p_res.set_defaults(func=cmd_resume)

    p_land = sub.add_parser("land", help="Land verified changes from worktree back into main repository.")
    p_land.add_argument("--run", required=True, help="Run ID to land.")
    p_land.add_argument("--mode", choices=["patch", "squash", "cherry-pick"], default="patch")
    p_land.add_argument("--commit-msg", default=None, help="Commit message for squash/cherry-pick modes.")
    p_land.add_argument("--allow-base-drift", action="store_true", help="Proceed even if main repo HEAD moved since worktree creation.")
    p_land.set_defaults(func=cmd_land)

    p_dis = sub.add_parser("discard", help="Remove worktree and temporary branch (used for cleanup after land or abandoning a failed run).")
    p_dis.add_argument("--run", required=True, help="Run ID to discard/clean up.")
    p_dis.set_defaults(func=cmd_discard)

    p_ls = sub.add_parser("list", help="List tracked Codex worktree runs.")
    p_ls.add_argument("--repo", default=None, help="Filter by repository path.")
    p_ls.add_argument("--all", action="store_true", help="Include cleaned and abandoned runs.")
    p_ls.set_defaults(func=cmd_list)

    p_gc = sub.add_parser("gc", help="Garbage-collect old landed/abandoned runs and logs.")
    p_gc.add_argument("--days", type=int, default=7, help="Age threshold in days (default: 7).")
    p_gc.add_argument("--dry-run", action="store_true", help="Preview runs that would be removed.")
    p_gc.set_defaults(func=cmd_gc)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

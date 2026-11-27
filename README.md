# Castor & Pollux

> **Gemini leads. Codex ships.**
> **Stay completely in the loop — while maxing out your coding throughput.**
> *Named after the twin stars of Gemini: one navigates and verifies, the other ships at full speed.*

[English](./README.md) | [简体中文](./README_ZH.md)

---

## The Core Idea: Clarity + Full-Throttle Productivity

Using a single AI coding agent often forces an annoying trade-off: either you babysit every edit slowly, or you hand over the keyboard and end up staring at a giant black-box diff you barely trust.

**Castor & Pollux** gives you the best of both worlds by splitting the job cleanly:

| Role | Who | What You Get |
| :--- | :--- | :--- |
| **Commander & Reviewer** | **Gemini** | **You can actually understand everything.** Gemini explores the repo, aligns on plans and edge cases with you, audits every diff, re-runs tests independently, and gives you a crisp, human-readable verification report. |
| **Parallel Execution Engine** | **Codex CLI** | **Maximum coding productivity.** Codex runs fast (`gpt-6.2-astra` + `low` + `fast` by default) in isolated Git worktrees (`1 session = 1 worktree`), so multiple coding tasks ship in parallel without touching your main working tree. |

You talk to **one clear-headed Tech Lead (Gemini)**; behind the scenes, **isolated Codex workers crank out verified code at full speed**.

---

## When to Use It (Target Scenarios)

### Best For
- **Shipping multi-file features without losing understanding**: You tell Gemini what you want; Gemini breaks down file scopes, contracts, and edge cases (`EC-1`, `EC-2`, ...), dispatches Codex to implement, and presents a clear diff & edge-case verification summary instead of an opaque wall of edits.
- **Parallelizing tasks to max out throughput**: Split independent modules, refactors, or bug fixes into parallel Codex sessions. Each session gets its own Git worktree outside the repo—zero file conflicts, zero pollution of your active working tree.
- **Edge-case-heavy bug fixes & hardening**: Gemini enumerates tricky edge cases upfront and enforces a strict JSON handoff report (`--output-schema`). Codex must map every edge case to code + test location, and Gemini independently checks both tracked diffs and newly created files so nothing slips through.
- **Fast, cost-efficient dual-model workflow**: Heavy reasoning (architecture, scoping, edge-case design, final code review) stays in Gemini; execution defaults to Codex's blazing-fast low-reasoning profile (`gpt-6.2-astra` + `low` + `fast`), escalating to `medium`/`high` only on genuinely tough rework turns.

### Skip It For
- Trivial one-line edits or quick Q&A (just let Gemini answer or edit directly).
- Non-Git directories (worktree isolation requires a Git repository).

---

## How It Works

1. **Plan & Contract (Gemini)** — Gemini explores the repo, aligns with you on scope and numbered edge cases (`EC-1..EC-N`), and writes a bounded task brief.
2. **Isolated Execution (Codex)** — `run_codex.py start` creates a dedicated Git worktree and branch (`1 session = 1 worktree`), runs `codex exec` in a sandbox, and captures a structured JSON report (`--output-schema`).
3. **Zero-Trust Verification (Gemini)** — Gemini runs `run_codex.py inspect`, audits the real diff and untracked files, re-runs tests independently, and sends surgical rework (`run_codex.py resume`) if anything is missing (capped by `--max-rounds`).
4. **Safe Landing & Clear Report (Gemini)** — Verified changes land via `patch`, `squash`, or `cherry-pick` with base-drift checks, temporary worktrees get cleaned up, and you get a plain-language summary of what changed and what was verified.

---

## Under the Hood: 7 Engineering Guardrails

To keep *"Gemini leads, Codex ships"* reliable in real repositories (not just demos), the bundled Python harness (`run_codex.py`) enforces 7 hard boundaries automatically:

1. **1 Session = 1 Out-of-Repo Git Worktree**: Every Codex session runs in `~/.cache/codex-worktrees/...` on its own temporary branch—never messing up your main branch, `git status`, or search tools.
2. **Dirty-Base Protection**: Because `git worktree` cuts from a commit (`HEAD`), `start` refuses to silently ignore uncommitted edits in your main tree unless you explicitly confirm with `--allow-dirty-base`.
3. **Instant Dependency Bootstrap & Secret Blocking**: `--copy-deps "node_modules,.venv"` uses macOS APFS copy-on-write (`cp -cR`, with standard fallback on Linux) for fast, write-isolated dependencies—while blocking `.env*`, `*.pem`, `*.key`, `id_rsa`, and `credentials.*` before any worktree is even created.
4. **No Rogue Git History Edits**: Worker briefs forbid `git commit / push / reset / checkout / rebase`. All Git mutations happen outside the Codex sandbox under Gemini's supervision.
5. **Exact Session Binding (No `--last` Collisions)**: Captures `session_id` / `thread_id` from `--json` JSONL events and binds `run_id ↔ session_id ↔ worktree_path` 1:1, making parallel sessions completely safe.
6. **Zero-Trust Anti-Cheating Audit**: `inspect` scans both tracked diffs **and newly created untracked files**, flagging touched tests, lockfiles, `@ts-ignore`, `# noqa`, `# type: ignore`, `.skip(`, bare `except:`, and `--no-verify`.
7. **Circuit Breaker & Base-Drift Landing Guard**: Stops runaway rework loops at `--max-rounds` (default `3`), checks if main `HEAD` moved while Codex was working, and enforces post-land re-verification before deleting the worktree.

---

## Quick Install

### Prerequisites
- **Git** (with `git worktree`)
- **Python 3.10+** (standard library only, zero third-party packages)
- **Codex CLI** installed and logged in (`codex --version`)

### Option 1: Install from `.skill` Release (Recommended)

Download `castor-pollux.skill` from [GitHub Releases](https://github.com/riverLaugh/castor-pollux/releases) (standard ZIP archive with executable permissions preserved):

```bash
# Global install (available across all projects)
unzip castor-pollux.skill -d ~/.gemini/config/skills/

# Or project-scoped install (shared inside a repository)
unzip castor-pollux.skill -d <your-repo>/.agents/skills/
```

### Option 2: Install as an Antigravity Plugin

```bash
git clone https://github.com/riverLaugh/castor-pollux.git \
  ~/.gemini/config/plugins/castor-pollux
```

### Option 3: Copy Directly from Source

```bash
git clone https://github.com/riverLaugh/castor-pollux.git
cp -R castor-pollux/skills/castor-pollux ~/.gemini/config/skills/
```

---

## Example Prompts

Once installed, just talk to Gemini naturally:
- *"Use `castor-pollux` to delegate this rate-limiter implementation to Codex, make sure all edge cases are tested, and give me a clear verification summary before landing."*
- *"Split these two refactors into parallel Codex worktrees, audit each diff independently, and apply the verified changes as clean patches."*

---

## CLI Reference (`run_codex.py`)

Gemini drives the workflow through `skills/castor-pollux/scripts/run_codex.py`:

| Command | Purpose |
| :--- | :--- |
| `preflight --repo <dir>` | Check Codex CLI, Git root, branch/HEAD, dirty files, submodules, and active runs |
| `start --repo <dir> --slug <name> --brief <file>` | Create isolated worktree & branch, launch `codex exec` with `--output-schema`, bind session |
| `inspect --run <run_id> [--full-diff]` | Return worker JSON report, tracked diff, untracked file previews, drift status, and audit warnings |
| `resume --run <run_id> --feedback <file>` | Send surgical feedback to the exact bound Codex session in the same worktree (enforces `--max-rounds`) |
| `land --run <run_id> --mode <patch\|squash\|cherry-pick>` | Land verified changes back to main repo (`patch` validates atomically via `git apply --check` first) |
| `discard --run <run_id>` | Remove temporary worktree and branch after landing or when discarding a failed approach |
| `list [--repo <dir>] [--all]` | List tracked runs with status, model/tier, and round count |
| `gc [--days 7] [--dry-run]` | Clean up old `landed` / `abandoned` worktrees, branches, and logs |

---

## Model & Speed Configuration

By default, Codex launches with an explicit fast worker profile so it won't silently inherit slow global `~/.codex/config.toml` defaults:

| Setting | Default | CLI Flag | Environment Variable |
| :--- | :--- | :--- | :--- |
| Model | `gpt-6.2-astra` | `--model` | `CASTOR_POLLUX_MODEL` |
| Reasoning effort | `low` | `--reasoning-effort` (`low\|medium\|high\|xhigh\|max\|ultra`) | `CASTOR_POLLUX_REASONING_EFFORT` |
| Service tier | `fast` | `--service-tier` (`fast`, or `standard` to disable `fast_mode`) | `CASTOR_POLLUX_SERVICE_TIER` |
| Worktree & registry root | `~/.cache/codex-worktrees` | — | `CASTOR_POLLUX_HOME` |

Escalate reasoning on a tough rework turn without changing global defaults:
```bash
python3 skills/castor-pollux/scripts/run_codex.py resume \
  --run "<run_id>" \
  --feedback "/tmp/feedback.md" \
  --reasoning-effort medium
```

---

## Repository Layout & Reproducible Build

```text
castor-pollux/
├── LICENSE
├── README.md
├── README_ZH.md
├── plugin.json
├── scripts/
│   └── build_skill.py                           # Reproducible, metadata-scrubbed .skill packer
└── skills/
    └── castor-pollux/
        ├── SKILL.md                             # 5-phase Manager-Worker SOP
        ├── scripts/
        │   └── run_codex.py                     # 1-session-1-worktree lifecycle harness
        ├── resources/
        │   ├── task_brief_template.md           # Contract task brief template
        │   └── worker_report.schema.json        # Strict JSON Schema for --output-schema
        └── references/
            └── edge_case_checklist.md           # 6-category edge-case & test-integrity checklist
```

Build `.skill` locally:
```bash
python3 scripts/build_skill.py
# -> dist/castor-pollux.skill
```

---

## License

[MIT](./LICENSE)

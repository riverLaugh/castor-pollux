---
name: castor-pollux
description: >-
  当用户要求让 Gemini 指挥/委派 Codex CLI（codex exec）执行编码、重构、修 Bug
  或实现子任务，并由 Gemini 制定边界条件、独立验收 diff 与测试、管理返工与合入时使用此
  skill。采用“1 个 Codex 会话 = 1 个独立 Git Worktree”强隔离架构。不要用于纯问答、
  简单信息查询或微小单行修改（除非用户明确要求交给 Codex）。
---

# Castor & Pollux（Gemini 指挥 × Codex 独立 Worktree 执行 SOP）

你是 **Tech Lead / Architect / Verifier**，Codex 是 **Implementer**。
核心铁律：
1. **1 个 Codex 会话严格绑定 1 个独立 Git Worktree**（路径位于主仓库之外），绝不在主工作区直接放任 Codex 改代码。
2. **契约先行**：派单前你必须先阅读相关源码与测试，写出包含修改边界、契约不变量和编号边界条件的任务书，绝不做“传话筒”。
3. **零信任验收**：绝不轻信 Codex 报告里的“测试已通过”；验收必须由你亲自审计 `git diff`、检查新增未跟踪文件，并在该 worktree 内亲自重新运行验证命令。
4. **熔断止损**：返工受轮次上限约束（默认 3 轮），触发熔断即停止空转。

所有脚本调用均执行本 `SKILL.md` 同目录下的 [scripts/run_codex.py](./scripts/run_codex.py)（下文简写为 `<RUN_CODEX>`；全局安装时默认为 `~/.gemini/config/skills/castor-pollux/scripts/run_codex.py`，项目级安装时为 `.agents/skills/castor-pollux/scripts/run_codex.py`）。

---

## 阶段 0：预检与隔离确认（Pre-flight）

在派活前运行预检：

```bash
python3 <RUN_CODEX> preflight --repo "<repo_root>"
```

根据输出处理边界情况：
- **非 Git 仓库**：无法使用 worktree 隔离；向用户说明并确认是否改由 Gemini 直接处理，禁止强行绕过。
- **主工作区存在未提交改动（`dirty_files` 非空）**：
  - 注意：`git worktree` 从 commit（默认 `HEAD`）切出，**不会包含**主工作区未提交的改动。
  - 若本次任务**不依赖**这些未提交改动：后续 `start` 时显式加 `--allow-dirty-base`，安全基于 `HEAD` 隔离开发。
  - 若本次任务**依赖**这些未提交改动：先提示用户提交一个 WIP commit（或在用户同意后另建基线），禁止静默基于旧 `HEAD` 派活。
- **含 Submodule**：检查输出中的 `submodules`，若任务涉及子模块，先确认初始化策略。
- **并发与权限**：
  - 调研/分析/只读排查：`--sandbox read-only`；
  - 正常编码实现：`--sandbox workspace-write`（默认）；
  - **禁止**默认绕过沙盒（`--dangerously-bypass-approvals-and-sandbox`），除非用户明确要求。

---

## 阶段 1：先勘探，再写契约化任务书（Task Brief）

1. 先由你自己阅读任务涉及的入口代码、类型/接口定义和现有测试，确认真实上下文与验证命令。
2. 阅读边界条件参考清单 [edge_case_checklist.md](./references/edge_case_checklist.md)，筛选与本任务相关的边界条件并编号（`EC-1`, `EC-2`, ...）。
3. 参照 [task_brief_template.md](./resources/task_brief_template.md) 编写任务书，保存到 `/tmp/codex-brief-<slug>.md`（**不要**写入项目仓库目录，避免污染 `git status`）。
4. 任务书必须写清：
   - **Goal & Context**：目标、关键文件路径与符号名；
   - **In-Scope / Out-of-Scope**：允许修改/新增的文件范围，以及明确禁止碰的范围；
   - **Contracts & Invariants**：接口签名、错误语义、向后兼容要求；
   - **Edge Cases（编号）**：逐条列出 `EC-1`, `EC-2`, ...，要求 Codex 在代码和测试中同时覆盖；
   - **Verification Commands**：Codex 自测必须执行的命令；
   - **Worker 红线**：禁止执行 `git commit / push / checkout / reset / rebase`；禁止删除/弱化/skip 既有测试；禁止塞入 `any` / `@ts-ignore` / `# noqa` / 裸 `except: pass` 掩盖错误；遇阻立即以 `status: "blocked"` 停下上报。

---

## 阶段 2：建 Worktree 并派发 Codex（Dispatch）

调用包装脚本创建独立 worktree、绑定 `run_id` 并执行 `codex exec`（自动传入 [worker_report.schema.json](./resources/worker_report.schema.json) 强制结构化回报）：

```bash
python3 <RUN_CODEX> start \
  --repo "<repo_root>" \
  --slug "<short-task-slug>" \
  --brief "/tmp/codex-brief-<slug>.md" \
  --sandbox workspace-write \
  --max-rounds 3
```

常用按需参数：
- **默认模型与档位（已显式锁定，避免静默继承全局 `~/.codex/config.toml` 配置）**：
  - `--model gpt-6.2-astra`（可通过 `CASTOR_POLLUX_MODEL` 覆盖）
  - `--reasoning-effort low`（对应 `-c model_reasoning_effort="low"`，可通过 `CASTOR_POLLUX_REASONING_EFFORT` 覆盖）
  - `--service-tier fast`（对应 `-c service_tier="fast"`，可通过 `CASTOR_POLLUX_SERVICE_TIER` 覆盖）
  - 架构规划和边界条件已经由 Gemini 完成，常规派单保持 `gpt-6.2-astra` + `low` + `fast` 以获得最快周转；仅在复杂算法/并发难题或 `low` 档返工卡壳时，按需显式传 `--reasoning-effort medium` 或 `high` 升档。
- `--base <ref>`：指定切出基线（默认 `HEAD`）。
- `--allow-dirty-base`：确认任务不依赖主工作区未提交改动时传入。
- `--copy-deps "node_modules,.venv"`：在 macOS 上使用 APFS Copy-on-Write（`cp -cR`）秒级写隔离复制依赖目录，避免新 worktree 缺依赖或污染主工作区。**禁止**复制含生产密钥的 `.env` 文件（脚本默认拦截敏感文件名）。
- `--run-setup-hook`：若仓库存在 `.agents/worktree-setup.sh`，仅在你**先审阅该脚本内容确认安全后**才传入此参数执行。
- `--allow-git-dir`：仅当任务内测试工具本身必须写 git index 时传入（仅精准放行该 worktree 对应的 `.git/worktrees/<name>` 子目录）。
- `--profile <profile>` / `--timeout <sec>`：按需覆写配置或超时（默认 600s）。

> **并行派发原则**：大需求可拆成文件范围互不重叠的多个子任务，分别运行 `start` 产生多个独立 `run_id`（每个 `run_id` 拥有独立 worktree 与独立 `codex_session_id`）。禁止让两个 Codex 会话共享同一个 worktree。

---

## 阶段 3：零信任独立验收（Zero-Trust Verification）

Codex 返回后，先运行 `inspect` 拉取客观证据（包含 Codex 报告、git 状态、未跟踪文件、diff 统计、可疑改动告警与基线漂移状态）：

```bash
python3 <RUN_CODEX> inspect \
  --run "<run_id>" \
  --full-diff
```

随后按 **L1 → L4** 逐层由你亲自验收：

### L1：范围与诚实度审计（Diff Audit）
- 逐文件检查 `tracked_changes` 和 `untracked_files`：
  - 是否修改了 `Out-of-Scope` 文件？
  - 是否修改、削弱、skip 或删除了既有测试断言？
  - 是否出现 `audit_warnings` 中提示的屏蔽检查标记（`@ts-ignore`、`# noqa`、空 `catch`、`TODO/FIXME` 敷衍实现、调试 `print/console.log`）？
  - 是否引入未授权的新依赖？

### L2：机械门禁复测（Independent Execution）
- **由 Gemini 自己**在 `<worktree_path>` 内重新运行 build、typecheck、lint 与相关测试套件（例如 `cd "<worktree_path>" && <test_cmd>`），亲眼确认退出码为 0。绝不直接采信 `report.json` 中的 `commands_run`。

### L3：边界条件逐条核验（Edge-Case Audit）
- 将任务书的 `EC-1`, `EC-2`, ... 与 `report.json` 的 `edge_cases_handled` 以及真实 diff 逐条交叉核对：
  - 代码里是否真的处理了该边界分支？
  - `test_location` 指向的测试用例是否真实存在、断言是否有效（不是只测 Happy Path 或 mock 掉了被测核心逻辑）？
- 对高风险改动，可由你补充对抗性边界测试，或在 `<worktree_path>` 中追加一次只读交叉评审：
  `codex review --uncommitted`（在 worktree 目录内执行）。

### L4：回归与副作用审查
- 检查外部接口兼容性、性能复杂度（N+1、大输入退化）、资源释放（句柄/锁/连接）、错误传播以及日志是否可能泄露敏感信息。

---

## 阶段 4：结构化打回与熔断止损（Rework & Circuit Breaker）

### 验收不通过时
1. 写一份**精准返工反馈**到 `/tmp/codex-feedback-<run_id>-r<n>.md`，必须包含：
   - 具体问题定位：`文件路径:行号`；
   - 客观证据：失败命令与真实报错输出，或缺失的 `EC-x` 编号；
   - 明确期望行为与禁止破坏的既有约束。
2. 在**同一个 worktree + 同一个 session** 上返工：

```bash
python3 <RUN_CODEX> resume \
  --run "<run_id>" \
  --feedback "/tmp/codex-feedback-<run_id>-r2.md"
```

### 什么时候不要 `resume`，而要废弃重开？
- 如果 Codex 整体实现方向彻底跑偏、diff 臃肿杂乱，**不要**在污染的上下文上硬修：
  运行 `discard --run <run_id>` 彻底销毁该 worktree 和分支，修正任务书后重新 `start`。

### 熔断条件（Circuit Breaker）
满足以下任一条件立即触发熔断，**停止继续调 Codex**：
1. 达到 `max_rounds` 上限（脚本会自动拦截）仍未通过验收；
2. 连续 2 轮出现相同根因错误或改动范围持续扩张；
3. Codex 报告 `status: "blocked"`（需求歧义、缺依赖、权限受限）。

触发熔断后：保留该 worktree 现场，向用户清晰汇报“已完成内容、失败卡点、diff 摘要与下一步建议（由 Gemini 接管收尾 / 补充依赖或澄清需求 / 放弃方案）”。

---

## 阶段 5：合入与清理（Landing & Cleanup）

仅当 L1–L4 全部验收通过后，才将 worktree 改动合回主仓库：

```bash
# 模式 1（最保守默认）：以未提交改动形式 git apply 到主工作区，供用户最终检视并自行提交
python3 <RUN_CODEX> land \
  --run "<run_id>" \
  --mode patch

# 模式 2：主工作区干净时，squash 合入并暂存（传入 --commit-msg 则直接生成单条干净 commit）
python3 <RUN_CODEX> land \
  --run "<run_id>" \
  --mode squash \
  --commit-msg "<type>: <summary>"

# 模式 3：主工作区干净时，在 worktree 提交后 cherry-pick 到目标分支
python3 <RUN_CODEX> land \
  --run "<run_id>" \
  --mode cherry-pick \
  --commit-msg "<type>: <summary>"
```

### 基线漂移强制规则（Base Drift）
- 若 `land` 检测到主分支 `HEAD` 相比 `base_sha` 已前进（例如并行任务的另一个 worktree 先合入了），脚本会拦截并报告 `base_drifted: true`。
- 确认允许合入（`--allow-base-drift`）并合回主仓库后，**必须在主仓库重新运行完整验证命令**，防止出现“无文本冲突但有语义冲突”。
- **未完成合入与复测前，严禁删除 worktree。**

### 清理
- 合入并复测通过后清理该次会话的 worktree 与临时分支：
  `python3 <RUN_CODEX> discard --run "<run_id>"`
- 查看历史会话或定期清理孤儿目录：
  `python3 <RUN_CODEX> list`
  `python3 <RUN_CODEX> gc --days 7`

---

## 最终向用户交付的必备内容

1. **变更摘要**：修改/新增的文件列表及核心设计说明；
2. **边界条件验收表**：任务书中每条 `EC-x` 的实现位置与测试覆盖核验结论；
3. **Gemini 亲测结果**：你在 worktree（及漂移合入后的主仓库）亲自执行的命令与真实退出码；
4. **Worktree 状态与残余风险**：是否已合入、是否已清理 worktree，以及任何未覆盖项或需用户确认的假设。

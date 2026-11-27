# Task Brief: <任务简短标题>

## 1. Goal & Context
- **目标**：<清晰描述要完成的功能/修复/重构，以及完成后的可观测行为>
- **相关入口文件与符号**：
  - `<path/to/file1>`（`<SymbolName>`）
  - `<path/to/file2>`

## 2. File Scope（严格修改边界）
- **允许修改 / 新增（In-Scope）**：
  - `<path/or/dir1>`
  - `<path/to/tests>`
- **明确禁止修改（Out-of-Scope）**：
  - 范围外业务模块与无关重构
  - 锁文件与依赖清单（如 `package-lock.json`、`uv.lock`、`Cargo.lock`），除非本任务明确允许
  - 既有测试用例的断言预期（不得通过改弱测试来通过验证）

## 3. Contracts & Invariants（接口契约与不变量）
- **输入 / 输出契约**：<函数签名、数据结构、错误类型/错误码>
- **不变量与兼容性**：<必须保持不变的既有行为、默认值、持久化格式或 API 兼容性>

## 4. Explicit Edge Cases（必查边界条件）
每个编号都必须在实现和自动化测试中覆盖，并在最终 JSON 报告的 `edge_cases_handled` 中逐条回填：
- `EC-1`：<例如：输入为 `null` / 空集合 / 空字符串时的明确行为>
- `EC-2`：<例如：数值为 `0` / 负数 / 上限溢出时的行为>
- `EC-3`：<例如：并发重入、重复提交或部分失败时的原子性/幂等性>
- `EC-4`：<例如：超时、文件不存在或畸形输入时的错误语义与资源释放>

## 5. Required Verification Commands（交付前必须执行）
在当前 worktree 根目录依次执行以下验证命令，并把真实命令与退出码如实填入 `commands_run`：
- `<command 1, e.g. pytest tests/test_xxx.py -q>`
- `<command 2, e.g. ruff check ... / mypy ... / npm run typecheck>`

## 6. Worker Hard Rules（不可违反）
1. **禁止 Git 写历史操作**：严禁运行 `git commit`、`git push`、`git checkout`、`git reset`、`git rebase` 或修改分支状态；由验收方在沙盒外统一处理 Git。
2. **禁止掩盖错误**：严禁使用 `@ts-ignore`、`as any`、`# type: ignore`、`# noqa`、裸 `except: pass`、`.skip` / `@pytest.mark.skip` 或 `--no-verify` 规避失败。
3. **禁止私自装包或扩大范围**：若发现必须安装新依赖、修改 `Out-of-Scope` 文件、或需求本身存在矛盾，**立即停止修改**，在最终 JSON 报告中返回 `status: "blocked"` 并在 `blockers` 中说明具体原因和建议。
4. **最终输出格式**：最终响应必须严格符合 `--output-schema` 指定的 JSON 结构，不要输出多余包裹文本。

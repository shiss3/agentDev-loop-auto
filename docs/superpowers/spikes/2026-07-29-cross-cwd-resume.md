# Spike: 跨 cwd `--resume` 可行性

日期：2026-07-29
计划：`docs/superpowers/plans/2026-07-29-executor-continuation.md` Task 0

## 结论：**不可行**

## 实测

### Step 1: 目录 A 建 session

```bash
cd .spike/resume-a
claude -p "记住暗号:蓝鲸42。只回复ok两个字。" --bare --model glm-5.2 \
  --strict-mcp-config --max-turns 5 --output-format stream-json --verbose \
  --permission-mode acceptEdits
```

- init 行：`session_id: c56ba1d2-a6aa-4dc3-8ab7-3772382618a6`，cwd=`...\resume-a`
- result：正常完成（duration_ms=7810）✅

### Step 2: 目录 B 跨 cwd resume

```bash
cd .spike/resume-b
claude -p --resume c56ba1d2-a6aa-4dc3-8ab7-3772382618a6 "暗号是什么?只回答暗号本身。" \
  --bare --model glm-5.2 --strict-mcp-config --max-turns 5 \
  --output-format stream-json --verbose --permission-mode acceptEdits
```

- result：`subtype: error_during_execution`，`is_error: true`，num_turns=0
- errors：`["No conversation found with session ID: c56ba1d2-..."]` ❌

## 分析

session 存储按 cwd 项目 slug 分目录（`~/.claude/projects/<slug-of-cwd>/`），`--resume` 只在**当前 cwd 对应的项目目录**下查找 session。cwd 不同 = 查不到。

对本特性的影响：模块执行器 session 建于已删除的 worktree（`.worktrees/deliver-<req>-<mid>`），续作在新 worktree（路径不同），`--resume` 必然查不到 → 不可行。

## 下一步

按 spec §5 降级：**上下文重注入**——不 resume，把该模块历史 dispatch manifest + 交付摘要拼进 prompt。降级方案需用户确认后实施（spec 既定流程）。

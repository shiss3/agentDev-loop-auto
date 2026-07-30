# Spike: 跨 cwd `--resume` 可行性

日期：2026-07-29
计划：`docs/superpowers/plans/2026-07-29-executor-continuation.md` Task 0

## 结论：跨 cwd 不可行；**同路径重建 cwd 后 resume 可行（采用此路）**

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

### Step 3（补充）: 同路径重建 cwd 后 resume —— **可行** ✅

```bash
# 删掉 resume-a 后,重建同路径空目录
mkdir -p .spike/resume-a && cd .spike/resume-a
claude -p --resume c56ba1d2-... "暗号是什么?只回答暗号本身。" (同前 flags)
```

- result 正常完成（duration_ms=6881），模型回复 **`蓝鲸42`** ✅
- 结论：session 查找只看 cwd 路径字符串；目录曾删除、同路径重建空目录不影响。

## 对本特性的落地方案

worktree 路径确定性：`deliver-<req_id>-<module_id>`（worktree.py:29）。**续作时用注册表里的原 req_id 重建 worktree = 恢复原 cwd = resume 命中**，无需降级重注入：

1. 注册表 `req_id` 字段（spec 已定）正是关键。
2. `continuation_flow` 把原 req_id 放进合成 spec（`_req_id` 键），`_spec_to_modules` 优先吃它而非新生成。
3. 分支从当前 HEAD 重建（模块代码已 merge 主干，起点正确）；`create_delivery_worktree` 幂等处理残留。

前提：同一台机器（session 存本机 `~/.claude/projects/`）——本地工具，成立。

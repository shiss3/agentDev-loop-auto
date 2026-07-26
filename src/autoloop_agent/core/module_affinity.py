"""module_id 归一化 - L0 灌队列前防同模块任务散多进程致 affinity 失效。

LLM 产的 module_id slug 可能脏（UserAuth/user-auth/userauth），致同模块亲和键不一致。
灌队列前经 normalize_module_id 统一。
"""

from __future__ import annotations

import re

# 业务模块同义词表：L0 维护（如 {"userauth": "user_auth"}），空则只 sanitize。
# key/value 均为已 sanitize 形式（小写+下划线）；表值会被再 sanitize 兜底。
SYNONYMS: dict[str, str] = {}


def sanitize_module_id(slug: str | None) -> str:
    """小写 -> 非 [a-z0-9_] 转 _ -> strip _；空串/None -> default。"""
    if not slug:
        return "default"
    s = slug.lower()
    s = re.sub(r"[^a-z0-9_]", "_", s)
    s = s.strip("_")
    return s or "default"


def normalize_module_id(slug: str | None) -> str:
    """sanitize -> SYNONYMS.get -> 再 sanitize（防表值脏）。"""
    sanitized = sanitize_module_id(slug)
    mapped = SYNONYMS.get(sanitized, sanitized)
    return sanitize_module_id(mapped)


def affinity_key(module_id: str, domain: str) -> str:
    """亲和键：normalize_module_id(module_id)/domain。"""
    return f"{normalize_module_id(module_id)}/{domain}"

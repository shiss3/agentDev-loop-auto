"""module_id 归一化 - 防 LLM 产的脏 slug(UserAuth/user-auth/userauth)散成多模块。"""

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

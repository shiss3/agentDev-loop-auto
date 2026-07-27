"""module_affinity 单元测试 - module_id 归一化。

覆盖：
- sanitize_module_id: 大小写/分隔符/空串/None/前后空格
- normalize_module_id: SYNONYMS 空时 = sanitize；填表后归一
"""

from __future__ import annotations

from autoloop_agent.core.module_affinity import (
    SYNONYMS,
    normalize_module_id,
    sanitize_module_id,
)


# ── sanitize_module_id ────────────────────────────────


def test_sanitize_uppercase():
    assert sanitize_module_id("UserAuth") == "userauth"


def test_sanitize_hyphen():
    assert sanitize_module_id("user-auth") == "user_auth"


def test_sanitize_space():
    assert sanitize_module_id("user auth") == "user_auth"


def test_sanitize_empty():
    assert sanitize_module_id("") == "default"


def test_sanitize_none():
    assert sanitize_module_id(None) == "default"


def test_sanitize_surrounding_space_and_hyphen():
    assert sanitize_module_id("  Pay-Ment ") == "pay_ment"


# ── normalize_module_id ───────────────────────────────


def test_normalize_no_synonyms_equals_sanitize():
    assert normalize_module_id("UserAuth") == "userauth"
    assert normalize_module_id("user-auth") == "user_auth"


def test_normalize_with_synonyms(monkeypatch):
    monkeypatch.setitem(SYNONYMS, "userauth", "user_auth")
    assert normalize_module_id("UserAuth") == "user_auth"
    assert normalize_module_id("user-auth") == "user_auth"


def test_normalize_dirty_synonym_value(monkeypatch):
    """表值脏（未 sanitize）-> 再 sanitize 兜底归一。"""
    monkeypatch.setitem(SYNONYMS, "userauth", "User Auth")
    assert normalize_module_id("userauth") == "user_auth"

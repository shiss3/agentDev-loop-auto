"""module_scheduler 单元测试:文件集归一化 / 边构建 / Kahn 分层。"""

from __future__ import annotations

from autoloop_agent.core.module_scheduler import (
    build_execution_layers,
    build_module_edges,
    module_file_set,
)


def _mod(mid: str, files: list[str] | None = None, deps: list[str] | None = None,
         no_files: bool = False) -> dict:
    """构造模块 dict。files 挂在单个子任务上;no_files=True 时子任务无 intended_files。"""
    st: dict = {"id": "t1", "summary": "s", "acceptance": []}
    if not no_files:
        st["intended_files"] = files or []
    m: dict = {"module_id": mid, "summary": "s", "acceptance": [], "subtasks": [st]}
    if deps is not None:
        m["deps"] = deps
    return m


# ── module_file_set ──────────────────────────────────


def test_file_set_unions_subtasks():
    m = {
        "module_id": "m",
        "subtasks": [
            {"id": "t1", "intended_files": ["a.py", "b.py"]},
            {"id": "t2", "intended_files": ["b.py", "c.py"]},
        ],
    }
    assert module_file_set(m) == {"a.py", "b.py", "c.py"}


def test_file_set_normalizes_separator_dot_case():
    m = _mod("m", files=["src\\A.py", "./src/a.py", "SRC/B.py"])
    assert module_file_set(m) == {"src/a.py", "src/b.py"}


def test_file_set_empty():
    assert module_file_set(_mod("m")) == set()
    assert module_file_set(_mod("m", no_files=True)) == set()


# ── build_module_edges ───────────────────────────────


def test_edges_disjoint_no_deps():
    mods = [_mod("a", ["a.py"]), _mod("b", ["b.py"])]
    assert build_module_edges(mods) == set()


def test_edges_overlap_forces_serial_low_index_first():
    mods = [_mod("a", ["x.py"]), _mod("b", ["x.py", "y.py"])]
    assert build_module_edges(mods) == {("a", "b")}


def test_edges_overlap_direction_follows_declaration_order():
    mods = [_mod("b", ["x.py"]), _mod("a", ["x.py"])]
    assert build_module_edges(mods) == {("b", "a")}


def test_edges_empty_file_set_serial_with_everything():
    mods = [_mod("a", ["a.py"]), _mod("b"), _mod("c", ["c.py"])]
    edges = build_module_edges(mods)
    assert ("a", "b") in edges and ("b", "c") in edges
    assert ("a", "c") not in edges


def test_edges_dep_hint():
    mods = [_mod("a", ["a.py"]), _mod("b", ["b.py"], deps=["a"])]
    assert build_module_edges(mods) == {("a", "b")}


def test_edges_unknown_and_self_dep_ignored():
    mods = [_mod("a", ["a.py"], deps=["ghost", "a"])]
    assert build_module_edges(mods) == set()


def test_edges_hint_plus_overlap():
    mods = [_mod("a", ["a.py"]), _mod("b", ["b.py"], deps=["a"]), _mod("c", ["a.py"])]
    edges = build_module_edges(mods)
    assert ("a", "b") in edges and ("a", "c") in edges


# ── build_execution_layers ───────────────────────────


def test_layers_disjoint_parallel():
    mods = [_mod("a", ["a.py"]), _mod("b", ["b.py"]), _mod("c", ["c.py"])]
    assert build_execution_layers(mods) == [["a", "b", "c"]]


def test_layers_overlap_serial():
    mods = [_mod("a", ["x.py"]), _mod("b", ["x.py"])]
    assert build_execution_layers(mods) == [["a"], ["b"]]


def test_layers_dep_chain():
    mods = [_mod("a", ["a.py"]), _mod("b", ["b.py"], deps=["a"]), _mod("c", ["c.py"], deps=["b"])]
    assert build_execution_layers(mods) == [["a"], ["b"], ["c"]]


def test_layers_mixed():
    """a/b 文件隔离可并行;c 依赖 a 落第二层。"""
    mods = [_mod("a", ["a.py"]), _mod("b", ["b.py"]), _mod("c", ["c.py"], deps=["a"])]
    assert build_execution_layers(mods) == [["a", "b"], ["c"]]


def test_layers_dep_cycle_terminates():
    mods = [_mod("a", ["a.py"], deps=["b"]), _mod("b", ["b.py"], deps=["a"])]
    layers = build_execution_layers(mods)
    assert sorted(mid for layer in layers for mid in layer) == ["a", "b"]
    assert len(layers) == 2  # 强制破环:逐个单层


def test_layers_single_module():
    assert build_execution_layers([_mod("only", ["o.py"])]) == [["only"]]


def test_layers_empty_modules():
    assert build_execution_layers([]) == []

"""模块执行分层:同层并行、层间串行。纯函数,无 I/O。

边规则(a 先于 b):
- deps 硬依赖:b.deps 含 a -> (a, b)。LLM 声明的逻辑硬依赖(文件不重叠时唯一串行依据)。
- 文件重叠硬门:intended_files 并集相交,或任一方为空(未知足迹)-> (低 index, 高 index)。
  重叠边恒低 index -> 高 index,天然无环;环只能来自 deps 提示。
"""
from __future__ import annotations


def _norm_path(p: str) -> str:
    """路径归一化防假不相交:'\\' -> '/',去 './' 前缀,lower。"""
    return p.replace("\\", "/").removeprefix("./").lower()


def module_file_set(module: dict) -> set[str]:
    """模块全部子任务 intended_files 归一化并集。"""
    files: set[str] = set()
    for st in module.get("subtasks") or []:
        for f in st.get("intended_files") or []:
            files.add(_norm_path(f))
    return files


def build_module_edges(modules: list[dict]) -> set[tuple[str, str]]:
    """(a, b) = a 必须先于 b。未知/自引用 dep 忽略。"""
    known = {m["module_id"] for m in modules}
    edges: set[tuple[str, str]] = set()
    for m in modules:
        for dep in m.get("deps") or []:
            if dep in known and dep != m["module_id"]:
                edges.add((dep, m["module_id"]))
    file_sets = [module_file_set(m) for m in modules]
    for i in range(len(modules)):
        for j in range(i + 1, len(modules)):
            fi, fj = file_sets[i], file_sets[j]
            if not fi or not fj or fi & fj:
                edges.add((modules[i]["module_id"], modules[j]["module_id"]))
    return edges


def build_execution_layers(modules: list[dict]) -> list[list[str]]:
    """Kahn 分层:同层可并行,层间串行。remaining 保声明序保证确定性。

    deps 提示成环:ready 为空时强制取 remaining[0] 为单层继续(不挂;调用方告警)。
    """
    edges = build_module_edges(modules)
    remaining = [m["module_id"] for m in modules]
    layers: list[list[str]] = []
    while remaining:
        settled = set(modules_by_id for layer in layers for modules_by_id in layer)
        ready = [
            mid for mid in remaining
            if all(a in settled for (a, b) in edges if b == mid)
        ]
        if not ready:  # deps 环:强制破环
            ready = [remaining[0]]
        layers.append(ready)
        remaining = [mid for mid in remaining if mid not in ready]
    return layers

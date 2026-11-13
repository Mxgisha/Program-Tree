"""Pass 1: turn a module's AST into symbols (its public shape)."""

from __future__ import annotations

import ast
from typing import Iterable

from .model import Graph, ModuleInfo, Symbol

BRANCH_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
                ast.IfExp, ast.comprehension)
MAX_DOC = 600


DYNAMIC_NAMES = {"getattr", "globals", "locals", "eval", "exec", "__import__"}


def index_module(mod: ModuleInfo, graph: Graph) -> None:
    if mod.tree is not None:
        mod.all_names = module_all_names(mod.tree)
        mod.has_main_guard = module_has_main_guard(mod.tree)
        mod.dynamic = module_is_dynamic(mod.tree)
    Indexer(mod, graph).run()


def _module_level(stmts: Iterable[ast.stmt]) -> Iterable[ast.stmt]:
    """Yield module-scope statements, descending into control flow only."""
    for stmt in stmts:
        yield stmt
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for block in _blocks(stmt):
            yield from _module_level(block)


def module_all_names(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for stmt in _module_level(tree.body):
        values: list[ast.expr] = []
        if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets):
            values = [stmt.value]
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) \
                and stmt.target.id == "__all__" and stmt.value is not None:
            values = [stmt.value]
        elif isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name) \
                and stmt.target.id == "__all__":
            values = [stmt.value]
        for value in values:
            if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                for elt in value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        if elt.value not in names:
                            names.append(elt.value)
    return names


def module_has_main_guard(tree: ast.Module) -> bool:
    return any(_is_main_guard(stmt.test) for stmt in _module_level(tree.body)
               if isinstance(stmt, ast.If))


def _is_main_guard(test: ast.expr) -> bool:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    pairs = [(test.left, test.comparators[0]), (test.comparators[0], test.left)]
    for left, right in pairs:
        if (isinstance(left, ast.Name) and left.id == "__name__"
                and isinstance(right, ast.Constant) and right.value == "__main__"):
            return True
    return False


def module_is_dynamic(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in DYNAMIC_NAMES:
            return True
        if isinstance(node, ast.Attribute) and node.attr == "import_module":
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name in ("__getattr__", "__getattribute__"):
            return True
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            return True
    return False


class Indexer:
    def __init__(self, mod: ModuleInfo, graph: Graph) -> None:
        self.mod = mod
        self.graph = graph
        self.tree = mod.tree

    def run(self) -> None:
        self._body(self.tree.body, self.mod.name, "module", "")

    # ------------------------------------------------------------------
    def _body(self, stmts: Iterable[ast.stmt], parent: str, container: str,
              prefix: str) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _is_overload(stmt):
                    continue
                sym = self._function(stmt, parent, container, prefix)
                self._body(stmt.body, sym.id, "function", sym.qualname)
            elif isinstance(stmt, ast.ClassDef):
                sym = self._class(stmt, parent, prefix)
                self._body(stmt.body, sym.id, "class", sym.qualname)
            elif container in ("module", "class"):
                self._variable(stmt, parent, container, prefix)
            # Walk control flow so conditional / loop-nested definitions are found.
            if isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While,
                                 ast.With, ast.AsyncWith, ast.Try, ast.Match)):
                for block in _blocks(stmt):
                    self._body(block, parent, container, prefix)

    def _new_symbol(self, node: ast.AST, name: str, kind: str, parent: str,
                    prefix: str, **kw) -> Symbol:
        existing = self.graph.symbols.get(parent)
        if existing is not None:
            sibling_id = existing.attrs.get(name)
            sibling = self.graph.symbols.get(sibling_id) if sibling_id else None
            if sibling is not None and _mergeable(sibling.kind, kind):
                self.mod.node_map[id(node)] = sibling.id
                return sibling
            if sibling is not None:
                sibling.superseded = True  # last definition wins at runtime
        qualname = f"{prefix}.{name}" if prefix else name
        sym = Symbol(
            id=f"{self.mod.name}::{qualname}",
            name=name,
            qualname=qualname,
            kind=kind,
            module=self.mod.name,
            path=self.mod.relpath,
            line=getattr(node, "lineno", 0),
            end_line=getattr(node, "end_lineno", getattr(node, "lineno", 0)) or 0,
            parent=parent,
            **kw,
        )
        sym.loc = max(1, sym.end_line - sym.line + 1) if sym.end_line else 1
        sym.is_test = (is_test_path(self.mod.relpath)
                       or name.startswith("test_")
                       or any("fixture" in d for d in sym.decorators))
        sym = self.graph.add_symbol(sym)
        self.mod.symbols.append(sym.id)
        self.mod.node_map[id(node)] = sym.id
        if parent in self.graph.symbols:
            self.graph.symbols[parent].attrs[name] = sym.id
        return sym

    # ------------------------------------------------------------------
    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, parent: str,
                  container: str, prefix: str) -> Symbol:
        if container == "class":
            kind = "method"
        elif container == "module":
            kind = "function"
        else:
            kind = "nested"
        returns = ast.unparse(node.returns) if node.returns else ""
        sym = self._new_symbol(
            node, node.name, kind, parent, prefix,
            signature=fmt_signature(node),
            doc=(ast.get_docstring(node, clean=True) or "")[:MAX_DOC],
            decorators=[_safe_unparse(d) for d in node.decorator_list],
            is_async=isinstance(node, ast.AsyncFunctionDef),
            complexity=complexity_of(node),
            returns=returns,
            param_types=param_annotations(node),
        )
        self.mod.nodes[sym.id] = node
        return sym

    def _class(self, node: ast.ClassDef, parent: str, prefix: str) -> Symbol:
        sym = self._new_symbol(
            node, node.name, "class", parent, prefix,
            decorators=[_safe_unparse(d) for d in node.decorator_list],
            bases=[_safe_unparse(b) for b in node.bases],
            doc=(ast.get_docstring(node, clean=True) or "")[:MAX_DOC],
        )
        self.mod.nodes[sym.id] = node
        return sym

    def _variable(self, stmt: ast.stmt, parent: str, container: str,
                  prefix: str) -> None:
        annotation = ""
        if isinstance(stmt, ast.AnnAssign):
            if not isinstance(stmt.target, ast.Name):
                return
            annotation = _safe_unparse(stmt.annotation)
            target = stmt.target
        elif isinstance(stmt, ast.Assign):
            if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                return
            target = stmt.targets[0]
        else:
            return
        if target.id == "__all__":
            return
        kind = "attribute" if container == "class" else "variable"
        self._new_symbol(stmt, target.id, kind, parent, prefix, annotation=annotation)


def _blocks(stmt: ast.stmt) -> list[list[ast.stmt]]:
    out: list[list[ast.stmt]] = []
    for attr in ("body", "orelse", "finalbody"):
        block = getattr(stmt, attr, None)
        if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
            out.append(block)
    for handler in getattr(stmt, "handlers", []) or []:
        out.append(handler.body)
    for case in getattr(stmt, "cases", []) or []:
        out.append(case.body)
    return out


def _is_overload(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        head = _safe_unparse(dec).split("(")[0]
        if head.rsplit(".", 1)[-1] == "overload":
            return True
    return False


def _mergeable(existing_kind: str, new_kind: str) -> bool:
    callables = {"function", "method", "nested"}
    if existing_kind in callables and new_kind in callables:
        return True
    if existing_kind in ("variable", "attribute") and new_kind in ("variable", "attribute"):
        return True
    return existing_kind == new_kind


def is_test_path(relpath: str) -> bool:
    parts = relpath.split("/")
    base = parts[-1]
    return (any(p in ("tests", "test") for p in parts[:-1])
            or base.startswith("test_") or base.endswith("_test.py"))


def _arg(arg: ast.arg, default: ast.expr | None = None) -> str:
    text = arg.arg
    if arg.annotation is not None:
        text += ": " + _safe_unparse(arg.annotation)
    if default is not None:
        text += " = " + _safe_unparse(default)
    return text


def fmt_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    a = node.args
    parts: list[str] = []
    defaults = [None] * (len(a.posonlyargs) + len(a.args) - len(a.defaults))
    defaults += list(a.defaults)
    all_pos = list(a.posonlyargs) + list(a.args)
    for i, (arg, default) in enumerate(zip(all_pos, defaults)):
        if a.posonlyargs and i == len(a.posonlyargs):
            parts.append("/")
        parts.append(_arg(arg, default))
    if a.posonlyargs and not a.args:
        parts.append("/")
    if a.vararg is not None:
        parts.append("*" + _arg(a.vararg))
    elif a.kwonlyargs:
        parts.append("*")
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        parts.append(_arg(arg, default))
    if a.kwarg is not None:
        parts.append("**" + _arg(a.kwarg))
    ret = f" -> {_safe_unparse(node.returns)}" if node.returns else ""
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    text = f"{prefix}{node.name}({', '.join(parts)}){ret}"
    return text if len(text) <= 300 else text[:297] + "..."


def param_annotations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str]:
    out = {}
    a = node.args
    for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs) + \
            [x for x in (a.vararg, a.kwarg) if x is not None]:
        if arg.annotation is not None:
            out[arg.arg] = _safe_unparse(arg.annotation)
    return out


def complexity_of(node: ast.AST) -> int:
    score = 1
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(n, BRANCH_NODES):
            score += 1
        elif isinstance(n, ast.BoolOp):
            score += max(0, len(n.values) - 1)
        elif isinstance(n, ast.Match):
            score += max(0, len(n.cases) - 1)
        stack.extend(ast.iter_child_nodes(n))
    return score


def _safe_unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        text = ast.unparse(node)
    except Exception:
        return "?"
    return text if len(text) <= 300 else text[:297] + "..."

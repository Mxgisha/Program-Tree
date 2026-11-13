"""Pass 2: resolve every name use to a symbol and emit graph edges.

This is deliberately heuristic, not a type checker. It follows Python's lexical
scoping, resolves imports/aliases/re-exports, infers simple local types from
``x = Cls()`` / annotations, and walks ``a.b.c`` chains through modules and
class hierarchies. Everything it cannot prove is simply left unresolved.
"""

from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass

from .binder import EXT, absolute_from_relative, is_external
from .model import Graph, ModuleInfo, Symbol

BUILTIN_NAMES = frozenset(dir(builtins))
MODULE_GLOBALS = {"__file__", "__name__", "__package__", "__doc__", "__spec__",
                  "__loader__", "__cached__", "__builtins__"}
CALLABLE_KINDS = {"function", "method", "nested"}
TRY_NODES = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())
MAX_UNRESOLVED = 2000


@dataclass
class Binding:
    target: str = ""
    # module | class | instance | function | method | variable | local | builtin
    kind: str = "unknown"
    type_id: str = ""


class Scope:
    __slots__ = ("kind", "parent", "bindings", "cls_id", "mod", "globals")

    def __init__(self, kind: str, parent: "Scope | None", mod: ModuleInfo,
                 cls_id: str | None = None) -> None:
        self.kind = kind
        self.parent = parent
        self.mod = mod
        self.cls_id = cls_id
        self.bindings: dict[str, Binding] = {}
        self.globals: set[str] = set()


class Resolver:
    def __init__(self, graph: Graph, modules: dict[str, ModuleInfo]) -> None:
        self.graph = graph
        self.modules = modules
        self.node_syms: dict[int, str] = {}
        self._scopes: dict[str, Scope] = {}
        for mod in modules.values():
            for sid, node in mod.nodes.items():
                self.node_syms[id(node)] = sid
            self.node_syms.update(mod.node_map)

    # ==================================================================
    # entry points
    # ==================================================================
    def run(self) -> None:
        self._phase0_types()
        self._scopes.clear()  # return types were unknown while phase 0 built them
        for mod in self.modules.values():
            scope = self._module_scope(mod)
            for stmt in mod.tree.body if mod.tree else []:
                self._stmt(stmt, scope, mod.name)

    def _phase0_types(self) -> None:
        """Resolve annotations and class bases before walking any bodies."""
        for mod in self.modules.values():
            scope = self._module_scope(mod)
            for sid in mod.symbols:
                sym = self.graph.symbols[sid]
                node = mod.nodes.get(sid)
                if sym.kind == "class":
                    for base_node in getattr(node, "bases", []) or []:
                        b = self._resolve_chain(base_node, scope)
                        if b and b.kind == "class" and b.target in self.graph.symbols:
                            if b.target not in sym.base_ids:
                                sym.base_ids.append(b.target)
                            self.graph.add_edge(sid, b.target, "inherits",
                                                getattr(base_node, "lineno", 0))
                    self._prescan_instance_attrs(sym, mod, scope)
                elif sym.kind in CALLABLE_KINDS:
                    if sym.returns:
                        sym.return_id = self._annotation_type(sym.returns, scope)
                    for pname, ptext in sym.param_types.items():
                        tid = self._annotation_type(ptext, scope)
                        if tid:
                            sym.param_ids[pname] = tid
                elif sym.kind in ("variable", "attribute") and sym.annotation:
                    sym.type_id = self._annotation_type(sym.annotation, scope)

    def _prescan_instance_attrs(self, cls: Symbol, mod: ModuleInfo, scope: Scope) -> None:
        init_id = cls.attrs.get("__init__")
        node = mod.nodes.get(init_id) if init_id else None
        if node is None:
            return
        fscope = Scope("function", scope, mod, cls_id=cls.id)
        positional = list(node.args.posonlyargs) + list(node.args.args)
        for arg in _all_args(node.args):
            type_id = ""
            if arg.annotation is not None:
                type_id = self._annotation_type(_safe_unparse(arg.annotation), scope)
            fscope.bindings[arg.arg] = Binding(kind="param", type_id=type_id)
        for special in ("self", "cls"):
            if positional and positional[0].arg == special:
                fscope.bindings[special] = Binding(target=cls.id, kind="instance",
                                                   type_id=cls.id)
        for stmt in _walk_block(node.body):
            targets, value = _assignment(stmt)
            if not targets:
                continue
            type_id = self._infer_value_type(value, fscope)
            annotation = ""
            if isinstance(stmt, ast.AnnAssign):
                annotation = _safe_unparse(stmt.annotation)
            annotated = self._annotation_type(annotation, fscope) if annotation else ""
            for target in targets:
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id in ("self", "cls")):
                    found = annotated or type_id
                    if found:
                        cls.instance_attrs[target.attr] = found

    # ==================================================================
    # scopes
    # ==================================================================
    def _module_scope(self, mod: ModuleInfo) -> Scope:
        scope = self._scopes.get(mod.name)
        if scope is not None:
            return scope
        scope = Scope("module", None, mod)
        for name, sid in mod.namespace.items():
            scope.bindings[name] = self._binding_for_id(sid)
        self._scopes[mod.name] = scope
        return scope

    def _lookup(self, scope: Scope, name: str) -> Binding | None:
        s: Scope | None = scope
        while s is not None:
            if name in s.globals and s.kind != "module":
                s = s.parent
                continue
            if s.kind != "class" and name in s.bindings:
                return s.bindings[name]
            s = s.parent
        for star in scope.mod.stars:
            b = self._module_attr(star, name, set())
            if b is not None:
                return b
        if name in BUILTIN_NAMES:
            return Binding(kind="builtin")
        return None

    def _binding_for_id(self, sid: str) -> Binding:
        if is_external(sid):
            return Binding(target=sid, kind="external")
        sym = self.graph.symbols.get(sid)
        if sym is None:
            return Binding(target=sid, kind="unknown")
        if sym.kind == "module":
            return Binding(target=sid, kind="module")
        if sym.kind == "class":
            return Binding(target=sid, kind="class", type_id=sid)
        if sym.kind in CALLABLE_KINDS:
            return Binding(target=sid, kind=sym.kind, type_id=sym.return_id)
        return Binding(target=sid, kind="variable", type_id=sym.type_id)

    # ==================================================================
    # expression resolution (pure: no edges, no scope mutation)
    # ==================================================================
    def _resolve_chain(self, node: ast.AST | None, scope: Scope) -> Binding | None:
        if node is None:
            return None
        if isinstance(node, ast.Name):
            return self._lookup(scope, node.id)
        if isinstance(node, ast.Attribute):
            base = self._resolve_chain(node.value, scope)
            if base is None:
                return None
            return self._attr(base, node.attr)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "super" and scope.cls_id:
                owner = self.graph.symbols.get(scope.cls_id)
                if owner is not None and owner.base_ids:
                    base = owner.base_ids[0]
                    return Binding(target=base, kind="class", type_id=base)
            callee = self._resolve_chain(node.func, scope)
            if callee is None:
                return None
            if callee.kind == "class":
                return Binding(target=callee.target, kind="instance",
                               type_id=callee.type_id or callee.target)
            if callee.kind in ("call-result", "instance"):
                return Binding(kind="call-result", type_id=callee.type_id)
            return Binding(kind="call-result", type_id=callee.type_id)
        if isinstance(node, (ast.Subscript, ast.Await, ast.NamedExpr, ast.Starred)):
            return self._resolve_chain(getattr(node, "value", None), scope)
        return None

    def _attr(self, base: Binding, attr: str) -> Binding | None:
        if base.kind == "module":
            return self._module_attr(base.target, attr, set())
        if base.kind in ("class", "instance"):
            return self._class_attr(base.type_id or base.target, attr, set())
        if base.type_id:
            return self._class_attr(base.type_id, attr, set())
        return None

    def _module_attr(self, mod_name: str, attr: str, seen: set[str]) -> Binding | None:
        mod = self.modules.get(mod_name)
        if mod is not None:
            if attr in mod.namespace:
                return self._binding_for_id(mod.namespace[attr])
            sub = f"{mod_name}.{attr}"
            if sub in self.modules:
                return Binding(target=sub, kind="module")
            for star in mod.stars:
                if star in seen:
                    continue
                seen.add(star)
                found = self._module_attr(star, attr, seen)
                if found is not None:
                    return found
        return None

    def _class_attr(self, cls_id: str, attr: str, seen: set[str]) -> Binding | None:
        if not cls_id or cls_id in seen:
            return None
        seen.add(cls_id)
        sym = self.graph.symbols.get(cls_id)
        if sym is None:
            return None
        if attr in sym.attrs:
            return self._binding_for_id(sym.attrs[attr])
        if attr in sym.instance_attrs:
            tid = sym.instance_attrs[attr]
            return Binding(kind="instance", type_id=tid)
        for base in sym.base_ids:
            found = self._class_attr(base, attr, seen)
            if found is not None:
                return found
        return None

    def _infer_value_type(self, value: ast.AST | None, scope: Scope) -> str:
        if value is None:
            return ""
        b = self._resolve_chain(value, scope)
        if b is None:
            return ""
        if b.kind in ("instance", "class", "call-result", "variable", "param", "local"):
            return b.type_id
        if b.kind in CALLABLE_KINDS:
            return b.type_id
        return ""

    def _annotation_type(self, text: str, scope: Scope) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        try:
            node = ast.parse(text, mode="eval").body
        except SyntaxError:
            return ""
        return self._type_of(node, scope)

    def _type_of(self, node: ast.AST, scope: Scope) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return self._annotation_type(node.value, scope)
        if isinstance(node, ast.Name):
            b = self._lookup(scope, node.id)
            return b.type_id if b and b.kind == "class" else ""
        if isinstance(node, ast.Attribute):
            b = self._resolve_chain(node, scope)
            return b.type_id if b and b.kind in ("class", "instance") else ""
        if isinstance(node, ast.Subscript):
            head = _last_name(node.value)
            if head in ("Optional", "Union", "Annotated", "Final", "ClassVar",
                        "Type", "Awaitable", "Coroutine"):
                args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
                for arg in args:
                    found = self._type_of(arg, scope)
                    if found:
                        return found
                return ""
            return self._type_of(node.value, scope)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self._type_of(node.left, scope) or self._type_of(node.right, scope)
        return ""

    # ==================================================================
    # statement / expression visiting
    # ==================================================================
    def _stmt(self, stmt: ast.stmt, scope: Scope, src: str) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._function(stmt, scope)
        elif isinstance(stmt, ast.ClassDef):
            self._class(stmt, scope, src)
        elif isinstance(stmt, ast.Assign):
            self._expr(stmt.value, scope, src)
            self._bind_targets(stmt.targets, stmt.value, scope)
        elif isinstance(stmt, ast.AnnAssign):
            if stmt.annotation is not None:
                self._expr(stmt.annotation, scope, src)
            if stmt.value is not None:
                self._expr(stmt.value, scope, src)
            self._bind_targets([stmt.target], stmt.value, scope,
                               annotation=_safe_unparse(stmt.annotation))
        elif isinstance(stmt, ast.AugAssign):
            self._expr(stmt.value, scope, src)
            if not isinstance(stmt.target, ast.Name):
                self._expr(stmt.target, scope, src)
        elif isinstance(stmt, ast.Expr):
            self._expr(stmt.value, scope, src)
        elif isinstance(stmt, ast.Return):
            self._expr(stmt.value, scope, src)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            if scope.kind != "module":
                self._local_import(stmt, scope)
        elif isinstance(stmt, (ast.Global, ast.Nonlocal)):
            scope.globals.update(stmt.names)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            self._expr(stmt.iter, scope, src)
            self._bind_targets([stmt.target], stmt.iter, scope)
            for s in stmt.body:
                self._stmt(s, scope, src)
            for s in stmt.orelse:
                self._stmt(s, scope, src)
        elif isinstance(stmt, (ast.While, ast.If)):
            self._expr(stmt.test, scope, src)
            for s in stmt.body:
                self._stmt(s, scope, src)
            for s in stmt.orelse:
                self._stmt(s, scope, src)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                self._expr(item.context_expr, scope, src)
                if item.optional_vars is not None:
                    self._bind_targets([item.optional_vars], item.context_expr, scope)
            for s in stmt.body:
                self._stmt(s, scope, src)
        elif isinstance(stmt, TRY_NODES):
            for s in stmt.body:
                self._stmt(s, scope, src)
            for handler in stmt.handlers:
                self._expr(handler.type, scope, src)
                if handler.name:
                    tid = self._infer_value_type(handler.type, scope)
                    scope.bindings[handler.name] = Binding(kind="local", type_id=tid)
                for s in handler.body:
                    self._stmt(s, scope, src)
            for s in stmt.orelse + stmt.finalbody:
                self._stmt(s, scope, src)
        elif isinstance(stmt, ast.Match):
            self._expr(stmt.subject, scope, src)
            for case in stmt.cases:
                for name in _pattern_names(case.pattern):
                    scope.bindings[name] = Binding(kind="local")
                if case.guard is not None:
                    self._expr(case.guard, scope, src)
                for s in case.body:
                    self._stmt(s, scope, src)
        elif isinstance(stmt, ast.Raise):
            self._expr(stmt.exc, scope, src)
            self._expr(stmt.cause, scope, src)
        elif isinstance(stmt, ast.Assert):
            self._expr(stmt.test, scope, src)
            self._expr(stmt.msg, scope, src)
        elif isinstance(stmt, ast.Delete):
            for target in stmt.targets:
                if not isinstance(target, ast.Name):
                    self._expr(target, scope, src)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef,
                  scope: Scope) -> None:
        sid = self.node_syms.get(id(node))
        if sid is None:
            return
        sym = self.graph.symbols[sid]

        for dec in node.decorator_list:
            self._expr(dec, scope, sid)
            target = dec.func if isinstance(dec, ast.Call) else dec
            b = self._resolve_chain(target, scope)
            if b and b.target in self.graph.symbols and b.kind in CALLABLE_KINDS | {"class"}:
                self.graph.add_edge(b.target, sid, "decorates", dec.lineno)

        for default in list(node.args.defaults) + list(node.args.kw_defaults):
            self._expr(default, scope, sid)
        for arg in _all_args(node.args):
            if arg.annotation is not None:
                self._expr(arg.annotation, scope, sid)
        if node.returns is not None:
            self._expr(node.returns, scope, sid)

        lexical_parent = scope.parent if scope.kind == "class" else scope
        cls_id = sym.parent if sym.kind == "method" else None
        if cls_id is not None:
            owner = self.graph.symbols.get(cls_id)
            if owner is None or owner.kind != "class":
                cls_id = None
        fscope = Scope("function", lexical_parent, scope.mod, cls_id=cls_id)

        positional = list(node.args.posonlyargs) + list(node.args.args)
        for arg in _all_args(node.args):
            tid = sym.param_ids.get(arg.arg, "")
            fscope.bindings[arg.arg] = Binding(kind="param", type_id=tid)
        if cls_id:
            for special in ("self", "cls"):
                if positional and positional[0].arg == special:
                    fscope.bindings[special] = Binding(target=cls_id, kind="instance",
                                                       type_id=cls_id)

        self._prescan(node.body, fscope, sym)
        for stmt in node.body:
            self._stmt(stmt, fscope, sid)

    def _class(self, node: ast.ClassDef, scope: Scope, src: str) -> None:
        sid = self.node_syms.get(id(node))
        if sid is None:
            return
        sym = self.graph.symbols[sid]
        for dec in node.decorator_list:
            self._expr(dec, scope, sid)
        for base_node in node.bases:
            self._inner_calls(base_node, scope, src)
        for kw in node.keywords:
            self._expr(kw.value, scope, src)

        cscope = Scope("class", scope, scope.mod, cls_id=sid)
        for name, child_id in sym.attrs.items():
            cscope.bindings[name] = self._binding_for_id(child_id)
        for stmt in node.body:
            self._stmt(stmt, cscope, sid)

    def _expr(self, node: ast.AST | None, scope: Scope, src: str) -> None:
        if node is None:
            return
        if isinstance(node, ast.Call):
            callee = self._resolve_chain(node.func, scope)
            self._emit_call(callee, src, node.lineno)
            self._emit_chain_receiver(node.func, scope, src, node.lineno)
            self._inner_calls(node.func, scope, src)
            for arg in node.args:
                self._expr(arg, scope, src)
            for kw in node.keywords:
                self._expr(kw.value, scope, src)
        elif isinstance(node, ast.Attribute):
            self._emit_ref(self._resolve_chain(node, scope), src, node.lineno)
            self._emit_chain_receiver(node, scope, src, node.lineno)
            self._inner_calls(node.value, scope, src)
        elif isinstance(node, ast.Name):
            binding = self._lookup(scope, node.id)
            self._emit_ref(binding, src, node.lineno)
            if binding is None and node.id not in BUILTIN_NAMES \
                    and node.id not in MODULE_GLOBALS:
                self._note_unresolved(node, scope)
        elif isinstance(node, ast.Lambda):
            lscope = Scope("function", scope, scope.mod, cls_id=scope.cls_id)
            for arg in _all_args(node.args):
                lscope.bindings[arg.arg] = Binding(kind="param")
            self._expr(node.body, lscope, src)
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for elt in node.elts:
                self._expr(elt, scope, src)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                self._expr(key, scope, src)
                self._expr(value, scope, src)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            self._comprehension(node, scope, src)
        elif isinstance(node, ast.IfExp):
            self._expr(node.test, scope, src)
            self._expr(node.body, scope, src)
            self._expr(node.orelse, scope, src)
        elif isinstance(node, (ast.BoolOp, ast.BinOp)):
            for value in node.values if isinstance(node, ast.BoolOp) else [node.left, node.right]:
                self._expr(value, scope, src)
        elif isinstance(node, ast.UnaryOp):
            self._expr(node.operand, scope, src)
        elif isinstance(node, ast.Compare):
            self._expr(node.left, scope, src)
            for comp in node.comparators:
                self._expr(comp, scope, src)
        elif isinstance(node, (ast.Subscript, ast.Starred)):
            self._expr(node.value, scope, src)
            if isinstance(node, ast.Subscript):
                self._expr(node.slice, scope, src)
        elif isinstance(node, ast.JoinedStr):
            for value in node.values:
                self._expr(value, scope, src)
        elif isinstance(node, ast.FormattedValue):
            self._expr(node.value, scope, src)
            self._expr(node.format_spec, scope, src)
        elif isinstance(node, (ast.Await, ast.Yield, ast.YieldFrom)):
            self._expr(node.value, scope, src)
        elif isinstance(node, ast.NamedExpr):
            self._expr(node.value, scope, src)
            tid = self._infer_value_type(node.value, scope)
            scope.bindings[node.target.id] = Binding(kind="local", type_id=tid)
        elif isinstance(node, ast.Slice):
            self._expr(node.lower, scope, src)
            self._expr(node.upper, scope, src)
            self._expr(node.step, scope, src)

    def _comprehension(self, node: ast.AST, scope: Scope, src: str) -> None:
        cscope = Scope("function", scope, scope.mod, cls_id=scope.cls_id)
        for i, gen in enumerate(node.generators):
            self._expr(gen.iter, scope if i == 0 else cscope, src)
            for name in _target_names(gen.target):
                cscope.bindings[name] = Binding(kind="local")
            for cond in gen.ifs:
                self._expr(cond, cscope, src)
        if isinstance(node, ast.DictComp):
            self._expr(node.key, cscope, src)
            self._expr(node.value, cscope, src)
        else:
            self._expr(node.elt, cscope, src)

    def _emit_chain_receiver(self, node: ast.AST, scope: Scope, src: str,
                             line: int) -> None:
        """a.b.c() uses ``a``; make sure a module-level variable stays alive."""
        cur = node
        while isinstance(cur, ast.Attribute):
            cur = cur.value
        if isinstance(cur, ast.Name):
            binding = self._lookup(scope, cur.id)
            if binding is not None and binding.kind == "variable":
                self._emit_ref(binding, src, line)

    def _inner_calls(self, node: ast.AST, scope: Scope, src: str) -> None:
        if isinstance(node, ast.Call):
            self._expr(node, scope, src)
        elif isinstance(node, ast.Attribute):
            self._inner_calls(node.value, scope, src)
        elif isinstance(node, ast.Subscript):
            self._inner_calls(node.value, scope, src)

    def _emit_call(self, binding: Binding | None, src: str, line: int) -> None:
        if binding is None or not binding.target or is_external(binding.target):
            return
        sym = self.graph.symbols.get(binding.target)
        if sym is None:
            return
        if sym.kind == "class":
            self.graph.add_edge(src, sym.id, "instantiates", line)
        elif sym.kind in CALLABLE_KINDS:
            self.graph.add_edge(src, sym.id, "calls", line)
        elif sym.kind == "variable":
            self.graph.add_edge(src, sym.id, "references", line)
            if binding.type_id:
                # calling an instance of a class that defines __call__
                call = self._class_attr(binding.type_id, "__call__", set())
                if call is not None and call.target:
                    self.graph.add_edge(src, call.target, "calls", line)

    def _emit_ref(self, binding: Binding | None, src: str, line: int) -> None:
        if binding is None or not binding.target or is_external(binding.target):
            return
        sym = self.graph.symbols.get(binding.target)
        if sym is None:
            return
        if sym.kind in CALLABLE_KINDS or sym.kind == "class":
            self.graph.add_edge(src, sym.id, "references", line)
        elif sym.kind in ("variable", "attribute"):
            self.graph.add_edge(src, sym.id, "references", line)

    # ==================================================================
    # local bindings
    # ==================================================================
    def _prescan(self, body: list[ast.stmt], scope: Scope, sym: Symbol) -> None:
        for stmt in body:
            self._prescan_stmt(stmt, scope, sym)

    def _prescan_stmt(self, stmt: ast.stmt, scope: Scope, sym: Symbol) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            child = self.graph.symbols[sym.id].attrs.get(stmt.name)
            if child:
                scope.bindings[stmt.name] = self._binding_for_id(child)
            return
        if isinstance(stmt, ast.Assign):
            self._bind_targets(stmt.targets, stmt.value, scope)
        elif isinstance(stmt, ast.AnnAssign):
            self._bind_targets([stmt.target], stmt.value, scope,
                               annotation=_safe_unparse(stmt.annotation))
        elif isinstance(stmt, ast.AugAssign):
            if isinstance(stmt.target, ast.Name):
                scope.bindings.setdefault(stmt.target.id, Binding(kind="local"))
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            for name in _target_names(stmt.target):
                scope.bindings.setdefault(name, Binding(kind="local"))
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if item.optional_vars is not None:
                    self._bind_targets([item.optional_vars], item.context_expr, scope)
        elif isinstance(stmt, TRY_NODES):
            for handler in stmt.handlers:
                if handler.name:
                    scope.bindings[handler.name] = Binding(kind="local")
        elif isinstance(stmt, ast.Match):
            for case in stmt.cases:
                for name in _pattern_names(case.pattern):
                    scope.bindings[name] = Binding(kind="local")
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            if scope.kind != "module":
                self._local_import(stmt, scope)
        elif isinstance(stmt, (ast.Global, ast.Nonlocal)):
            scope.globals.update(stmt.names)
        for block in _child_blocks(stmt):
            for inner in block:
                self._prescan_stmt(inner, scope, sym)

    def _bind_targets(self, targets: list[ast.AST], value: ast.AST | None,
                      scope: Scope, annotation: str = "") -> None:
        type_id = self._annotation_type(annotation, scope) if annotation else ""
        if not type_id:
            type_id = self._infer_value_type(value, scope)
        for target in targets:
            if isinstance(target, ast.Name):
                if scope.kind == "function":
                    scope.bindings[target.id] = Binding(kind="local", type_id=type_id)
                elif target.id not in scope.bindings:
                    # module-level loop / with targets are plain globals
                    scope.bindings[target.id] = Binding(kind="local", type_id=type_id)
                elif type_id:
                    sid = scope.mod.namespace.get(target.id)
                    sym = self.graph.symbols.get(sid) if sid else None
                    if sym is not None and not sym.type_id:
                        sym.type_id = type_id
                        cached = scope.bindings.get(target.id)
                        if cached is not None:
                            cached.type_id = type_id
            elif isinstance(target, ast.Attribute):
                if (isinstance(target.value, ast.Name)
                        and target.value.id in ("self", "cls")
                        and scope.cls_id and type_id):
                    cls = self.graph.symbols.get(scope.cls_id)
                    if cls is not None:
                        cls.instance_attrs[target.attr] = type_id
                else:
                    self._expr(target.value, scope, "")
            elif isinstance(target, (ast.Tuple, ast.List)):
                self._bind_targets(list(target.elts), None, scope)
            elif isinstance(target, ast.Starred):
                self._bind_targets([target.value], value, scope)

    def _local_import(self, node: ast.stmt, scope: Scope) -> None:
        mod = scope.mod
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = alias.name if alias.asname else alias.name.split(".")[0]
                bound = alias.asname or alias.name.split(".")[0]
                if target in self.modules:
                    scope.bindings[bound] = Binding(target=target, kind="module")
                    self.graph.add_edge(mod.name, target, "imports", node.lineno,
                                        lazy=True)
                else:
                    scope.bindings[bound] = Binding(target=EXT + target, kind="external")
        elif isinstance(node, ast.ImportFrom):
            base = absolute_from_relative(mod, node.level, node.module)
            for alias in node.names:
                if alias.name == "*":
                    continue
                candidate = f"{base}.{alias.name}" if base else alias.name
                if candidate in self.modules:
                    scope.bindings[alias.asname or alias.name] = Binding(
                        target=candidate, kind="module")
                    self.graph.add_edge(mod.name, candidate, "imports", node.lineno,
                                        lazy=True)
                elif base in self.modules and alias.name in self.modules[base].namespace:
                    scope.bindings[alias.asname or alias.name] = self._binding_for_id(
                        self.modules[base].namespace[alias.name])
                else:
                    scope.bindings[alias.asname or alias.name] = Binding(
                        target=EXT + candidate, kind="external")

    def _note_unresolved(self, node: ast.Name, scope: Scope) -> None:
        if len(self.graph.unresolved) >= MAX_UNRESOLVED:
            return
        self.graph.unresolved.append(
            {"name": node.id, "module": scope.mod.name, "line": node.lineno})


# ======================================================================
# helpers
# ======================================================================
def _all_args(args: ast.arguments) -> list[ast.arg]:
    out = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    if args.vararg:
        out.append(args.vararg)
    if args.kwarg:
        out.append(args.kwarg)
    return out


def _last_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _safe_unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _assignment(stmt: ast.stmt) -> tuple[list[ast.AST], ast.AST | None]:
    if isinstance(stmt, ast.Assign):
        return list(stmt.targets), stmt.value
    if isinstance(stmt, ast.AnnAssign):
        return [stmt.target], stmt.value
    return [], None


def _target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        out: list[str] = []
        for elt in node.elts:
            out.extend(_target_names(elt))
        return out
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    return []


def _pattern_names(node: ast.AST) -> list[str]:
    out: list[str] = []
    for child in ast.walk(node):
        name = getattr(child, "name", None)
        if isinstance(child, (ast.MatchAs, ast.MatchStar)) and isinstance(name, str):
            out.append(name)
        elif isinstance(child, ast.MatchMapping) and child.rest:
            out.append(child.rest)
    return out


def _child_blocks(stmt: ast.stmt) -> list[list[ast.stmt]]:
    out = []
    for attr in ("body", "orelse", "finalbody"):
        block = getattr(stmt, attr, None)
        if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
            out.append(block)
    for handler in getattr(stmt, "handlers", []) or []:
        out.append(handler.body)
    for case in getattr(stmt, "cases", []) or []:
        out.append(case.body)
    return out


def _walk_block(stmts: list[ast.stmt]):
    """Yield statements, descending into control flow but not nested defs."""
    for stmt in stmts:
        yield stmt
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for block in _child_blocks(stmt):
            yield from _walk_block(block)

# Architecture notes

## Pipeline

```
discover        files + module names (gitignore-aware; src/ layout; package roots)
    │
parse           ast.parse, one file at a time, errors collected not raised
    │
index  (pass 1) every def -> Symbol: id, qualname, signature, complexity, bases, attrs
    │
bind            module namespaces: imports, aliases, re-exports, star imports
    │
resolve (pass 2) walk bodies with lexical scopes -> typed Edges
    │
analyze         fan-in/out, SCCs (call graph + module graph), hubs
    │
dead code       root set -> mark & sweep -> candidates + evidence
    │
history         git log: churn, co-change coupling, hotspots (optional)
    │
export          map.json · explorer.html · report.md · graph.dot
```

Two passes exist for one reason: `def f() -> User` and `x = Service()` may be
resolved *before* the module that defines `User` has been visited. Pass 1 can
therefore only record shape; pass 2 needs every module's namespace to already
exist. Anything that would make resolution order-dependent (return types,
parameter types, class bases, `self.x = ...` instance attributes) is resolved in
a pre-pass over all symbols before any body is walked.

Module liveness is a separate notion from symbol liveness: a live module means
its imports execute and its top-level code runs. This is what makes `app.cli`
detectable as a dead module while a lazily imported helper module stays live.

## Symbols and ids

`module::qualname`, e.g. `click.core::Command.invoke`, `click.utils::echo`.
Containment is a `parent` pointer, not an edge: module → class → method, and
module → function for top-level functions. Redefinitions merge (last one wins,
matching runtime); only genuinely distinct same-name symbols get a `~2` suffix.

The module name of a file is derived from its path, not from its imports:

- analyzing `click/` (a package dir) yields `click.core`, never `core`;
- `src/pkg/mod.py` yields `pkg.mod`;
- a bare project directory yields `pkg.mod` as written on disk.

This is what stops `click/types.py` from being confused with stdlib `types` — a
real bug that shows up as dozens of unresolved names.

## Resolution rules

For a name use, the lookup order is: locals (pre-scanned per function), enclosing
function scopes (class scopes are skipped, matching Python), module namespace,
`from x import *` namespaces, builtins. `global`/`nonlocal` skip the declaring
scope.

For `a.b.c`:

1. resolve `a`, then fold each attribute;
2. module base → module namespace, then submodule, then star imports;
3. class/instance base → class attrs and methods, then bases depth-first
   (a cheap MRO; `super()` starts at the first base);
4. otherwise use the inferred type of a variable/parameter/return value.

Types are inferred only from things the source states: annotations (including
`Optional[X]`, `X | None`, string annotations), `x = Cls()`, `self.x = value`,
constructor parameters, and return annotations. That is enough for the common
`service.create(...)` / `self.repo.get(...)` / `user.describe()` chains without
building a type checker.

Calls are emitted as `calls` (function/method), `instantiates` (class), or
`calls` to `__call__` when calling an instance that defines one. A function used
without being called (passed as a callback, stored, aliased) yields
`references`. That distinction is why "who calls this?" lists are trustworthy
even when a function is only used as a value.

Imports guarded by `if TYPE_CHECKING:` are recorded with `type_only: true`.
They are real type dependencies, but they cannot create a runtime import cycle,
so the module cycle analysis ignores them. (Without this, `click` appears to
have a 10-module cycle; with it, only the genuine conditional `_compat ↔
_winconsole` cycle remains.)

Everything is deduplicated per `(src, dst, kind)` with the first line and a hit
count; a runtime import of a pair upgrades a type-only edge.

## What is deliberately not done

- **No type checker.** Running mypy/pyright and consuming its index would be more
  precise; it also adds a dependency, a build step, and a lot of time for a
  hobby-scale tool. The current binder is ~1k lines and resolves ~11k LOC in
  0.4 s.
- **No points-to analysis.** Dynamic dispatch is resolved statically to the
  declared class; overrides are shown as a hierarchy, not merged into the call.
  Unused analysis is therefore closed-world — a deliberate trade-off, handled
  by confidence levels rather than silence.

## Unused code

The wrong version is "in-degree == 0" — it flags exported API, framework
callbacks, and anything reached through `getattr`. The implementation instead
builds a root set and sweeps:

**Roots** (in order of confidence): explicit `--root` specs, `[project.scripts]`
entry points, `__main__` modules and main guards, stand-alone scripts (top-level
calls, never imported), package `__init__` exports and `__all__`, tests,
framework-decorated handlers (`command`, `route`, `fixture`, `task`, `on_*`, …).
In a library (no entry points at all) public top-level symbols are roots too;
`--root-public` / `--no-root-public` override that.

**Sweep** over `calls`, `instantiates`, `references` and `inherits`:

- a live function makes its module live (which imports other modules and runs
  their top-level code);
- a live class keeps all of its methods, its base classes, and module-level
  `__getattr__` hooks;
- a live method keeps its class live;
- lazy imports (`import` inside a function) are runtime dependencies and are
  followed, but they do not create import cycles.

Everything left is a candidate. Each gets a reason (`no callers or references`,
`only used by dead code (a, b)`) and a confidence derived from name visibility,
dynamic markers (`getattr`, `importlib`, `globals`, star imports) and whether the
module is a platform-specific branch. Dead modules are reported once; symbols
inside them are not listed again. Symbols reached only from test roots go into a
separate `test_only` list — they are live, just not production-live.

`references` edges are what keep this safe: a callback never called still has a
reference, so it will not be reported as dead.

## Change coupling

One `git log --no-merges --name-only --relative` pass over the last N commits.
For each commit, the set of changed modules (skipping commits touching >40
modules) increments per-module support and pairwise co-change counts. Reported
confidence is `count / min(support[a], support[b])` — "when one changes, how
often does the other" — plus lift against chance. Hotspots are
`module churn × function complexity`. Nothing is inferred when git is absent.

## Scale

Hobby scale is the target: everything is in memory, JSON is the store, layout is
computed on demand. On 662 stdlib files (324k lines) indexing takes ~10 s and
produces 15 MB of JSON; the explorer stays responsive because it renders ego
graphs, not the whole graph. Past ~50k symbols the next steps would be SQLite
for the edge store, per-file caching keyed by content hash, and a layout worker.

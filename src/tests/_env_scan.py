"""Enumerate the `LAPS_*` environment-variable switches actually read
anywhere under `laps/`, by walking the AST rather than grepping.

This exists so `test_env_switches_documented.py` can catch drift mechanically:
a switch added to the code without a matching row in `docs/env-switches.md`
(or vice versa) fails the test instead of rotting quietly. Not a linter — a
small, dependency-free AST walk kept in one file on purpose (it is test
infrastructure, not shipped code).

What it catches
----------------
1. Direct reads: ``os.environ.get("LAPS_X"[, default])`` and
   ``os.environ["LAPS_X"]``, including when the key is a module-level
   string constant instead of a literal (e.g. ``postedit.py``'s
   ``ENV_ENABLE = "LAPS_POSTEDIT"`` then ``os.environ.get(ENV_ENABLE)``).
2. One level of indirection through a small "read env var by name" helper —
   a function whose body reads ``os.environ.get(<param>, ...)`` /
   ``os.environ[<param>]`` / ``<mapping_param>.get(<param>, ...)`` where
   ``<param>`` is one of its own parameters — resolved at every call site that
   passes a literal (or module-const) ``LAPS_*`` name. This is what
   catches ``agent.py``'s ``_limit_from_env("LAPS_MAX_SECONDS", 600)``,
   ``postedit.py``'s ``_int_env(ENV_MAX_RUNS, ...)``,
   ``checkpoint.py``'s ``_env_int(env, "LAPS_CHECKPOINT_MAX_FILES", ...)``,
   and the nested ``_f("LAPS_TOP_P", 0.8)`` closure in ``backend.py``.
3. ``<mapping>.get(CONST, ...)`` anywhere, when ``CONST`` resolves (directly or
   via a module-level string constant) to a ``LAPS_*``-prefixed string —
   catches wrapper functions that take a ``Mapping`` instead of touching
   ``os.environ`` directly (``term/wave.py``'s ``env.get(ENV_VAR, "")``,
   ``term/config.py``'s ``e.get("LAPS_WAVE", "")``,
   ``checkpoint.py``'s ``e.get("LAPS_CHECKPOINT", "1")``).
4. Dict-driven fan-out: ``for var, key in SOME_DICT.items(): mapping.get(var)``,
   where ``SOME_DICT`` is a module-level dict literal (plain ``Assign`` or
   annotated ``AnnAssign`` — the latter tripped up an earlier version of this
   scanner, since ``NAME: dict[str, str] = {...}`` is a different AST node
   shape than ``NAME = {...}``) whose keys are all string literals, and the
   loop body reads the loop's key-variable back out of *some* ``.get()`` call.
   Every dict key that starts with ``LAPS_`` is then a discovered switch.
   This is what catches ``term/config.py``'s ``ENV_KEYS`` table — the terminal
   client's ``resolve()`` reads ``LAPS_MAX_ITERS``, ``LAPS_TELEMETRY``
   and ``LAPS_PROSE_WIDTH`` this way, and none of the three showed up
   until this fourth technique was added (found by cross-checking README.md's
   hand-written env-var table against the scanner's output — the README knew
   about ``LAPS_MAX_ITERS``, the scanner didn't, which is exactly the
   kind of doc/code drift this file exists to catch mechanically instead of by
   luck).

Known blind spot
----------------
A variable name reached only through a *data* indirection where the dict
itself is not what gets ``.items()``-walked — pulled out by unpacking a
narrower structure (a tuple nested one level down) — is not found by any of
the above. As of 2026-09-04 there is exactly one: ``LAPS_TESTVM2_PW`` in
``laps/tools/remote_read.py``
(``_PASSWORD_SOURCE = {"testvm2": ("env", "LAPS_TESTVM2_PW")}``, read via
``os.environ.get(ref)`` where ``ref`` comes from unpacking the tuple value,
not the dict key — so technique 4 above does not apply: it only harvests
*keys*, and here the switch name is a *value* one level in). It is listed in
``KNOWN_INDIRECT_EXTRA`` below by hand, with this comment as the reason a
future scan won't rediscover it on its own. If you add another switch in this
shape, add it here too — or better, extend the scanner.

Deliberately not attempted: resolving indirection through more than one
call layer, or through instance attributes / class state. The two-line
regex-only alternative (grep for ``LAPS_[A-Z0-9_]+``) was rejected
because it also matches the string inside comments and doc examples (e.g.
policy.py's own comment naming an *observed* ``VAR=value`` shell prefix,
``LAPS_TELEMETRY_URL=``, which nothing in the codebase reads) — an AST
walk restricted to actual `os.environ` / mapping-argument reads does not.
"""
from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "laps"

#: Switches reachable only through a data-literal indirection (see docstring).
#: (name, file, line, default) — file/line point at the `os.environ.get(ref)`
#: call itself, not the dict literal, so it lines up with the other rows.
KNOWN_INDIRECT_EXTRA: list[dict] = []


def _literal_or_none(node):
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


class _FileScan:
    def __init__(self, path: Path, tree: ast.AST):
        self.path = path
        self.tree = tree
        self.str_consts: dict[str, str] = {}
        self.dict_consts: dict[str, dict[str, str]] = {}
        for node in ast.walk(tree):
            # Plain `NAME = "STR"` and annotated `NAME: str = "STR"` both count —
            # `ENV_KEYS: dict[str, str] = {...}` (below) is an AnnAssign, not a
            # plain Assign, and was silently invisible to this scanner until this
            # was split out (found via LAPS_MAX_ITERS/TELEMETRY/PROSE_WIDTH,
            # which term/config.py reads only through that dict — see docstring).
            targets, value = None, None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.target is not None and node.value is not None:
                targets, value = [node.target], node.value
            if targets is None:
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                for t in targets:
                    if isinstance(t, ast.Name):
                        self.str_consts[t.id] = value.value
            # `NAME: dict[str, str] = {"LAPS_X": "x", ...}` (all-string-literal
            # dict literals only — good enough for a settings/env lookup table,
            # and safely skips anything with dynamic keys or values).
            if isinstance(value, ast.Dict):
                d = value
                if d.keys and all(
                    isinstance(k, ast.Constant) and isinstance(k.value, str)
                    and isinstance(v, ast.Constant) and isinstance(v.value, str)
                    for k, v in zip(d.keys, d.values)
                ):
                    mapping = {k.value: v.value for k, v in zip(d.keys, d.values)}
                    for t in targets:
                        if isinstance(t, ast.Name):
                            self.dict_consts[t.id] = mapping

    def resolve(self, node):
        if node is None:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id in self.str_consts:
            return self.str_consts[node.id]
        return None


def _is_os_environ(node) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _find_arg_spec(func_args: ast.arguments, target: str):
    names = [a.arg for a in func_args.args]
    if target in names:
        return ("pos", names.index(target))
    ko = [a.arg for a in func_args.kwonlyargs]
    if target in ko:
        return ("kw", target)
    return None


def scan(root: Path = PACKAGE_ROOT) -> list[dict]:
    """Return one dict per (var, file, line) occurrence found in the tree
    rooted at `root` (default: the `laps/` package)."""
    results: list[dict] = []
    indirections: list[dict] = []
    file_scans: dict[str, _FileScan] = {}

    paths = sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        fs = _FileScan(path, tree)
        rel = str(path.relative_to(root.parent))
        file_scans[rel] = fs

        # --- direct hits -----------------------------------------------
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr == "get" and _is_os_environ(f.value):
                    if node.args:
                        key = fs.resolve(node.args[0])
                        default = _literal_or_none(node.args[1]) if len(node.args) > 1 else None
                        for kw in node.keywords:
                            if kw.arg == "default":
                                default = _literal_or_none(kw.value)
                        if key and key.startswith("LAPS_"):
                            results.append(dict(name=key, file=rel, line=node.lineno,
                                                 default=default, via="direct"))
            if isinstance(node, ast.Subscript):
                v = node.value
                if _is_os_environ(v) and isinstance(node.ctx, ast.Load):
                    slice_node = node.slice
                    if isinstance(slice_node, ast.Index):  # py<3.9 AST shape
                        slice_node = slice_node.value
                    key = fs.resolve(slice_node)
                    if key and key.startswith("LAPS_"):
                        results.append(dict(name=key, file=rel, line=node.lineno,
                                             default=None, via="direct[]"))

        # --- dict-driven fan-out: `for var, key in SOME_DICT.items(): x.get(var)` ---
        # (term/config.py's ENV_KEYS: every dict key is an env var name read in a
        # loop, not a literal at any single call site — nothing above finds this.)
        for node in ast.walk(tree):
            if not isinstance(node, ast.For):
                continue
            it = node.iter
            if not (isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute)
                    and it.func.attr == "items" and isinstance(it.func.value, ast.Name)):
                continue
            mapping = fs.dict_consts.get(it.func.value.id)
            if not mapping:
                continue
            if not (isinstance(node.target, ast.Tuple) and len(node.target.elts) == 2
                    and isinstance(node.target.elts[0], ast.Name)):
                continue
            key_var = node.target.elts[0].id
            # Only count it if the loop body actually reads that var back out of
            # *some* mapping's .get() — otherwise the dict might be unrelated to
            # env vars entirely (e.g. iterating to build a different structure).
            used_as_env_lookup = any(
                isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "get" and inner.args
                and isinstance(inner.args[0], ast.Name) and inner.args[0].id == key_var
                for inner in ast.walk(node)
            )
            if not used_as_env_lookup:
                continue
            for key in mapping:
                if key.startswith("LAPS_"):
                    results.append(dict(name=key, file=rel, line=node.lineno,
                                         default=None, via="dict-fanout"))

        # --- indirection: functions that read one of their own params ---
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            arg_names = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "get"):
                    recv = inner.func.value
                    name_param = None
                    if (_is_os_environ(recv) and inner.args
                            and isinstance(inner.args[0], ast.Name)
                            and inner.args[0].id in arg_names):
                        name_param = inner.args[0].id
                    elif (isinstance(recv, ast.Name) and recv.id in arg_names and inner.args
                          and isinstance(inner.args[0], ast.Name)
                          and inner.args[0].id in arg_names):
                        name_param = inner.args[0].id
                    if name_param:
                        name_spec = _find_arg_spec(node.args, name_param)
                        default_spec = (_find_arg_spec(node.args, "default")
                                        if "default" in arg_names else None)
                        indirections.append(dict(func=node.name, file=rel,
                                                  name_param_spec=name_spec,
                                                  default_param_spec=default_spec))
                    # <mapping>.get(CONST, ...) where CONST is a resolvable
                    # module-level LAPS_ string constant (not a param) —
                    # this alone is enough, no call-site tracing needed.
                    if inner.args:
                        key = fs.resolve(inner.args[0])
                        if key and key.startswith("LAPS_"):
                            default = (_literal_or_none(inner.args[1])
                                       if len(inner.args) > 1 else None)
                            results.append(dict(name=key, file=rel, line=inner.lineno,
                                                 default=default, via="mapping.get"))
                if isinstance(inner, ast.Subscript) and isinstance(inner.ctx, ast.Load):
                    recv = inner.value
                    key_node = inner.slice
                    if isinstance(key_node, ast.Index):
                        key_node = key_node.value
                    if (_is_os_environ(recv) and isinstance(key_node, ast.Name)
                            and key_node.id in arg_names):
                        name_spec = _find_arg_spec(node.args, key_node.id)
                        default_spec = (_find_arg_spec(node.args, "default")
                                        if "default" in arg_names else None)
                        indirections.append(dict(func=node.name, file=rel,
                                                  name_param_spec=name_spec,
                                                  default_param_spec=default_spec))

    # --- resolve indirection call sites, across every file ---------------
    by_func: dict[str, list[dict]] = {}
    for ind in indirections:
        by_func.setdefault(ind["func"], []).append(ind)

    for rel, fs in file_scans.items():
        for node in ast.walk(fs.tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in by_func):
                continue
            for ind in by_func[node.func.id]:
                spec = ind["name_param_spec"]
                if spec is None or spec[0] != "pos" or spec[1] >= len(node.args):
                    continue
                key = fs.resolve(node.args[spec[1]])
                if not (key and key.startswith("LAPS_")):
                    continue
                default = None
                dspec = ind["default_param_spec"]
                if dspec and dspec[0] == "pos" and dspec[1] < len(node.args):
                    default = _literal_or_none(node.args[dspec[1]])
                results.append(dict(name=key, file=rel, line=node.lineno,
                                     default=default,
                                     via=f"indirect:{ind['func']}@{ind['file']}"))

    results.extend(KNOWN_INDIRECT_EXTRA)
    return results


def names(root: Path = PACKAGE_ROOT) -> set[str]:
    return {r["name"] for r in scan(root)}


if __name__ == "__main__":
    for n in sorted(names()):
        print(n)

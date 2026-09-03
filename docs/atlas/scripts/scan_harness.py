#!/usr/bin/env python3
"""Scan the harness package and emit the atlas data file.

Everything the canvas shows is read out of the repository here: line counts
and import edges come from the Python AST, tool names and risk classes from
the literal `ToolSpec(...)` calls in `harness/execution/tools.py`, CLI verbs from the
`add_parser` calls in `harness/cli.py`, event types from the string literals
handed to the event log, and per-module history from `git log`.

One thing in this file is editorial rather than measured: the ordered stages
of the authority boundary (BOUNDARY). It is marked as such in the output so
the canvas can say which of its claims are counted and which are argued. A
module's layer used to be a judgement too; since the package was reorganised
it is simply the module's subpackage, and is read off the filesystem.

    python3 docs/atlas/scripts/scan_harness.py            # writes public/atlas.json
    python3 docs/atlas/scripts/scan_harness.py --out X    # writes elsewhere
    python3 docs/atlas/scripts/scan_harness.py --check    # fail if stale
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Running the file directly puts its directory on the path; being imported by
# path (as the tests do) does not. Adding it explicitly makes both work.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import taxonomy  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
PKG = REPO / "harness"
TESTS = REPO / "tests"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "public" / "atlas.json"

# --- measured: the layer a module sits in ---------------------------------
# No longer a judgement. Since the package was reorganised, a module's layer
# IS its subpackage, so this is read off the filesystem rather than declared.
# The order is the dependency order the import-linter contract enforces,
# lowest first; "" is the entry-point tier at the package root.
# `config` is the composition root: it sits with the entry points because it
# imports every module it configures. `repl` is above it, being one of the
# things you run.
LAYER_ORDER = ["core", "models", "record", "execution", "run", "autonomy", "mcp_demo", "", "repl"]
LAYER_TITLES: dict[str, tuple[str, str]] = {
    "core": ("Core", "Shared vocabulary that depends on nothing."),
    "models": ("Model access", "Credentials, adapters, streaming, route ordering."),
    "record": ("Durable record", "Ledger, checkpoints, evidence, spans, replay."),
    "execution": ("Execution", "Workspace, policy gate, sandbox, the tools themselves."),
    "run": ("One run", "Context, compaction, verification, delegation, faults."),
    "autonomy": ("Autonomy", "Goals, backlogs, the loop, evals, review, delivery."),
    "repl": ("Conversation", "The REPL loop: one thread, the operator's directory."),
    "mcp_demo": ("Demo", "A sample MCP server used by the workshop."),
    "": ("Entry points", "The composition root and the two things you can run."),
}

# --- editorial: the mandatory chain every side effect passes through --------
BOUNDARY: list[dict[str, str]] = [
    {"stage": "Task contract", "module": "config", "note": "Strict YAML, frozen into the run directory."},
    {"stage": "Context", "module": "run.context", "note": "A bounded prompt. No filesystem, no shell, no network."},
    {"stage": "Route plan", "module": "models.llm_light", "note": "Priorities resolved once, then frozen for the run."},
    {"stage": "Model call", "module": "models.providers", "note": "The one place a model is spoken to."},
    {"stage": "Proposal", "module": "core.contracts", "note": "A tool call, a finish claim, or a question. Nothing else."},
    {"stage": "Schema check", "module": "execution.tools", "note": "Arguments validated against the tool's JSON schema."},
    {"stage": "Policy gate", "module": "execution.policy", "note": "Risk class, allowlist, approval. The gate that can say no."},
    {"stage": "Sandbox", "module": "execution.sandbox", "note": "Where the command is actually allowed to run."},
    {"stage": "Workspace", "module": "execution.workspace", "note": "A per-run copy with canonical-path containment."},
    {"stage": "Ledger", "module": "record.events", "note": "Append-only, sequence-checked, redacted."},
    {"stage": "Checkpoint", "module": "record.state", "note": "Atomic write after every boundary, so resume is real."},
    {"stage": "Verifier", "module": "run.verifier", "note": "The only thing that may declare a run COMPLETED."},
]

# Modules the model side of the boundary can influence, for the boundary view.
MODEL_SIDE = {"models.providers", "models.streaming", "models.model_router",
              "models.llm_light", "models.auth"}


def _source_path(name: str) -> str:
    """The repository path a dotted module name came from."""
    return "harness/" + name.replace(".", "/") + ".py"


def sloc(tree_src: str) -> int:
    """Lines that are neither blank nor a whole-line comment."""
    return sum(
        1
        for line in tree_src.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def public_api(tree: ast.Module) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            methods = [
                m.name
                for m in node.body
                if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)
                and not m.name.startswith("_")
            ]
            out.append(
                {
                    "kind": "class",
                    "name": node.name,
                    "line": node.lineno,
                    "doc": first_line(ast.get_docstring(node)),
                    "methods": methods,
                }
            )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith("_"):
            out.append(
                {
                    "kind": "function",
                    "name": node.name,
                    "line": node.lineno,
                    "doc": first_line(ast.get_docstring(node)),
                    "methods": [],
                }
            )
    return out


def first_line(doc: str | None) -> str:
    if not doc:
        return ""
    return doc.strip().splitlines()[0].strip()


def full_doc(doc: str | None) -> str:
    return (doc or "").strip()


def internal_imports(tree: ast.Module, package: str) -> set[str]:
    """Modules of this package that *tree* imports, by dotted name.

    Absolute imports now carry the subpackage, so `harness.core.contracts`
    has to resolve to `core.contracts` rather than to `core`. Relative
    imports are returned as written and bound to a sibling by the caller,
    which is the only place that knows which package the importer is in.
    """
    found: set[str] = set()
    prefix = f"{package}."
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                found.add(node.module)
            elif node.module and node.module.startswith(prefix):
                found.add(node.module[len(prefix):])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(prefix):
                    found.add(alias.name[len(prefix):])
    return found


def external_imports(tree: ast.Module, package: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and not node.module.startswith(package):
                found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith(package):
                    found.add(alias.name.split(".")[0])
    return found


def string_of(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # Implicit concatenation of adjacent string literals arrives as a single
    # Constant, but an explicit `a + b` does not.
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = string_of(node.left), string_of(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def attr_name(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def scan_tools(tree: ast.Module) -> list[dict[str, Any]]:
    """Every literal ToolSpec(...) construction in tools.py."""
    tools: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and attr_name(node.func).endswith("ToolSpec")):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        name = string_of(node.args[0]) if node.args else string_of(kwargs.get("name"))
        desc = string_of(node.args[1]) if len(node.args) > 1 else string_of(kwargs.get("description"))
        risk_node = node.args[3] if len(node.args) > 3 else kwargs.get("risk")
        risk = attr_name(risk_node).split(".")[-1].lower() if risk_node is not None else "unknown"
        # A schema built from a literal dict tells us the argument names.
        schema_node = node.args[2] if len(node.args) > 2 else kwargs.get("input_schema")
        args, required = tool_schema(schema_node)
        if not name:
            continue
        tools.append(
            {
                "name": name,
                "description": desc or "",
                "risk": risk,
                "line": node.lineno,
                "arguments": args,
                "required": required,
            }
        )
    return sorted(tools, key=lambda t: t["line"])


def tool_schema(node: ast.expr | None) -> tuple[list[str], list[str]]:
    """Argument names out of an `_object_schema({...}, (...))` call."""
    if not isinstance(node, ast.Call) or not node.args:
        return [], []
    props = node.args[0]
    names = (
        [string_of(k) or "?" for k in props.keys] if isinstance(props, ast.Dict) else []
    )
    required: list[str] = []
    if len(node.args) > 1 and isinstance(node.args[1], ast.Tuple | ast.List):
        required = [string_of(e) or "?" for e in node.args[1].elts]
    return names, required


def scan_cli(tree: ast.Module) -> list[dict[str, Any]]:
    """Every add_parser(...) call, with its subcommand parent when nested."""
    commands: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and attr_name(node.func).endswith("add_parser")):
            continue
        name = string_of(node.args[0]) if node.args else None
        if not name:
            continue
        helps = next((string_of(kw.value) for kw in node.keywords if kw.arg == "help"), "")
        owner = attr_name(node.func).rsplit(".", 1)[0]
        commands.append(
            {
                "name": name,
                "help": helps or "",
                "group": "" if owner in {"commands", "sub"} else owner,
                "line": node.lineno,
            }
        )
    return sorted(commands, key=lambda c: c["line"])


def scan_enum(tree: ast.Module, class_name: str) -> list[str]:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            values = []
            for item in node.body:
                if isinstance(item, ast.Assign) and isinstance(item.targets[0], ast.Name):
                    literal = string_of(item.value)
                    if literal is not None:
                        values.append(literal)
            return values
    return []


def scan_dataclass_fields(tree: ast.Module, class_name: str) -> list[dict[str, str]]:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            fields = []
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    default = ""
                    if item.value is not None:
                        default = ast.unparse(item.value)
                    fields.append(
                        {
                            "name": item.target.id,
                            "type": ast.unparse(item.annotation),
                            "default": default,
                        }
                    )
            return fields
    return []


def is_event_name(value: str | None) -> bool:
    """Event types are the SCREAMING_SNAKE literals and nothing else."""
    return bool(value) and value.isupper() and "_" in value and value.replace("_", "").isalpha()


def scan_events(trees: dict[str, ast.Module]) -> list[dict[str, Any]]:
    """Event type literals reaching the ledger, and who writes each one."""
    writers: dict[str, set[str]] = defaultdict(set)
    for module, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            leaf = attr_name(node.func).rsplit(".", 1)[-1]
            if leaf not in {"append", "_emit", "emit"} or not node.args:
                continue
            first = node.args[0]
            literal = string_of(first)
            if is_event_name(literal):
                writers[literal].add(module)
                continue
            # The terminal events are emitted through a status-to-event map
            # subscripted in place: {RunStatus.X: "RUN_X", ...}[status].
            if isinstance(first, ast.Name | ast.Subscript):
                for inner in ast.walk(first):
                    inner_literal = string_of(inner) if isinstance(inner, ast.Constant) else None
                    if is_event_name(inner_literal):
                        writers[inner_literal].add(module)

    # A run always terminates on exactly one of these; replay.py is the
    # declaration of which, so read the set from there rather than guessing.
    terminal: set[str] = set()
    if "record.replay" in trees:
        for node in ast.walk(trees["record.replay"]):
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id == "terminal_types" and isinstance(node.value, ast.Set):
                    terminal = {
                        s for e in node.value.elts if is_event_name(s := string_of(e))
                    }
    for name in terminal:
        writers.setdefault(name, {"runtime"})

    return [
        {"type": name, "writers": sorted(mods), "terminal": name in terminal}
        for name, mods in sorted(writers.items())
    ]


def git_history(paths: list[str]) -> dict[str, dict[str, Any]]:
    """Commit count and last-touched date per file, from the real log."""
    stats: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            out = subprocess.run(
                ["git", "log", "--follow", "--format=%ad", "--date=short", "--", path],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        dates = [line for line in out.stdout.splitlines() if line.strip()]
        stats[path] = {
            "commits": len(dates),
            "last": dates[0] if dates else "",
            "first": dates[-1] if dates else "",
        }
    return stats


def scan_tests() -> dict[str, dict[str, Any]]:
    """Which harness modules each test file imports, and how long it is."""
    result: dict[str, dict[str, Any]] = {}
    if not TESTS.is_dir():
        return result
    for path in sorted(TESTS.glob("test_*.py")):
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        covered = internal_imports(tree, "harness")
        result[path.name] = {
            "lines": len(src.splitlines()),
            "sloc": sloc(src),
            "covers": sorted(covered),
            "cases": sum(
                1
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
                and n.name.startswith("test_")
            ),
        }
    return result


def _resolve_imports(found: set[str], importer: str, known: set[str]) -> set[str]:
    """Bind each bare import name to a module that actually exists.

    A relative import inside a subpackage names a sibling, so `agent.py` in
    `harness/repl` importing `.approvals` means `repl.approvals`, not a
    top-level `approvals`. Siblings are tried first for that reason.
    """
    package = importer.rpartition(".")[0]
    resolved: set[str] = set()
    for name in found:
        sibling = f"{package}.{name}" if package else ""
        if sibling and sibling in known:
            resolved.add(sibling)
        elif name in known:
            resolved.add(name)
    return resolved - {importer}


def build() -> dict[str, Any]:
    sources: dict[str, str] = {}
    trees: dict[str, ast.Module] = {}
    for path in sorted(PKG.rglob("*.py")):
        if path.stem in {"__init__", "__main__"}:
            continue
        # Subpackage modules are named by dotted path, so `repl/agent.py` is
        # `repl.agent` and cannot collide with a top-level `agent.py`.
        name = ".".join(path.relative_to(PKG).with_suffix("").parts)
        src = path.read_text(encoding="utf-8")
        sources[name] = src
        trees[name] = ast.parse(src)

    layer_of = {name: (name.split(".")[0] if "." in name else "") for name in trees}

    tests = scan_tests()
    tested_by: dict[str, list[str]] = defaultdict(list)
    test_lines: dict[str, int] = defaultdict(int)
    test_cases: dict[str, int] = defaultdict(int)
    for filename, info in tests.items():
        for module in info["covers"]:
            tested_by[module].append(filename)
            test_lines[module] += info["sloc"]
            test_cases[module] += info["cases"]

    history = git_history([_source_path(n) for n in trees])

    imports = {
        n: sorted(_resolve_imports(internal_imports(t, "harness"), n, set(trees)))
        for n, t in trees.items()
    }
    dependents: dict[str, list[str]] = defaultdict(list)
    for name, deps in imports.items():
        for dep in deps:
            dependents[dep].append(name)

    modules = []
    for name, tree in trees.items():
        src = sources[name]
        api = public_api(tree)
        hist = history.get(_source_path(name), {})
        modules.append(
            {
                "name": name,
                "path": _source_path(name),
                "layer": layer_of[name],
                "side": "model" if name in MODEL_SIDE else "harness",
                "doc": first_line(ast.get_docstring(tree)),
                "doc_full": full_doc(ast.get_docstring(tree)),
                "lines": len(src.splitlines()),
                "sloc": sloc(src),
                "imports": imports[name],
                "imported_by": sorted(dependents.get(name, [])),
                "fan_out": len(imports[name]),
                "fan_in": len(dependents.get(name, [])),
                "external": sorted(external_imports(tree, "harness")),
                "api": api,
                "classes": sum(1 for a in api if a["kind"] == "class"),
                "functions": sum(1 for a in api if a["kind"] == "function"),
                "api_count": len(api),
                "tests": sorted(tested_by.get(name, [])),
                "test_sloc": test_lines.get(name, 0),
                "test_cases": test_cases.get(name, 0),
                "commits": hist.get("commits", 0),
                "last_touched": hist.get("last", ""),
                "in_boundary": any(s["module"] == name for s in BOUNDARY),
            }
        )
    order = LAYER_ORDER
    modules.sort(key=lambda m: (order.index(m["layer"]), -m["sloc"]))

    tools = scan_tools(trees["execution.tools"]) if "execution.tools" in trees else []
    commands = scan_cli(trees["cli"]) if "cli" in trees else []
    contracts_tree = trees.get("core.contracts")

    totals = {
        "modules": len(modules),
        "lines": sum(m["lines"] for m in modules),
        "sloc": sum(m["sloc"] for m in modules),
        "api": sum(m["api_count"] for m in modules),
        "edges": sum(m["fan_out"] for m in modules),
        "tools": len(tools),
        "commands": len(commands),
        "test_files": len(tests),
        "test_cases": sum(t["cases"] for t in tests.values()),
        "test_sloc": sum(t["sloc"] for t in tests.values()),
        "commits": max((m["commits"] for m in modules), default=0),
    }

    return {
        "generated_by": "docs/atlas/scripts/scan_harness.py",
        "repo": "yatra-harness",
        "head": head_commit(),
        "totals": totals,
        "layers": [
            {
                "key": key,
                "title": LAYER_TITLES[key][0],
                "blurb": LAYER_TITLES[key][1],
                "side": "model" if key == "models" else "harness",
                "modules": sorted(n for n in trees if layer_of[n] == key),
            }
            for key in LAYER_ORDER
            if any(layer_of[n] == key for n in trees)
        ],
        "modules": modules,
        "boundary": [
            dict(stage, present=stage["module"] in trees) for stage in BOUNDARY
        ],
        "primitives": _primitives(modules, set(trees)),
        "lanes": taxonomy.LANES,
        "steps": [dict(step, present=step["module"] in trees) for step in taxonomy.STEPS],
        "gates": [dict(gate, present=gate["module"] in trees) for gate in taxonomy.GATES],
        "transitions": taxonomy.TRANSITIONS,
        "state_columns": taxonomy.STATE_COLUMNS,
        "loops": [
            dict(loop, present=loop["root"] in trees and loop["entry"] in trees)
            for loop in taxonomy.LOOPS
        ],
        "shared": sorted(m for m in taxonomy.SHARED if m in trees),
        "tools": tools,
        "commands": commands,
        "events": scan_events(trees),
        "statuses": scan_enum(contracts_tree, "RunStatus") if contracts_tree else [],
        "actions": scan_enum(contracts_tree, "ActionKind") if contracts_tree else [],
        "risks": scan_enum(contracts_tree, "RiskLevel") if contracts_tree else [],
        "budgets": scan_dataclass_fields(contracts_tree, "BudgetSpec") if contracts_tree else [],
        "tests": tests,
    }


def _primitives(modules: list[dict[str, Any]], known: set[str]) -> list[dict[str, Any]]:
    """The coverage matrix, with each cell's real size and test count.

    A named module that does not exist is reported rather than dropped: the
    taxonomy is hand-written and a rename would otherwise silently turn a
    covered primitive into a thin-looking one.
    """
    by_name = {m["name"]: m for m in modules}
    rows = []
    for row in taxonomy.PRIMITIVES:
        cells = {}
        for loop in ("batch", "repl"):
            named = list(row[loop])
            missing = [n for n in named if n not in known]
            present = [n for n in named if n in known]
            cells[loop] = {
                "modules": present,
                "missing": missing,
                "sloc": sum(by_name[n]["sloc"] for n in present),
                "tests": sum(by_name[n]["test_cases"] for n in present),
                "api": sum(by_name[n]["api_count"] for n in present),
            }
        rows.append({
            "key": row["key"],
            "name": row["name"],
            "asks": row["asks"],
            "batch": cells["batch"],
            "repl": cells["repl"],
            "sloc": cells["batch"]["sloc"] + cells["repl"]["sloc"],
        })
    return rows


def head_commit() -> dict[str, str]:
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h%n%ad%n%s", "--date=short"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    parts = out.stdout.splitlines()
    if len(parts) < 3:
        return {}
    return {"sha": parts[0], "date": parts[1], "subject": parts[2]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the file on disk is not what a fresh scan produces",
    )
    args = parser.parse_args()

    payload = json.dumps(build(), indent=1, sort_keys=True) + "\n"

    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != payload:
            print(f"{args.out} is stale; re-run {Path(__file__).name}", file=sys.stderr)
            return 1
        print(f"{args.out} is current")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so the bytes written are the payload's own, rather than the
    # platform's. Without it a scan on Windows rewrites every line as CRLF and
    # the artifact shows up as changed in full, hiding the handful of lines
    # that actually moved -- and `--check` then fails on a file it just wrote.
    with args.out.open("w", encoding="utf-8", newline="") as handle:
        handle.write(payload)
    data = json.loads(payload)
    t = data["totals"]
    print(
        f"{args.out}: {t['modules']} modules, {t['sloc']} sloc, {t['edges']} import edges, "
        f"{t['tools']} tools, {t['commands']} commands, {t['test_cases']} test cases"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

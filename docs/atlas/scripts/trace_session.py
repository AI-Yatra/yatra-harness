"""Run one `ay` session end to end and record what the harness actually did.

The other scanner reads the package at rest: what imports what, how big each
module is. This one reads it in motion. It drives a real conversation against a
real provider on a real repository with a real failing test suite, and records
every component the work passed through on the way to making that suite pass.

Nothing here is instrumented by hand. A profile hook watches every call the
interpreter makes and keeps the ones that cross from one harness component into
another, so the recorded path cannot drift from the code the way a drawn one
can. If a module stops being on the path, it stops being in the picture.

The goal is not judged by the model and not judged by me. It is a failing test
suite before and the same suite after, run as a subprocess either side of the
conversation, with tests/ write-protected so the only way to turn them green is
to fix the code.

    python docs/atlas/scripts/trace_session.py --route inception

Writes docs/atlas/public/trace.json.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_harness import LAYER_ORDER  # noqa: E402

from harness.config import load_config  # noqa: E402
from harness.execution.workspace import Workspace  # noqa: E402
from harness.models import prompting  # noqa: E402
from harness.repl import prompt  # noqa: E402
from harness.repl.agent import Agent, Events  # noqa: E402
from harness.repl.approvals import Gate, Mode  # noqa: E402
from harness.repl.conversation import Conversation  # noqa: E402
from harness.repl.model import ChatModel  # noqa: E402
from harness.repl.tools import ReplToolset  # noqa: E402

TASK = """\
The test suite in this repository fails. Make it pass.

Run `python -m pytest -q` to see the failures, read the code, fix what is
wrong and add what is missing. You may not edit anything under tests/, because
the tests define the goal and changing them would be changing the goal. When
the whole suite passes, say so and show the final test output.
"""


def component_of(module: str) -> str:
    """Turn a module name into a component name, or "" if it is not ours."""
    if not module.startswith("harness."):
        return ""
    return module[len("harness.") :]


def layer_of(component: str) -> str:
    """The package a component sits in, for colouring it by layer."""
    head = component.split(".")[0]
    return head if "." in component and head in LAYER_ORDER else ""


@dataclass
class Span:
    """One entry into a component from somewhere that was not that component."""

    id: int
    parent: int
    component: str
    func: str
    t0: float
    t1: float = 0.0
    step: int = 0
    #: How much of this span's duration was spent inside spans it called.
    #: Without subtracting it, summing components double-counts every nested
    #: call and the total exceeds the wall clock.
    child_ms: float = 0.0

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent": self.parent,
            "component": self.component,
            "layer": layer_of(self.component),
            "func": self.func,
            "t0": round(self.t0 * 1000, 3),
            "ms": round((self.t1 - self.t0) * 1000, 3),
            "step": self.step,
        }


class Recorder:
    """A profile hook that keeps only the component boundary crossings.

    Every call inside a component that is already on the stack is counted and
    dropped. What survives is the shape of the work: which component handed to
    which, in what order, and how long the callee held the turn.
    """

    def __init__(self) -> None:
        self.spans: list[Span] = []
        self.edges: dict[tuple[str, str], int] = {}
        self.calls: dict[str, int] = {}
        self.self_ms: dict[str, float] = {}
        self.held_ms: dict[str, float] = {}
        self.step = 0
        self.entry = ""
        self.t0 = 0.0
        self._stack: list[Span] = []
        self._open: list[Span | None] = []

    def __call__(self, frame: Any, event: str, _arg: Any) -> None:
        # This runs on every call in the process, so it decides as early as it
        # can that a call is not interesting.
        if event == "call":
            module = frame.f_globals.get("__name__", "")
            component = component_of(module)
            if not component:
                self._open.append(None)
                return
            self.calls[component] = self.calls.get(component, 0) + 1
            caller = self._stack[-1].component if self._stack else "<entry>"
            if caller == component:
                self._open.append(None)
                return
            self.edges[caller, component] = self.edges.get((caller, component), 0) + 1
            span = Span(
                id=len(self.spans),
                parent=self._stack[-1].id if self._stack else -1,
                component=component,
                func=frame.f_code.co_name,
                t0=time.perf_counter() - self.t0,
                step=self.step,
            )
            if not self.entry:
                self.entry = f"{component}.{frame.f_code.co_name}"
            self.spans.append(span)
            self._stack.append(span)
            self._open.append(span)
        elif event == "return":
            if not self._open:
                return
            span = self._open.pop()
            if span is None:
                return
            span.t1 = time.perf_counter() - self.t0
            if self._stack and self._stack[-1] is span:
                self._stack.pop()
            held = (span.t1 - span.t0) * 1000
            if self._stack:
                self._stack[-1].child_ms += held
            self.held_ms[span.component] = self.held_ms.get(span.component, 0.0) + held
            self.self_ms[span.component] = (
                self.self_ms.get(span.component, 0.0) + held - span.child_ms
            )

    def now_ms(self) -> float:
        return round((time.perf_counter() - self.t0) * 1000, 1)

    def start(self) -> None:
        self.t0 = time.perf_counter()
        sys.setprofile(self)

    def stop(self) -> None:
        sys.setprofile(None)
        end = time.perf_counter() - self.t0
        for span in self.spans:
            if not span.t1:
                span.t1 = end


def pytest_report(cwd: Path) -> dict[str, Any]:
    """Run the suite as a subprocess and report what it said."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [line for line in out.splitlines() if line.strip()]
    failed = [
        line.split("::")[-1].split(" ")[0]
        for line in out.splitlines()
        if line.startswith("FAILED")
    ]
    return {
        "exit_code": proc.returncode,
        "summary": lines[-1].strip() if lines else "",
        "failed": failed,
        "passed": proc.returncode == 0,
    }


def build_agent(
    config: Any,
    route_name: str,
    root: Path,
    rec: Recorder,
    steps: list[dict[str, Any]],
    profile: Any = None,
) -> Agent:
    """The same objects `ay` builds for a real session, wired to the recorder."""
    route = config.router.routes[route_name]

    def on_tool_start(call: Any, _spec: Any) -> None:
        rec.step += 1
        steps.append(
            {
                "n": rec.step,
                "kind": "tool",
                "name": call.name,
                "component": "repl.tools",
                "args": {k: str(v)[:120] for k, v in list(call.arguments.items())[:3]},
                "t": rec.now_ms(),
            }
        )

    def on_tool_end(_call: Any, detail: str, ok: bool) -> None:
        if steps:
            steps[-1]["detail"] = detail
            steps[-1]["ok"] = ok
            steps[-1]["ms"] = round(rec.now_ms() - steps[-1]["t"], 1)

    def on_tool_denied(call: Any, reason: str) -> None:
        rec.step += 1
        steps.append(
            {
                "n": rec.step,
                "kind": "denied",
                "name": call.name,
                "component": "repl.approvals",
                "detail": reason,
                "ok": False,
                "t": rec.now_ms(),
            }
        )

    def on_text(text: str) -> None:
        if not text.strip():
            return
        rec.step += 1
        steps.append(
            {
                "n": rec.step,
                "kind": "say",
                "component": "repl.model",
                "detail": text.strip()[:400],
                "t": rec.now_ms(),
            }
        )

    return Agent(
        model=ChatModel(route),
        conversation=Conversation(
            prompt.build(config, root, mode=Mode.FULL_AUTO, profile=profile)
        ),
        toolset=ReplToolset(Workspace(root, ()), config),
        gate=Gate(config.policy, mode=Mode.FULL_AUTO),
        config=config,
        events=Events(
            on_text=on_text,
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
            on_tool_denied=on_tool_denied,
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Record one ay session end to end.")
    ap.add_argument("--route", default="inception", help="route name from the config")
    ap.add_argument("--config", default=str(ROOT / "configs" / "ay.yaml"))
    ap.add_argument("--subject", default=str(ROOT / "demo" / "tictactoe"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "atlas" / "public" / "trace.json"))
    ap.add_argument("--keep", action="store_true", help="keep the working copy")
    ap.add_argument(
        "--profile",
        default="",
        help="prompt profile to force; default is whatever the route resolves to",
    )
    args = ap.parse_args()

    config = load_config(Path(args.config))
    if args.route not in config.router.routes:
        print(f"no route named {args.route!r}; have: {', '.join(config.router.routes)}")
        return 2
    route = config.router.routes[args.route]

    work = Path(tempfile.mkdtemp(prefix="ay-trace-"))
    root = work / "subject"
    shutil.copytree(
        args.subject, root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache")
    )

    print(f"subject   {root}")
    print(f"route     {args.route} ({route.model})")
    before = pytest_report(root)
    print(f"before    {before['summary']}")

    steps: list[dict[str, Any]] = []
    rec = Recorder()
    profile = prompting.for_route(route, args.profile)
    print(f"profile   {profile.name}")
    agent = build_agent(config, args.route, root, rec, steps, profile)

    started = time.time()
    rec.start()
    try:
        stats: Any = agent.send(TASK)
        failure = ""
    except Exception as exc:  # the trace of a failed run is still a trace
        stats = None
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        rec.stop()
    wall = time.time() - started

    after = pytest_report(root)
    print(f"after     {after['summary']}")
    print(f"steps     {len(steps)}  spans {len(rec.spans)}  wall {wall:.1f}s")
    if failure:
        print(f"error     {failure}")

    diff = subprocess.run(
        ["git", "diff", "--no-index", "--stat", str(Path(args.subject)), str(root)],
        capture_output=True,
        text=True,
    ).stdout

    components = sorted(
        (
            {
                "name": name,
                "layer": layer_of(name),
                "calls": count,
                "ms": round(rec.self_ms.get(name, 0.0), 1),
                "held_ms": round(rec.held_ms.get(name, 0.0), 1),
                "first_step": next(
                    (s.step for s in rec.spans if s.component == name), 0
                ),
            }
            for name, count in rec.calls.items()
        ),
        key=lambda c: -int(c["calls"]),
    )

    trace = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "task": TASK.strip(),
        "subject": str(Path(args.subject).relative_to(ROOT)).replace("\\", "/"),
        "route": {
            "name": args.route,
            "model": route.model,
            "base_url": route.base_url,
            "stream": bool(route.stream),
        },
        "profile": {
            "name": profile.name,
            "dials": dict(prompting.describe(profile)),
        },
        "entry": rec.entry,
        "wall_ms": round(wall * 1000, 1),
        "ok": bool(after["passed"]) and not failure,
        "error": failure,
        "stats": {
            "steps": getattr(stats, "steps", 0),
            "tool_calls": getattr(stats, "tool_calls", 0),
            "input_tokens": getattr(stats, "input_tokens", 0),
            "output_tokens": getattr(stats, "output_tokens", 0),
            "errors": getattr(stats, "errors", 0),
        },
        "before": before,
        "after": after,
        "diffstat": diff.strip().splitlines()[-6:],
        "steps_taken": steps,
        "components": components,
        "edges": [
            {"from": a, "to": b, "calls": n}
            for (a, b), n in sorted(rec.edges.items(), key=lambda kv: -kv[1])
        ],
        "spans": [s.as_json() for s in rec.spans[:4000]],
        "span_total": len(rec.spans),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(trace, indent=2), encoding="utf-8")
    print(f"wrote     {out}  ({out.stat().st_size // 1024} KB)")

    if args.keep:
        print(f"kept      {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if trace["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

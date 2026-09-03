"""The editorial half of the atlas: a reading of the design, not a measurement.

Everything in `scan_harness.py` is read out of the code. Everything here is a
judgement, kept in one file so it can be argued with in one place and so the
canvas can label it as such.

The primitive list is not invented. It follows the "Design Primitives" of the
awesome-harness-engineering catalogue, cross-checked against LangChain's
"Anatomy of an Agent Harness" and OpenAI's published account of the Codex
harness. Where those sources disagree on naming, the more specific name wins.
Three rows (evals, reliability, retrieval) are additions this repository
earns by having dedicated modules for them.
"""

from __future__ import annotations

from typing import Any

# ── the harness-engineering primitives ─────────────────────────────────────
# Rows of the coverage matrix. `batch` and `repl` name the modules that
# implement each primitive on each of the two loops. An empty list is a real
# statement -- that loop does not do this -- and the matrix draws it as a gap
# rather than omitting the row.
PRIMITIVES: list[dict[str, Any]] = [
    {
        "key": "autonomy.loop",
        "name": "Agent loop",
        "asks": "observe, propose, act, repeat, stop",
        "batch": ["runtime"],
        "repl": ["repl.agent"],
    },
    {
        "key": "run.context",
        "name": "Context and compaction",
        "asks": "what the model sees, and what happens when it stops fitting",
        "batch": ["run.context", "run.compaction", "run.instructions"],
        "repl": ["repl.conversation", "repl.prompt"],
    },
    {
        "key": "execution.tools",
        "name": "Tool design",
        "asks": "typed capabilities, validated arguments, legible failures",
        "batch": ["execution.tools", "core.schema"],
        "repl": ["repl.tools"],
    },
    {
        "key": "skills",
        "name": "Skills and MCP",
        "asks": "capability sets declared outside the code that runs them",
        "batch": ["execution.mcp", "config"],
        "repl": [],
    },
    {
        "key": "permissions",
        "name": "Permissions",
        "asks": "who may do what, and what may never be authorized at all",
        "batch": ["execution.policy"],
        "repl": ["repl.approvals"],
    },
    {
        "key": "memory",
        "name": "Memory and state",
        "asks": "what survives a turn, a crash, and a session",
        "batch": ["record.state", "run.session", "autonomy.backlog"],
        "repl": ["repl.conversation"],
    },
    {
        "key": "planning",
        "name": "Planning and decomposition",
        "asks": "breaking work up, and pursuing it across attempts",
        "batch": ["autonomy.goal", "autonomy.loop", "run.subagents"],
        "repl": [],
    },
    {
        "key": "execution.sandbox",
        "name": "Sandbox and execution",
        "asks": "where a command runs and what it can reach",
        "batch": ["execution.sandbox", "execution.process", "execution.workspace"],
        "repl": ["repl.tools"],
    },
    {
        "key": "routing",
        "name": "Model access and routing",
        "asks": "which model, which credential, what if it fails",
        "batch": ["models.providers", "models.model_router", "models.llm_light", "models.auth", "models.streaming"],
        "repl": ["repl.model"],
    },
    {
        "key": "verification",
        "name": "Verification",
        "asks": "who decides the work is done, and on what evidence",
        "batch": ["run.verifier", "record.artifacts", "autonomy.delivery"],
        "repl": [],
    },
    {
        "key": "observability",
        "name": "Observability and tracing",
        "asks": "what was recorded, and can it be replayed",
        "batch": ["record.events", "record.tracing", "record.replay", "record.redaction"],
        "repl": [],
    },
    {
        "key": "hitl",
        "name": "Human in the loop",
        "asks": "where a person is asked, and how the answer is remembered",
        "batch": ["cli"],
        "repl": ["repl.shell", "repl.render"],
    },
    {
        "key": "autonomy.evals",
        "name": "Evals",
        "asks": "does the harness still do the job after a change",
        "batch": ["autonomy.evals", "autonomy.rubric"],
        "repl": [],
    },
    {
        "key": "reliability",
        "name": "Reliability",
        "asks": "retries, fallback, deliberate failure injection",
        "batch": ["models.model_router", "run.faults", "core.errors"],
        "repl": ["repl.model"],
    },
    {
        "key": "execution.search",
        "name": "Search",
        "asks": "finding the relevant part of a repository too big to read",
        "batch": ["execution.search"],
        "repl": ["repl.tools"],
    },
]

# ── one turn, as a sequence ────────────────────────────────────────────────
LANES: list[dict[str, str]] = [
    {"key": "operator", "name": "operator", "side": "human"},
    {"key": "intake", "name": "intake", "side": "harness"},
    {"key": "run.context", "name": "run.context", "side": "harness"},
    {"key": "router", "name": "router", "side": "harness"},
    {"key": "model", "name": "MODEL", "side": "model"},
    {"key": "gate", "name": "execution.policy", "side": "harness"},
    {"key": "tool", "name": "execution.tools", "side": "harness"},
    {"key": "world", "name": "files, shell", "side": "world"},
    {"key": "ledger", "name": "ledger", "side": "harness"},
    {"key": "run.verifier", "name": "run.verifier", "side": "harness"},
]

#: `emits` is the ledger event written at that step, where one is written.
#: `loops` records which of the two loops performs the step, so the diagram
#: can show that the REPL has no verifier and writes no ledger.
STEPS: list[dict[str, Any]] = [
    {"n": 1, "at": "operator", "to": "intake", "label": "task or message",
     "module": "cli", "emits": "RUN_CREATED", "loops": ["batch", "repl"]},
    {"n": 2, "at": "intake", "to": "run.context", "label": "frozen contract",
     "module": "config", "emits": "", "loops": ["batch"]},
    {"n": 3, "at": "run.context", "to": "router", "label": "bounded prompt",
     "module": "run.context", "emits": "CONTEXT_BUILT", "loops": ["batch", "repl"]},
    {"n": 4, "at": "router", "to": "model", "label": "messages + tool schemas",
     "module": "models.providers", "emits": "", "loops": ["batch", "repl"]},
    {"n": 5, "at": "model", "to": "router", "label": "text + tool calls",
     "module": "models.providers", "emits": "MODEL_RESPONSE", "loops": ["batch", "repl"]},
    {"n": 6, "at": "router", "to": "gate", "label": "proposal",
     "module": "core.contracts", "emits": "TOOL_REQUESTED", "loops": ["batch", "repl"]},
    {"n": 7, "at": "gate", "to": "tool", "label": "allowed",
     "module": "execution.policy", "emits": "", "loops": ["batch", "repl"]},
    {"n": 8, "at": "tool", "to": "world", "label": "side effect",
     "module": "execution.workspace", "emits": "", "loops": ["batch", "repl"]},
    {"n": 9, "at": "world", "to": "ledger", "label": "result",
     "module": "execution.tools", "emits": "TOOL_RESULT", "loops": ["batch", "repl"]},
    {"n": 10, "at": "ledger", "to": "run.context", "label": "observation + checkpoint",
     "module": "record.events", "emits": "CHECKPOINT_SAVED", "loops": ["batch"]},
    {"n": 11, "at": "run.context", "to": "run.verifier", "label": "finish claim",
     "module": "run.verifier", "emits": "VERIFICATION_STARTED", "loops": ["batch"]},
    {"n": 12, "at": "run.verifier", "to": "operator", "label": "verdict",
     "module": "run.verifier", "emits": "RUN_COMPLETED", "loops": ["batch"]},
]

# ── what each gate refuses ─────────────────────────────────────────────────
#: `final` marks a refusal no operator may override.
GATES: list[dict[str, Any]] = [
    {"gate": "skill", "module": "config", "autonomy.loop": "batch",
     "rule": "tool not in allowed_tools", "verdict": "not registered", "final": True},
    {"gate": "core.schema", "module": "execution.tools", "autonomy.loop": "both",
     "rule": "arguments fail the JSON schema", "verdict": "invalid arguments", "final": True},
    {"gate": "deny-list", "module": "execution.policy", "autonomy.loop": "both",
     "rule": "command matches a denied pattern", "verdict": "never approvable", "final": True},
    {"gate": "allowlist", "module": "execution.policy", "autonomy.loop": "batch",
     "rule": "command prefix not allowed", "verdict": "off the allowlist", "final": True},
    {"gate": "network", "module": "execution.policy", "autonomy.loop": "both",
     "rule": "network risk, network disabled", "verdict": "network disabled", "final": True},
    {"gate": "approval", "module": "execution.policy", "autonomy.loop": "both",
     "rule": "write or execute risk", "verdict": "asks a human", "final": False},
    {"gate": "containment", "module": "execution.workspace", "autonomy.loop": "both",
     "rule": "path resolves outside the root", "verdict": "escapes workspace", "final": True},
    {"gate": "protected", "module": "run.verifier", "autonomy.loop": "batch",
     "rule": "a protected path changed", "verdict": "run fails", "final": True},
]

# ── the run state machine ──────────────────────────────────────────────────
TRANSITIONS: list[dict[str, str]] = [
    {"from": "CREATED", "to": "RUNNING", "on": "RUN_STARTED"},
    {"from": "RUNNING", "to": "WAITING_APPROVAL", "on": "approval required"},
    {"from": "WAITING_APPROVAL", "to": "RUNNING", "on": "approved"},
    {"from": "WAITING_APPROVAL", "to": "BLOCKED", "on": "declined"},
    {"from": "RUNNING", "to": "VERIFYING", "on": "finish claimed"},
    {"from": "VERIFYING", "to": "COMPLETED", "on": "VERIFICATION_PASSED"},
    {"from": "VERIFYING", "to": "RUNNING", "on": "VERIFICATION_FAILED"},
    {"from": "RUNNING", "to": "BUDGET_EXHAUSTED", "on": "budget hit"},
    {"from": "RUNNING", "to": "FAILED", "on": "fatal error"},
    {"from": "RUNNING", "to": "BLOCKED", "on": "model asks"},
    {"from": "RUNNING", "to": "CANCELLED", "on": "interrupt"},
]

#: Laid out in columns so the state machine draws left to right.
STATE_COLUMNS: list[list[str]] = [
    ["CREATED"],
    ["RUNNING", "WAITING_APPROVAL"],
    ["VERIFYING"],
    ["COMPLETED", "FAILED", "BLOCKED", "BUDGET_EXHAUSTED", "CANCELLED"],
]

# ── the two loops, compared ────────────────────────────────────────────────
LOOPS: list[dict[str, Any]] = [
    {
        "key": "batch",
        "name": "harness run",
        "entry": "cli",
        "shape": "a task contract against a copied workspace",
        "ends": "a verifier's verdict",
        "execution.workspace": ".runs/<id>/workspace (a copy)",
        "root": "runtime",
    },
    {
        "key": "repl",
        "name": "ay",
        "entry": "repl.shell",
        "shape": "a conversation in the operator's own directory",
        "ends": "the model stops asking for tools",
        "execution.workspace": "the current directory",
        "root": "repl.agent",
    },
]

#: What both loops are built on. Named so the canvas can show the seam.
SHARED: list[str] = [
    "models.providers", "models.streaming", "models.auth", "config", "core.contracts", "execution.policy",
    "execution.workspace", "execution.process", "core.errors", "core.util", "core.schema",
]

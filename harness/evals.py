"""Evals: does the harness still do the job.

The unit suite proves each part behaves. It cannot answer the question that
matters when a prompt, a model, a budget or a skill changes: does a run still
finish the task. That needs a set of cases, a recorded outcome for each, and a
threshold that fails CI when the number moves the wrong way.

A case expected to *fail* is as important as one expected to pass. A suite
where everything succeeds cannot tell a working harness from one whose
verifier has quietly stopped verifying, and that is precisely the regression
worth catching here -- every other test in this repository would still be
green.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .contracts import RunStatus
from .errors import ConfigurationError
from .util import atomic_write_json, utc_now


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    task: Path
    config: Path
    skill: Path
    expect: RunStatus = RunStatus.COMPLETED
    model: str = ""


@dataclass(frozen=True, slots=True)
class EvalSuite:
    suite_id: str
    cases: tuple[EvalCase, ...]
    min_pass_rate: float = 1.0


@dataclass(frozen=True, slots=True)
class EvalResult:
    case_id: str
    expected: RunStatus
    actual: str
    passed: bool
    detail: str
    duration_ms: int
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class EvalReport:
    suite_id: str
    results: tuple[EvalResult, ...]
    pass_rate: float
    passed: bool
    report_path: Path | None = None
    duration_ms: int = 0


Runner = Callable[[EvalCase], Any]


def run_suite(
    suite: EvalSuite, *, runner: Runner, report_dir: Path, on_case: Any = None
) -> EvalReport:
    """Run every case, then judge the suite as a whole."""
    started = time.monotonic()
    results: list[EvalResult] = []
    for case in suite.cases:
        case_started = time.monotonic()
        try:
            run = runner(case)
            actual = run.status.value
            passed = run.status is case.expect
            detail = (
                f"expected {case.expect.value}, got {actual}: {run.terminal_reason}"
                if not passed
                else run.terminal_reason
            )
            run_id = getattr(run, "run_id", "")
        except Exception as exc:  # noqa: BLE001
            # One case exploding must not end the suite. An operator asked for
            # every number and a partial answer is the least useful outcome:
            # it looks like a result and is not one.
            actual, passed, run_id = "ERROR", False, ""
            detail = f"{type(exc).__name__}: {exc}"
        result = EvalResult(
            case_id=case.case_id,
            expected=case.expect,
            actual=actual,
            passed=passed,
            detail=detail,
            duration_ms=int((time.monotonic() - case_started) * 1000),
            run_id=run_id,
        )
        results.append(result)
        if on_case is not None:
            on_case(result)
    rate = (sum(1 for item in results if item.passed) / len(results)) if results else 0.0
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"eval-{suite.suite_id}.json"
    duration = int((time.monotonic() - started) * 1000)
    atomic_write_json(
        path,
        {
            "suite_id": suite.suite_id,
            "created_at": utc_now(),
            "pass_rate": rate,
            "min_pass_rate": suite.min_pass_rate,
            "passed": rate >= suite.min_pass_rate,
            "duration_ms": duration,
            "results": [
                {
                    "case_id": item.case_id,
                    "expected": item.expected.value,
                    "actual": item.actual,
                    "passed": item.passed,
                    "detail": item.detail,
                    "duration_ms": item.duration_ms,
                    "run_id": item.run_id,
                }
                for item in results
            ],
        },
    )
    return EvalReport(
        suite_id=suite.suite_id,
        results=tuple(results),
        pass_rate=rate,
        passed=rate >= suite.min_pass_rate,
        report_path=path,
        duration_ms=duration,
    )


def load_suite(path: str | Path) -> EvalSuite:
    from . import schema  # noqa: PLC0415 - avoids a cycle at import time

    suite_path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(suite_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"eval suite could not be read: {suite_path}: {exc}") from exc
    raw = schema.mapping(raw, "suite")
    schema.reject_unknown(raw, {"version", "id", "defaults", "cases", "min_pass_rate"}, "suite")
    if schema.integer(schema.require(raw, "version", "suite"), "suite.version") != 1:
        raise ConfigurationError("suite.version must be 1")
    base = suite_path.parent
    defaults = schema.mapping(raw.get("defaults", {}), "suite.defaults")
    schema.reject_unknown(defaults, {"config", "skill", "model"}, "suite.defaults")
    cases_raw = raw.get("cases") or []
    if not isinstance(cases_raw, list) or not cases_raw:
        raise ConfigurationError("suite.cases must be a non-empty list")
    cases = []
    for index, entry in enumerate(cases_raw):
        where = f"suite.cases[{index}]"
        item = schema.mapping(entry, where)
        schema.reject_unknown(item, {"id", "task", "config", "skill", "expect", "model"}, where)
        expect_value = str(item.get("expect", "COMPLETED"))
        try:
            expect = RunStatus(expect_value)
        except ValueError as exc:
            known = ", ".join(status.value for status in RunStatus)
            raise ConfigurationError(f"{where}.expect must be one of {known}") from exc
        cases.append(
            EvalCase(
                case_id=schema.string(schema.require(item, "id", where), f"{where}.id"),
                task=_file(item.get("task"), base, f"{where}.task"),
                config=_file(item.get("config") or defaults.get("config"), base, f"{where}.config"),
                skill=_file(item.get("skill") or defaults.get("skill"), base, f"{where}.skill"),
                expect=expect,
                model=str(item.get("model") or defaults.get("model") or ""),
            )
        )
    return EvalSuite(
        suite_id=schema.string(schema.require(raw, "id", "suite"), "suite.id"),
        cases=tuple(cases),
        min_pass_rate=schema.number(
            raw.get("min_pass_rate", 1.0), "suite.min_pass_rate", minimum=0.0
        ),
    )


def _file(value: Any, base: Path, where: str) -> Path:
    if not value:
        raise ConfigurationError(f"{where} is required (set it on the case or in defaults)")
    resolved = Path(str(value)).expanduser()
    if not resolved.is_absolute():
        resolved = (base / resolved).resolve()
    if not resolved.is_file():
        # Checked at load so a typo fails before the first case runs, not
        # somewhere in the middle of a suite that takes minutes.
        raise ConfigurationError(f"{where} is not a file: {resolved}")
    return resolved

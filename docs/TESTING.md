# Testing

The suite is `unittest`-based and runs offline with zero external services.
Every test that needs an LLM uses the deterministic replay provider or a
mocked HTTP transport.

## Running

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check harness tests
```

## Coverage areas

| File | What it covers |
|---|---|
| `test_llm_light.py` | priority ordering, weighted blending, constraints, profiles, determinism, round-trip serialization, credential-free plans |
| `test_providers.py` | replay provider, OpenAI-compatible and Anthropic adapters (mocked HTTP), error classification, text-action parsing, provider factory, config loading |
| `test_runtime.py` | end-to-end happy path, verifier-driven repair, fixture immutability, fault injection, fallback, budget exhaustion, crash/resume, LLM Light run integration, determinism |
| `test_tools.py` | workspace containment, protected paths, policy decisions, schema validation, tool registry behavior, MCP normalization |

## The acceptance criteria from the workshop plan

Each criterion maps to a test:

- one setup command + `doctor` → `test_teaching_config_loads*` and manual runbook
- deterministic path needs no API key/network → `test_happy_path_completes_and_writes_evidence`
- provider/model swap changes config only → `test_kind_mapping`
- declared primary failure falls back once → `test_broken_primary_falls_back_to_teaching`
- side effects attributable to tool/event ids → ledger checks in `test_verifier_driven_repair_is_visible_in_events`
- MCP tool passes same contracts → `test_registry_lists_all_expected_tools`
- writes outside workspace / protected paths rejected → `WorkspaceTests`, `test_workspace_escape_is_blocked`
- commands allowlisted and time-bounded → `test_command_allowlist`
- finish cannot COMPLETED without verification → `test_verifier_driven_repair_is_visible_in_events`
- failed verification re-enters loop → `test_verifier_driven_repair_is_visible_in_events`
- simulated timeout retries → `test_fault_model_timeout_recovers_and_completes`
- simulated crash resumes without repeating mutation → `test_crash_injects_and_resume_completes`
- budgets produce explicit stop → `test_budget_exhaustion_is_explicit`
- run emits diff/verification/state/events/summary → `test_happy_path_completes_and_writes_evidence`
- source fixtures unchanged → `test_source_fixture_stays_unchanged`
- happy path + every fault scenario covered → the suite as a whole

## Isolating runs in tests

Tests set `HARNESS_RUNS_DIR` to a temp directory so runs never touch the
repository's `.runs/`.

## Adding a scenario

1. Add the action script to `scenarios/` (`version: 1`, `actions:` list).
2. Add a route in the config with `kind: replay` and `script:`.
3. Add a test that runs the scenario and asserts the expected terminal status
   and ledger events.

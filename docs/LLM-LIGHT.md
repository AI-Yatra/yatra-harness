# LLM Light: priority-based model routing

LLM Light turns *declared operator priorities* into an *ordered route plan*.
It answers one question: **in which order should the configured model routes
be tried?** It never transports a request, never retries, and never sees a
credential.

## Why it exists

A harness that can talk to several models needs a routing decision. The
default in most tools is a hard-coded primary and fallback list. LLM Light
makes that decision explicit and policy-driven:

```
llm_light:
  mode: lexicographic
  priorities: [privacy, quality, cost, latency]
```

The harness then ranks every configured route by those priorities, with
privacy meaning "prefer local over remote", quality meaning "prefer higher
quality", cost meaning "prefer cheaper", latency meaning "prefer faster", and
context meaning "prefer larger context window".

## Decision vs mechanism

The split is the teaching point:

| Layer | Owns | Module |
|---|---|---|
| LLM Light | *Order*: which route first, which fallback next | `harness/models/llm_light.py` |
| Model Router | *Reliability*: retries, backoff, circuit breaking | `harness/models/model_router.py` |

An operator restating what they care about (`--priority cost --priority
latency`) must not perturb retry behavior, and a change to retry behavior must
not silently reorder an operator's preferences. This is why the two are
separate modules with a narrow interface: the router asks LLM Light for a plan
once per run and freezes it.

## Route attributes

Each route in `model_router.routes` carries two kinds of attributes:

```yaml
local-ollama:
  kind: ollama                    # transport: how to reach it
  base_url: http://127.0.0.1:11434/v1
  latency: medium                 # routing: how to rank it
  quality: 2.5
  context_window: 32768
```

The routing attributes are the only things LLM Light sees. Endpoints and
credential variable names are stripped at the boundary
(`profile_from_route`), so a routing decision can be logged and replayed
without leaking anything.

| Attribute | Meaning | Direction |
|---|---|---|
| `local` | whether the route is on this machine | derived from kind, or explicit |
| `cost_per_1m_input` / `cost_per_1m_output` | USD per 1M tokens | blended 75/25 toward input |
| `latency` | `low` / `medium` / `high` | lower is better |
| `quality` | 0 to 5 subjective model quality | higher is better |
| `context_window` | tokens | higher is better |
| `tool_support` | whether tool calls work | hard filter when required |

## Modes

### Lexicographic (default)

Compare routes key by key, in priority order. The first key that separates two
routes decides; the route name is the final deterministic tie-break.

```yaml
priorities: [privacy, quality, cost, latency]
```

means: first prefer local over remote; among those, prefer higher quality;
among those, prefer lower cost; then lower latency.

### Weighted

Blend every key at once into a 0 to 1 score per route. Each key's values are
normalized across candidates, multiplied by the key's weight, and summed.

```yaml
mode: weighted
weights:
  quality: 0.45
  cost: 0.30
  latency: 0.15
  privacy: 0.10
```

Weights do not need to sum to 1; they are normalized internally.

## Constraints

Constraints are hard filters. A route failing any constraint is excluded from
the plan and reported as excluded, never silently dropped.

```yaml
constraints:
  require_local: false          # exclude remote routes
  require_tools: true           # exclude routes without tool support
  min_context_window: 100000    # exclude small-context routes
  max_cost_per_1m: 0.30         # exclude expensive routes
  allowed: [teaching]           # only these routes (allowlist)
  denied: [broken]              # never these routes
```

## Profiles

Profiles bundle a mode, priorities/weights, and constraints under a name:

```yaml
profiles:
  offline:
    constraints:
      require_local: true
  budget:
    priorities: [cost, quality, latency]
  long-context:
    priorities: [context, quality]
    constraints:
      min_context_window: 100000
```

Select with `--profile offline` or `default_profile: offline` in config.

## Operator CLI

```bash
# Show the plan without running anything (no network, no credentials)
uv run harness routes --config configs/llm_light.yaml

# Pick a named profile
uv run harness routes --config configs/llm_light.yaml --profile budget

# Ad-hoc priorities (implies lexicographic)
uv run harness routes --config configs/llm_light.yaml --priority cost --priority latency

# Ad-hoc constraints
uv run harness routes --config configs/llm_light.yaml --require-local
uv run harness routes --config configs/llm_light.yaml --max-cost 0.30
```

The same flags work on `harness run` and appear in `harness explain`.

## Precedence

1. An explicit `--model` pin outranks everything: it fixes the primary route
   and the plan's other routes become fallbacks. A direct operator instruction
   beats a derived plan.
2. `--profile` selects a named profile.
3. `--priority ...` applies an ad-hoc lexicographic order.
4. The config's default mode/priorities apply.

## Observability

Every run records the plan in the event ledger:

- `LLM_LIGHT_PLAN` carries the full plan with per-route metrics, scores, and
  exclusion reasons.
- `MODEL_ROUTES_RESOLVED` carries the final ordered route list used by the run.

`harness doctor` verifies the plan can be computed before any run starts, so
an unsatisfiable routing config fails loudly at readiness time.

## Determinism

For the same config and route set, `LLMLight.plan` returns the same order
every time. The plan is resolved once per run and frozen, so a retry or resume
never silently uses a different model ordering.

## Design notes

- **No credentials.** `RouteProfile` carries only decision attributes.
- **No transport.** The router still owns HTTP, retries, and circuit
  breaking.
- **Honest defaults.** A route without declared attributes is assumed
  mediocre but capable: quality 3.0, latency medium, 8k context, tool support
  on.
- **Deterministic tie-breaks.** Route name decides final ordering, so the plan
  is reproducible.
- **Explicit exclusion.** Constraint failures are surfaced in the plan with
  reasons instead of being dropped from output.

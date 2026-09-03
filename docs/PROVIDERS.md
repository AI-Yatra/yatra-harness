# Providers

The harness talks to models through a provider port: every adapter returns the
same normalized `ActionProposal`, so the agent loop never learns which vendor
it is talking to. Adapters own wire format; the loop owns orchestration.

## Provider kinds

| `kind` | Adapter | Wire format | Typical use |
|---|---|---|---|
| `replay` | `ReplayProvider` | scripted YAML scenario | deterministic teaching, no network |
| `openai_compatible` | `OpenAICompatibleProvider` | `POST /chat/completions` | OpenAI, Groq, Together, OpenRouter, ... |
| `ollama` | `OpenAICompatibleProvider` | `POST /v1/chat/completions` | local Ollama |
| `vllm` | `OpenAICompatibleProvider` | `POST /v1/chat/completions` | local vLLM |
| `anthropic` | `AnthropicProvider` | `POST /messages` | Claude via Anthropic API |
| `gmi_router` | `GmiRouterProvider` | `POST .../ie/recommendation/autoroute` | GMI Router picks the model per request |

`ollama` and `vllm` are distinct kinds so routing and locality derive from the
kind, but they share the OpenAI-shaped adapter because their wire format is
identical.

## The GMI Router

GMI Cloud offers two things behind one key. `https://api.gmi-serving.com/v1`
is an ordinary OpenAI-shaped endpoint reached with `kind: openai_compatible`,
where you name the model. The router is a separate service on a separate host
that reads the prompt, applies your org's routing settings, and chooses the
model for you.

`GmiRouterProvider` subclasses `OpenAICompatibleProvider` because the response
is an ordinary completion. Everything that reads one is inherited unchanged.
Three things about the request differ, and each would fail confusingly if left
alone.

**The endpoint is a fixed URL on another host.** `base_url` is not appended to,
because appending would produce a 404 against the inference service. Setting
`base_url` on the route overrides the whole URL, for a staging endpoint.

**There is no `model` field.** The request carries a `mode`, one of `cost`,
`balanced` or `quality`. The route's `model:` is where you write it, because
the mode is what "which model do you want" means when a router answers. A
model id there is refused at construction rather than sent as a mode the
router will not recognise.

**`stream` defaults to true**, alone among the endpoints the harness calls. It
is sent explicitly as `false` for a non-streaming request. Left unsaid, the
reply is an event stream to a caller decoding one JSON object, which reads as
a malformed response: classified transient, so it retries, and fails
identically every time.

### Reading what it chose

A router that does not say what it picked cannot be audited, so the response's
`routing_metadata` is kept and printed after the turn: which model answered,
the detected task type, and whether a fallback fired and why. In the streamed
transport the metadata arrives in a frame of its own after the content, so
`StreamAccumulator` carries it through reassembly.

The same reassembly now also notices an `error` frame. A stream that fails
after output has started still ends with a 200 and a plausible partial
completion; without this a truncated turn is indistinguishable from a short
one. It is raised as transient, since another attempt can win.

The note travels on the `AssistantTurn`, not as a method on the model. A model
here can be a `RouteChain`, which delegates to whichever route is currently
working; asking it afterwards both misses the answer and made every model
implementation owe a method it had nothing to say for.

### Tool calling is still unverified, and the router is unusable without credit

Checked against a live key: the router bills for every request whatever the
mode, and an account with no balance gets `HTTP 402: Insufficient credits`
before the body is looked at. So whether it accepts a `tools` array is still
unknown, and `tool_support: false` stays. That 402 is classified permanent, so
it fails immediately rather than retrying an outcome that cannot change.

### The free models

Two of the 82 models GMI serves are free: `MiniMaxAI/MiniMax-M3` and
`MiniMaxAI/MiniMax-M2.7`. Both were confirmed live to call tools and to
stream, and the shipped `gmi` and `gmi-m27` routes point at them.

Verification probes a free model deliberately. GMI answers 402 on the first
billed request rather than refusing at signup, so probing a billed model would
fail a key that is perfectly good. This is the same trap Cerebras sets, for the
same reason, and `auth verify` already existed because of it.

## Normalized contracts

Every provider returns:

- `action` is an `ActionProposal`: `tool` (name + arguments), `finish`
  (summary), or `clarify` (question).
- `route` and `provider` carry attribution.
- `raw_summary` is a short human-readable trace of what the model said.
- `usage` is token usage when the provider reports it.
- `next_cursor` is the replay position (replay only).

The loop consumes only these fields. Swapping a provider never touches tools,
policy, state, verification, or the loop itself.

## Tool calls

- OpenAI-shaped responses: the first `tool_calls[0].function` becomes the
  proposal; `arguments` is parsed as JSON.
- Anthropic responses: the first `tool_use` content block becomes the
  proposal; `input` is used as arguments.

## Text actions

When a provider returns no tool call, the harness expects a JSON envelope:

```json
{"type": "finish", "summary": "done"}
{"type": "clarify", "question": "which module?"}
```

The parser tolerates prose around the envelope (small local models frequently
wrap it in explanation) by extracting the first complete top-level JSON
object.

## Error classification

| Failure | Class | Router behavior |
|---|---|---|
| HTTP 429, 408, 5xx | `TransientProviderError` | retry with backoff, then next route |
| network/connect timeout | `TransientProviderError` | retry, then next route |
| invalid JSON body | `TransientProviderError` | retry, then next route |
| HTTP 4xx (other) | `PermanentProviderError` | open circuit, next route |
| malformed tool call | `PermanentProviderError` | open circuit, next route |
| replay script exhausted | `PermanentProviderError` | open circuit, next route |

## Credentials

- `api_key_env` names the environment variable for a route.
- When unset, the provider's conventional variable is used:
  `OPENAI_API_KEY` for OpenAI-shaped, `ANTHROPIC_API_KEY` for Anthropic.
- Local servers (Ollama/vLLM) run unauthenticated; a missing key is only an
  error when a variable was explicitly configured.
- Secrets never appear in config files, events, or artifacts (see
  [SECURITY.md](SECURITY.md)).

## Replay scripts

```yaml
version: 1
actions:
  - type: tool
    call_id: inspect-tree
    name: repo_tree
    arguments:
      max_entries: 50
  - type: error
    transient: true
    message: deterministic primary-provider outage
  - type: finish
    call_id: final-finish
    summary: All acceptance tests pass.
```

Action types: `tool`, `finish`, `clarify`, `error`. An `error` action injects
a transient or permanent provider failure (used by the reliability
exercises).

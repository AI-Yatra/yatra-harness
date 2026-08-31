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

`ollama` and `vllm` are distinct kinds so routing and locality derive from the
kind, but they share the OpenAI-shaped adapter because their wire format is
identical.

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

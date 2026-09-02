# ay

A coding agent in your terminal, in your working directory.

```bash
cd ~/my-project
ay
```

```
  ay  yatra-harness 1.0.0
  qwen3-coder-plus · suggest · ~/my-project
  /help for commands, @file to include a file, !cmd to run one

> the auth test fails on windows. why?

⏺ Grep(def test_.*windows)
  ⎿ 3 matches in 2 files
⏺ Read(tests/test_auth.py)
  ⎿ 156 lines
⏺ Read(harness/models/auth.py)
  ⎿ 131 lines

The test asserts the key comes from the environment, but on Windows
`os.environ` is case-insensitive while the lookup in `_resolve` is not,
so `dashscope_api_key` set by the shell never matches `DASHSCOPE_API_KEY`.

> fix it and run the test

⏺ Edit(harness/models/auth.py)
  ⎿ Edit harness/models/auth.py?
       1. Yes   2. Yes, and edit_file any file for the rest of this session
       3. No, tell the model what to do instead
  ⎿ +4 -2
⏺ Run(python -m unittest tests.test_auth)
  ⎿ Run python -m unittest tests.test_auth?
       1. Yes   2. Yes, and run any python command for the rest of this session
       3. No, tell the model what to do instead
  ⎿ 14 lines

Fixed. `_resolve` now folds the name before comparing. All 12 tests pass.
```

One conversation, one working directory, many turns.

## What it is, and what it is not

`ay` is the conversational half of this repository. `harness run` is the other
half: it takes a task contract, copies a workspace, executes it against a
budget and independently verifies the result. That is the right shape for a
batch job and the wrong shape for a conversation, because every message
becomes a separate run with its own workspace and its own verdict, and turn
two cannot see what turn one did.

`ay` keeps one message history and works where you are standing. It edits the
files you are looking at. There is no task file, no acceptance command and no
verdict, because a conversation does not have one.

Both are still here and both still work:

```bash
ay                          # conversation, in this directory
ay run task.yaml            # batch task, in a copied workspace, verified
```

Anything `ay` does not recognise as a conversation is handed to the harness
CLI unchanged, so `ay auth`, `ay run`, `ay inspect` and the rest are the same
code path as `harness ...`.

## Getting a credential in

```bash
ay auth add <key>        # the provider is inferred from the key's prefix
ay auth status
ay auth verify opencode  # a real call, not a check that a variable is set
```

Or export the variable the active route names (`DASHSCOPE_API_KEY` by
default), or put it in a `.env`. All three are resolved the same way.

`configs/ay.yaml` ships a route for each of these, so once a key is stored you
can switch with `/model`:

| `/model` | Provider | Key from | Variable |
|---|---|---|---|
| `qwen` | Alibaba DashScope (`qwen3-coder-next`) | dashscope console | `DASHSCOPE_API_KEY` |
| `qwen-max` | the same key, `qwen3.7-max` for harder work | dashscope console | `DASHSCOPE_API_KEY` |
| `gemini` | Google AI Studio | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | `GEMINI_API_KEY` |
| `opencode` | OpenCode Zen | [opencode.ai/auth](https://opencode.ai/auth) | `OPENCODE_API_KEY` |
| `commandcode` | Command Code | Studio, Pro plan or higher | `COMMAND_CODE_API_KEY` |
| `local` | Ollama | no key needed | — |

Google keys carry an `AIza` or `AQ.` prefix and are detected automatically;
both formats are current, and a newly created AI Studio key is usually `AQ.`.
The two gateways publish no prefix, so name the provider when storing one:

```bash
ay auth add --provider opencode <key>
ay auth add --provider commandcode <key>
```

### Gemini and thought signatures

Gemini 3 returns an encrypted `thought_signature` on every function call, and
rejects the next request with a 400 if it is not sent back:

```
Function call is missing a thought_signature in functionCall parts.
```

A conversation hits this on the turn straight after its first tool call, which
is to say immediately. The REPL carries the field back untouched, through the
streaming path and across a saved session, so nothing needs configuring. It is
kept as opaque passthrough rather than parsed: the contents are Google's, and
this layer's only job is to refuse to lose them.

### Choosing a model

The shipped default is `qwen3-coder-next`, picked by running the same
multi-file bug-fix task three times against each candidate rather than by
reading benchmarks:

| Model | Passed | Tokens | Wall |
|---|---|---|---|
| `qwen3-coder-next` | 3/3 | ~31k | ~14s |
| `qwen3.7-max` | 3/3 | ~15k | ~33s |
| `qwen3.8-max` | 3/3 | ~18k | ~47s |
| `qwen3-coder-flash` | **0/3** | - | - |

`coder-next` spends about twice the tokens of the max models, and they cost
roughly fifteen times more per token, so a task lands far cheaper as well as
faster. Use `/model qwen-max` when a problem wants more thinking than speed.

`qwen3-coder-flash` is worth naming because it looks like the obvious cheap
choice and is not usable here: it writes `<function=list_dir>` into the
message body instead of making a tool call, so the loop sees prose, no tool
runs, and the turn ends having done nothing. A single-shot probe of it
returns a correct tool call, which is why this only shows up when you run a
real task.

### Free providers, and automatic fallover

When a route runs out, the REPL moves to the next one that has a credential
and carries on in the same conversation:

```
gemini is unavailable (HTTP 429: quota exhausted: free-tier limit of
                       20 requests per day for gemini-3.7-flash)
switched to qwen; continuing
```

The switch is sticky, so later turns start from the working route rather than
paying the dead one's failure again. It moves on for anything the *route* owns
-- a quota, a dead key, an outage, a refused connection -- but never for a 400,
which would fail identically everywhere and would burn every key you have.
Local routes are tried last, because a server that is not running looks
exactly like one that is until you ask it.

`configs/ay.yaml` ships routes for these. Add whichever keys you want; the
chain uses all of them.

| Route | Free allowance | The catch |
|---|---|---|
| `openrouter` | ~17 free tool-calling models, up to 1M context | 20/min, 50/day; a one-time $10 credit purchase raises the day cap to 1000 |
| `groq` | highest requests/day of the lot | low tokens-per-minute ceiling, which long context hits first |
| `gemini` | per-model daily allowance | 20/day on `gemini-3.7-flash`; other Gemini models have their own |
| `cerebras` | ~1M tokens/day | free-tier context capped near 8K, so it is a last fallback, not a primary |
| `opencode` | free models on the gateway | needs an account |
| `commandcode` | free models on the gateway | Pro plan or higher |
| `local` | unlimited | you run it |

OpenRouter is the one to add first: one key, many vendors, and the free models
support tool calling, which several free tiers quietly do not.

Model ids churn. `/models` asks the current provider what it actually serves:

```
> /models free
  openrouter: 17 models
    minimax/minimax-m3:free      nvidia/nemotron-3.5-lightning:free
    z-ai/glm-5.2:free            cohere/north-mini-code:free
  use one with /model openrouter:<id>
```

### Picking a model precisely

`--model` and `/model` take three forms:

```bash
ay --model gemini                        # a route name
ay --model gemini-3.7-flash              # a model some route already declares
ay --model gemini:gemini-3.5-flash       # route:model, pinned explicitly
```

A bare name that is neither a route nor a configured model has to be attached
to *some* endpoint, and the REPL uses the current route and says so. Use the
`route:model` form when that guess would be wrong -- otherwise a Gemini model
id typed while the DashScope route is active goes to DashScope and comes back
as a 404 from the wrong provider.

### When a route stops working

Quotas run out. `/model` lists every route, the model it uses, and whether it
has a credential:

```
  in use: qwen (qwen3-coder-plus)

    commandcode   claude-sonnet-4-6       no key
    gemini        gemini-3.7-flash        ready
    local         qwen2.5-coder:7b        ready
    opencode      claude-sonnet-5         no key
  * qwen          qwen3-coder-plus        ready

  switch with /model <name>, for example /model gemini
```

A turn that fails names the alternatives, and the conversation survives it --
switch and carry on. Transient failures (429, 5xx) are retried with backoff
before you see them, which matters for Gemini, since it returns 503 freely
under load.

Quotas are usually per model rather than per key, so a quota failure suggests
sibling models on the same provider first:

```
provider HTTP 429: quota exhausted: free-tier limit of 20 requests per day
                   for gemini-3.7-flash. Retry in 41s
The conversation is intact.
This quota is per model. Same provider, different model:
  /model gemini:gemini-3.5-flash
Other routes with a credential: /model local, /model qwen
```

That limit is real: Google AI Studio's free tier allows **20 requests per day**
for `gemini-3.7-flash`, and a single coding task spends five or six. The limit
differs per model, so `gemini-3.5-flash` has its own separate allowance. The
numbers are read out of the structured `details` in Google's reply rather than
its message text, where they sit behind a paragraph of links and get truncated
away.

## Approval modes

The REPL edits your real files, so it asks before it does. Reads never ask.

| Mode | Behaviour |
|---|---|
| `suggest` (default) | asks before every edit and every command |
| `auto-edit` | edits freely, still asks before running anything |
| `full-auto` | asks about nothing |

```bash
ay --mode auto-edit
```

or `/mode auto-edit` mid-session. Answering *"Yes, and …"* to a prompt grants
that shape of action for the rest of the session: per program for commands
(`git`), per tool for edits. `/approvals` lists what you have granted.

**The deny-list is not negotiable.** Commands matching it are refused with or
without approval and are never offered for consent, because a human clicking
yes on a prompt is exactly the mistake it exists to prevent. It is in
`configs/ay.yaml` and covers the three things whose damage does not undo:
destroying uncommitted work (`rm -rf`, `git reset --hard`, `git clean -fd`),
publishing without you (`git push`), and changing the machine (`sudo`,
`pip install`, `curl`). A pattern matches anywhere in the command, so
`sudo rm -rf` is caught by the `rm -rf` rule.

## Commands

| | |
|---|---|
| `/help` | the list |
| `/model [name]` | show or switch the model |
| `/models [filter]` | what the current provider actually serves |
| `/mode [name]` | approval mode |
| `/approvals` | what you have blanket-approved |
| `/tools` | what the model can call |
| `/context` | how full the context window is |
| `/cost` | tokens used this session |
| `/compact` | summarise the conversation to free context |
| `/clear` | forget the conversation; files are untouched |
| `/init` | write an AGENTS.md for this repository |
| `/config` | the active config, route and directory |
| `/exit` | leave |

And in the prompt itself:

- `@path/to/file` inlines that file with your message.
- `!command` runs a command yourself; the model never sees it.
- A trailing `\` continues on the next line.
- Ctrl-C stops the current turn without ending the session. Twice leaves.

## Flags

```
ay [message ...]
  -p, --print          run the message, print the answer, exit
  -C, --cwd DIR        work in DIR instead of here
  --config FILE        harness config YAML (default: configs/ay.yaml)
  --model NAME         route name or model id
  --mode MODE          suggest | auto-edit | full-auto
  --session ID         name this session, and reopen it if it exists
  --resume             reopen the most recent session in this directory
```

`-p` makes it scriptable:

```bash
ay -p "list every module that imports harness.policy"
```

## Sessions

A session is one message history. It is written to `.ay/<id>.json` in the
working directory after every turn, so `ay --resume` picks the thread back up
where it stopped. `--session <name>` gives it a name you can return to.

When the context window fills, the earlier part of the conversation is
summarised by the same model and replaced with the summary. `/compact` does it
on demand and `/context` shows how close you are.

## The tools

Shaped for editing rather than for batch patching:

| Tool | Risk | |
|---|---|---|
| `read_file` | read | contents with line numbers, `offset`/`limit` for slices |
| `list_dir` | read | one directory, build and vendor directories skipped |
| `glob` | read | find files by pattern, most recently modified first |
| `grep` | read | regex over contents, with file and line number |
| `write_file` | write | create or replace a whole file |
| `edit_file` | write | replace an exact block of text |
| `run_command` | execute | argument array, no shell |

`edit_file` is the important one. The batch path's `apply_patch` takes a whole
unified diff, which asks the model to get line numbers and context right in one
shot; when it is wrong you get a rejected hunk and a wasted turn. `edit_file`
takes the exact text to replace and refuses when that text is absent or appears
more than once, so a mistake comes back as a specific message the model can act
on:

```
old_string appears 3 times in harness/models/auth.py. Include more surrounding
lines so it matches exactly one place, or pass replace_all: true.
```

Every path goes through the same containment the batch path uses, so nothing
reaches outside the directory you started in. `run_command` returns a non-zero
exit as output rather than as an error, because a failing test is information
the model needs.

## Configuration

`configs/ay.yaml` is the default. It differs from the batch configs in the
places that matter for a REPL: a real deny-list, a larger output budget, and
`AGENTS.md` / `CLAUDE.md` read into the system prompt at startup so the model
follows the repository's own conventions without being told to.

It carries routes for DashScope, Google AI Studio, OpenCode Zen, Command Code
and a local Ollama server. Point it somewhere else with `--config`, or add your
own routes and switch with `/model`.

```bash
ollama serve && ollama pull qwen2.5-coder:7b
ay --model local
```

A route is five lines. The `kind` is `openai_compatible` for anything speaking
`/chat/completions`, `anthropic` for anything speaking `/messages`:

```yaml
    gemini:
      kind: openai_compatible
      model: gemini-3.7-flash
      base_url: https://generativelanguage.googleapis.com/v1beta/openai
      api_key_env: GEMINI_API_KEY
```

## How it works

```
your message
   ↓
context: system prompt (stable, cached) + the whole thread
   ↓
model: prose and zero or more tool calls, streamed
   ↓
for each call: approval gate → tool → result appended to the thread
   ↓
repeat until the model answers without asking for a tool
```

The system prompt is built once and always sent first, because providers cache
on prompt prefixes and moving it costs a cache miss on the entire history.

Bounds exist so a loop cannot run away unattended: 40 steps and 60 tool calls
per message, and the loop stops itself after six consecutive failing steps
rather than spinning. Reaching a bound is reported, and `continue` picks up.

The pieces are `harness/repl/`: `conversation.py` (the thread and its
compaction), `model.py` (both wire formats), `tools.py`, `approvals.py`,
`agent.py` (the loop), `render.py` (the screen), `shell.py` (input and slash
commands). The loop talks to the outside world through callbacks, so it runs
with no terminal attached and the tests drive it directly.

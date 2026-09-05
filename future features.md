# Future Features

## Existing Features

| Feature | Description |
| --- | --- |
| Agent loop | Model proposes one action at a time, harness executes it |
| Skill / Policy / Workspace gates | Control which tools exist, what is allowed, and which paths are reachable |
| Verifier | Runs acceptance commands and checks diff to decide pass / fail |
| Checkpoints and resume | Saves state after each turn, resumes after crash |
| Replay | Rebuilds a run from event ledger with hash check |
| Model router / LLM Light | Orders routes by privacy, quality, cost, latency, context |
| Providers and auth | 25 built-in providers, stored keys with redaction |
| ay REPL | Conversational agent with approvals, sessions, and compaction |
| Harness CLI | run, goal, loop, eval, review, deliver, inspect, doctor |
| Goal mode | Retries until acceptance command passes |
| Backlog loop | Works through feature_list.json until done or stuck |
| Evals | Runs suites and gates on pass rate |
| Review | Scores a completed run against a fixed rubric |
| Docker sandbox | Runs commands in isolated container |
| Sub-agents | Read-only helper agents working on workspace copy |
| Evidence bundles | Each run stored in .runs with ledger, patch, and summary |
| Teaching config | Scripted local model, no API key needed |

## Future Features

| Feature | Description |
| --- | --- |
| Web UI | View runs, ledgers, and evidence in a browser |
| Parallel runs | Run multiple tasks or backlog items at once |
| More providers | Add new models and gateways as needed |
| Better context search | Improved file search and repo map |
| Notifications | Alert on run completion or failure |
| Scheduled goals | Run goals and backlogs on a schedule |
| Metrics dashboard | Track cost, latency, and pass rate over time |

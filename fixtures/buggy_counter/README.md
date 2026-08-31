# Buggy counter fixture

`counter.clamp` should enforce both inclusive bounds. The lower-bound behavior
is intentionally incorrect so the harness can demonstrate verification-driven
self-repair. The `tests/` directory is protected by the task contract.


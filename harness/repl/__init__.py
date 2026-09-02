"""The conversational REPL: one thread, one working directory, many turns.

`harness run` executes a task contract against a copied workspace and hands
back a verdict. That is the right shape for a batch job and the wrong shape
for a conversation: every message becomes a separate run with its own
workspace, so turn two cannot see what turn one did, and the model never gets
to simply answer a question.

This package is the other shape. A session holds one message history and one
working directory, the model streams prose and tool calls into it, side
effects pass an approval gate on the way out, and the loop keeps going until
the model stops asking for tools.
"""

from __future__ import annotations

__all__ = ["agent", "approvals", "conversation", "model", "render", "tools"]

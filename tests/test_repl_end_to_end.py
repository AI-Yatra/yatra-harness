"""End-to-end: a real HTTP server, the real provider, the real tools.

Everything between the shell and the socket is the production path here. Only
the model's judgment is scripted, because that is the one part a test cannot
own. This is what catches the failures unit tests structurally cannot: a body
the provider builds wrongly, a streamed tool call reassembled wrongly, a
tool result the next request does not carry back.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from harness.config import load_config
from harness.execution.workspace import Workspace
from harness.repl.agent import Agent, Events
from harness.repl.approvals import Gate, Mode
from harness.repl.conversation import Conversation
from harness.repl.model import ChatModel
from harness.repl.tools import ReplToolset

ROOT = Path(__file__).resolve().parents[1]


def tool_call(index: int, name: str, arguments: dict) -> dict:
    return {
        "id": f"call_{index}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def completion(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {
        "choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def sse(chunks: list[dict]) -> bytes:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return (body + "data: [DONE]\n\n").encode("utf-8")


class ScriptedServer:
    """An OpenAI-compatible endpoint that replays a prepared script.

    A scripted entry is a dict (a JSON completion), bytes (a server-sent
    event stream), or an int (that HTTP status, so failure paths can be
    driven as precisely as successes).
    """

    def __init__(self, responses: list[dict | bytes | int]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append(json.loads(self.rfile.read(length) or b"{}"))
                nxt = outer.responses.pop(0) if outer.responses else completion("nothing scripted")
                status = 200
                streaming = False
                if isinstance(nxt, int):
                    status = nxt
                    payload = json.dumps(
                        {"error": {"message": f"scripted HTTP {status}"}}
                    ).encode()
                elif isinstance(nxt, bytes):
                    streaming = True
                    payload = nxt
                else:
                    payload = json.dumps(nxt).encode()
                self.send_response(status)
                self.send_header(
                    "Content-Type", "text/event-stream" if streaming else "application/json"
                )
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> ScriptedServer:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.config = load_config(ROOT / "configs" / "ay.yaml")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def agent_for(self, server: ScriptedServer, *, stream: bool = False, deltas=None) -> Agent:
        route = replace(
            self.config.router.routes[self.config.router.primary],
            base_url=server.base_url,
            api_key_env="",
            stream=stream,
            timeout_seconds=20,
        )
        toolset = ReplToolset(Workspace(self.root, ()), self.config)
        return Agent(
            model=ChatModel(route),
            conversation=Conversation("You are a test agent."),
            toolset=toolset,
            gate=Gate(self.config.policy, mode=Mode.FULL_AUTO),
            config=self.config,
            events=Events(on_delta=deltas.append if deltas is not None else None),
        )

    def test_a_full_edit_and_verify_cycle(self) -> None:
        """Read, edit, run the tests, answer -- over real HTTP, on real files."""
        (self.root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        (self.root / "check.py").write_text(
            "from calc import add\nassert add(2, 3) == 5\nprint('ok')\n", encoding="utf-8"
        )
        script = [
            completion("Let me look.", [tool_call(1, "read_file", {"path": "calc.py"})]),
            completion(
                "Found it: subtraction instead of addition.",
                [
                    tool_call(
                        2,
                        "edit_file",
                        {"path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"},
                    )
                ],
            ),
            completion("", [tool_call(3, "run_command", {"command": ["python", "check.py"]})]),
            completion("Fixed: add used `-`. It now returns a + b and check.py passes."),
        ]
        with ScriptedServer(script) as server:
            agent = self.agent_for(server)
            stats = agent.send("The check fails. Fix it and prove it.")

        self.assertEqual(stats.steps, 4)
        self.assertEqual(stats.tool_calls, 3)
        self.assertEqual(stats.errors, 0)
        # The file on disk actually changed.
        self.assertIn("return a + b", (self.root / "calc.py").read_text(encoding="utf-8"))
        # Token accounting came off the wire, not out of an estimate.
        self.assertEqual(stats.input_tokens, 400)

        # The last request carried the whole thread, in order, with results.
        final = server.requests[-1]["messages"]
        self.assertEqual(final[0]["role"], "system")
        roles = [m["role"] for m in final]
        self.assertEqual(roles.count("tool"), 3)
        run_result = next(
            m for m in final if m["role"] == "tool" and m.get("name") == "run_command"
        )
        self.assertIn("ok", run_result["content"])

    def test_the_tool_schemas_are_sent_to_the_model(self) -> None:
        with ScriptedServer([completion("hi")]) as server:
            self.agent_for(server).send("hello")
        tools = server.requests[0]["tools"]
        names = {t["function"]["name"] for t in tools}
        self.assertIn("edit_file", names)
        edit = next(t for t in tools if t["function"]["name"] == "edit_file")
        self.assertEqual(
            set(edit["function"]["parameters"]["required"]), {"path", "old_string", "new_string"}
        )

    def test_a_streamed_turn_reassembles_text_and_a_tool_call(self) -> None:
        """Arguments arrive as JSON fragments that are only valid concatenated."""
        (self.root / "a.txt").write_text("hello\n", encoding="utf-8")
        stream = sse(
            [
                {"choices": [{"delta": {"content": "Look"}}]},
                {"choices": [{"delta": {"content": "ing now."}}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": "read_file", "arguments": '{"pa'},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th":"a.txt"}'}}]}}
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )
        deltas: list[str] = []
        with ScriptedServer([stream, completion("It says hello.")]) as server:
            agent = self.agent_for(server, stream=True, deltas=deltas)
            stats = agent.send("what is in a.txt?")

        self.assertEqual("".join(deltas), "Looking now.")
        self.assertEqual(stats.tool_calls, 1)
        result = next(m for m in server.requests[1]["messages"] if m["role"] == "tool")
        self.assertIn("hello", result["content"])

    def test_a_denied_command_never_reaches_the_shell(self) -> None:
        script = [
            completion("", [tool_call(1, "run_command", {"command": ["rm", "-rf", "."]})]),
            completion("I cannot do that."),
        ]
        with ScriptedServer(script) as server:
            agent = self.agent_for(server)
            agent.send("delete everything")
        result = next(m for m in server.requests[1]["messages"] if m["role"] == "tool")
        self.assertIn("deny-list", result["content"])
        # The directory is untouched.
        self.assertTrue(self.root.is_dir())

    def test_a_bad_edit_comes_back_as_a_fixable_message(self) -> None:
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        script = [
            completion(
                "",
                [tool_call(1, "edit_file", {"path": "a.py", "old_string": "y = 2", "new_string": "y = 3"})],
            ),
            completion("", [tool_call(2, "read_file", {"path": "a.py"})]),
            completion("It was x, not y."),
        ]
        with ScriptedServer(script) as server:
            agent = self.agent_for(server)
            stats = agent.send("change y")
        self.assertEqual(stats.errors, 1)
        first = next(m for m in server.requests[1]["messages"] if m["role"] == "tool")
        self.assertIn("not found", first["content"])
        self.assertEqual((self.root / "a.py").read_text(encoding="utf-8"), "x = 1\n")

    def test_a_provider_error_is_reported_without_losing_the_thread(self) -> None:
        from harness.repl.agent import ModelUnavailable

        with ScriptedServer([]) as server:
            server.server.shutdown()  # refuse the connection
            agent = self.agent_for(server)
            with self.assertRaises(ModelUnavailable):
                agent.send("hello")
            self.assertEqual(agent.conversation.messages[0]["content"], "hello")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

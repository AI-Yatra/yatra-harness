"""Streaming: showing work as it arrives instead of after it finishes.

A turn against a remote model is a blocking request. The operator sees
nothing for however long it takes and then sees everything at once, which
reads as a hang and is the single most common reason someone kills a run that
was about to succeed.

The wire format is server-sent events, and reassembling one is the part that
breaks: deltas arrive split at arbitrary boundaries, tool-call arguments come
as string fragments that only parse once concatenated, and the terminating
sentinel is not JSON. All of that is a pure function over recorded payloads
here, so streaming is tested without a provider.
"""

from __future__ import annotations

import json
import unittest

from harness.models.streaming import StreamAccumulator, iter_sse_data


def sse(*chunks: dict | str) -> str:
    lines = []
    for chunk in chunks:
        body = chunk if isinstance(chunk, str) else json.dumps(chunk)
        lines.append(f"data: {body}\n\n")
    return "".join(lines)


def delta(**kwargs) -> dict:
    return {"choices": [{"delta": kwargs}]}


class SSEParsingTests(unittest.TestCase):
    def test_data_lines_are_yielded_in_order(self) -> None:
        payload = sse({"a": 1}, {"a": 2})
        self.assertEqual([json.loads(item)["a"] for item in iter_sse_data(payload.splitlines())], [1, 2])

    def test_the_done_sentinel_is_not_yielded(self) -> None:
        # "[DONE]" is not JSON. Handing it to a parser is the classic way a
        # streaming client dies on the last line of a successful response.
        payload = sse({"a": 1}, "[DONE]")
        self.assertEqual(len(list(iter_sse_data(payload.splitlines()))), 1)

    def test_blank_lines_and_comments_are_ignored(self) -> None:
        lines = ["", ": keep-alive", "data: {\"a\": 1}", ""]
        self.assertEqual(len(list(iter_sse_data(lines))), 1)

    def test_non_data_fields_are_ignored(self) -> None:
        lines = ["event: message", "id: 7", "data: {\"a\": 1}"]
        self.assertEqual(len(list(iter_sse_data(lines))), 1)

    def test_a_stream_with_no_data_yields_nothing(self) -> None:
        self.assertEqual(list(iter_sse_data(["", ": ping"])), [])


class ContentTests(unittest.TestCase):
    def accumulate(self, *chunks: dict) -> StreamAccumulator:
        accumulator = StreamAccumulator()
        for chunk in chunks:
            accumulator.feed(chunk)
        return accumulator

    def test_content_deltas_are_concatenated(self) -> None:
        result = self.accumulate(delta(content='{"type":'), delta(content='"finish"}'))
        self.assertEqual(result.content, '{"type":"finish"}')

    def test_deltas_are_reported_as_they_arrive(self) -> None:
        seen: list[str] = []
        accumulator = StreamAccumulator(on_delta=seen.append)
        accumulator.feed(delta(content="one "))
        accumulator.feed(delta(content="two"))
        self.assertEqual(seen, ["one ", "two"])

    def test_an_empty_delta_reports_nothing(self) -> None:
        seen: list[str] = []
        accumulator = StreamAccumulator(on_delta=seen.append)
        accumulator.feed(delta())
        accumulator.feed(delta(content=""))
        self.assertEqual(seen, [])

    def test_a_chunk_with_no_choices_is_survivable(self) -> None:
        # Providers send usage-only and keep-alive chunks.
        self.assertEqual(self.accumulate({"usage": {"total_tokens": 3}}).content, "")


class ToolCallTests(unittest.TestCase):
    def accumulate(self, *chunks: dict) -> StreamAccumulator:
        accumulator = StreamAccumulator()
        for chunk in chunks:
            accumulator.feed(chunk)
        return accumulator

    def test_a_tool_call_is_reassembled_from_fragments(self) -> None:
        # Arguments arrive as string fragments that are only valid JSON once
        # concatenated. Parsing each fragment is the mistake this prevents.
        result = self.accumulate(
            delta(tool_calls=[{"index": 0, "id": "call-1",
                               "function": {"name": "read_file", "arguments": '{"pa'}}]),
            delta(tool_calls=[{"index": 0, "function": {"arguments": 'th": "a.py"}'}}]),
        )
        calls = result.tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], "call-1")
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertEqual(calls[0]["function"]["arguments"], '{"path": "a.py"}')

    def test_the_name_may_arrive_after_the_first_fragment(self) -> None:
        result = self.accumulate(
            delta(tool_calls=[{"index": 0, "function": {"arguments": "{}"}}]),
            delta(tool_calls=[{"index": 0, "id": "c", "function": {"name": "finish"}}]),
        )
        self.assertEqual(result.tool_calls()[0]["function"]["name"], "finish")

    def test_parallel_tool_calls_stay_separate(self) -> None:
        result = self.accumulate(
            delta(tool_calls=[{"index": 0, "id": "a", "function": {"name": "x", "arguments": "{}"}}]),
            delta(tool_calls=[{"index": 1, "id": "b", "function": {"name": "y", "arguments": "{}"}}]),
        )
        self.assertEqual([call["id"] for call in result.tool_calls()], ["a", "b"])

    def test_calls_are_ordered_by_their_index(self) -> None:
        result = self.accumulate(
            delta(tool_calls=[{"index": 1, "id": "b", "function": {"name": "y", "arguments": "{}"}}]),
            delta(tool_calls=[{"index": 0, "id": "a", "function": {"name": "x", "arguments": "{}"}}]),
        )
        self.assertEqual([call["id"] for call in result.tool_calls()], ["a", "b"])


class MessageTests(unittest.TestCase):
    def test_the_stream_becomes_the_message_a_normal_response_would_have(self) -> None:
        # The point of this class: after streaming, the rest of the adapter
        # must not be able to tell the difference.
        accumulator = StreamAccumulator()
        accumulator.feed(delta(content='{"type":"finish","summary":"done"}'))
        payload = accumulator.as_payload()
        self.assertEqual(
            payload["choices"][0]["message"]["content"], '{"type":"finish","summary":"done"}'
        )

    def test_tool_calls_appear_where_the_adapter_expects_them(self) -> None:
        accumulator = StreamAccumulator()
        accumulator.feed(
            delta(tool_calls=[{"index": 0, "id": "c1",
                               "function": {"name": "read_file", "arguments": '{"path":"a"}'}}])
        )
        message = accumulator.as_payload()["choices"][0]["message"]
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "read_file")

    def test_usage_is_carried_through(self) -> None:
        accumulator = StreamAccumulator()
        accumulator.feed({"usage": {"total_tokens": 42}})
        self.assertEqual(accumulator.as_payload()["usage"]["total_tokens"], 42)

    def test_an_empty_stream_still_produces_a_wellformed_payload(self) -> None:
        payload = StreamAccumulator().as_payload()
        self.assertEqual(payload["choices"][0]["message"]["content"], "")


if __name__ == "__main__":
    unittest.main()

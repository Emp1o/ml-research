#!/usr/bin/env python3
"""Checks for scripts/load_test.py. Hits a local SSE stub, not a cluster."""

from __future__ import annotations

import io
import json
import os
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import load_test


def _sse(payload: dict | str) -> bytes:
    if isinstance(payload, str):
        body = payload
    else:
        body = json.dumps(payload, separators=(",", ":"))
    return f"data: {body}\n\n".encode("utf-8")


class _StreamingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 — stdlib handler name
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if self.headers.get("Authorization") != "Bearer test-key":
            message = b'{"error":"unauthorized"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)
            return
        body = json.loads(raw.decode("utf-8"))
        if body.get("stream") is not True:
            message = b'{"error":"stream required"}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)
            return

        chunks = [
            _sse(
                {
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": ""}}
                    ]
                }
            ),
            None,  # pause before the first generated token
            _sse({"choices": [{"index": 0, "delta": {"content": "Hi"}}]}),
            _sse({"choices": [{"index": 0, "delta": {"content": "!"}}]}),
            _sse(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 2,
                        "total_tokens": 13,
                    },
                }
            ),
            _sse("[DONE]"),
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in chunks:
            if chunk is None:
                time.sleep(0.08)
                continue
            self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class LoadTestTests(unittest.TestCase):
    def test_completion_tokens_fall_back_to_content_events(self) -> None:
        payload = (
            b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        result = load_test.consume_sse(io.BytesIO(payload), time.perf_counter())
        self.assertTrue(result.ok)
        self.assertEqual(result.completion_tokens, 1)
        self.assertIsNone(result.prompt_tokens)
        self.assertIsNotNone(result.ttft_s)

    def test_role_only_delta_is_not_content(self) -> None:
        event = {"choices": [{"delta": {"role": "assistant", "content": ""}}]}
        self.assertEqual(load_test.content_from_event(event), "")
        token = {"choices": [{"delta": {"content": "Hi"}}]}
        self.assertEqual(load_test.content_from_event(token), "Hi")

    def test_usage_and_rates(self) -> None:
        spec = load_test.ScenarioSpec("short", "короткий", "hi", 32)
        ok = load_test.CallResult(True, 0.1, 0.5, 10, 4, None)
        ok_late = load_test.CallResult(True, 0.3, 1.5, 30, 6, None)
        failed = load_test.CallResult(False, None, 0.2, None, None, "HTTP 500")
        summary = load_test.ScenarioSummary(spec, (ok, failed, ok_late), 2.0, 2)
        rates = load_test.scenario_rates(summary)
        self.assertAlmostEqual(rates["ttft_p50"], 0.2)
        self.assertAlmostEqual(rates["latency_p50"], 1.0)
        self.assertAlmostEqual(rates["req_s"], 1.0)
        self.assertAlmostEqual(rates["output_tok_s"], 5.0)
        self.assertAlmostEqual(rates["total_tok_s"], 25.0)
        report = load_test.render_markdown([summary], "preamble")
        self.assertIn("| короткий | 200.0 | 200.0 | 1000.0 | 1000.0 | 1.000 | 5.000 | 25.000 |", report)
        self.assertIn("| короткий | 2 | 32 | 3 | 2 | 2/3 | 20.0 | 5.0 |", report)
        self.assertIn("HTTP 500", report)

    def test_missing_api_key(self) -> None:
        env = os.environ.copy()
        env.pop("API_KEY", None)
        previous = os.environ
        try:
            os.environ.clear()
            os.environ.update(env)
            code = load_test.main(["--requests", "1", "--warmup", "0"])
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.assertEqual(code, 2)

    def test_streaming_stub(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        denied = load_test.post_chat_stream(
            base, "wrong", "model", "hello", 8, 5.0
        )
        self.assertFalse(denied.ok)
        self.assertIn("HTTP 401", denied.error or "")

        one = load_test.post_chat_stream(base, "test-key", "stub", "hello", 8, 5.0)
        self.assertTrue(one.ok)
        self.assertIsNotNone(one.ttft_s)
        self.assertIsNotNone(one.latency_s)
        assert one.ttft_s is not None and one.latency_s is not None
        self.assertGreaterEqual(one.ttft_s, 0.06)
        self.assertGreaterEqual(one.latency_s, one.ttft_s)
        self.assertEqual(one.prompt_tokens, 11)
        self.assertEqual(one.completion_tokens, 2)

        os.environ["API_KEY"] = "test-key"
        stdout = io.StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                code = load_test.main(
                    [
                        "--base-url",
                        base,
                        "--model",
                        "stub",
                        "--concurrency",
                        "2",
                        "--requests",
                        "4",
                        "--max-tokens",
                        "16",
                        "--long-repeats",
                        "2",
                        "--warmup",
                        "1",
                        "--timeout",
                        "5",
                        "--scenarios",
                        "short,long",
                    ]
                )
        finally:
            os.environ.pop("API_KEY", None)
        self.assertEqual(code, 0)
        report = stdout.getvalue()
        self.assertIn("| короткий |", report)
        self.assertIn("| длинный |", report)
        self.assertIn("| 4/4 |", report)


if __name__ == "__main__":
    unittest.main()

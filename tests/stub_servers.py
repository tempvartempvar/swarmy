"""Local HTTP stubs for the Anthropic Messages API (SSE streaming) and OpenRouter chat completions.

Each stub answers with a scripted sequence of responses and records the request bodies,
so provider code can be tested end to end without API keys or network access.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Stub:
    def __init__(self, responder):
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.responder = responder
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                stub.requests.append(body)
                stub.headers.append({k.lower(): v for k, v in self.headers.items()})
                status, ctype, payload = stub.responder(len(stub.requests) - 1, body)
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()


def _sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def anthropic_tool_call(name: str, args: dict, model: str = "claude-haiku-5-5", usage: dict | None = None) -> bytes:
    usage = usage or {"input_tokens": 1200, "output_tokens": 1, "cache_read_input_tokens": 800,
                      "cache_creation_input_tokens": 300}
    return _sse([
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                                              "content": [], "stop_reason": None, "stop_sequence": None,
                                              "usage": usage}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Working on it."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": f"toolu_{name}", "name": name, "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": json.dumps(args)}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
         "usage": {"output_tokens": 150}},
        {"type": "message_stop"},
    ])


def anthropic_stub(script: list[bytes]):
    def responder(i, body):
        return 200, "text/event-stream", script[min(i, len(script) - 1)]
    return _Stub(responder)


def openrouter_tool_call(name: str, args: dict, cost: float = 0.00012) -> bytes:
    return json.dumps({
        "id": "gen-1", "model": "openai/gpt-oss-120b",
        "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"call_{name}", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 60, "cost": cost,
                  "prompt_tokens_details": {"cached_tokens": 100}},
    }).encode()


def openrouter_stub(script: list[tuple[int, bytes]]):
    def responder(i, body):
        status, payload = script[min(i, len(script) - 1)]
        return status, "application/json", payload
    return _Stub(responder)

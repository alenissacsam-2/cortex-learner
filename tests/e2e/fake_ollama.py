"""A fake Ollama on a loopback port the OS picks: GET /api/tags lists `models`; POST /api/chat
answers in Ollama's shape (with `format: json`, the first allowed label of a classify prompt;
otherwise `answer`). Every request is recorded in `requests` as {"path", "body"}."""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER = "A minimum spanning tree joins every vertex at the least total edge weight [1]."
_LABELS = re.compile(r"Allowed labels: (\[.*?\])")


class FakeOllama:
    def __init__(self, models=("qwen3.5:2b",), answer=ANSWER):
        self.models, self.answer, self.requests = list(models), answer, []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.requests.append({"path": self.path, "body": None})
                if self.path == "/api/tags":
                    self._send(200, {"models": [{"name": m} for m in fake.models]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                fake.requests.append({"path": self.path, "body": body})
                if self.path != "/api/chat":
                    self._send(404, {"error": "not found"})
                    return
                self._send(200, {"model": body.get("model"), "done": True,
                                 "message": {"role": "assistant", "content": fake.reply(body)}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def reply(self, body: dict) -> str:
        if body.get("format") != "json":
            return self.answer
        system = next((m["content"] for m in body.get("messages", [])
                       if m.get("role") == "system"), "")
        m = _LABELS.search(system)
        label = json.loads(m.group(1))[0] if m else "other"
        return json.dumps({"label": label, "confidence": 0.9})

    def chats(self) -> list[dict]:
        """The bodies of every /api/chat request."""
        return [r["body"] for r in self.requests if r["path"] == "/api/chat"]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

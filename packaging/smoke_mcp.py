"""The MCP handshake of a built Keepfold (release-apps.yml):
`python3 packaging/smoke_mcp.py <keepfold CLI> <vault>` starts `<CLI> mcp --vault <vault>`, sends
`initialize`, `notifications/initialized` and `tools/list` over stdio, and exits 0 when the server
lists keepfold_* tools. Stdlib only: the CI runners and the clean Ubuntu containers run it with
their own python3."""

import json
import queue
import subprocess
import sys
import threading

TIMEOUT_S = 120  # a cold frozen start on a CI runner


def main(cli: str, vault: str) -> int:
    p = subprocess.Popen([cli, "mcp", "--vault", vault], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, encoding="utf-8")
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [lines.put(s) for s in p.stdout] + [lines.put(None)],
                     daemon=True).start()

    def send(msg: dict) -> None:
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()

    def answer(rid: int) -> dict:  # the response to request `rid`; notifications are skipped
        while True:
            line = lines.get(timeout=TIMEOUT_S)
            if line is None:
                raise RuntimeError(f"the server exited (code {p.wait()}) before answering {rid}")
            try:
                msg = json.loads(line)
            except ValueError:
                raise RuntimeError(f"not JSON-RPC on stdout: {line[:200]!r}") from None
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"request {rid} failed: {msg['error']}")
                return msg["result"]

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "keepfold-smoke", "version": "1"}}})
        init = answer(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = [t["name"] for t in answer(2)["tools"]]
    except (RuntimeError, queue.Empty, OSError, KeyError) as e:
        print(f"mcp smoke FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        p.kill()
        p.wait()
    ours = [t for t in tools if t.startswith("keepfold_")]
    info = init.get("serverInfo") or {}
    print(f"mcp smoke: {info.get('name')} {info.get('version')}, protocol "
          f"{init.get('protocolVersion')}, {len(tools)} tools ({len(ours)} keepfold_*)")
    return 0 if ours else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))

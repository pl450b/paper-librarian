"""Minimal MCP server over stdio (JSON-RPC, newline-delimited), no SDK.

`claude -p` launches this for each chat turn. Tool calls are forwarded to the
running daemon (which already has the embedding model loaded); if the daemon
is not up, the tools run in this process instead.

  python -m librarian.mcp
"""

import json
import sys
import urllib.error
import urllib.request

from . import config, tools

PROTOCOL = "2025-06-18"


def _schema(props, required):
    return {"type": "object", "properties": props, "required": required}


def tool_list():
    return [{"name": n, "description": tools.TOOLS[n][1],
             "inputSchema": _schema(tools.TOOLS[n][2], tools.TOOLS[n][3])} for n in tools.TOOLS]


def call_tool(name, args):
    url = f"http://{config.DAEMON_HOST}:{config.DAEMON_PORT}/tool/{name}"
    req = urllib.request.Request(url, data=json.dumps(args or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return tools.call(name, args)


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:                      # a notification: never answered
        return None
    if method == "initialize":
        return {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", PROTOCOL),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "librarian", "version": "0.1"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tool_list()}
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            result = call_tool(p.get("name"), p.get("arguments") or {})
            is_error = isinstance(result, dict) and "error" in result and len(result) == 1
            return {"content": [{"type": "text",
                                 "text": json.dumps(result, ensure_ascii=False, indent=1)}],
                    "isError": is_error}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"tool failed: {e}"}], "isError": True}
    raise KeyError(method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            result = handle(msg)
            if result is None:
                continue
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        except KeyError as e:
            reply = {"jsonrpc": "2.0", "id": msg.get("id"),
                     "error": {"code": -32601, "message": f"method not found: {e}"}}
        except Exception as e:
            reply = {"jsonrpc": "2.0", "id": msg.get("id"),
                     "error": {"code": -32603, "message": str(e)}}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

"""Run 낭독 부스 as a local website, outside the Claude app.

    python web/serve.py            # then open http://127.0.0.1:8780/

Inside the Claude app the page reaches the voice servers through `window.claude`.
Here this server stands in for it: it serves the page with a small shim, and relays the
page's tool calls to the same MCP servers (localvoice, typecast) over stdio.
Claude-written answers (the app's `sample` capability) come from the Claude API here:
put ANTHROPIC_API_KEY=... in .env at the repository root (pip install anthropic). Without a key, free-form questions fall back to the
prepared answers.

Listens on 127.0.0.1 only.
"""

import http.server
import json
import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
VOICE = os.path.dirname(HERE)
PAGE = os.path.join(HERE, "nangdok-booth.html")
PORT = int(os.environ.get("PORT", "8780"))
NODE = os.environ.get("NODE", "node")

SERVERS = {
    "host:localvoice": {
        # The voice server needs the pipeline's packages (requirements.txt); by default the
        # same Python that runs this file, or set VOICE_PYTHON.
        "cmd": [os.environ.get("VOICE_PYTHON", sys.executable), os.path.join(VOICE, "src/server.py")],
        # First run downloads the models from Hugging Face (a few GB); set HF_HUB_OFFLINE=1 afterwards if you like.
        "env": {},
    },
    "host:typecast": {
        "cmd": [NODE, os.path.join(VOICE, "src/typecast_server.mjs")],
        "env": {},
    },
}

SHIM = """<script>
// Stand-in for the Claude app's window.claude: tool calls go to this local server.
(() => {
  async function callTool(server, tool, args) {
    let res;
    try {
      res = await fetch('/api/call', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ server, tool, args: args || {} }) });
    } catch (e) { throw { code: 'server_unavailable', message: '로컬 웹서버에 연결하지 못했어요.' }; }
    const out = await res.json();
    if (out.error) throw { code: out.error, message: out.message };
    let payload = null;
    try { payload = JSON.parse(out.result.content[0].text); } catch (e) {}
    const result = { payload, content: out.result.content };
    if (out.result.isError) throw { code: 'tool_error', result, message: (payload && payload.message) || '' };
    return result;
  }
  const mcp = { callTool };
  // Stand-in for the sample capability: Claude answers come from this server's /api/sample.
  async function json(prompt, opts) {
    let res;
    try {
      res = await fetch('/api/sample', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt }), signal: opts && opts.signal });
    } catch (e) {
      if (e && e.name === 'AbortError') throw { code: 'cancelled', message: '취소됐어요.' };
      throw { code: 'upstream_error', message: '로컬 웹서버에 연결하지 못했어요.' };
    }
    const out = await res.json();
    if (out.error) throw { code: out.error, message: out.message };
    return out.value;
  }
  const sample = { json };
  window.claude = { use: async (name) => (name === 'mcp' ? mcp : name === 'sample' ? sample : null) };
})();
</script>
"""


class McpClient:
    """One long-lived MCP server process, one request at a time."""

    def __init__(self, cmd, env):
        self.cmd, self.env = cmd, env
        self.proc = None
        self.lock = threading.Lock()
        self.next_id = 0

    def _start(self):
        self.proc = subprocess.Popen(
            self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, **self.env}, text=True, encoding="utf-8", bufsize=1)
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "booth-web", "version": "1.0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg):
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _rpc(self, method, params):
        self.next_id += 1
        mid = self.next_id
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise ConnectionError("server exited")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "error"))
                return msg["result"]

    def call(self, tool, args):
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.proc is None or self.proc.poll() is not None:
                        self._start()
                    return self._rpc("tools/call", {"name": tool, "arguments": args})
                except ConnectionError:
                    self.proc = None
                    if attempt:
                        raise


CLIENTS = {name: McpClient(**spec) for name, spec in SERVERS.items()}


# ---------- Claude answers (stands in for the app's sample capability) ----------
ENV_FILE = os.path.join(VOICE, ".env")
MODEL = os.environ.get("BOOTH_MODEL", "claude-opus-5-5")
# The page's only Claude call asks which claim a question is about and what to say aloud.
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"item": {"type": "integer"}, "answer": {"type": "string"}},
    "required": ["item", "answer"],
    "additionalProperties": False,
}


def _api_key():
    """ANTHROPIC_API_KEY from the environment, else from .env at the repository root (read on every call)."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            name, _, value = line.strip().partition("=")
            if name.strip() == "ANTHROPIC_API_KEY":
                return value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


def ask_claude(prompt):
    """Return {"value": {...}} or {"error": code, "message": text} in the page's sample error codes."""
    key = _api_key()
    if not key:
        return {"error": "capability_disabled", "message": ".env에 ANTHROPIC_API_KEY가 없어요."}
    try:
        import anthropic
    except ImportError:
        return {"error": "capability_disabled", "message": "anthropic 패키지가 없어요. pip install anthropic 후 다시 실행해 주세요."}
    client = anthropic.Anthropic(api_key=key, timeout=60.0)
    try:
        # Quick spoken answers: low effort. If a safety classifier declines, the API re-runs
        # the request on a fallback model it picks ("default") inside the same call.
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError:
        return {"error": "capability_disabled", "message": "API 키가 올바르지 않아요."}
    except anthropic.RateLimitError:
        return {"error": "rate_limited", "message": "요청이 너무 잦아요."}
    except anthropic.APIStatusError as e:
        return {"error": "upstream_error", "message": f"Claude API 오류 {e.status_code}"}
    except anthropic.APIConnectionError:
        return {"error": "upstream_error", "message": "Claude API에 연결하지 못했어요."}
    if response.stop_reason == "refusal":
        return {"error": "upstream_error", "message": "Claude가 이 질문에 답하지 않았어요."}
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        return {"value": json.loads(text)}
    except json.JSONDecodeError:
        return {"error": "empty_completion", "message": "Claude 답변을 읽지 못했어요."}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=HERE, **k)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html", "/nangdok-booth.html"):
            html = open(PAGE, encoding="utf-8").read()
            i = html.find("<title>")
            html = (html[:i] + SHIM + html[i:]) if i >= 0 else SHIM + html
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/sample":
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            return self._json(ask_claude(str(req.get("prompt") or "")))
        if self.path != "/api/call":
            return self._json({"error": "not_found"}, 404)
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        client = CLIENTS.get(req.get("server"))
        if not client:
            return self._json({"error": "server_not_connected", "message": f"{req.get('server')} 서버가 없어요."})
        try:
            return self._json({"result": client.call(req["tool"], req.get("args") or {})})
        except Exception as e:
            return self._json({"error": "server_unavailable", "message": f"음성 서버가 응답하지 않았어요: {e}"})

    def log_message(self, fmt, *a):
        sys.stderr.write("%s\n" % (fmt % a))


if __name__ == "__main__":
    print(f"낭독 부스: http://127.0.0.1:{PORT}/  (끄려면 Ctrl+C)", flush=True)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 끄는 중이에요…", flush=True)
    finally:
        httpd.server_close()
        for c in CLIENTS.values():
            if c.proc and c.proc.poll() is None:
                c.proc.terminate()  # the voice servers; the shared model worker exits after 5 idle minutes
        print("꺼졌어요.", flush=True)

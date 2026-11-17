"""Local voice-clone connector for the 낭독 부스 artifact.

A dependency-light MCP server (stdio, newline-delimited JSON-RPC) that speaks Korean
in the voices of the people in this folder's recording, using the open-source
Chatterbox Multilingual model (MIT) with a reference clip per voice.

Tools (same shape as the Typecast connector, so the page can switch between them):
  list_voices  – the reference voices in voices.json, plus voices people added
  speak        – one sentence as base64 WAV
  add_voice    – register a person's own recording (WAV) as a new voice
  remove_voice – delete a voice added with add_voice
Generated audio is cached in ./cache so replaying a sentence is instant.

The model itself runs in worker.py, one process shared by every copy of this server
(the Claude app starts several) and started only when a sentence is not cached yet,
so this process stays small.
"""

import base64
import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback

# Keep a private copy of stdout for JSON-RPC and point fd 1 (Python and C level) at stderr,
# so nothing printed by accident can corrupt the protocol stream.
_OUT = os.fdopen(os.dup(1), "w", encoding="utf-8")
os.dup2(2, 1)
sys.stdout = sys.stderr

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, "cache")
VOICES = json.load(open(os.path.join(ROOT, "voices.json"), encoding="utf-8"))
# Unix socket paths are limited to about 100 bytes, so the socket lives in /tmp under a name
# derived from this folder (one worker per install) instead of next to this file.
SOCK = os.path.join("/tmp", "localvoice-" + hashlib.sha1(ROOT.encode()).hexdigest()[:12] + ".sock")
# Voices people record themselves live apart from voices.json, so the built-in list stays as shipped.
CUSTOM_DIR = os.path.join(ROOT, "refs", "custom")
CUSTOM_FILE = os.path.join(ROOT, "custom_voices.json")

# How each emotion choice maps onto Chatterbox's controls.
# exaggeration: expressiveness (0.25 flat … 1.0+ animated); cfg_weight: lower = slower, more deliberate.
EMOTIONS = {
    "normal": {"exaggeration": 0.5, "cfg_weight": 0.5},
    "toneup": {"exaggeration": 0.7, "cfg_weight": 0.45},  # 0.8/0.4 blurred some words in testing
    "tonedown": {"exaggeration": 0.3, "cfg_weight": 0.5},
}

# Bump when worker.py's post-processing changes so older cached takes are not replayed.
POST = "voice-only-2"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class ToolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _connect():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(SOCK)
    except OSError:
        s.close()
        raise
    return s


def _worker():
    """A connection to the shared worker, starting it when no copy of this server has yet."""
    try:
        return _connect()
    except OSError:
        pass
    with open(os.path.join(ROOT, ".worker.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # one server starts it; the others wait here and then connect
        try:
            return _connect()
        except OSError:
            pass
        with open(os.path.join(ROOT, "worker.log"), "a") as out:
            subprocess.Popen(
                [sys.executable, os.path.join(ROOT, "worker.py")],
                stdin=subprocess.DEVNULL, stdout=out, stderr=out, cwd=ROOT, start_new_session=True,
            )
        deadline = time.time() + 30  # the worker listens before it loads the model
        while time.time() < deadline:
            time.sleep(0.2)
            try:
                return _connect()
            except OSError:
                pass
    raise ToolError("worker", "음성 엔진을 시작하지 못했어요. localvoice/worker.log를 확인해 주세요.")


def _generate(req):
    line = (json.dumps(req, ensure_ascii=False) + "\n").encode()
    for attempt in range(2):
        with _worker() as s:
            s.sendall(line)
            reply = s.makefile("r", encoding="utf-8").readline()
        if reply:
            return json.loads(reply)
        # The worker went idle and closed just as this request arrived; a new one is started on retry.
        log("worker closed without a reply, retrying")
    raise ToolError("worker", "음성 엔진이 응답하지 않았어요. 잠시 후 다시 읽어 주세요.")


def _custom():
    """Voices added with add_voice, read fresh on every call (several server copies share the file)."""
    try:
        with open(CUSTOM_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_custom(fn):
    """Read-modify-write custom_voices.json under a lock, so two copies never lose each other's change."""
    with open(os.path.join(ROOT, ".custom.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        voices = fn(_custom())
        tmp = CUSTOM_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(voices, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CUSTOM_FILE)
        return voices


def _all_voices():
    # A voice whose recording is missing (e.g. a fresh clone without refs/) is left out.
    return [v for v in VOICES + _custom() if os.path.exists(os.path.join(ROOT, v["ref"]))]


def _cache_dir(voice):
    """Added voices keep their sentences in their own folder, so removing the voice removes them too."""
    return os.path.join(CACHE_DIR, voice["id"]) if voice.get("user_added") else CACHE_DIR


def add_voice(args):
    import io
    import uuid
    import wave

    name = str(args.get("name") or "").strip()
    if not name or len(name) > 20:
        raise ToolError("bad_request", "이름은 1~20자로 적어 주세요.")
    try:
        data = base64.b64decode(str(args.get("audio_base64") or ""), validate=True)
    except ValueError:
        raise ToolError("bad_request", "녹음 파일을 읽지 못했어요.")
    if len(data) > 8 * 1024 * 1024:
        raise ToolError("bad_request", "녹음 파일이 너무 커요. 30초 안쪽으로 녹음해 주세요.")
    try:
        with wave.open(io.BytesIO(data)) as w:
            seconds = w.getnframes() / float(w.getframerate())
            ok = w.getsampwidth() == 2 and w.getnchannels() in (1, 2)
    except (wave.Error, EOFError, ZeroDivisionError):
        raise ToolError("bad_request", "WAV 형식의 녹음만 등록할 수 있어요.")
    if not ok:
        raise ToolError("bad_request", "16비트 WAV 녹음만 등록할 수 있어요.")
    if seconds < 5:
        raise ToolError("bad_request", f"녹음이 {seconds:.1f}초로 너무 짧아요. 10초 안팎으로 읽어 주세요.")
    if seconds > 31:
        raise ToolError("bad_request", f"녹음이 {seconds:.0f}초로 너무 길어요. 30초 안쪽으로 줄여 주세요.")
    vid = "u_" + uuid.uuid4().hex[:10]  # a new id per recording, so cached takes of an older one are never reused
    os.makedirs(CUSTOM_DIR, exist_ok=True)
    with open(os.path.join(CUSTOM_DIR, vid + ".wav"), "wb") as f:
        f.write(data)
    entry = {"id": vid, "name": name, "ref": os.path.join("refs", "custom", vid + ".wav"),
             "user_added": True, "seconds": round(seconds, 1), "added_at": int(time.time())}
    _save_custom(lambda vs: vs + [entry])
    return {"voice": {"id": vid, "name": name, "seconds": entry["seconds"]}}


def remove_voice(args):
    vid = str(args.get("voice_id") or "")
    if not any(v["id"] == vid for v in _custom()):
        raise ToolError("bad_request", "직접 등록한 목소리만 지울 수 있어요.")
    _save_custom(lambda vs: [v for v in vs if v["id"] != vid])
    try:
        os.remove(os.path.join(CUSTOM_DIR, vid + ".wav"))
    except FileNotFoundError:
        pass
    # The sentences read in this voice go with it.
    folder = os.path.join(CACHE_DIR, vid)
    cached = len([n for n in os.listdir(folder) if n.endswith(".wav")]) if os.path.isdir(folder) else 0
    shutil.rmtree(folder, ignore_errors=True)
    return {"removed": vid, "cached_removed": cached}


def list_voices(_args):
    return {
        "voices": [
            {
                "id": v["id"],
                "name": v["name"],
                "name_eng": v.get("name_eng", ""),
                "gender": v.get("gender"),
                "age": v.get("age"),
                "use_cases": v.get("use_cases", []),
                "custom": True,
                "user_added": bool(v.get("user_added")),
                "models": [{"version": "chatterbox-ml", "emotions": list(EMOTIONS)}],
            }
            for v in _all_voices()
        ]
    }


def speak(args):
    text = str(args.get("text") or "").strip()
    if not text:
        raise ToolError("bad_request", "읽을 문장이 비어 있어요.")
    if len(text) > 300:
        raise ToolError("bad_request", "한 번에 300자까지만 읽을 수 있어요.")
    voice = next((v for v in _all_voices() if v["id"] == args.get("voice_id")), None)
    if not voice:
        raise ToolError("bad_request", "목소리를 먼저 골라 주세요.")
    emotion = args.get("emotion") if args.get("emotion") in EMOTIONS else "normal"
    params = dict(EMOTIONS[emotion], temperature=0.8)

    key = hashlib.sha256(json.dumps([text, voice["id"], voice["ref"], params, POST], ensure_ascii=False).encode()).hexdigest()[:32]
    path = os.path.join(_cache_dir(voice), key + ".wav")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return {"mime": "audio/wav", "audio_base64": base64.b64encode(f.read()).decode(), "chars": len(text), "cached": True}

    out = _generate({"text": text, "ref": os.path.join(ROOT, voice["ref"]), "params": params})
    if "error" in out:
        raise ToolError(out["error"], out.get("message", ""))
    data = base64.b64decode(out["wav"])
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    except OSError as e:
        log("cache write failed:", e)
    return {"mime": "audio/wav", "audio_base64": out["wav"], "chars": len(text), "cached": False}


TOOLS = [
    {
        "name": "list_voices",
        "description": "List the voices cloned from the recording in this folder.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"title": "녹음 속 목소리 목록", "readOnlyHint": True},
    },
    {
        "name": "speak",
        "description": "Speak one Korean sentence in a voice cloned from the recording; returns base64 WAV.",
        "inputSchema": {
            "type": "object",
            "required": ["text", "voice_id"],
            "properties": {
                "text": {"type": "string", "maxLength": 300},
                "voice_id": {"type": "string", "description": "id from list_voices"},
                "emotion": {"type": "string", "enum": list(EMOTIONS)},
            },
        },
        # Generates audio only and changes no data, so the app need not confirm every sentence.
        "annotations": {"title": "녹음 목소리로 읽기", "readOnlyHint": True, "destructiveHint": False},
    },
]
TOOLS += [
    {
        "name": "add_voice",
        "description": "Register a person's own recording (16-bit WAV, 5-30 s of clear speech) as a new voice to read with.",
        "inputSchema": {
            "type": "object",
            "required": ["name", "audio_base64"],
            "properties": {
                "name": {"type": "string", "maxLength": 20},
                "audio_base64": {"type": "string", "description": "base64 16-bit PCM WAV"},
            },
        },
        "annotations": {"title": "내 목소리 등록", "readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "remove_voice",
        "description": "Delete a voice that was added with add_voice, with the sentences cached in it (built-in voices cannot be removed).",
        "inputSchema": {"type": "object", "required": ["voice_id"], "properties": {"voice_id": {"type": "string"}}},
        "annotations": {"title": "등록한 목소리 지우기", "readOnlyHint": False, "destructiveHint": True},
    },
]
HANDLERS = {"list_voices": list_voices, "speak": speak, "add_voice": add_voice, "remove_voice": remove_voice}


def handle(msg):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "localvoice", "version": "1.0.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        fn = HANDLERS.get(params.get("name"))
        if not fn:
            raise LookupError(f"Unknown tool: {params.get('name')}")
        try:
            out = fn(params.get("arguments") or {})
            return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]}
        except Exception as e:
            code = e.code if isinstance(e, ToolError) else "internal"
            if not isinstance(e, ToolError):
                log(traceback.format_exc())
            return {"content": [{"type": "text", "text": json.dumps({"error": code, "message": str(e)}, ensure_ascii=False)}], "isError": True}
    if "id" not in msg:
        return None  # notification
    raise NotImplementedError(f"Method not found: {method}")


def send(obj):
    _OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
    _OUT.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        try:
            result = handle(msg)
            if "id" in msg and result is not None:
                send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except Exception as e:
            if "id" in msg:
                code = -32601 if isinstance(e, NotImplementedError) else -32602 if isinstance(e, LookupError) else -32603
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": code, "message": str(e)}})


if __name__ == "__main__":
    main()

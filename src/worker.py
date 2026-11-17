"""Shared speech engine for localvoice/server.py.

The Claude app starts several copies of the MCP server (and each Claude Code session in
this folder one more). Loading the voice model in each took ~5 GB apiece, so the model
lives here instead: one process, started by whichever server first needs a new sentence,
listening on a Unix socket next to this file, and exiting after LOCALVOICE_IDLE_MIN
minutes (default 5) without a request so an idle Mac gets the memory back. While
reading it holds ~9–11 GB (model on MPS plus Demucs and Whisper on CPU); past
LOCALVOICE_MAX_GB (default 12) it exits after the current sentence and the next
sentence starts a fresh one.

Protocol: one JSON line per connection, {"text", "ref", "params"} ->
{"wav": base64 WAV} or {"error": code, "message": text}.
"""

import base64
import ctypes
import hashlib
import io
import json
import os
import socket
import sys
import traceback
import wave

ROOT = os.path.dirname(os.path.abspath(__file__))
# Unix socket paths are limited to about 100 bytes, so the socket lives in /tmp under a name
# derived from this folder (one worker per install) instead of next to this file.
SOCK = os.path.join("/tmp", "localvoice-" + hashlib.sha1(ROOT.encode()).hexdigest()[:12] + ".sock")
IDLE = float(os.environ.get("LOCALVOICE_IDLE_MIN", "5")) * 60
MAX_MB = float(os.environ.get("LOCALVOICE_MAX_GB", "12")) * 1024


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class _RUsage(ctypes.Structure):  # rusage_info_v0 from <libproc.h>
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins",
        "wired_size", "resident_size", "phys_footprint", "proc_start_abstime", "proc_exit_abstime")]


def _footprint_mb():
    """This process's memory as Activity Monitor counts it, GPU memory included."""
    ru = _RUsage()
    if ctypes.CDLL("/usr/lib/libproc.dylib").proc_pid_rusage(os.getpid(), 0, ctypes.byref(ru)) != 0:
        return 0
    return ru.phys_footprint / 2**20


_model = None
_sep = None  # Demucs, used to keep only the voice stem of each take
_asr = None  # faster-whisper, used to find where the sentence really ends


def _load():
    global _model, _sep, _asr
    import torch
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    try:
        _model = ChatterboxMultilingualTTS.from_pretrained(device=device)
    except Exception as e:  # some ops are missing on MPS in older torch builds
        log("loading on", device, "failed, falling back to cpu:", e)
        _model = ChatterboxMultilingualTTS.from_pretrained(device="cpu")
    try:
        from demucs.pretrained import get_model

        _sep = get_model("htdemucs").eval()
    except Exception as e:
        log("demucs unavailable, takes are returned unfiltered:", e)
    try:
        from faster_whisper import WhisperModel

        _asr = WhisperModel("small", device="cpu", compute_type="int8")
    except Exception as e:
        log("faster-whisper unavailable, takes are not cut at the last word:", e)
    log("model ready")


def _wav_bytes(samples, sr):
    import numpy as np

    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _voice_only(samples, sr):
    """Drop everything but the voice.

    Chatterbox sometimes ends a take with a burst of noise or music (louder than the
    voice itself in testing), which sounds like a background track between sentences.
    """
    if _sep is None:
        return samples
    import numpy as np
    import torch
    import torchaudio.functional as AF
    from demucs.apply import apply_model

    x = AF.resample(torch.from_numpy(samples).float()[None], sr, _sep.samplerate).repeat(2, 1)
    mean, std = x.mean(), x.std() + 1e-8
    # On CPU (~1 s slower per sentence): alternating Demucs and Chatterbox on MPS grew the GPU heap
    # by ~1 GB with every sentence, although each alone levels off.
    with torch.no_grad():
        est = apply_model(_sep, ((x - mean) / std)[None], device="cpu", shifts=1, overlap=0.25, progress=False)[0]
    voice = est[_sep.sources.index("vocals")].mean(0) * std + mean
    voice = AF.resample(voice.cpu(), _sep.samplerate, sr).numpy()[: len(samples)]
    if np.abs(voice).max() < 0.1 * np.abs(samples).max():
        return samples  # separation lost the voice; better the raw take than silence
    return voice


def _last_word_end(samples, sr, text):
    """Seconds at which the text's last words were spoken, from a transcript of the take."""
    if _asr is None:
        return None
    import re
    import unicodedata
    from difflib import SequenceMatcher

    import torch
    import torchaudio.functional as AF

    audio = AF.resample(torch.from_numpy(samples).float(), sr, 16000).numpy()
    # Greedy and without temperature fallback: fast, and the same take always gets the same cut.
    segs, _ = _asr.transcribe(audio, language="ko", beam_size=1, temperature=0.0, word_timestamps=True, condition_on_previous_text=False)
    words = [w for s in segs for w in s.words]
    # Compare jamo rather than syllables: dialect is often heard slightly off (죽겄네/주건데, 요/여).
    letters = lambda s: re.sub(r"[\W_]", "", unicodedata.normalize("NFD", s))
    target, heard, owner = letters(text), "", []
    for k, w in enumerate(words):
        t = letters(w.word)
        heard += t
        owner += [k] * len(t)
    blocks = [b for b in SequenceMatcher(None, target, heard, autojunk=False).get_matching_blocks() if b.size]
    # Allow about a syllable to go unheard, but leave the take whole when the end of the text was not recognised.
    if not blocks or blocks[-1].a + blocks[-1].size < min(0.85 * len(target), len(target) - 3):
        return None
    return words[owner[blocks[-1].b + blocks[-1].size - 1]].end


def _cut_after_text(samples, sr, text):
    """Cut what Chatterbox adds once the sentence is done.

    It sometimes keeps going after the last word (a laugh, a breath, a stray "안녕!"),
    which sounds like someone talking in the background between sentences. The take is
    cut at the first pause after the last word, then the quiet tail is trimmed.
    """
    import numpy as np

    hop = sr // 50  # 20 ms frames
    rms = np.sqrt([np.mean(samples[i : i + hop] ** 2) for i in range(0, len(samples), hop)])
    quiet = rms < rms.max() * 10 ** (-30 / 20)
    end = len(samples)
    t_end = _last_word_end(samples, sr, text)
    if t_end is not None:
        for i in range(max(0, int((t_end - 0.1) * 50)), len(quiet) - 8):
            if quiet[i : i + 8].all():  # a 160 ms pause
                end = (i + 4) * hop
                break
    loud = np.nonzero(rms[: -(-end // hop)] > rms.max() * 10 ** (-35 / 20))[0]
    if len(loud):
        end = min(end, (loud[-1] + 1) * hop + sr * 12 // 100)
    out = samples[:end].copy()
    fade = min(len(out), sr * 3 // 100)
    out[-fade:] *= np.linspace(1.0, 0.0, fade)
    return out


def _attention_hooks():
    return [layer.self_attn._forward_hooks for layer in _model.t3.tfmr.layers]


_conds = {}  # (ref, mtime, exaggeration) -> Chatterbox conditionals


def speak(req):
    import torch

    # Process each voice's reference clip once: re-reading it for every sentence grew the GPU
    # heap by ~1 GB per sentence on MPS, while reusing the conditionals keeps it flat.
    key = (req["ref"], os.path.getmtime(req["ref"]), req["params"]["exaggeration"])
    if key not in _conds:
        _model.prepare_conditionals(req["ref"], exaggeration=req["params"]["exaggeration"])
        _conds[key] = _model.conds
    _model.conds = _conds[key]
    before = [set(h) for h in _attention_hooks()]
    try:
        wav = _model.generate(req["text"], language_id="ko", **req["params"])
    finally:
        # Chatterbox 0.1.7 adds attention hooks on every generate() and never removes them; left in
        # place they keep each sentence's attention maps alive (~1 GB more per sentence) and slow
        # every later sentence down.
        for hooks, keep in zip(_attention_hooks(), before):
            for k in [k for k in hooks if k not in keep]:
                del hooks[k]
    samples = _cut_after_text(_voice_only(wav.squeeze().detach().cpu().numpy(), _model.sr), _model.sr, req["text"])
    data = _wav_bytes(samples, _model.sr)
    if torch.backends.mps.is_available():
        torch.mps.empty_cache()  # hand back the sentence's working memory between requests
    return {"wav": base64.b64encode(data).decode()}


def _bind():
    """Listen on SOCK, or return None when another worker already does."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(SOCK)
        return None
    except OSError:
        pass
    finally:
        probe.close()
    try:
        os.unlink(SOCK)  # left behind by a worker that did not exit cleanly
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(16)
    return srv


def main():
    srv = _bind()
    if srv is None:
        log("another worker is already running")
        return
    log("worker", os.getpid(), "listening; exits after", IDLE / 60, "idle minutes")
    load_error = None
    try:
        _load()
    except Exception as e:
        load_error = str(e)
        log(traceback.format_exc())
    srv.settimeout(IDLE)
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                log("idle, exiting")
                break
            with conn:
                conn.settimeout(None)
                try:
                    req = json.loads(conn.makefile("r", encoding="utf-8").readline())
                    if load_error:
                        out = {"error": "model", "message": f"음성 모델을 불러오지 못했어요: {load_error}"}
                    else:
                        out = speak(req)
                except Exception as e:
                    log(traceback.format_exc())
                    out = {"error": "internal", "message": str(e)}
                try:
                    conn.sendall((json.dumps(out) + "\n").encode())
                except OSError:
                    pass  # the server gave up waiting
            if _footprint_mb() > MAX_MB:
                log(f"using {_footprint_mb():.0f} MB, over the {MAX_MB:.0f} MB limit; exiting so the next sentence starts fresh")
                break
    finally:
        # Unlink first: a server arriving now then starts a fresh worker instead of reaching this one.
        try:
            os.unlink(SOCK)
        except FileNotFoundError:
            pass
        srv.close()


if __name__ == "__main__":
    main()

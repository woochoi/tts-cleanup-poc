"""Read Korean text in a cloned voice and cut the junk Chatterbox adds after the last word.

    python src/clean_tts.py --ref refs/my_voice.wav --text "안녕하세요." --out out.wav
    python src/clean_tts.py --ref refs/my_voice.wav --text "..." --out out.wav --keep-stages

Pipeline (the same code the local voice server uses, see worker.py):
  1. Chatterbox Multilingual  - voice-cloned speech from the reference clip
  2. Demucs htdemucs          - keep only the vocals stem (drops music/noise bursts)
  3. faster-whisper small     - find when the script's last word was spoken
  4. cut at the first 160 ms pause after it, trim the quiet tail, 30 ms fade
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import worker  # noqa: E402

PARAMS = {"exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8}


def load():
    worker._load()
    if worker._sep is None or worker._asr is None:
        sys.exit("Demucs or faster-whisper failed to load; see the log above.")


def generate(text, ref, seed=0, params=PARAMS):
    """Return (raw, voice_only, cleaned, sr, timings) for one sentence."""
    import torch

    m = worker._model
    key = (ref, os.path.getmtime(ref), params["exaggeration"])
    if key not in worker._conds:
        m.prepare_conditionals(ref, exaggeration=params["exaggeration"])
        worker._conds[key] = m.conds
    m.conds = worker._conds[key]

    torch.manual_seed(seed)
    t0 = time.perf_counter()
    before = [set(h) for h in worker._attention_hooks()]
    try:
        wav = m.generate(text, language_id="ko", **params)
    finally:
        for hooks, keep in zip(worker._attention_hooks(), before):
            for k in [k for k in hooks if k not in keep]:
                del hooks[k]
    raw = wav.squeeze().detach().cpu().numpy()
    t1 = time.perf_counter()
    voice = worker._voice_only(raw, m.sr)
    t2 = time.perf_counter()
    clean = worker._cut_after_text(voice, m.sr, text)
    t3 = time.perf_counter()
    if torch.backends.mps.is_available():
        torch.mps.empty_cache()
    return raw, voice, clean, m.sr, {"generate_s": t1 - t0, "demucs_s": t2 - t1, "cut_s": t3 - t2}


def save(path, samples, sr):
    with open(path, "wb") as f:
        f.write(worker._wav_bytes(samples, sr))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", required=True, help="reference voice clip (10-15 s, clean speech)")
    ap.add_argument("--text", required=True, help="Korean text to read")
    ap.add_argument("--out", required=True, help="output .wav")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--keep-stages", action="store_true", help="also save <out>.raw.wav and <out>.voice.wav")
    a = ap.parse_args()

    load()
    raw, voice, clean, sr, t = generate(a.text, os.path.abspath(a.ref), a.seed)
    save(a.out, clean, sr)
    if a.keep_stages:
        base = os.path.splitext(a.out)[0]
        save(base + ".raw.wav", raw, sr)
        save(base + ".voice.wav", voice, sr)
    print(f"{a.out}: {len(raw) / sr:.2f}s -> {len(clean) / sr:.2f}s "
          f"(generate {t['generate_s']:.1f}s, demucs {t['demucs_s']:.1f}s, cut {t['cut_s']:.1f}s)")


if __name__ == "__main__":
    main()

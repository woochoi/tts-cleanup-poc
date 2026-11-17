"""Generate every sentence in sentences.tsv once and compare the raw take with the cleaned one.

    python eval/run_eval.py --ref refs/my_voice.wav            # all 20
    python eval/run_eval.py --ref refs/my_voice.wav --only s01  # one sentence

Writes eval/out/<id>.raw.wav, <id>.voice.wav, <id>.clean.wav and eval/results.csv.
Automatic checks (a human listening check goes in eval/listening.csv):
  - tail_raw / tail_clean: what Whisper hears after the script's last word (extra speech)
  - tail_db_raw / tail_db_clean: loudness after the last word, relative to the speech (noise)
  - cer_clean: character error rate of the cleaned take vs the script (words cut off)
"""

import argparse
import csv
import os
import re
import sys
import time
import unicodedata
from difflib import SequenceMatcher

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
import clean_tts  # noqa: E402
import worker  # noqa: E402


def letters(s):
    return re.sub(r"[\W_]", "", s)


def transcribe(samples, sr):
    import torch
    import torchaudio.functional as AF

    audio = AF.resample(torch.from_numpy(samples).float(), sr, 16000).numpy()
    # Evaluation uses beam search (stricter than the greedy pass inside the pipeline).
    segs, _ = worker._asr.transcribe(audio, language="ko", beam_size=5, temperature=0.0,
                                     word_timestamps=True, condition_on_previous_text=False)
    words = [w for s in segs for w in s.words]
    return "".join(w.word for w in words).strip(), words


def split_tail(script, words):
    """(text heard after the script's end, time the script's last word ended)."""
    nfd = lambda s: letters(unicodedata.normalize("NFD", s))
    target, heard, owner = nfd(script), "", []
    for k, w in enumerate(words):
        t = nfd(w.word)
        heard += t
        owner += [k] * len(t)
    blocks = [b for b in SequenceMatcher(None, target, heard, autojunk=False).get_matching_blocks() if b.size]
    if not blocks:
        return "".join(w.word for w in words).strip(), 0.0
    last = owner[blocks[-1].b + blocks[-1].size - 1]
    tail = "".join(w.word for w in words[last + 1:]).strip()
    return tail, words[last].end


def tail_db(samples, sr, t_end):
    """Loudness after the last word, in dB relative to the loudest 20 ms of speech."""
    hop = sr // 50
    rms = np.sqrt([np.mean(samples[i:i + hop] ** 2) + 1e-12 for i in range(0, len(samples) - hop + 1, hop)])
    after = rms[int((t_end + 0.2) * 50):]
    if len(after) == 0:
        return None
    return float(20 * np.log10(after.max() / rms.max()))


def cer(ref, hyp):
    r, h = letters(ref), letters(hyp)
    d = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hc in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rc != hc))
    return d[len(h)] / max(1, len(r))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    rows = list(csv.DictReader(open(os.path.join(HERE, "sentences.tsv"), encoding="utf-8"), delimiter="\t"))
    if a.only:
        rows = [r for r in rows if r["id"] in a.only]
    out_dir = os.path.join(HERE, "out")
    os.makedirs(out_dir, exist_ok=True)

    t0 = time.perf_counter()
    clean_tts.load()
    print(f"models loaded in {time.perf_counter() - t0:.0f}s", flush=True)

    results = []
    for k, r in enumerate(rows):
        raw, voice, clean, sr, t = clean_tts.generate(r["text"], os.path.abspath(a.ref), seed=a.seed + k)
        for name, x in (("raw", raw), ("voice", voice), ("clean", clean)):
            clean_tts.save(os.path.join(out_dir, f"{r['id']}.{name}.wav"), x, sr)
        hyp_raw, w_raw = transcribe(raw, sr)
        hyp_clean, w_clean = transcribe(clean, sr)
        tail_raw, end_raw = split_tail(r["text"], w_raw)
        tail_clean, end_clean = split_tail(r["text"], w_clean)
        row = {
            "id": r["id"], "kind": r["kind"], "text": r["text"],
            "dur_raw_s": round(len(raw) / sr, 2), "dur_clean_s": round(len(clean) / sr, 2),
            "removed_s": round((len(raw) - len(clean)) / sr, 2),
            "heard_raw": hyp_raw, "heard_clean": hyp_clean,
            "tail_raw": tail_raw, "tail_clean": tail_clean,
            "tail_db_raw": None if (d := tail_db(raw, sr, end_raw)) is None else round(d, 1),
            "tail_db_clean": None if (d := tail_db(clean, sr, end_clean)) is None else round(d, 1),
            "cer_raw": round(cer(r["text"], hyp_raw), 3), "cer_clean": round(cer(r["text"], hyp_clean), 3),
            **{k2: round(v, 2) for k2, v in t.items()},
        }
        results.append(row)
        print(f"{r['id']} {row['dur_raw_s']}s->{row['dur_clean_s']}s tail_raw='{tail_raw}' tail_clean='{tail_clean}' "
              f"cer {row['cer_raw']}->{row['cer_clean']} gen {t['generate_s']:.1f}s", flush=True)

    path = os.path.join(HERE, "results.csv" if not a.only else "results.partial.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0]))
        w.writeheader()
        w.writerows(results)
    print("wrote", path)


if __name__ == "__main__":
    main()

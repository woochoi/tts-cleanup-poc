"""Make eval/listening.csv from results.csv for the human listening check.

Listen to eval/out/<id>.raw.wav and <id>.clean.wav and fill in:
  raw_tail_junk   Y if the raw take has anything after the last word (noise, laugh, extra words)
  clean_tail_junk Y if the cleaned take still has it
  clean_word_cut  Y if the cleaned take lost part of a word
  manual_edit_s   (optional) seconds it took you to trim the raw take by hand in an audio editor
"""

import csv
import os

HERE = os.path.dirname(os.path.abspath(__file__))
rows = list(csv.DictReader(open(os.path.join(HERE, "results.csv"), encoding="utf-8-sig")))
path = os.path.join(HERE, "listening.csv")
if os.path.exists(path):
    raise SystemExit(f"{path} already exists; not overwriting your answers")
with open(path, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["id", "kind", "text", "raw_tail_junk", "clean_tail_junk", "clean_word_cut", "manual_edit_s", "note"])
    for r in rows:
        w.writerow([r["id"], r["kind"], r["text"], "", "", "", "", ""])
print("wrote", path)

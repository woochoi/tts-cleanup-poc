"""Local listening page for the evaluation: play raw vs cleaned takes and record judgments.

    python eval/listen_server.py        # then open http://127.0.0.1:8770/

Serves eval/ on 127.0.0.1 only. Judgments are written to eval/listening.csv on every change.
"""

import csv
import http.server
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SHEET = os.path.join(HERE, "listening.csv")
FIELDS = ["id", "kind", "text", "raw_tail_junk", "clean_tail_junk", "clean_word_cut", "manual_edit_s", "note"]
PORT = int(os.environ.get("PORT", "8770"))


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


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
        if self.path in ("/", "/index.html"):
            self.path = "/listen.html"
        if self.path == "/api/data":
            return self._json({"results": read_csv(os.path.join(HERE, "results.csv")), "sheet": read_csv(SHEET)})
        return super().do_GET()

    def do_POST(self):
        if self.path != "/api/save":
            return self._json({"error": "not found"}, 404)
        rows = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        tmp = SHEET + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, SHEET)
        return self._json({"ok": True, "saved": len(rows)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"listening page: http://127.0.0.1:{PORT}/", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

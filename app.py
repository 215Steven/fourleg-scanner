#!/usr/bin/env python3
"""
四腿外站票掃描器：本機網頁版
執行：python app.py  → 自動開啟 http://127.0.0.1:8765
只用 Python 內建模組，額外只需要 pip install fast-flights
"""
from __future__ import annotations

import json
import threading
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fourleg_scanner as fs

HERE = Path(__file__).resolve().parent
PORT = 8765


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.reset()

    def reset(self):
        self.rows: list[dict] = []
        self.done = 0
        self.total = 0
        self.state = "idle"     # idle / running / finished / error
        self.message = ""
        self.report = ""
        self.rev = 0          # 已送出的列被更新（補上台北來回）時加 1，網頁據此重抓全部

    def on_row(self, row, n, total):
        with self.lock:
            self.done, self.total = n, total
            if row is not None:
                self.rows.append(asdict(row))

    def on_update(self, row, n, total):
        with self.lock:
            self.done, self.total = n, total
            d = asdict(row)
            key = (d["airline"], d["outstation"], d["dest"], d["depart"])
            for i, x in enumerate(self.rows):
                if (x["airline"], x["outstation"], x["dest"], x["depart"]) == key:
                    self.rows[i] = d
                    break
            self.rev += 1

    def run(self, cfg: dict, offline: bool):
        try:
            rows = fs.scan(cfg, offline, None, None, on_row=self.on_row, stop_event=self.stop,
                           on_update=self.on_update)
            if rows:
                _, html_path = fs.write_outputs(rows, cfg, offline)
                self.report = str(html_path)
            with self.lock:
                self.state = "finished"
                self.message = fs.LAST_STOP_REASON or ("已停止" if self.stop.is_set() else "完成")
                self.message += f" · 本次查詢 {fs.STATS.get('calls', 0)} 次"
                if fs.STATS.get("coarse_saved"):
                    self.message += f"（快速模式少查 {fs.STATS['coarse_saved']} 組）"
                if fs.CAL_CHECKS:
                    self.message += " · 月曆價核對：" + "、".join(fs.CAL_CHECKS)
                if fs.FIXED_PICKS:
                    self.message += " · 固定段改用前後最便宜的一天：" + "、".join(fs.FIXED_PICKS)
        except Exception as e:
            with self.lock:
                self.state = "error"
                self.message = f"{type(e).__name__}: {e}"

    def start(self, cfg: dict, offline: bool) -> str | None:
        if self.thread and self.thread.is_alive():
            return "已有掃描進行中"
        self.reset()
        self.stop.clear()
        self.state = "running"
        self.thread = threading.Thread(target=self.run, args=(cfg, offline), daemon=True)
        self.thread.start()
        return None

    def snapshot(self, since: int, asince: int = 0, rev: int = -1) -> dict:
        with self.lock:
            reset = rev != self.rev
            if reset:
                since = 0
            return {"state": self.state, "done": self.done, "total": self.total,
                    "message": self.message, "report": self.report,
                    "rows": self.rows[since:], "count": len(self.rows),
                    "fixed_picks": list(fs.FIXED_PICKS), "calls": fs.STATS.get("calls", 0), "rev": self.rev, "reset": reset,
                    "activity": [a for a in list(fs.ACTIVITY) if a["i"] > asince][-150:]}


JOB = Job()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/config":
            self.send_json(json.loads((HERE / "config.json").read_text(encoding="utf-8")))
        elif path == "/api/status":
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            def num(k):
                try:
                    return int(qs.get(k, ["0"])[0])
                except ValueError:
                    return 0
            self.send_json(JOB.snapshot(num("since"), num("asince"), num("rev") if "rev" in qs else -1))
        elif path == "/api/routes":
            try:
                self.send_json(json.loads((HERE / "routes.json").read_text(encoding="utf-8")))
            except FileNotFoundError:
                self.send_json({})
        elif path == "/api/history":
            self.send_json(fs.load_history(HERE / "output" / "history.csv"))
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/scan":
            cfg = data.get("config", {})
            if data.get("save"):
                (HERE / "config.json").write_text(
                    json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            err = JOB.start(cfg, bool(data.get("dry_run")))
            self.send_json({"ok": err is None, "error": err})
        elif self.path == "/api/stop":
            JOB.stop.set()
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "not found"}, 404)


def main():
    if fs.create_query is None:
        raise SystemExit("請先安裝：pip install fast-flights")
    (HERE / "output").mkdir(exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"四腿外站票掃描器已啟動：{url}（Ctrl+C 結束）")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

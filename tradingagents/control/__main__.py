"""python -m tradingagents.control --config control.toml serve  (localhost only)."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app import ControlApp, load_config


def make_handler(app: ControlApp):
    class Handler(BaseHTTPRequestHandler):
        def _go(self, method: str):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(n, 4096)).decode("utf-8", "replace") if n else ""
            code, ctype, out = app.handle(method, self.path, dict(self.headers), body)
            data = out.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

        def log_message(self, *a):  # request lines are in the audit log, not stderr
            pass

    return Handler


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m tradingagents.control")
    p.add_argument("--config", required=True)
    p.add_argument("cmd", choices=["serve"])
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    srv = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(ControlApp(cfg)))
    print(f"control page on http://{cfg.host}:{cfg.port} (put Tailscale Serve in front)", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

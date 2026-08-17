#!/usr/bin/env python3
"""Serve the live dashboard over a set of session transcripts.

    ./serve-mesh.py ~/.claude/projects/<slug>/*.jsonl
    ./serve-mesh.py --watch ~/.claude/projects/<slug>   # every .jsonl in a dir

Stdlib only, binds to localhost. Polling rather than SSE: the page fetches
/feed?since=<seq> on a timer, which means the static page is the same page with
the data inlined and the cursor never advancing. One renderer, two data sources.

The wire is mostly-append. Deltas carry new events; when the model revises
something it already emitted — two-pass pairing merging an unpaired outbound and
an unpaired inbound into one message, or a rename changing a resolved recipient —
the snapshot_id changes and the client refetches. That avoids inventing a
tombstone protocol for a dataset small enough to just resend.
"""
import argparse
import glob
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from chatter import render as R  # noqa: E402


class State:
    """Holds the current snapshot. Rebuilt on a timer by one background thread;
    served to any number of clients."""

    def __init__(self, paths, watch_dir, refresh):
        self.paths, self.watch_dir, self.refresh = paths, watch_dir, refresh
        self.lock = threading.Lock()
        self.data = {"events": [], "sessions": {}, "sources": [],
                     "snapshot_id": None, "seq": 0}
        self.model = self._load_model()
        self.err = None

    @staticmethod
    def _load_model():
        try:
            from chatter import model
        except ImportError:
            sys.exit("chatter/model.py not found — the parser half is not installed yet.")
        return model

    def sources(self):
        if self.watch_dir:
            return sorted(glob.glob(os.path.join(self.watch_dir, "*.jsonl")))
        return [p for p in self.paths if os.path.exists(p)]

    def rebuild(self):
        try:
            data = self.model.build(self.sources())
            with self.lock:
                self.data, self.err = data, None
        except Exception as exc:                       # keep serving the last good snapshot
            with self.lock:
                self.err = f"{type(exc).__name__}: {exc}"

    def loop(self):
        while True:
            self.rebuild()
            time.sleep(self.refresh)

    def since(self, seq):
        """Events after `seq`, plus the always-current session table. seq is
        monotonic in DISCOVERY order, not time, so a transcript added later never
        invalidates a cursor already issued — the page sorts by clock for display
        and tolerates an event arriving older than everything on screen."""
        with self.lock:
            d = self.data
            events = [e for e in d["events"] if e.get("seq", 0) > seq] if seq >= 0 else d["events"]
            # exchanges are recomputed whole on every rebuild, so they ride along
            # with every delta rather than being diffed — they describe the
            # conversation, and a conversation changes shape as it grows.
            return {"events": events, "sessions": d["sessions"], "sources": d["sources"],
                    "exchanges": d.get("exchanges", []),
                    "exchange_gap_s": d.get("exchange_gap_s"),
                    "snapshot_id": d["snapshot_id"], "seq": d.get("seq", 0),
                    "error": self.err}


def handler_for(state, page):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            # The page is self-contained, so lock it down to match.
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            if u.path == "/feed":
                q = parse_qs(u.query)
                try:
                    seq = int(q.get("since", ["-1"])[0])
                except ValueError:
                    seq = -1
                return self._send(200, json.dumps(state.since(seq), ensure_ascii=False),
                                  "application/json; charset=utf-8")
            self._send(404, "not found", "text/plain; charset=utf-8")

        def log_message(self, *a):                     # quiet; this runs in a terminal
            pass
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcripts", nargs="*")
    ap.add_argument("--watch", metavar="DIR",
                    help="serve every .jsonl in DIR, picking up sessions that start later")
    ap.add_argument("-p", "--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1",
                    help="default localhost; transcripts contain everything ever "
                         "pasted into a session, so do not bind this publicly")
    ap.add_argument("--refresh", type=float, default=2.0, help="seconds between rebuilds")
    ap.add_argument("--poll", type=int, default=2000, help="client poll interval, ms")
    ap.add_argument("--silence", type=int, default=600,
                    help="seconds before a quiet session raises an alert")
    ap.add_argument("--findings", metavar="FILE",
                    help="curated findings JSON; see README. Never generated.")
    ap.add_argument("--title", default="Agent mesh")
    args = ap.parse_args()

    if not args.transcripts and not args.watch:
        sys.exit("give transcript paths or --watch DIR")

    findings = []
    if args.findings:
        with open(args.findings, encoding="utf-8") as fh:
            findings = json.load(fh)

    state = State(args.transcripts, args.watch, args.refresh)
    state.rebuild()
    threading.Thread(target=state.loop, daemon=True).start()

    page = R.render(None, title=args.title, feed="/feed", findings=findings,
                    poll_ms=args.poll, silence_s=args.silence)
    srv = ThreadingHTTPServer((args.host, args.port), handler_for(state, page))
    n = len(state.sources())
    # flush: when this is backgrounded with output redirected, stdout is a pipe
    # and block-buffered, so the banner never appears and it looks like nothing
    # started — even though the socket is open and serving.
    print(f"agent-chatter live on http://{args.host}:{args.port}  "
          f"({n} transcript{'s' if n != 1 else ''}, rebuilding every {args.refresh}s)",
          flush=True)
    print("Ctrl-C to stop.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()

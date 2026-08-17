#!/usr/bin/env python3
"""Serve the live dashboard over a set of session transcripts.

    agent-chatter
    agent-chatter --watch <dir>          # every .jsonl in a directory

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
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import render as R
from . import summarize as S


def _progress(msg):
    # flush, for the same reason the startup banner does: backgrounded with
    # output redirected, stdout is a block-buffered pipe and nothing appears.
    print(f"agent-chatter: {msg}", file=sys.stderr, flush=True)


class State:
    """Holds the current snapshot. Rebuilt on a timer by one background thread;
    served to any number of clients."""

    def __init__(self, paths, watch_dir, refresh, summarizer=None):
        self.paths, self.watch_dir, self.refresh = paths, watch_dir, refresh
        self.summarizer = summarizer
        self.lock = threading.Lock()
        self.data = {"events": [], "sessions": {}, "sources": [],
                     "snapshot_id": None, "seq": 0}
        self.model = self._load_model()
        self.err = None
        self.summarizing = False
        self.pending = 0

    @staticmethod
    def _load_model():
        try:
            from . import model
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
            # Cache only, so the rebuild that runs every two seconds stays free
            # in both senses. Anything not summarised yet is picked up by the
            # background pass below and lands on a later rebuild.
            pending = 0
            if self.summarizer:
                self.summarizer.attach(data)
                if self.summarizer.ready:
                    # The count the button shows has to be the server's, not
                    # `total - done` worked out in the page: pending() also drops
                    # conversations inside the settle window and anything past
                    # the ceiling, so the page's subtraction can offer work that
                    # comes back "nothing". Also refreshes .skipped for the note.
                    pending = len(self.summarizer.pending(data))
            with self.lock:
                self.data, self.err, self.pending = data, None, pending
        except Exception as exc:                       # keep serving the last good snapshot
            with self.lock:
                self.err = f"{type(exc).__name__}: {exc}"

    def loop(self):
        while True:
            self.rebuild()
            time.sleep(self.refresh)

    def summarize_now(self):
        """Run one fill on a worker thread. Returns what to tell the caller.

        Never blocks the request: an API call takes seconds and a browser fetch
        that hangs for that long looks broken. The results land in the cache, the
        next rebuild attaches them, and the page's existing poll picks them up —
        the same path the --summarize background loop already uses.
        """
        with self.lock:
            if self.summarizing:
                return {"status": "busy"}
            data = self.data
            todo = self.summarizer.pending(data)
            if not todo:
                return {"status": "nothing"}
            self.summarizing = True
            chars, tokens, usd = self.summarizer.estimate(todo)

        def run():
            try:
                self.summarizer.fill(data, progress=_progress)
            except Exception as exc:
                _progress(f"summariser: {type(exc).__name__}: {exc}")
            finally:
                with self.lock:
                    self.summarizing = False
                self.rebuild()          # publish immediately rather than waiting
        threading.Thread(target=run, daemon=True).start()
        return {"status": "started", "n": len(todo), "tokens": tokens,
                "usd": round(usd, 2)}

    def summarize_loop(self):
        """Generate summaries off the serving path.

        Doing this inside rebuild() would hold the first snapshot back by however
        long the API takes — the page would show "no messages yet" while the
        transcripts sat parsed in memory. Instead the summaries land in the cache
        and the next rebuild attaches them, so the dashboard is useful
        immediately and gets better a few seconds later. Exchanges ride along
        with every delta, so no snapshot bump is needed for the client to see
        them.
        """
        while True:
            try:
                with self.lock:
                    data = self.data
                if data.get("exchanges"):
                    self.summarizer.fill(data, progress=_progress)
            except Exception as exc:
                _progress(f"summariser: {type(exc).__name__}: {exc}")
            time.sleep(max(self.refresh * 5, 10))

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
                    "summarizing": self.summarizing,
                    "pending": self.pending,
                    "error": self.err}


def handler_for(state, page, token):
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

        def do_POST(self):
            """The one endpoint that spends money.

            Guarded by a per-process token that only the page we served carries.
            The server binds localhost, but localhost is reachable by every page
            in the user's browser, and a cross-origin script can POST here even
            though it cannot read the response. Without the token, any page they
            happened to have open could run up a bill on their key. Same-origin
            policy stops that page ever seeing the token, so requiring it is
            enough — no CORS headers are sent, deliberately.
            """
            if urlparse(self.path).path != "/summarize":
                return self._send(404, "not found", "text/plain; charset=utf-8")
            if not token or self.headers.get("X-Chatter-Token") != token:
                return self._send(403, json.dumps({"status": "forbidden"}),
                                  "application/json; charset=utf-8")
            if not (state.summarizer and state.summarizer.ready):
                return self._send(409, json.dumps({"status": "not-configured"}),
                                  "application/json; charset=utf-8")
            out = state.summarize_now()
            self._send(200, json.dumps(out), "application/json; charset=utf-8")

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
    ap.add_argument("--title", default=None,
                    help="page heading and browser title. Defaults to the "
                         "project name when every transcript came from one, "
                         f"else {R.DEFAULT_TITLE!r}.")
    S.add_arguments(ap)
    args = ap.parse_args()

    if not args.transcripts and not args.watch:
        sys.exit("give transcript paths or --watch DIR")

    findings = []
    if args.findings:
        with open(args.findings, encoding="utf-8") as fh:
            findings = json.load(fh)

    summarizer = S.from_args(args)
    S.report(summarizer)

    state = State(args.transcripts, args.watch, args.refresh, summarizer)
    state.rebuild()
    threading.Thread(target=state.loop, daemon=True).start()
    # Only --summarize generates unasked. Without it the key is still resolved,
    # the button is offered, and nothing is spent until it is pressed.
    if summarizer.ready and summarizer.auto:
        threading.Thread(target=state.summarize_loop, daemon=True).start()

    # state.rebuild() has already run, so the hint reflects what was actually
    # loaded rather than what was asked for on the command line.
    title = args.title or state.data.get("title_hint") or R.DEFAULT_TITLE
    got = any(x.get("ai") for x in state.data.get("exchanges") or [])
    if summarizer.ready and summarizer.auto:
        # Prime summarizer.skipped so the page can say what the cap left out. The
        # page is rendered once, at startup, and generation happens later on the
        # background thread — without this the note would always report zero.
        # Costs nothing: pending() only builds strings and reads the cache.
        summarizer.pending(state.data)
    token = secrets.token_urlsafe(24)
    page = R.render(None, title=title, feed="/feed", findings=findings,
                    poll_ms=args.poll, silence_s=args.silence,
                    summarize=S.panel_config(summarizer, got, live=True, token=token))
    srv = ThreadingHTTPServer((args.host, args.port), handler_for(state, page, token))
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

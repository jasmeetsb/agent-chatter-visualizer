#!/usr/bin/env python3
"""Render a frozen, publishable page from one or more session transcripts.

    ./build-mesh-page.py ~/.claude/projects/<slug>/*.jsonl -o mesh.html

Self-contained: no external fonts, scripts or images, so it works from disk and
survives a strict CSP. Carries no attention panel — alerts are live state, and a
published artifact must not assert which exchange mattered.

Same renderer as serve-mesh.py, with the data inlined instead of polled. That is
deliberate: two view codebases drift, and the failure is silent.
"""
import argparse
import json
import os
import sys

from . import render as R


def load_model():
    """chatter/model.py owns parsing. Fall back with a clear error rather than a
    traceback, since the two halves of this tool land independently."""
    try:
        from . import model
    except ImportError:
        sys.exit("chatter/model.py not found — the parser half is not installed yet.")
    return model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcripts", nargs="+")
    ap.add_argument("-o", "--out", default="mesh.html")
    ap.add_argument("--title", default="Agent mesh")
    ap.add_argument("--heading", help="page heading, if different from --title")
    ap.add_argument("--subtitle", default=R.DEFAULT_SUBTITLE)
    ap.add_argument("--findings", metavar="FILE",
                    help="JSON file of curated findings. Never generated — a "
                         "heuristic can tag a message but cannot decide which "
                         "exchange mattered. Omit and the section is hidden.")
    ap.add_argument("--attention", action="store_true",
                    help="keep the attention panel (off by default: a frozen page "
                         "should not carry live alerts)")
    args = ap.parse_args()

    findings = []
    if args.findings:
        with open(args.findings, encoding="utf-8") as fh:
            findings = json.load(fh)

    paths = [t for t in args.transcripts if os.path.exists(t)]
    for t in args.transcripts:
        if t not in paths:
            print(f"skip (missing): {t}", file=sys.stderr)
    if not paths:
        sys.exit("no readable transcripts")

    data = load_model().build(paths)
    if not data["events"]:
        sys.exit("no messages found")

    page = R.render(data, title=args.title, heading=args.heading,
                    subtitle=args.subtitle, feed=None, findings=findings)
    if not args.attention:
        page = R.strip_attention(page)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"{args.out}: {len(data['events'])} messages, "
          f"{len(data['sessions'])} sessions, {len(findings)} findings, "
          f"{len(page)//1024} KB")


if __name__ == "__main__":
    main()

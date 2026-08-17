#!/usr/bin/env python3
"""One command. Run it with no arguments and it finds your transcripts and serves.

    ./agent-chatter                 live dashboard of every conversation found
    ./agent-chatter --list          what it found, without starting anything
    ./agent-chatter --demo          the bundled example, no real data touched
    ./agent-chatter --build p.html  frozen self-contained page
    ./agent-chatter --log p.md      plain markdown log
    ./agent-chatter <paths...>      explicit transcripts, skipping discovery

Everything here is a thin front door onto chatter.server, chatter.page and
chatter.mdlog, which can also be used directly. The reason it exists is
that all three required paths, and a newcomer has no way to know that Claude Code
stores transcripts under ~/.claude/projects/<flattened-project-path>/<uuid>.jsonl
or which of those UUIDs ever spoke to a peer. Requiring that knowledge up front
is the difference between a tool someone tries and a tool someone reads about.

Standard library only, like the rest of the repo. Nothing to install.
"""
import argparse
import glob
import os
import sys

from . import discover, mdlog, page, server

HERE = os.path.dirname(os.path.abspath(__file__))
# The demo fixture ships inside the package, so it is found whether this was
# cloned or installed into a virtualenv by uv/pip.
DEMO = sorted(glob.glob(os.path.join(HERE, "examples", "mesh", "*.jsonl")))
SAMPLE_FINDINGS = os.path.join(HERE, "examples", "sample-findings.json")


def resolve(args):
    """Transcript paths: explicit, else the demo, else discovered."""
    if args.watch:
        return [], "watching " + args.watch
    if args.paths:
        return args.paths, "given on the command line"
    if args.demo:
        # Ship the sample findings with the demo. Without them a first run shows
        # an empty curated tier, and the distinction between "a person judged
        # this" and "a participant said this" — the whole point of the insights
        # panel — is invisible exactly when someone is deciding whether to care.
        if not args.findings:
            if os.path.exists(SAMPLE_FINDINGS):
                args.findings = SAMPLE_FINDINGS
        return DEMO, "the bundled example"
    try:
        rows = discover.find(project=args.project)
    except ValueError as exc:
        sys.exit(str(exc))
    if not rows:
        print(discover.describe(rows), file=sys.stderr)
        sys.exit(1)
    return [r[0] for r in rows], f"{len(rows)} discovered transcript(s)"


def main():
    ap = argparse.ArgumentParser(
        description="Dashboard for conversations between Claude Code sessions.",
        epilog="With no arguments it finds your transcripts and serves them.\n\n"
               "EXPERIMENTAL: this reads an undocumented transcript format that "
               "can change without notice. It only ever reads your transcripts, "
               "never writes to them, but output is scrubbed on a best-effort "
               "basis — check anything before you share it. No warranty.")
    ap.add_argument("paths", nargs="*", help="transcripts (default: discover them)")
    ap.add_argument("--list", action="store_true", help="show what was found and exit")
    ap.add_argument("--demo", action="store_true", help="use the bundled example")
    ap.add_argument("--project", metavar="NAME",
                    help="only this project: a path to it, its folder name, "
                         "or any distinctive part of the name")
    ap.add_argument("--watch", metavar="DIR",
                    help="serve every transcript in DIR, re-reading as files appear "
                         "— the cross-machine workflow, where you rsync both sides "
                         "into one directory")
    ap.add_argument("--build", metavar="FILE", help="write a self-contained page")
    ap.add_argument("--log", metavar="FILE", help="write a markdown log")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--title")
    ap.add_argument("--findings", metavar="FILE")
    args, extra = ap.parse_known_args()

    if args.list:
        try:
            rows = discover.find(project=args.project)
        except ValueError as exc:
            sys.exit(str(exc))
        print(discover.describe(rows))
        return

    paths, why = resolve(args)
    print(f"agent-chatter: {why}", file=sys.stderr)

    def run(mod, *rest):
        """Call the sibling command in-process.

        It used to spawn `python <script>.py`, which works from a clone and
        breaks the moment pip installs the package — there is no sibling file to
        point at. Swapping argv and calling main() keeps one implementation for
        both.
        """
        argv = list(paths) + list(rest)
        if args.watch:
            argv += ["--watch", args.watch]
        if args.title:
            argv += ["--title", args.title]
        if args.findings and mod is not mdlog:
            argv += ["--findings", args.findings]
        argv += extra
        saved = sys.argv
        sys.argv = [mod.__name__] + argv
        try:
            mod.main()
            return 0
        except SystemExit as exc:
            return exc.code or 0
        finally:
            sys.argv = saved

    if args.watch and (args.build or args.log):
        import glob as _g
        paths = sorted(_g.glob(os.path.join(args.watch, "*.jsonl")))
        args.watch = None
        if not paths:
            sys.exit(f"no .jsonl transcripts in {args.watch}")
    if args.build:
        sys.exit(run(page, "-o", args.build))
    if args.log:
        sys.exit(run(mdlog, "-o", args.log))
    sys.exit(run(server, "--port", str(args.port)))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""One command. Run it with no arguments and it finds your transcripts and serves.

    ./agent-chatter                 live dashboard of every conversation found
    ./agent-chatter --list          what it found, without starting anything
    ./agent-chatter --demo          the bundled example, no real data touched
    ./agent-chatter --build p.html  frozen self-contained page
    ./agent-chatter --log p.md      plain markdown log
    ./agent-chatter --summarize     have Claude write the conversation summaries
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

from . import discover, mdlog, page, server, summarize

HERE = os.path.dirname(os.path.abspath(__file__))
# The demo fixture ships inside the package, so it is found whether this was
# cloned or installed into a virtualenv by uv/pip.
DEMO = sorted(glob.glob(os.path.join(HERE, "examples", "mesh", "*.jsonl")))
SAMPLE_FINDINGS = os.path.join(HERE, "examples", "sample-findings.json")
DEMO_SUMMARIES = os.path.join(HERE, "examples", "demo-summaries.json")


def _shortest_unique(project, others):
    """The least a user can type that still means this project and no other.

    Suggesting the last dashed segment gives "dotfiles" for a project called
    jsb-dotfiles, which is shorter than the name and can collide with anything
    else ending the same way. Grow the suffix until it matches once, so whatever
    is printed can be pasted and will work.
    """
    parts = project.strip("-").split("-")
    for n in range(1, len(parts) + 1):
        frag = "-".join(parts[-n:])
        if sum(1 for p in others if frag.lower() in p.lower()) == 1:
            return frag
    return project


def _project_flag(project, others):
    """A --project argument that can be pasted and will work.

    Two traps. The shortest unique fragment is usually a plain word, but when one
    project's name is a prefix of another's there is no fragment that separates
    them and the whole directory name is the only answer — and that begins with a
    dash, which argparse reads as another flag. The `=` form gets a leading-dash
    value past it.
    """
    frag = _shortest_unique(project, others)
    return f"--project={frag}" if frag.startswith("-") else f"--project {frag}"


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
        # Same argument for the summaries. The demo exists to show what the
        # dashboard looks like when it is set up, and a tier that is missing
        # unless you have an API key is a tier nobody evaluating this will see.
        # Shipped pre-generated, so --demo still costs nothing and works offline.
        if not args.summary_cache and not args.summarize:
            if os.path.exists(DEMO_SUMMARIES):
                args.summary_cache = DEMO_SUMMARIES
        return DEMO, "the bundled example"
    try:
        rows = discover.find(project=args.project)
    except ValueError as exc:
        sys.exit(str(exc))
    if not rows:
        print(discover.describe(rows), file=sys.stderr)
        sys.exit(1)

    # Load everything by default and let the page filter. Splitting projects at
    # the command line meant deciding before you could see what was there;
    # narrowing is a question you answer while looking, so the selector lives in
    # the page. Naming a project still loads only that one, which is what you
    # want when the page is going to be published or the transcripts are large.
    projects = sorted({os.path.basename(os.path.dirname(r[0])) for r in rows})
    if args.project:
        label = f"{len(rows)} transcript(s) in {args.project}"
    elif len(projects) > 1:
        label = f"{len(rows)} transcript(s) across {len(projects)} projects"
    else:
        label = f"{len(rows)} transcript(s)"
    return [r[0] for r in rows], label


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
    ap.add_argument("--all", dest="all_projects", action="store_true",
                    help=argparse.SUPPRESS)   # now the default; accepted so old
                                              # invocations keep working
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
    summarize.add_arguments(ap)
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
        # The markdown log has nowhere to put generated prose without blurring it
        # into the quotes around it, and the whole point of the tier is that it
        # is visibly separate. Same reason curated findings are excluded.
        if args.summarize and mod is not mdlog:
            argv += ["--summarize", "--summarize-model", args.summarize_model]
        if args.summary_cache and mod is not mdlog:
            argv += ["--summary-cache", args.summary_cache]
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

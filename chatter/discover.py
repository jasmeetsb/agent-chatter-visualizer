"""Find the transcripts worth reading, so nobody has to know where they live.

Every entry point used to require paths. That means a newcomer must already know
that Claude Code keeps transcripts under `~/.claude/projects/`, that the
directory name is their project path flattened with dashes, that the files are
named by session UUID, and which of those UUIDs actually talked to a peer. On
this machine that was 7 transcripts across 3 projects, of which 4 carried peer
traffic — findable in about a second by a script, and not at all by a person who
has not read the source.

Two-stage, because both halves matter:

  1. a cheap byte scan to shortlist candidates, and
  2. an actual parse to confirm, because the cheap scan cannot be trusted.

Stage 2 is not caution for its own sake. `cross-session-message` appears in any
transcript that merely READ this repository, which is the same trap that made the
original extractor fabricate messages. A file that discusses the format is not a
file that used it. Only the parser can tell those apart, so the parser decides.
"""
import glob
import os

# Structural markers, not prose. `"kind": "peer"` is written by the delivering
# record and `"name": "SendMessage"` by the sending tool call. Both still appear
# in a transcript that quoted source code, which is what stage 2 is for.
_MARKERS = (b'"kind": "peer"', b'"kind":"peer"',
            b'"name": "SendMessage"', b'"name":"SendMessage"',
            b'<cross-session-message')

DEFAULT_ROOT = os.path.expanduser("~/.claude/projects")


def candidates(root=None, project=None):
    """Transcript paths whose bytes suggest cross-session traffic."""
    root = root or DEFAULT_ROOT
    pattern = os.path.join(root, project or "*", "*.jsonl")
    out = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError:
            continue
        if any(m in blob for m in _MARKERS):
            out.append(path)
    return out


def find(root=None, project=None, model=None):
    """Transcripts that genuinely carry peer messages, newest first.

    Returns [(path, n_events, session_name)]. Confirmed by parsing, because a
    transcript that merely mentions the message format is not one that used it.
    """
    if model is None:
        from . import model as model
    found = []
    for path in candidates(root, project):
        try:
            model.reset()
            data = model.build([path])
        except Exception:
            continue
        peers = [e for e in data["events"] if e.get("kind") == "peer"]
        if not peers:
            continue
        own = None
        for meta in data["sessions"].values():
            if not meta.get("ghost"):
                own = meta.get("name")
                break
        found.append((path, len(peers), own or os.path.basename(path)[:8]))
    model.reset()
    found.sort(key=lambda r: os.path.getmtime(r[0]), reverse=True)
    return found


def describe(rows):
    """Human-readable listing, with no filesystem paths in it.

    A transcript path is `/home/<username>/…`, which is a real person's name and
    is exactly what `src_id` exists to keep out of anything published. The same
    reasoning applies to something a user might paste into an issue.
    """
    if not rows:
        return ("No transcripts with cross-session traffic found under\n"
                "  " + DEFAULT_ROOT.replace(os.path.expanduser("~"), "~") + "\n"
                "Pass paths explicitly, or try --demo to see it working on the "
                "bundled example.")
    out = []
    for path, n, name in rows:
        project = os.path.basename(os.path.dirname(path))
        out.append(f"  {name:<28} {n:>4} peer messages   ({project})")
    return "\n".join(out)

"""Build the dashboard page.

ONE renderer, two data sources. The static page is the live page with the data
inlined and the cursor never advancing — that is the whole reason this is a
single template rather than a separate "archival" builder. Two codebases of view
logic drift, and the failure is silent: the published page and the monitor
disagree about what happened.

The static output keeps the guarantees the archival tool always had — self
contained, no external fonts/scripts/images, works from file://, survives a
strict CSP — and it deliberately carries no attention panel. Alerts are ephemeral
live state; persisting them into a published artifact would be the tool asserting
which exchange mattered, which is the one thing it must never do.
"""
import html
import json
import os

from .scrub import scrub

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "dashboard.html")

# Used when nobody passed --title and the transcripts do not all come from one
# identifiable project. The former default was "Agent mesh", left over from when
# the commands were serve-mesh.py and build-mesh-page.py — a word that appears
# nowhere in the tool's name, its README or its install instructions, so the
# first place a new user met it was the heading of their own dashboard.
DEFAULT_TITLE = "Agent conversations"

DEFAULT_SUBTITLE = (
    "Every message exchanged between the coordinating sessions, with who sent it, "
    "when it was sent, when it arrived, and when the recipient actually read it."
)


def render(data, *, title=DEFAULT_TITLE, heading=None, subtitle=DEFAULT_SUBTITLE,
           feed=None, poll_ms=2000, silence_s=600, findings=None,
           summary_note=None):
    """Return the complete page.

    data     — a snapshot dict {events, sessions, sources, snapshot_id, seq}, or
               None for a live page that fetches everything from `feed`.
    feed     — URL path of the delta endpoint, or None for a frozen static page.
    findings — curated findings, or None. Supplied, never generated: a heuristic
               can tag a message but cannot decide which exchange mattered, and a
               generator would produce confident nonsense. Scrubbed like anything
               else, because findings quote the transcript they describe.
    summary_note — a line for the top of the insights panel when --summarize was
               asked for and could not run. Only ever set when it was asked for:
               explaining an optional feature nobody enabled, on every run, makes
               that feature everyone's problem.
    """
    findings = [
        {k: (scrub(v) if isinstance(v, str) else v) for k, v in f.items()}
        for f in (findings or [])
    ]
    with open(TEMPLATE, encoding="utf-8") as fh:
        page = fh.read()

    # json.dumps for anything reaching JS, html.escape for anything reaching
    # markup. Session names and message bodies are peer-controlled: a peer can
    # rename itself to anything at all, and that string flows into graph labels,
    # the legend, the chips and the alerts. The template binds all of it with
    # textContent; these two calls cover the boot payload and the masthead.
    page = (page
            .replace("__DATA__", json.dumps(data, ensure_ascii=False) if data else "null")
            .replace("__FEED__", json.dumps(feed) if feed else "null")
            .replace("__FINDINGS__", json.dumps(findings, ensure_ascii=False))
            .replace("__SUMMARYNOTE__", json.dumps(summary_note) if summary_note else "null")
            .replace("__POLL__", str(int(poll_ms)))
            .replace("__SILENCE__", str(int(silence_s)))
            .replace("__TITLE__", html.escape(title))
            .replace("__HEADING__", html.escape(heading or title))
            .replace("__SUBTITLE__", html.escape(subtitle)))
    assert_text(page)
    return page


def assert_text(page):
    """The output must contain no NUL, and this is a safety property rather than
    tidiness.

    A single NUL anywhere makes grep classify the whole file as binary. The
    credential scan AGENTS.md mandates before publishing then prints *nothing* and
    exits 1 — and no output is indistinguishable from a clean "0". The mandated
    check silently stops running, on the one path the repo says must never
    regress. This caught a real defect: four raw NULs used as map-key separators
    in the template disabled the scan on every page built from it.
    """
    n = page.count("\0")
    if n:
        raise ValueError(
            f"refusing to write: page contains {n} NUL byte(s), which makes grep "
            f"treat it as binary and silently disables the credential scan in "
            f"AGENTS.md. Use an escape sequence in the template, not a raw byte.")


def strip_attention(page):
    """Hide the attention panel. Alerts never reach a published artifact."""
    return page.replace('<section id="attention-sec">',
                        '<section id="attention-sec" hidden>')

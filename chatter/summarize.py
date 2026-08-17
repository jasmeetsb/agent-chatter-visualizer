"""Optional: have Claude write the per-conversation summaries.

Everything else in this repository is standard library, offline, and reads only
files you already have. This module is none of those things — it sends message
bodies to Anthropic's API and costs money — so it is opt-in behind `--summarize`,
imports its dependency lazily, and is never on a default path.

WHY IT EXISTS. `model.exchanges()` describes a conversation *extractively*: the
headlines the senders wrote, and the sentences in which one of them marked a
conclusion. That is honest and free, and it is also thin — six headlines are six
subject lines, not an account of what was argued or what came of it. A summary is
the thing people actually want from the panel, and there is no way to produce one
from text alone.

WHERE THE OUTPUT GOES, AND WHY THAT MATTERS. AGENTS.md forbids the tool deciding
which exchange mattered, and a generated summary is exactly the kind of confident
prose that rule exists to keep out of a published page. The resolution is not to
weaken the rule but to make the authorship visible: generated text renders in its
own tier, labelled with the model that wrote it, kept separate from the verbatim
participant quotes beside it and from the curated entries a person wrote. Three
tiers, three authors, never blended:

    curated    a person judged this          (--findings)
    generated  a model wrote this            (--summarize, this module)
    surfaced   a participant said this       (verbatim, always free)

A reader can therefore discount the middle tier at a glance, which is the only
thing that makes it safe to show at all.
"""
import hashlib
import json
import os
import sys
import threading

from .model import _epoch, leaked
from .scrub import scrub

MODEL = "claude-opus-5"

# Enough headroom for reasoning plus the JSON. Thinking is on by default on this
# model and shares the ceiling with the response, so a snug value truncates the
# answer rather than the thinking.
MAX_TOKENS = 8000

# One conversation is a short, well-scoped summarisation task, which is what low
# effort is for. It is also billed per conversation on someone else's key, and a
# tool people re-run should not quietly spend more than the job needs.
EFFORT = "low"

# Bound what one message can contribute. A pasted stack trace should not decide
# the cost of summarising the exchange it appeared in.
BODY_CHARS = 4000

# Leave a conversation alone until it has been quiet this long. Without it, a
# live exchange is re-summarised on every message that lands — each one changes
# the text, so each one misses the cache and bills again.
SETTLE_S = 120

SYSTEM = """\
You are summarising one conversation between autonomous coding agents that were \
working together on a software project. The messages below are what they sent \
each other, in order.

Write for someone who did not watch it happen and wants to know what came of it.

summary: two or three sentences. Name the concrete subject and where it ended \
up — "agreed the queue-operation records are the authoritative set, after vm2's \
data showed the origin records miss mid-turn deliveries" rather than "the agents \
discussed an implementation detail and reached agreement". Describing the shape \
of the exchange instead of its content is the failure mode to avoid.

decisions: the things that were actually settled, one short line each. A \
decision is something that changed what either of them would do next — a claim \
withdrawn, an approach chosen, a cause identified, a plan agreed. Return an \
empty list rather than padding it; most conversations settle one or two things \
and some settle none.

Say only what these messages support. Where they are inconclusive, say so \
plainly instead of resolving it for them."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "decisions"],
    "additionalProperties": False,
}

NO_KEY_NOTE = (
    "Summaries are off: no API key configured. Create a .env file containing "
    "ANTHROPIC_API_KEY=sk-ant-… in the directory you run from, or at "
    "~/.config/agent-chatter/.env. Everything below is still the participants' "
    "own words.")

NO_SDK_NOTE = (
    "Summaries are off: the anthropic package is not installed. "
    "pip install anthropic — or run: uvx --with anthropic agent-chatter "
    "--summarize. Everything below is still the participants' own words.")

# Shown on a normal run, where nobody asked for anything. This started out
# silent, on the reasoning that a tool nagging about an unconfigured optional
# feature makes that feature everyone's problem. That was wrong in the only way
# that matters: the flag is the single thing here you cannot discover by looking
# at the page, so staying quiet meant the feature was invisible to exactly the
# people it was built for. One muted line, once, in the panel it applies to.
OFF_NO_KEY_NOTE = (
    "Summaries are off. --summarize has Claude write an account of each "
    "conversation instead of just the senders' headlines. It needs an "
    "ANTHROPIC_API_KEY in a .env file, in the directory you run from or at "
    "~/.config/agent-chatter/.env.")

OFF_HAVE_KEY_NOTE = (
    "Summaries are off. Add --summarize and Claude will write an account of "
    "each conversation instead of just the senders' headlines — your API key is "
    "already configured.")

# The demo, which ships its summaries so the tier is visible without a key. Say
# so, or the one page most people see first quietly implies it came free.
SHIPPED_NOTE = (
    "These summaries ship with the demo, already generated. On your own "
    "transcripts --summarize writes them, which needs an ANTHROPIC_API_KEY in a "
    ".env file.")


# --------------------------------------------------------------------------
# Key resolution — a .env file, deliberately
# --------------------------------------------------------------------------

def env_paths():
    """Where a key may live, most specific first."""
    out = []
    override = os.environ.get("AGENT_CHATTER_ENV")
    if override:
        out.append(override)
    out.append(os.path.join(os.getcwd(), ".env"))
    out.append(os.path.expanduser("~/.config/agent-chatter/.env"))
    return out


def read_env(path):
    """The KEY=value pairs in a .env file. Missing or unreadable is not an error.

    A hand-rolled six-line parser rather than python-dotenv, because a runtime
    dependency for the default path is the one thing this repo does not do — and
    the format people actually write is this small.
    """
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k.strip()] = v
    except OSError:
        pass
    return out


def resolve_key():
    """(key, where it came from). None if there is nowhere to get one."""
    for path in env_paths():
        val = read_env(path).get("ANTHROPIC_API_KEY")
        if val:
            return val, path
    # An exported key still works. Requiring the file when the environment
    # already has one would fail for no reason a user could act on.
    val = os.environ.get("ANTHROPIC_API_KEY")
    if val:
        return val, "ANTHROPIC_API_KEY in the environment"
    return None, None


# --------------------------------------------------------------------------
# Cache — so a re-run and a 2-second rebuild loop are free
# --------------------------------------------------------------------------

def cache_path():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "agent-chatter", "summaries.json")


class Cache:
    """Keyed by the exact text sent, so a conversation that has not changed is
    never paid for twice, and one that has grown is re-summarised."""

    def __init__(self, path=None):
        self.path = path or cache_path()
        self.lock = threading.Lock()
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except Exception:
            self.data = {}

    def get(self, key):
        with self.lock:
            return self.data.get(key)

    def put(self, key, value):
        with self.lock:
            self.data[key] = value
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh, ensure_ascii=False)
                os.replace(tmp, self.path)
            except OSError:
                pass          # a cache that cannot be written still works


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------

def _name(sessions, sid):
    meta = (sessions or {}).get(sid) or {}
    return meta.get("name") or (sid or "?")[:12]


def transcript(exchange, events_by_id, sessions):
    """The conversation as plain text, in order.

    Bodies come from the built payload, so they have already been through
    scrub() and unhome(). Nothing unredacted leaves this machine.
    """
    lines = []
    for eid in exchange.get("events") or []:
        ev = events_by_id.get(eid)
        if not ev:
            continue
        body = (ev.get("body") or ev.get("preview") or "").strip()
        if not body:
            continue
        if len(body) > BODY_CHARS:
            body = body[:BODY_CHARS] + " …[truncated]"
        when = (ev.get("sent") or ev.get("enqueued") or ev.get("delivered") or "")
        lines.append("[%s] %s → %s:\n%s" % (
            when, _name(sessions, ev.get("from_id")),
            _name(sessions, ev.get("to_id")), body))
    return "\n\n".join(lines)


def _key(model, text):
    return hashlib.sha1(f"{model}\n{text}".encode("utf-8", "replace")).hexdigest()


# --------------------------------------------------------------------------
# The summariser
# --------------------------------------------------------------------------

class Summarizer:
    """Attaches generated summaries to `data["exchanges"]`.

    Two entry points, because the live server and the static build want opposite
    things. `attach()` is instant and reads only the cache, so a rebuild loop
    running every two seconds never blocks and never bills. `fill()` does the
    API calls. The server runs fill() on a background thread and lets the next
    attach() publish the results; the page builder runs both and waits.
    """

    def __init__(self, model=MODEL, cache=None, settle_s=SETTLE_S, generate=True):
        self.model = model
        self.cache = cache if cache is not None else Cache()
        self.settle_s = settle_s
        self.key, self.key_from = resolve_key()
        self.client = None
        self.note = None            # what to tell the reader, or None
        self.available = False

        # Cache-only mode. `--summary-cache` without `--summarize` reads a file
        # somebody already generated and never calls anything — which is how the
        # bundled demo shows this tier without a key, and how you can hand a
        # colleague summaries you paid for once.
        if not generate:
            return

        if not self.key:
            self.note = NO_KEY_NOTE
            return
        try:
            import anthropic
        except ImportError:
            self.note = NO_SDK_NOTE
            return
        self.client = anthropic.Anthropic(api_key=self.key)
        self.available = True

    # ---- reading ---------------------------------------------------------

    def attach(self, data):
        """Copy any cached summary onto its exchange. No network, no cost."""
        events_by_id = {e["id"]: e for e in data.get("events") or []}
        sessions = data.get("sessions") or {}
        n = 0
        for x in data.get("exchanges") or []:
            text = transcript(x, events_by_id, sessions)
            if not text:
                continue
            hit = self.cache.get(_key(self.model, text))
            if hit:
                x["ai"] = hit
                n += 1
        return n

    def pending(self, data):
        """(exchange, text) for conversations with no cached summary yet."""
        events_by_id = {e["id"]: e for e in data.get("events") or []}
        sessions = data.get("sessions") or {}
        now = _now()
        out = []
        for x in data.get("exchanges") or []:
            end = _epoch(x.get("end"))
            if end is not None and now - end < self.settle_s:
                continue        # still talking; summarising now buys a stale
                                # answer and pays for it again on the next message
            text = transcript(x, events_by_id, sessions)
            if text and not self.cache.get(_key(self.model, text)):
                out.append((x, text))
        return out

    # ---- writing ---------------------------------------------------------

    def fill(self, data, progress=None, workers=4):
        """Summarise everything not already cached. Returns (done, failed)."""
        if not self.available:
            return 0, 0
        todo = self.pending(data)
        if not todo:
            return 0, 0
        if progress:
            progress(f"summarising {len(todo)} conversation(s) with {self.model}…")

        done = failed = 0
        results = {}
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as pool:
            futures = {pool.submit(self._one, text): (x, text) for x, text in todo}
            for fut in futures:
                x, text = futures[fut]
                try:
                    got = fut.result()
                except Exception as exc:
                    failed += 1
                    if progress:
                        progress(f"  {type(exc).__name__}: {exc}")
                    continue
                if got:
                    results[_key(self.model, text)] = got
                    x["ai"] = got
                    done += 1
        for k, v in results.items():
            self.cache.put(k, v)
        if progress:
            progress(f"summarised {done} conversation(s)"
                     + (f", {failed} failed" if failed else ""))
        return done, failed

    def _one(self, text):
        """One conversation → {summary, decisions, model}."""
        import anthropic
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM,
                output_config={
                    "effort": EFFORT,
                    "format": {"type": "json_schema", "schema": SCHEMA},
                },
                messages=[{"role": "user", "content": text}],
            )
        except anthropic.AuthenticationError:
            raise RuntimeError(
                f"the key from {self.key_from} was rejected") from None
        except anthropic.RateLimitError:
            raise RuntimeError("rate limited; try again shortly") from None
        except anthropic.APIStatusError as exc:
            raise RuntimeError(f"API error {exc.status_code}: {exc.message}") from None
        except anthropic.APIConnectionError:
            raise RuntimeError("could not reach the API") from None

        # Refusals return 200 with an empty or partial body, so check before
        # reading content — indexing content[0] here would raise instead.
        if resp.stop_reason == "refusal":
            raise RuntimeError("the request was declined by safety classifiers")

        raw = next((b.text for b in resp.content if b.type == "text"), None)
        if not raw:
            raise RuntimeError(f"empty response (stop_reason={resp.stop_reason})")
        got = json.loads(raw)

        # Scrubbed and leak-checked like anything else that reaches the page.
        # The input was already clean, so this should never fire — which is the
        # point: five separate routes have leaked a username into this payload,
        # and every one of them was invisible to the fix before it.
        summary = scrub((got.get("summary") or "").strip())
        decisions = [scrub(d.strip()) for d in (got.get("decisions") or []) if d.strip()]
        for val in [summary] + decisions:
            hit = leaked(val)
            if hit:
                raise RuntimeError(
                    f"refusing to attach a summary containing {hit!r}")
        if not summary:
            raise RuntimeError("no summary in the response")
        return {"summary": summary, "decisions": decisions[:6], "model": self.model}


def _now():
    import time
    return time.time()


def note_for(asked, summarizer=None, any_summaries=False):
    """What the insights panel says above the entries, or None.

    Returns {"level", "text"} — `warn` when someone asked for summaries and did
    not get them, `info` when nothing is wrong and there is simply something they
    may not know exists. Both render, and the distinction is the difference
    between a failure and an invitation.

    Silent in exactly one case: summaries are being generated, so the panel is
    already showing the thing this would be telling them about.
    """
    if summarizer is not None and summarizer.available:
        return None                        # generating; nothing to explain
    if any_summaries:
        # Summaries on the page but nothing new will be added — the shipped demo
        # cache, or a cache someone handed over.
        if asked and summarizer is not None and summarizer.note:
            return {"level": "warn", "text": summarizer.note}
        return {"level": "info", "text": SHIPPED_NOTE}
    if asked and summarizer is not None and summarizer.note:
        return {"level": "warn", "text": summarizer.note}
    return {"level": "info",
            "text": OFF_HAVE_KEY_NOTE if resolve_key()[0] else OFF_NO_KEY_NOTE}


def add_arguments(ap):
    """The two flags, defined once so every front end spells them the same."""
    ap.add_argument("--summarize", action="store_true",
                    help="have Claude write a summary of each conversation. "
                         "Needs ANTHROPIC_API_KEY in a .env file; costs money; "
                         "generated text is labelled as such in the page.")
    ap.add_argument("--summarize-model", default=MODEL, metavar="ID",
                    help=f"model for --summarize (default {MODEL})")
    ap.add_argument("--summary-cache", metavar="FILE",
                    help="where generated summaries are kept (default "
                         "~/.cache/agent-chatter/summaries.json). Given without "
                         "--summarize it is read and never written, which shows "
                         "summaries somebody else generated without spending "
                         "anything.")


def from_args(args):
    """A Summarizer if there is any reason to have one, else None."""
    gen = bool(getattr(args, "summarize", False))
    path = getattr(args, "summary_cache", None)
    if not gen and not path:
        return None
    return Summarizer(model=getattr(args, "summarize_model", None) or MODEL,
                      cache=Cache(path) if path else None, generate=gen)


def report(summarizer, stream=sys.stderr):
    """One line about where the key came from, or why there is none."""
    if summarizer is None or summarizer.note is None and not summarizer.available:
        return                      # cache-only: nothing was asked for
    if summarizer.available:
        print(f"agent-chatter: summarising with {summarizer.model} "
              f"(key from {summarizer.key_from})", file=stream, flush=True)
    else:
        print(f"agent-chatter: {summarizer.note}", file=stream, flush=True)

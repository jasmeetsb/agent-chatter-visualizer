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

    your note  a person judged this          (--findings)
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

# Bound the whole conversation. Measured on a real project: seven conversations
# came to 147,000 characters, one of them 52,000 on its own — so without this the
# bill is set by whichever session pasted the most, which is nobody's intent.
# When a conversation is over budget the MIDDLE is dropped, not the tail: the
# opening frames what the exchange is about and the closing carries how it
# resolved, and those are the two things a summary is made of.
CONV_CHARS = 12000

# Bound how many conversations one run pays for, newest first. A long-running
# project has months of them and the recent ones are what anyone is looking at.
# 0 means no limit, for someone who has decided otherwise.
MAX_CONVERSATIONS = 20

# Rough cost per 1M input tokens, only for the estimate printed before spending.
# Deliberately approximate and deliberately stated as such: a precise-looking
# number that is quietly stale is worse than an honest order of magnitude.
USD_PER_MTOK_IN = 5.0
CHARS_PER_TOKEN = 4

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
    "Summaries are off: no API key configured. Put ANTHROPIC_API_KEY=sk-ant-… "
    "in a .env file, in the directory you run from or at "
    "~/.config/agent-chatter/.env, then restart. Every message is still in the "
    "stream below.")

def no_sdk_note():
    """Name the interpreter that needs the package.

    "pip install anthropic" is not actionable on a machine with several Pythons,
    which is most of them: the clone's ./agent-chatter runs under
    `/usr/bin/env python3`, and a package installed into a virtualenv that is not
    active, or under a different `pip`, is invisible to it. Printing the exact
    interpreter turns a guess into a command.
    """
    return ("Summaries are off: the anthropic package is not installed for "
            f"{sys.executable}. Install it with: {sys.executable} -m pip install "
            "anthropic — then restart.")

# Key is here and the server is live, so the panel can offer a button instead of
# telling someone to restart with a flag. Short, because the button beside it is
# the actual explanation.
READY_NOTE = (
    "Nothing summarised yet. Claude reads each conversation and writes what was "
    "decided in it — this calls the API and costs money, billed to your key.")

# Key is here but this is a frozen page, so there is nothing to press.
STATIC_NOTE = (
    "Nothing summarised yet. Rebuild with --summarize to have Claude write what "
    "was decided in each conversation; it calls the API and costs money.")

# Generating, but a cap left something out. Reported rather than silent: an
# unmentioned limit cannot be told apart from the tool having judged those
# conversations not worth summarising.
CAPPED_NOTE = (
    "The {n} oldest conversation(s) were not summarised. This run summarises at "
    "most {limit} of them, newest first, so a long history does not become a "
    "large bill — and leaving the dashboard open does not keep spending. Raise "
    "it with --summarize-limit, or pass 0 for all of them.")

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


def transcript(exchange, events_by_id, sessions, budget=CONV_CHARS):
    """The conversation as plain text, in order, within `budget` characters.

    Bodies come from the built payload, so they have already been through
    scrub() and unhome(). Nothing unredacted leaves this machine.

    Over budget, messages are dropped from the MIDDLE outwards and the gap is
    marked, so the model is told what it is not being shown rather than being
    handed a truncated exchange that reads complete. Keeping the head and the
    tail keeps the two parts a summary is actually made of: what this was about,
    and how it came out.
    """
    blocks = []
    for eid in exchange.get("events") or []:
        ev = events_by_id.get(eid)
        if not ev:
            continue
        body = (ev.get("body") or ev.get("preview") or "").strip()
        if not body:
            continue
        if len(body) > BODY_CHARS:
            body = body[:BODY_CHARS] + " …[message truncated]"
        when = (ev.get("sent") or ev.get("enqueued") or ev.get("delivered") or "")
        blocks.append("[%s] %s → %s:\n%s" % (
            when, _name(sessions, ev.get("from_id")),
            _name(sessions, ev.get("to_id")), body))

    if not budget or sum(len(b) + 2 for b in blocks) <= budget:
        return "\n\n".join(blocks)

    # Take from both ends until the budget is gone. Head first on each round, so
    # a budget that fits only one message keeps the one that says what this is.
    head, tail, used = [], [], 0
    lo, hi = 0, len(blocks) - 1
    take_head = True
    while lo <= hi:
        i = lo if take_head else hi
        cost = len(blocks[i]) + 2
        if used + cost > budget:
            break
        (head if take_head else tail).append(blocks[i])
        used += cost
        if take_head:
            lo += 1
        else:
            hi -= 1
        take_head = not take_head
    dropped = hi - lo + 1
    if dropped <= 0:
        return "\n\n".join(head + list(reversed(tail)))
    gap = f"[… {dropped} message(s) omitted from the middle of this conversation …]"
    return "\n\n".join(head + [gap] + list(reversed(tail)))


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

    def __init__(self, model=MODEL, cache=None, settle_s=SETTLE_S, auto=False,
                 limit=MAX_CONVERSATIONS, conv_chars=CONV_CHARS):
        self.model = model
        self.cache = cache if cache is not None else Cache()
        self.settle_s = settle_s
        self.limit = limit
        self.conv_chars = conv_chars
        self.skipped = 0            # over the limit, so never sent; reported
        # Counted for the life of this process, not per call. The live server
        # calls fill() every ten seconds forever, so a per-call cap is a batch
        # size and not a ceiling: a dashboard left open would work through a
        # project's entire history twenty conversations at a time. The flag reads
        # like a ceiling, so it has to be one.
        self.generated = 0
        self.key, self.key_from = resolve_key()
        self.client = None
        self.note = None            # why it cannot generate, or None

        # `ready` and `auto` are different questions, and conflating them is why
        # the flag used to be the only way in. `ready` means a key and the SDK
        # are here, so generating is possible; `auto` means go ahead without
        # being asked. A live server that is ready but not auto shows a button
        # and spends nothing until it is pressed.
        self.auto = auto
        self.ready = False
        self.shipped = False        # cache came from --summary-cache, not this run

        if not self.key:
            self.note = NO_KEY_NOTE
            return
        try:
            import anthropic
        except ImportError:
            self.note = no_sdk_note()
            return
        self.client = anthropic.Anthropic(api_key=self.key)
        self.ready = True

    # ---- reading ---------------------------------------------------------

    def attach(self, data):
        """Copy any cached summary onto its exchange. No network, no cost."""
        events_by_id = {e["id"]: e for e in data.get("events") or []}
        sessions = data.get("sessions") or {}
        n = 0
        for x in data.get("exchanges") or []:
            text = transcript(x, events_by_id, sessions, self.conv_chars)
            if not text:
                continue
            hit = self.cache.get(_key(self.model, text))
            if hit:
                x["ai"] = hit
                n += 1
        return n

    def pending(self, data):
        """(exchange, text) for conversations with no cached summary yet.

        Newest first, and capped at `self.limit`. `exchanges` already arrives
        newest-first, so the cap keeps the recent end — which is what anyone
        looking at a dashboard is looking at. Whatever the cap dropped is counted
        into self.skipped rather than vanishing: a limit nobody is told about
        reads as the tool having decided those conversations did not matter.
        """
        events_by_id = {e["id"]: e for e in data.get("events") or []}
        sessions = data.get("sessions") or {}
        now = _now()
        out = []
        self.skipped = 0
        room = (self.limit - self.generated) if self.limit else None
        # The settle window is for UNATTENDED generation only. Its job is to stop
        # the background loop paying for a conversation twice because a message
        # landed mid-run — nobody asked for that summary, so waiting costs
        # nothing. A button press is somebody asking, and holding their request
        # for two minutes without saying so is how the button came to vanish
        # entirely on a machine whose sessions were still talking: every
        # conversation was inside the window, pending fell to zero, and the panel
        # offered nothing while claiming nothing had been summarised.
        settle = self.settle_s if self.auto else 0
        for x in data.get("exchanges") or []:
            end = _epoch(x.get("end"))
            if settle and end is not None and now - end < settle:
                continue
            text = transcript(x, events_by_id, sessions, self.conv_chars)
            if not text or self.cache.get(_key(self.model, text)):
                continue
            if room is not None and len(out) >= max(room, 0):
                self.skipped += 1
                continue
            out.append((x, text))
        return out

    def estimate(self, todo):
        """Roughly what `todo` will cost, for saying so before spending it."""
        chars = sum(len(t) for _, t in todo)
        tokens = chars // CHARS_PER_TOKEN
        return chars, tokens, tokens / 1_000_000 * USD_PER_MTOK_IN

    # ---- writing ---------------------------------------------------------

    def fill(self, data, progress=None, workers=4):
        """Summarise everything not already cached. Returns (done, failed)."""
        if not self.ready:
            return 0, 0
        todo = self.pending(data)
        if not todo:
            return 0, 0
        if progress:
            chars, tokens, usd = self.estimate(todo)
            progress(f"summarising {len(todo)} conversation(s) with {self.model} — "
                     f"~{tokens:,} input tokens, roughly ${usd:.2f} plus output")
            if self.skipped:
                progress(f"  {self.skipped} older conversation(s) will not be "
                         f"summarised — ceiling of {self.limit} for this run "
                         f"(--summarize-limit; pass 0 for all)")

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
        self.generated += done
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


def panel_config(summarizer, any_summaries, live, token=None):
    """Everything the panel needs to explain or offer itself.

        can    — a key and the SDK are here, so the button can do something
        token  — required on POST /summarize; see server.py on why it exists
        note   — {level, text} to show, or None
        model  — named on each card, so the prose says who wrote it

    The panel is summaries or nothing, so when there are none this is the whole
    of its content: either the reason it cannot generate, or the offer to.
    """
    ready = bool(summarizer and summarizer.ready)
    cfg = {"can": bool(ready and live), "model": summarizer.model if summarizer else MODEL,
           "token": token if (ready and live) else None, "note": None}

    if summarizer and summarizer.skipped:
        cfg["note"] = {"level": "info", "text": CAPPED_NOTE.format(
            n=summarizer.skipped, limit=summarizer.limit)}
        return cfg
    if any_summaries:
        # Summaries are on the page. Say nothing unless there is something the
        # reader would otherwise get wrong — the demo's shipped cache, which
        # implies it came free.
        if summarizer and summarizer.shipped:
            cfg["note"] = {"level": "info", "text": SHIPPED_NOTE}
        return cfg
    if not ready:
        # Nothing to show and no way to fix it from here. This is the whole panel.
        cfg["note"] = {"level": "warn",
                       "text": summarizer.note if summarizer else NO_KEY_NOTE}
    elif live:
        cfg["note"] = {"level": "info", "text": READY_NOTE}
    else:
        cfg["note"] = {"level": "info", "text": STATIC_NOTE}
    return cfg


def add_arguments(ap):
    """The two flags, defined once so every front end spells them the same."""
    ap.add_argument("--summarize", action="store_true",
                    help="summarise immediately, without waiting to be asked. "
                         "On the live dashboard you do not need this — there is "
                         "a button. SENDS YOUR MESSAGE BODIES TO THE ANTHROPIC "
                         "API AND COSTS MONEY, billed to your key, once per "
                         "conversation; see --summarize-limit and "
                         "--summarize-chars. Needs ANTHROPIC_API_KEY in a .env "
                         "file.")
    ap.add_argument("--summarize-model", default=MODEL, metavar="ID",
                    help=f"model for --summarize (default {MODEL})")
    ap.add_argument("--summarize-limit", type=int, default=MAX_CONVERSATIONS,
                    metavar="N",
                    help=f"never summarise more than N conversations, newest "
                         f"first — a ceiling for the whole run, including a "
                         f"server left open (default {MAX_CONVERSATIONS}, 0 for "
                         f"no limit)")
    ap.add_argument("--summarize-chars", type=int, default=CONV_CHARS, metavar="N",
                    help=f"characters sent per conversation; over this, the "
                         f"middle is dropped and the gap marked (default "
                         f"{CONV_CHARS}, 0 for no limit)")
    ap.add_argument("--summary-cache", metavar="FILE",
                    help="where generated summaries are kept (default "
                         "~/.cache/agent-chatter/summaries.json). Given without "
                         "--summarize it is read and never written, which shows "
                         "summaries somebody else generated without spending "
                         "anything.")


def from_args(args):
    """Always a Summarizer. It is the thing that knows whether a key exists.

    It used to be None unless a flag was passed, which meant the page could not
    tell "no key" from "nobody asked" and the only way to generate anything was
    to restart with the flag. Constructing one always costs a .env read.
    """
    limit = getattr(args, "summarize_limit", MAX_CONVERSATIONS)
    path = getattr(args, "summary_cache", None)
    s = Summarizer(model=getattr(args, "summarize_model", None) or MODEL,
                   cache=Cache(path) if path else None,
                   auto=bool(getattr(args, "summarize", False)),
                   limit=MAX_CONVERSATIONS if limit is None else limit,
                   conv_chars=getattr(args, "summarize_chars", CONV_CHARS))
    # Only the bundled demo cache, not any --summary-cache. `bool(path)` put
    # "these summaries ship with the demo" on top of a real project the moment
    # someone pointed --summary-cache at a file of their own.
    s.shipped = bool(path) and os.path.dirname(os.path.abspath(path)).endswith(
        os.path.join("chatter", "examples"))
    return s


def report(summarizer, stream=sys.stderr, live=False):
    """One line about summaries, on the terminal that started it. Always.

    This used to speak only when --summarize was passed, which left the command
    line silent about the one feature whose state is not obvious: someone starts
    the server from a clone, sees the usual two lines, opens the page and finds
    no button, and has nothing to go on. The terminal is where they already are.
    """
    if summarizer is None:
        return
    if not summarizer.ready:
        print(f"agent-chatter: {summarizer.note}", file=stream, flush=True)
    elif summarizer.auto:
        print(f"agent-chatter: summarising with {summarizer.model} "
              f"(key from {summarizer.key_from})", file=stream, flush=True)
    elif live:
        print(f"agent-chatter: summaries ready — press Summarise on the "
              f"dashboard (key from {summarizer.key_from})", file=stream, flush=True)
    else:
        print(f"agent-chatter: key found ({summarizer.key_from}); add "
              f"--summarize to write summaries into this page",
              file=stream, flush=True)

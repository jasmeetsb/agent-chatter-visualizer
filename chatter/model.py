"""The single model: parse, identity, pairing, tailing.

ONE model, not one parse. The original rule in AGENTS.md was "only one parse",
because two parsers drift and the failure is silent. At N sessions that is not
enough: identity resolution, ULID normalisation and cross-transcript pairing can
drift exactly the same way. If normalisation lived in the server only, the static
page would show four sessions while the live page showed seven, and nothing would
raise an error. So both front-ends import this, and this is the only thing that
reads a transcript.

    build(paths) -> {"events": [...], "sessions": {...}, "sources": [...],
                     "snapshot_id": "...", "seq": N}

Every non-obvious rule here exists because the obvious version was tried and was
wrong on real data. DESIGN.md carries the measurements; the comments carry the
reason.
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

# The scrubber has ONE definition, in chatter/scrub.py. A transcript contains
# everything ever pasted into the session — in the project this was written for
# that included a live cloud token, which reached a committed file. Do not write a
# second one here; import that one.
from .scrub import scrub  # noqa: E402

# Heuristic tags. For FILTERING only. They never decide importance and they never
# drive an alert: a keyword tagger fires on `wrong|corrected|refuted` throughout
# any healthy design argument, so as an alert it is a stuck horn, and as a
# judgment it is the tool asserting which exchange mattered.
TAGS = [
    ("security",   r"\b(API key|credential|secret|scrub|leak|plaintext|rotate|token)\b"),
    ("stats",      r"\b(p ?= ?0|permutation|McNemar|noise floor|replicate|significan|ceiling)\b"),
    ("correction", r"\b(wrong|corrected|retract|refuted|falsified|supersede|withdraw)\b"),
    ("decision",   r"\b(standing (rule|convention)|AUTHORISED|GO —|STOP|owner's call|abort)\b"),
    ("result",     r"\b(LANDED|COMPLETE|pass rate|\d\d\.\d%|rows in)\b"),
    ("infra",      r"\b(GCS|bucket|scope|replica|migration|backfill|batch|upload|lag|index)\b"),
    ("prompt",     r"\b(prompt|runbook|schema|contract)\b"),
]

# The same session is addressed three ways with the same ULID core. Joining on
# the raw string splits every session into two nodes — one from its own
# transcript, one from how peers address it — silently, and the graph still looks
# plausible. Confirmed from both ends of a live pair.
_PREFIXES = ("bridge:session_", "cse_", "session_")

# A home directory contains a real person's name. `scrub()` does not catch it —
# it is not a credential — and it reaches a published page through more routes
# than the obvious one: `src_id` exists precisely so transcript PATHS never get
# emitted, and then `cwd` in the sessions table carried the same string anyway.
# Message bodies quote paths routinely too. Rewriting to `~/` is lossless for
# every use the page has.
_HOME = re.compile(r"(?:/home/|/Users/|\\Users\\)[^/\\\s\"']+")

# The SECOND form, which is not a path and which the pattern above does not see:
# Claude Code names a project directory by flattening its path with dashes, so a
# session's own slug is literally `-home-<username>-<project>`. It turns up in
# `~/.claude/projects/<slug>/`, in scratchpad paths under /tmp, and therefore in
# any message body that quotes one — which is how it reached a rendered page
# after `/home/` had already been fixed twice. Same name, different shape.
_SLUG = re.compile(r"-(home|Users)-(?!redacted-)[^/\s\"'\\]+?-")

# The THIRD shape, and the one that shows there will be a fourth: a shell prompt.
# Transcripts are full of pasted terminal output, and a prompt reads
# `user@host:~/path$`. That is a real name, in a form neither of the patterns
# above can see — it is not a path at all. Found only when discovery widened to
# other projects, i.e. by looking at more data rather than by thinking harder.
# Requiring `:~` or `:/` after the host keeps `git@github.com:owner/repo` intact.
_PROMPT = re.compile(r"\b([\w.-]+)@([\w.-]+):(?=[~/])")


def unhome(text):
    if not text:
        return text
    text = _HOME.sub("~", text)
    text = _SLUG.sub(r"-\1-redacted-", text)
    return _PROMPT.sub(r"user@\2:", text)

_OPEN_TAG = re.compile(r"<cross-session-message([^>]*)>")
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
_INNER = re.compile(r"<cross-session-message[^>]*>(.*?)</cross-session-message>", re.S)


def is_core(v):
    """A session id must be an opaque token, never an address.

    A peer on the SAME machine is addressed by unix socket rather than by
    session: `uds:/run/user/1000/cc-socks/2577673.sock`. That is a filesystem
    path carrying a PID — it is not stable across restarts, it never joins to
    the peer's real identity, and because an unresolved id renders as a node
    label it would print a socket path on the page. Two agents on one machine is
    the easiest setup anyone will try, and it was the broken one: messages to a
    local peer never paired, so every one was stamped "never landed" while the
    peer was visibly answering them.
    """
    return bool(v) and "/" not in v and "\\" not in v and ":" not in v


def ulid(raw):
    """Strip the namespace prefix to the bare ULID core."""
    if not raw:
        return None
    for p in _PREFIXES:
        if raw.startswith(p):
            return raw[len(p):]
    return raw


def _ts(raw):
    if not raw:
        return None
    try:
        datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None
    return str(raw)


def _epoch(stamp):
    if not stamp:
        return None
    try:
        d = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        return None


def tag(text):
    return [n for n, pat in TAGS if re.search(pat, text, re.I)]


def _blocks(msg):
    """message.content is usually a list of blocks but can be a bare string."""
    c = (msg or {}).get("content")
    if isinstance(c, list):
        return [b for b in c if isinstance(b, dict)]
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return []


def _attributed(attrs):
    """Reject a tag we cannot attribute to a sender. NEVER guess.

    `[^>]*` matches any text that merely DESCRIBES the format, so reading this
    repo's own source or docs fabricates messages — and they are attributed to a
    real peer, which makes them phantom edges on a real node rather than obvious
    junk. Requiring a well-formed bridge sender separated 6 candidate tags down
    to the 1 real message on a live transcript, on both machines.
    """
    d = dict(_ATTR.findall(attrs or ""))
    frm = d.get("from") or ""
    if not frm.startswith("bridge:"):
        return None
    return d


def _inner(raw):
    m = _INNER.search(raw or "")
    return m.group(1).strip() if m else None


def preview(summary, body, limit=120):
    """A headline for every message, derived when the sender didn't supply one.

    `summary` only exists on the OUTBOUND side — it is an argument the sender
    passed to SendMessage. So on a transcript where we hold one machine, every
    inbound message and every human turn has none, and the stream renders as
    rows of "(no summary)": measured 42 of 63 events, 66%, on a live transcript,
    each of which had a perfectly readable first line sitting in its body.

    Kept as a separate field rather than filled into `summary`, because `summary`
    means "what the sender called this" and that has to stay falsifiable. This
    means "what to show in a list".
    """
    s = (summary or "").strip()
    if s:
        return s
    for line in (body or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        # Drop the leading markdown/wrapper noise a first line often carries so
        # the preview starts on real words: === HEADING ===, ## h2, - bullet, > quote.
        line = re.sub(r"^[#>*\-=\s]+", "", line)
        line = re.sub(r"[=\s]+$", "", line)
        line = re.sub(r"[`*_]", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) < 3:
            continue
        return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"
    return ""



# Phrases with which a participant marks their OWN conclusion. This is not the
# tool deciding which exchange mattered — that stays forbidden, and a sentiment
# tagger fires on `wrong|corrected|refuted` throughout any healthy argument. It
# is closer to quoting: an agent writing "I withdraw that" or "confirmed on my
# transcript" has told you a conclusion was reached, in its own words, and the
# surfaced text is the sentence it wrote. Precision matters far more than recall
# here — a panel of near-misses is worse than a short panel, so each pattern must
# be a phrase people use to CONCLUDE, not merely to discuss.
MARKS = [
    ("reversed", r"\b(?:I (?:was wrong|withdraw|retract)|"
                 r"you(?:'re| are| were) right|my (?:mistake|error)|"
                 r"I (?:stand )?corrected|withdrawing (?:that|my)|"
                 r"that (?:was|is) backwards)\b"),
    ("decided",  r"\b(?:agreed(?:,| and| —)|we(?:'ll| will) go with|"
                 r"adopting (?:your|that|this)|decision:|settled(?:\.|,| —)|"
                 r"standing rule|accepted(?:\.|,| —| as)|taking your)\b"),
    ("found",    r"\b(?:root cause|turned out (?:to be|that)|"
                 r"reproduced (?:it|on|the)|confirmed on (?:my|both|the)|"
                 r"measured(?:,| on| it)|the (?:actual )?bug (?:is|was))\b"),
]
_MARKS = [(k, re.compile(p, re.I)) for k, p in MARKS]


def mark(body):
    """The sentence in which the sender marked a conclusion, or None.

    Returns {"kind", "quote"}. The quote is the sentence itself so a reader can
    judge the basis immediately — a surfaced item that cannot be checked against
    what was actually written is an assertion, which is the thing this project
    does not do.
    """
    text = (body or "").strip()
    if not text:
        return None
    for kind, pat in _MARKS:
        m = pat.search(text)
        if not m:
            continue
        start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start()))
        start = 0 if start < 0 else start + 1
        end = min([x for x in (text.find(". ", m.end()), text.find("\n", m.end()))
                   if x > 0] or [len(text)])
        quote = re.sub(r"\s+", " ", text[start:end + 1]).strip(" .\n")
        # A fragment ending in a colon is a lead-in to something, not the
        # conclusion itself ("Measured on the fixture at 1440px:").
        if len(quote) < 20 or quote.endswith(":"):
            continue
        return {"kind": kind, "quote": quote[:240]}
    return None

# --------------------------------------------------------------------------
# Per-transcript parse, with tailing
# --------------------------------------------------------------------------

class Source:
    """One transcript. Keeps its parse state so a re-read only consumes the tail.

    Transcripts are append-only and 10-20 MB; re-parsing every one on every poll
    does not scale. Only whole lines are consumed — a live transcript routinely
    has a partial write at the tail.
    """

    def __init__(self, path, sid):
        self.path, self.sid = path, sid
        self.offset = 0
        self.uuid = self.core = None
        self.cwd = self.branch = None
        self.name = None
        self.aka = []
        self.out = []          # outbound events, pre-resolution
        self.inbound = []      # inbound events, already merged within transcript
        self.human = []
        self.first = self.last = None
        self._pending = []     # enqueued, not yet resolved (FIFO)
        self._by_body = {}     # inner body -> inbound record, for origin enrichment
        self._human_by_body = {}   # same job for human turns; see _human()

    # -- identity ---------------------------------------------------------
    def _rename(self, n):
        if not n or n == self.name:
            return
        if self.name and self.name not in self.aka:
            self.aka.append(self.name)
        self.name = n

    def _stamp(self, s):
        if not s:
            return
        self.first = s if self.first is None or s < self.first else self.first
        self.last = s if self.last is None or s > self.last else self.last

    # -- the walk ---------------------------------------------------------
    def read(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.offset:            # truncated/rotated -> full reparse
            self.__init__(self.path, self.sid)
        with open(self.path, "r", errors="replace") as fh:
            fh.seek(self.offset)
            buf = fh.read()
        # Only consume complete lines; leave a partial tail for the next poll.
        cut = buf.rfind("\n")
        if cut < 0:
            return
        self.offset += len(buf[:cut + 1].encode("utf-8", errors="replace"))
        for line in buf[:cut + 1].splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue          # partial writes are normal, not fatal
            self._row(d)

    def _row(self, d):
        t = d.get("type")
        stamp = _ts(d.get("timestamp"))
        self._stamp(stamp)
        self.uuid = self.uuid or d.get("sessionId")
        self.cwd = self.cwd or d.get("cwd")
        self.branch = self.branch or d.get("gitBranch")

        # Names are time-varying: agent-name appears once per RENAME. Taking the
        # first one labels a session as whatever it used to be called and
        # reverses every edge, with no error anywhere.
        if t == "agent-name":
            return self._rename(d.get("agentName"))
        if t == "custom-title":
            return self._rename(d.get("customTitle"))
        if t == "bridge-session":
            self.core = ulid(d.get("bridgeSessionId"))
            return
        if t == "queue-operation":
            return self._queue(d, stamp)

        origin = d.get("origin")
        if isinstance(origin, dict):
            if origin.get("kind") == "peer":
                return self._origin_peer(origin, stamp)
            if origin.get("kind") == "human":
                return self._human_boundary(d, stamp)

        for b in _blocks(d.get("message")):
            if b.get("type") == "tool_use" and b.get("name") == "SendMessage":
                self._send(b, stamp)
            elif b.get("type") == "text":
                self._legacy(b.get("text") or "", stamp)
            # tool_result is NEVER a message source. It is where the phantoms
            # come from: reading this repo's source puts the pattern in one.

    # -- outbound ---------------------------------------------------------
    def _send(self, b, stamp):
        inp = b.get("input") or {}
        # `to` and `recipient` ARE duplicates — verified equal on every real
        # SendMessage seen so far.
        to = inp.get("to") or inp.get("recipient")

        # `message` and `content` are NOT. This was documented as a duplicate
        # pair and it is wrong: `content` is a ~50-character UI PREVIEW ending in
        # an ellipsis, and `message` is the full body. Measured on a live
        # transcript: 5044/2927/9835-char messages against a 50-char content
        # every time. So `content` must never be used as a body fallback — doing
        # so silently truncates every outbound message to a preview, and the
        # result still looks like prose, so nothing downstream would flag it.
        body = inp.get("message")
        preview = inp.get("content")
        if body is None:
            if preview is not None:
                print(f"model: SendMessage has no `message`, only the truncated "
                      f"`content` preview — dropping rather than storing a "
                      f"50-char stub ({os.path.basename(self.path)})",
                      file=sys.stderr)
            return
        if not to:
            return
        self.out.append({"to_name": to, "sent": stamp,
                         "summary": inp.get("summary") or "", "body": body})

    # -- inbound ----------------------------------------------------------
    def _queue(self, d, stamp):
        """queue-operation is the AUTHORITATIVE inbound set.

        The tempting rule — "only build a peer event from an origin record,
        queue-operation is telemetry" — is exactly backwards and fails silently:
        `origin` is written only for turn-boundary deliveries, so on a busy
        session it covered 2 of 5 inbound messages. A message injected into a
        turn that is already running has no origin record anywhere, ever.
        """
        op, content = d.get("operation"), d.get("content")
        if op == "enqueue":
            if content is None:
                return
            if content.lstrip().startswith("<cross-session-message"):
                m = _OPEN_TAG.search(content)
                attrs = _attributed(m.group(1) if m else "")
                if not attrs:
                    return                       # unattributable -> drop, never guess
                body = _inner(content)
                if body is None:
                    return
                rec = {"from_id": ulid(attrs.get("from")),
                       "from_name": attrs.get("from-name"),
                       "hops": [h for h in (attrs.get("hop-chain") or "").split(",") if h],
                       "body": body, "enqueued": stamp,
                       "delivered": None, "delivery": None, "kind": "peer"}
                self._pending.append(rec)
                self.inbound.append(rec)
                self._by_body.setdefault(body, rec)
            else:
                rec = self._human(content, enqueued=stamp)
                self._pending.append(rec)
            return

        if op in ("dequeue", "remove"):
            # dequeue carries no content; remove repeats it. Match remove by body
            # when we can, else fall back to FIFO — a queue drains in order.
            rec = None
            if content:
                body = _inner(content) if content.lstrip().startswith("<") else content
                rec = next((r for r in self._pending if r["body"] == body), None)
            if rec is None and self._pending:
                rec = self._pending[0]
            if rec is None:
                return
            self._pending.remove(rec)
            rec["delivered"] = stamp
            # Observed: dequeue = consumed at a turn boundary, remove = pulled out
            # mid-turn. Deliberately NOT derived from a dwell threshold: the two
            # regimes overlap (0.018s boundary against 0.109s mid-turn on a live
            # transcript), so no threshold separates them without misclassifying.
            rec["delivery"] = "boundary" if op == "dequeue" else "midturn"

    def _origin_peer(self, origin, stamp):
        """An origin record is the PREFERRED source where it exists — clean
        unescaped body, resolved name, hopChain — but it is a partial subset, so
        it enriches an enqueued record rather than creating one."""
        body = (origin.get("body") or "").strip()
        rec = self._by_body.get(body)
        if rec is None:
            rec = {"from_id": ulid(origin.get("from")),
                   "from_name": origin.get("name"),
                   "hops": origin.get("hopChain") or [],
                   "body": body, "enqueued": None, "delivered": stamp,
                   "delivery": "boundary", "kind": "peer"}
            self.inbound.append(rec)
            self._by_body[body] = rec
            return
        rec["body"] = body or rec["body"]
        rec["from_name"] = origin.get("name") or rec.get("from_name")
        rec["from_id"] = ulid(origin.get("from")) or rec.get("from_id")
        if origin.get("hopChain"):
            rec["hops"] = origin["hopChain"]

    def _human_boundary(self, d, stamp):
        text = ""
        for b in _blocks(d.get("message")):
            if b.get("type") == "text":
                text += b.get("text") or ""
        if not text.strip():
            return
        self._human(text.strip(), delivered=stamp, delivery="boundary")

    def _human(self, body, enqueued=None, delivered=None, delivery=None):
        """One record per human turn, merged within the transcript.

        A typed message reaches the transcript by up to two routes: the queue
        (enqueue/remove, which is the only route for a mid-turn injection) and a
        `user` record carrying origin.kind == "human". A message that goes
        through the queue and is then delivered normally produces BOTH, so
        appending each one showed the same sentence twice in the stream, 28
        seconds apart, as though the user had repeated themselves. Measured on a
        live transcript: 25 human events for 22 distinct bodies.

        Same shape as the peer duplication and the same fix — merge on the body
        within one transcript, keeping whichever clock each route supplies.
        """
        body = (body or "").strip()
        if not body:
            return None
        rec = self._human_by_body.get(body)
        if rec is None:
            rec = {"body": body, "enqueued": None, "delivered": None,
                   "delivery": None, "kind": "human"}
            self._human_by_body[body] = rec
            self.human.append(rec)
        if enqueued and not rec["enqueued"]:
            rec["enqueued"] = enqueued
        if delivered and not rec["delivered"]:
            rec["delivered"] = delivered
        if delivery and not rec["delivery"]:
            rec["delivery"] = delivery
        return rec

    def _legacy(self, raw, stamp):
        """Pre-`origin` encoding: the tag inside a text block, with no
        queue-operation and no origin record. Still has to be readable."""
        if "<cross-session-message" not in raw:
            return
        for m in _OPEN_TAG.finditer(raw):
            attrs = _attributed(m.group(1))
            if not attrs:
                continue
            body = _inner(raw[m.start():])
            if body is None or body in self._by_body:
                continue
            rec = {"from_id": ulid(attrs.get("from")),
                   "from_name": attrs.get("from-name"),
                   "hops": [h for h in (attrs.get("hop-chain") or "").split(",") if h],
                   "body": body, "enqueued": None, "delivered": stamp,
                   "delivery": None, "kind": "peer"}
            self.inbound.append(rec)
            self._by_body[body] = rec



# --------------------------------------------------------------------------
# Exchanges: one conversation, not one message
# --------------------------------------------------------------------------

def _gap_threshold(times):
    """Where one conversation ends and the next begins.

    Adaptive rather than fixed, because a pair trading messages every 30 seconds
    and a pair trading them every 10 minutes are both continuous. Six times the
    median gap, floored at 10 minutes so a fast burst is not chopped up, capped
    at an hour so a slow day is not welded into one blob. The chosen value is
    reported alongside the result, so the split is inspectable rather than magic.
    """
    if len(times) < 3:
        return 20 * 60
    gaps = sorted(b - a for a, b in zip(times, times[1:]))
    med = gaps[len(gaps) // 2] or 60
    return max(10 * 60, min(60 * 60, med * 6))


def exchanges(events):
    """Group agent-to-agent messages into conversations, and describe each.

    The description is EXTRACTIVE, deliberately. There is no model at render time
    and there never will be — the page is stdlib and self-contained. So "what was
    discussed" is built from what the participants themselves wrote: the summary
    a sender passed to SendMessage, or the opening line it wrote when it passed
    none. "What was decided" is the sentences in which one of them marked a
    conclusion. Nothing here is the tool's opinion of what mattered; it is their
    words, selected structurally by time and position.
    """
    stamped = []
    for e in events:
        if e.get("kind") != "peer":
            continue
        t = _epoch(e.get("sent") or e.get("enqueued") or e.get("delivered"))
        if t is not None:
            stamped.append((t, e))
    stamped.sort(key=lambda r: r[0])
    if not stamped:
        return [], 20 * 60

    thresh = _gap_threshold([t for t, _ in stamped])
    groups, cur = [], [stamped[0]]
    for prev, nxt in zip(stamped, stamped[1:]):
        if nxt[0] - prev[0] > thresh:
            groups.append(cur)
            cur = [nxt]
        else:
            cur.append(nxt)
    groups.append(cur)

    out = []
    for g in groups:
        evs = [e for _, e in g]
        who = []
        for e in evs:
            for side in (e.get("from_id"), e.get("to_id")):
                if side and side not in who:
                    who.append(side)
        topics, seen = [], set()
        for e in evs:
            head = (e.get("summary") or "").strip() or (e.get("preview") or "").strip()
            key = head.lower()[:60]
            if head and key not in seen:
                seen.add(key)
                topics.append({"who": e.get("from_id"), "text": head[:150],
                               "event": e["id"]})
        decisions = [{"who": e.get("from_id"), "kind": e["mark"]["kind"],
                      "quote": e["mark"]["quote"], "event": e["id"]}
                     for e in evs if e.get("mark")]
        out.append({
            "id": evs[0]["id"],
            "who": who,
            "start": evs[0].get("sent") or evs[0].get("enqueued") or evs[0].get("delivered"),
            "end": evs[-1].get("delivered") or evs[-1].get("sent") or evs[-1].get("enqueued"),
            "n": len(evs),
            "topics": topics[:6],
            "decisions": decisions[:6],
            "n_decisions": len(decisions),
        })
    out.reverse()
    return out, int(thresh)


# --------------------------------------------------------------------------
# Merge across transcripts
# --------------------------------------------------------------------------

_STATE = {"sources": {}, "seq_by_id": {}, "next_seq": 1,
          "prev": {}, "snapshot": 0}


def _key(from_id, to_id, body):
    h = hashlib.sha1(f"{from_id}|{to_id}|{body}".encode("utf-8", "replace"))
    return h.hexdigest()


def _event_id(from_id, body, ordinal):
    """Stable across identity resolution — deliberately excludes `to_id`.

    Ordinal disambiguates repeated short bodies ("ack", "ping"). It is never
    assumed to agree across sides: a lost message desynchronises the counters,
    and a lost message is precisely what the delivery alert exists to detect, so
    the two features would fail together. Cross-side identity comes from pairing,
    not from hash equality.

    `to_id` is NOT hashed, because it is not stable. An outbound to a session we
    have never seen resolves to `name:<whatever>` until that session's transcript
    appears, and then becomes a ULID — measured: the same message changed id from
    5533b6a4cc55 to 713579d70dd6 when a peer's transcript joined. A transcript
    joining mid-run is the normal case for a live monitor, and anything anchored
    to an id (a curated finding, a client-side selection) would silently point at
    nothing. Ordinals are assigned per (from_id, body) in clock order, so a true
    broadcast — the same body sent to two peers — still gets distinct ids.
    """
    h = hashlib.sha1(f"{from_id}|{body}|{ordinal}".encode("utf-8", "replace"))
    return h.hexdigest()[:12]


def build(paths):
    paths = [p for p in paths if os.path.exists(p)]

    # -- tail each transcript, reusing state so only the new bytes are parsed
    for i, p in enumerate(sorted(paths)):
        src = _STATE["sources"].get(p)
        if src is None:
            src = _STATE["sources"][p] = Source(p, f"t{len(_STATE['sources'])}")
        src.read()
    live = [_STATE["sources"][p] for p in sorted(paths)]

    # -- sessions table -------------------------------------------------
    sessions, name_to_id, alias_to_id = {}, {}, {}
    for s in live:
        # A transcript may carry no identity records at all — the pre-`origin`
        # fixture has no bridge-session, no agent-name and no sessionId. The id
        # must still be stable, and it must NEVER be the path: session ids render
        # as node labels when no name resolves, so a path here captions the graph
        # with the user's home directory. That is the same leak `src_id` exists to
        # prevent, arriving through the identity field instead of the source one.
        core = s.core or s.uuid or f"anon:{hashlib.sha1(
            s.path.encode('utf-8', 'replace')).hexdigest()[:10]}"
        s.resolved = core
        # Display name never derives from the id either, for the same reason:
        # core[:8] of a path is a path fragment.
        label = s.name or (s.uuid[:8] if s.uuid else f"session {s.sid}")
        sessions[core] = {
            "name": label, "aka": list(s.aka), "uuid": s.uuid,
            "cwd": unhome(s.cwd), "branch": s.branch, "first": s.first, "last": s.last,
            "ghost": False, "src_id": s.sid,
        }
        # CURRENT names and FORMER names are not interchangeable, and treating
        # them as one namespace inverts the graph. A session renamed session-b -> session-a
        # keeps "session-b" in its aka list, while the real peer is *called*
        # session-b now.
        # Outbound carries a NAME, so every message addressed to "session-b" then
        # resolved to the sender itself and the mesh drew a 20-message self-loop
        # with no edge to the peer at all — observed on a live transcript.
        # Current names win outright; an alias only ever fills a gap.
        if s.name:
            name_to_id[s.name] = core
        for n in s.aka:
            if n:
                alias_to_id.setdefault(n, core)

    # A peer we have no transcript for still exists in the mesh. Marked
    # explicitly rather than inferred from a null uuid: "we hold no transcript"
    # and "we could not read a uuid" are different facts and will diverge.
    def ghost(core, name=None):
        if core and core not in sessions:
            sessions[core] = {"name": name or core[:8], "aka": [], "uuid": None,
                              "cwd": None, "branch": None, "first": None,
                              "last": None, "ghost": True, "src_id": None}
            if name:
                name_to_id.setdefault(name, core)

    for s in live:
        for rec in s.inbound:
            fid = rec.get("from_id")
            if is_core(fid):
                ghost(fid, rec.get("from_name"))

    # -- collect both sides ---------------------------------------------
    outs, ins = [], []
    for s in live:
        for rec in s.out:
            # Resolve current names first, then aliases — and never to the sender
            # itself. A session does not SendMessage to itself, so a self-resolve
            # is proof the name belongs to someone else whose transcript we may
            # not hold; minting a ghost is honest, a self-loop is a fabrication.
            # A `to` that is an ADDRESS rather than a name (a same-machine peer
            # answered at its socket) identifies nobody we can join to, and must
            # never be printed: it is a filesystem path, which is the one thing
            # `src_id` exists to keep off the page.
            if not is_core(rec["to_name"]) and ("/" in rec["to_name"] or ":" in rec["to_name"]):
                key = "local:" + hashlib.sha1(
                    rec["to_name"].encode("utf-8", "replace")).hexdigest()[:8]
                ghost(key, "unidentified local peer")
                rec = dict(rec, to_name=key)
                to_id = key
            else:
                to_id = name_to_id.get(rec["to_name"])
            if to_id is None or to_id == s.resolved:
                alias = alias_to_id.get(rec["to_name"])
                to_id = alias if alias and alias != s.resolved else None
            if to_id is None:
                # Outbound `to` is a NAME while inbound `from` is an ID; an
                # unknown name is a session we have never seen either way.
                to_id = f"name:{rec['to_name']}"
                ghost(to_id, rec["to_name"])
            outs.append({"from_id": s.resolved, "to_id": to_id, "src": s,
                         "sent": rec["sent"], "summary": rec["summary"],
                         "body": unhome(scrub(rec["body"])).strip()})
        for rec in s.inbound:
            fid = rec.get("from_id")
            if not is_core(fid):
                # Fall back to the display name the record carries, which is the
                # only stable handle a socket-addressed peer gives us.
                nm = rec.get("from_name")
                if nm and not is_core(nm) and ("/" in nm or ":" in nm):
                    fid = "local:" + hashlib.sha1(
                        nm.encode("utf-8", "replace")).hexdigest()[:8]
                    nm = "unidentified local peer"
                else:
                    fid = (name_to_id.get(nm) or alias_to_id.get(nm)
                           or ("name:" + nm if nm else None) or "unknown")
                if fid and fid not in sessions:
                    ghost(fid, nm)
            ins.append({"from_id": fid, "to_id": s.resolved, "src": s,
                        "enqueued": rec.get("enqueued"), "delivered": rec.get("delivered"),
                        "delivery": rec.get("delivery"), "hops": rec.get("hops") or [],
                        "body": unhome(scrub(rec["body"])).strip()})

    # -- pass 1: exact match on (from, to, scrubbed body), paired in order
    # Grouping and zipping rather than comparing ordinal-salted hashes is what
    # makes the desync case degrade honestly: three "ack"s out and two in leaves
    # ONE unpaired outbound, instead of a false "never landed" plus an orphan.
    def group(rows):
        g = {}
        for r in rows:
            g.setdefault(_key(r["from_id"], r["to_id"], r["body"]), []).append(r)
        return g

    g_out, g_in = group(outs), group(ins)
    merged, used_in = [], set()
    for k, o_rows in g_out.items():
        i_rows = g_in.get(k, [])
        for n, o in enumerate(o_rows):
            if n < len(i_rows):
                merged.append(("both", o, i_rows[n]))
                used_in.add(id(i_rows[n]))
            else:
                merged.append(("out", o, None))
    leftover_in = [r for r in ins if id(r) not in used_in]

    # -- pass 2: whitespace-normalised retry for near-misses
    def norm(b):
        return re.sub(r"\s+", " ", b).strip()

    unpaired_out = [m for m in merged if m[0] == "out"]
    if leftover_in and unpaired_out:
        idx = {}
        for r in leftover_in:
            idx.setdefault((r["from_id"], r["to_id"], norm(r["body"])), []).append(r)
        for n, m in enumerate(merged):
            if m[0] != "out":
                continue
            o = m[1]
            cand = idx.get((o["from_id"], o["to_id"], norm(o["body"])))
            if cand:
                i = cand.pop(0)
                merged[n] = ("both", o, i)
                used_in.add(id(i))
        leftover_in = [r for r in ins if id(r) not in used_in]

    # -- pass 3: whatever is still unpaired is genuinely one-sided
    for r in leftover_in:
        merged.append(("in", None, r))

    # -- human turns: a self-edge, excluded from the mesh by the renderer.
    for s in live:
        for rec in s.human:
            merged.append(("human", None, {
                "from_id": s.resolved, "to_id": s.resolved, "src": s,
                "enqueued": rec.get("enqueued"), "delivered": rec.get("delivered"),
                "delivery": rec.get("delivery"), "hops": [],
                "body": unhome(scrub(rec["body"])).strip()}))

    # -- emit ------------------------------------------------------------
    ordinals = {}
    events = []
    # Ordinals are assigned in CLOCK order, not in grouping order, so that the id
    # does not depend on how `merged` happened to be bucketed — bucketing is keyed
    # on to_id, which moves when a name resolves.
    def _when(m):
        b = m[1] or m[2]
        return (b.get("sent") or b.get("enqueued") or b.get("delivered") or "",
                b["from_id"] or "", b["body"][:64])
    for sides, o, i in sorted(merged, key=_when):
        base = o or i
        from_id, to_id = base["from_id"], base["to_id"]
        body = base["body"]
        k = (from_id, body)
        ordinals[k] = ordinals.get(k, -1) + 1
        eid = _event_id(from_id, body, ordinals[k])
        kind = "human" if sides == "human" else "peer"
        ev = {
            "id": eid,
            "kind": kind,
            "from_id": from_id,
            "to_id": to_id,
            "sent": (o or {}).get("sent"),
            "enqueued": (i or {}).get("enqueued"),
            "delivered": (i or {}).get("delivered"),
            "sides": "both" if sides == "both" else ("out" if sides == "out" else "in"),
            "delivery": (i or {}).get("delivery"),
            "hops": (i or {}).get("hops") or [],
            "summary": (o or {}).get("summary") or "",
            "preview": preview((o or {}).get("summary"), body),
            "mark": mark(body),
            "body": body,
            "tags": tag(body),
            "words": len(body.split()),
            "src_id": (base.get("src").sid if base.get("src") else None),
        }
        if kind == "human":
            ev["sides"] = "both"
        events.append(ev)

    # -- seq: monotonic in DISCOVERY order, never time order. A transcript added
    # later must not want seq numbers already issued, or every live cursor is
    # invalidated. The page sorts by clock for display.
    for ev in sorted(events, key=lambda e: (e["sent"] or e["enqueued"]
                                            or e["delivered"] or "", e["id"])):
        if ev["id"] not in _STATE["seq_by_id"]:
            _STATE["seq_by_id"][ev["id"]] = _STATE["next_seq"]
            _STATE["next_seq"] += 1
    for ev in events:
        ev["seq"] = _STATE["seq_by_id"][ev["id"]]
    events.sort(key=lambda e: e["seq"])

    # -- snapshot_id changes ONLY on a revision to an already-emitted event, not
    # on an append. Pass-2 pairing merges two emitted events into one, which is
    # an update plus a retraction; the client refetches rather than us inventing
    # a tombstone protocol. Bumping on every append would make it refetch always.
    digests = {e["id"]: hashlib.sha1(
        json.dumps({k: v for k, v in e.items() if k != "seq"},
                   sort_keys=True).encode()).hexdigest() for e in events}
    prev = _STATE["prev"]
    revised = any(k in prev and prev[k] != v for k, v in digests.items()) or \
        any(k not in digests for k in prev)
    if revised:
        _STATE["snapshot"] += 1
    _STATE["prev"] = digests

    sources = [{"id": s.sid, "name": (s.name or os.path.basename(s.path))}
               for s in live]

    xchg, thresh = exchanges(events)
    out = {"events": events, "sessions": sessions, "sources": sources,
           "exchanges": xchg, "exchange_gap_s": thresh,
           "snapshot_id": f"s{_STATE['snapshot']:04d}",
           "seq": _STATE["next_seq"] - 1}

    # Fail loudly rather than emit a path. This leak has now been introduced
    # twice by two different routes — `cwd` in the sessions table, and the
    # session id itself when a transcript carries no identity records — after
    # `src_id` was designed specifically to prevent it. A check that can only be
    # skipped by deleting it is worth more than remembering the rule.
    def _leak(val):
        if not isinstance(val, str):
            return None
        m = _HOME.search(val) or _SLUG.search(val) or _PROMPT.search(val)
        return m.group(0) if m else None

    for sid, meta in sessions.items():
        for field, val in (("id", sid), ("name", meta.get("name")),
                           ("cwd", meta.get("cwd"))):
            hit = _leak(val)
            if hit:
                raise ValueError(
                    f"refusing to emit: session {field} contains a username "
                    f"({hit!r}). Session ids and names render as node labels; a "
                    f"path there publishes the user's name. See DESIGN.md on src_id.")
    # Bodies too. This leak has now arrived by four different routes — src_id was
    # built to stop it, then cwd carried it, then the session id, then a project
    # slug quoted inside a message. Each fix was correct and the next route was
    # invisible to it, so the guard checks the emitted payload rather than the
    # places we currently believe it can come from.
    for ev in events:
        hit = (_leak(ev.get("body")) or _leak(ev.get("summary"))
               or _leak(ev.get("preview")))
        if hit:
            raise ValueError(
                f"refusing to emit: event {ev['id']} still contains a username "
                f"({hit!r}) after redaction. A new path shape reached the page; "
                f"extend unhome() rather than relaxing this check.")
    return out


def reset():
    """Drop all cached state. For tests and for a clean rebuild."""
    _STATE.update({"sources": {}, "seq_by_id": {}, "next_seq": 1,
                   "prev": {}, "snapshot": 0})

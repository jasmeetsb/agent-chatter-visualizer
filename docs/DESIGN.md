# Live multi-session dashboard — design contract

What the transcript format actually does, and why this tool reads it the way it
does. Every claim here was measured on real transcripts from two machines rather
than inferred from documentation — the two are referred to as machine A and
machine B throughout.

Re-verify before trusting any of it against a new Claude Code version.

Status of each section is marked SETTLED or OPEN. Do not build against an OPEN
item.

## Why the current tools can't just be made live

`extract-peer-conversation.py` was written for a *pair* of sessions and a
*finished* transcript. Four things break when you go to N sessions and live:

1. **Identity is binary and hardcoded.** `extract()` emits the literal strings
   a hardcoded pair of direction strings, and the page derived its channel by
   testing which one a message started with. There was no room for a third
   session.
2. **Merging N transcripts double-counts.** Every logical message exists twice —
   outbound in the sender's JSONL, inbound in the receiver's. Merging is
   additive today, so a mesh inflates.
3. **Full re-parse doesn't scale.** Transcripts are 10–20 MB and append-only.
4. **The regex fabricates messages.** See below. This is the serious one.

## Verified facts about the transcript format

Everything here was confirmed empirically on both machines' live transcripts,
not inferred from documentation. Re-verify before trusting it on a new
Claude Code version.

### Peer messages arrive as structured data (primary path)

The delivering record carries the message at **top level**, not in the text:

```json
{"type": "user", "isMeta": true, "promptSource": "system",
 "origin": {"kind": "peer",
            "from": "bridge:session_01EXAMPLEVM2BBBBBBBBBBBB",
            "name": "session-b",
            "fromMode": "prompting",
            "hopChain": ["aaaa1111bbbb2222cccc3333"],
            "body": "<full body, unescaped>"}}
```

Sender id, sender display name, mode, hop chain and a clean body — no regex, no
attribute parsing, no HTML unescaping, no truncation risk. Where an `origin`
record exists it is always the **preferred** source for that message.

**But it does not exist for every message.** `origin` records are written only
for messages delivered at a turn boundary; a message injected into a turn that is
already running never gets one. See "Delivery mode" below — on one of the two
transcripts here, `origin` covers 2 of 5 inbound peer messages. Tag parsing is
therefore *not* a legacy compatibility path: it is the only source for the rest,
and it has to be as solid as the `origin` path.

### Outbound messages

An assistant `tool_use` block with `name == "SendMessage"`. Input keys:
`['content', 'message', 'recipient', 'summary', 'to', 'type']`.

`to` and `recipient` **are** duplicates — verified equal on every real
`SendMessage` observed. Take `to || recipient`.

`message` and `content` are **not**, and this document previously said they were.
`content` is a ~50-character **UI preview ending in an ellipsis**; `message` is
the full body. Measured on a live transcript, 16 of 16 diverged:

```
message : 5044 chars      content : 50 chars      'Taking the migration in two halves so we are not b…'
message : 2927 chars      content : 50 chars      'Staging applied in 6 minutes, backfill running. Fl…'
message : 9835 chars      content : 50 chars      'Runbook amended to 500 and marked REQUIRES VERIFIC…'
```

So **never fall back to `content` for the body.** `message || content` silently
truncates every outbound message to a preview, and the result still reads as
prose, so nothing downstream flags it. If `message` is absent, drop the record and
say so; a 50-character stub is worse than a gap because it looks like data.

How the wrong claim survived: the first inspection printed the field truncated for
display, and the ellipsis in the *data* was read as the ellipsis added by the
*printing*. The check that caught it was the "log when the pair disagrees" rule
this section already carried — it fired 16 times the first time the model was run
against a real transcript rather than the fixture. The rule worked; the claim it
was guarding was the thing that was wrong.

Note the fixture does not reproduce this: `make-mesh-fixture.py` writes `message`
and `content` identically, because it was built from the same wrong belief. Any
tool that regresses to `content` will pass the fixture and truncate live data.

Note the asymmetry: outbound `to` is a **name**, inbound `origin.from` is an
**id**. Outbound edges therefore need name→id resolution through the sessions
table, including across renames.

### Human input has two shapes, and one of them is invisible

Turns the user typed appear in **two different forms**, and a single transcript
can contain both — so this is not a version difference between machines:

| Shape | Where it lives |
|---|---|
| Typed at a turn boundary | `user` record, `origin.kind == "human"`, `promptSource: "typed"` |
| Injected **mid-turn** | `queue-operation` records only — `enqueue` then `remove`, `content` = the typed text. **No `origin` record is written at all.** |

Handling only the first shape under-counts human intervention, and the failure is
biased in the worst possible direction: mid-turn injection happens precisely when
a session is in a long run, so the *most-steered* sessions would be reported as
the *most autonomous*. Directional, plausible, and unfalsifiable after the fact —
the same signature as the first-`agent-name` trap.

Observed operation vocabulary:

```
PEER   message:  enqueue [content=<cross-session-message…>]  ->  dequeue (no content)
HUMAN  mid-turn: enqueue [content=raw text]                  ->  remove  (content repeated)
```

Consistent with `dequeue` = consumed at a turn boundary and `remove` = pulled out
and injected mid-turn, but that is **observed, not proven** (n=1 for the human
case). Do not build the detector on the operation name. The safe discriminator is
the content: a `queue-operation` whose `content` starts with
`<cross-session-message` is peer traffic, anything else is human.

### queue-operation duplicates peer messages *within* one transcript

Dedup was designed around cross-transcript duplication — the sender's copy versus
the receiver's. That is not the only source. Every inbound peer message appears in
the receiving transcript **twice**:

1. a `queue-operation` / `enqueue` whose `content` is the full raw
   `<cross-session-message …>` text, and
2. the delivered `user` record carrying `origin.kind == "peer"` and a clean body.

Two records, one machine, one logical message. Two-pass pairing keys on
`(from_id, to_id, body-hash)` *across* transcripts and does not deduplicate within
one, so a naive implementation doubles every edge in the mesh, doubles per-session
message counts, and lets reply-latency pair against whichever copy it reaches
first. The `queue-operation` copy also has no `origin`, so it falls to the tag
path and lands differently attributed.

It is a phantom source too: that `content` carries the raw tag text, so "scan every
record for `<cross-session-message`" hits it.

### Delivery mode — why `origin` alone loses messages

The obvious fix to the duplication above is "only ever construct a peer event
from a record carrying `origin.kind == 'peer'`; `queue-operation` is telemetry,
never a message source." **That rule is wrong and it fails silently.** Measured
on the other machine's transcript:

```
distinct peer messages via queue-operation enqueue : 5
peer messages via origin.kind == "peer" records    : 2
>>> lost if origin is the only source              : 3 of 5
```

The three missing messages have no `origin` record anywhere — searching every
record for a phrase unique to one of them returns `queue-operation` enqueue,
`queue-operation` remove, and an `attachment` record, and nothing else.

The two delivery modes are fully distinguishable, and this also settles the
`dequeue`/`remove` question that was `n=1`:

| Mode | Record sequence | `origin` written? |
|---|---|---|
| Boundary | `enqueue`(content) → `dequeue`(**empty** content) | yes |
| Mid-turn | `enqueue`(content) → `remove`(**full** content) | **no, never** |

the other machine's transcript: 2 boundary, 3 mid-turn peer, 1 mid-turn human.

Why the two machines disagreed, which is the part worth remembering: every
message machine B sent happened to reach machine A at a turn boundary, so one machine's sample
contained zero mid-turn peer deliveries and supported "`origin` is always
present". the other machine's sample is the complement. Neither transcript alone can show this,
and either alone generalises confidently to the wrong rule.

**Model rule, corrected:** `queue-operation` / `enqueue` whose `content` starts
with `<cross-session-message` is the *authoritative* inbound set. Where an
`origin` record exists for that message, prefer it for the body, resolved name
and `hopChain`. Deduplicate the two views **within** the transcript — the goal is
to merge them, not to discard either.

Two things about that merge:

- **Hash the inner body, not the raw content.** Raw `enqueue` and `remove`
  content differ for the same message — by 62 and 112 bytes on the other machine's transcript —
  so a raw hash fails to match them and reproduces the doubled counts the dedup
  was meant to prevent. **Nothing is truncated:** extracting the text between the
  tags makes the two copies byte-identical, sha1-for-sha1, in every observed
  case. The entire delta is one attribute the `remove` copy drops — see below.
- Record counts per inbound message: the boundary path writes three records
  (`enqueue`, contentless `dequeue`, `origin`) but only **two payloads**. The
  mid-turn path writes two records and two payloads. Key the merge on payloads.

#### The dropped attribute is `hop-chain`, and it is worth recovering

The `enqueue` copy carries an attribute the delivered copy does not:

```
enqueue: <cross-session-message from="bridge:session_…"
                                hop-chain="aaaa1111…,dddd4444…"
                                from-name="…" from-mode="prompting">
remove:  <cross-session-message from="bridge:session_…" from-name="…" from-mode="prompting">
```

The byte deltas are exactly this attribute's length: 62 bytes for a 2-element
chain, 112 for a 4-element one. That accounts for the difference completely, and
is why no truncation-tolerant merge is needed.

It also means **`hop-chain` is available for mid-turn deliveries after all**, via
the `enqueue` tag attribute rather than via `origin`. Coverage is all inbound
messages, not just boundary ones.

### Delivery mode and queue dwell are NOT the same measurement

Both are mechanically observable and both answer something the mesh graph cannot:
was the recipient busy when this landed? `delivery` is the category (read from
`dequeue` vs `remove`); dwell is `delivered - enqueued`, the continuous version.

**They are not derivable from each other, and the regimes overlap.** Measured on
the other machine's transcript, which has 7 mid-turn samples where one machine's has none:

```
boundary  n=2   0.014s .. 0.018s
midturn   n=7   0.109s .. 73.757s
fastest midturn / slowest boundary = 6.1x
```

Mid-turn spans nearly three orders of magnitude *within itself*, and its fast end
is 6× the slow end of boundary — not the clean thousand-fold separation a small
sample suggested. So do not derive `delivery` from a dwell threshold, and do not
drop one as the other's "boolean shadow"; a later simplification that thresholds
dwell would misclassify the fast mid-turn cases. Keep both, measured
independently.

### Dwell measures interruptibility, not backlog

Worth stating before dwell gets read as a saturation index. Mid-turn dwell is
"how long until the recipient's current tool call finished", which is a property
of *work granularity*. A session running one 70-second command and a session with
a deep backlog of queued messages produce the same high dwell, and they are not
the same condition.

The honest saturation measure is **queue depth** — the count of enqueued-but-not-
yet-resolved messages at time *t*, which is directly computable by walking
`enqueue` against its matching `dequeue`/`remove`. Depth separates the two cases
that dwell conflates: one long operation is depth 1 with high dwell; drowning is
depth > 1. Render depth as the saturation signal and dwell as latency.

**Untestable on a pair.** Max observed depth across an entire transcript is 1,
never 2. On two sessions it may be structurally impossible to exceed 1, since a
session has only one peer that can talk at it. So the saturation view is dead on
arrival for the only configuration currently testable, and becomes meaningful
only at 3+ sessions. Not an argument against depth — it is still the right
metric — but it needs a three-session test before the view is trusted, and the
fixture must carry a genuine depth-2 interval so the computation is exercised
even though neither live transcript can produce one.

### Session identity is time-varying

Sessions carry their own name in dedicated records, not message records:

```json
{"type": "agent-name",   "agentName":   "session-a", "sessionId": "..."}
{"type": "custom-title", "customTitle": "session-a", "sessionId": "..."}
```

**These appear once per rename.** The transcript that produced this document
contains `session-b` followed by `session-a`, because the
session was renamed mid-run. A parser that takes the *first* `agent-name` labels
this session as its own peer and reverses every edge in the graph, with no error
raised anywhere. Track the current name as you walk the file.

Because names are mutable they are **never stored on an event** — see the schema.

### The three-namespace join problem

The same session is addressed three ways, with the same ULID core:

| Where | Form |
|---|---|
| Own transcript, `bridge-session` record | `cse_01EXAMPLEVM1AAAAAAAAAAAA` |
| Peer's inbound `origin.from` | `bridge:session_01EXAMPLEVM1AAAAAAAAAAAA` |
| Harness session URL | `session_01EXAMPLEVM1AAAAAAAAAAAA` |

Confirmed from both ends, both directions. Join on the ULID core after stripping
`bridge:session_` / `cse_` / `session_`. Joining on the raw string splits every
session into two nodes — one from its own transcript, one from how peers address
it — silently, and the graph still looks plausible.

The local `sessionId` uuid is **not** usable as the join key: it does not appear
in any peer's transcript, and in a mesh we cannot assume we hold every
transcript. Keep it in the sessions table as nullable enrichment.

## The regex fabricates messages — SETTLED, must fix

The current inbound pattern is:

```python
r"<cross-session-message[^>]*>(.*?)</cross-session-message>"
```

`[^>]*` matches any text that merely *describes* the format. Run against this
project's own live transcripts, the current `extract()` returns **5 messages, of
which 2 are fabricated**:

```
#1  body='…'          (1 char)   attributed to a real peer
#2  body=']*>(.*?)'   (8 chars)  attributed to a real peer
```

`#2` is a fragment of the extractor's own source, captured
because the file was read into the session. Both machines reproduced the same
two phantoms independently. Sources were: reads of the extractor, the README,
`AGENTS.md`, and the fixture.

Two properties make this worse than a cosmetic bug:

- The phantoms are attributed to a **real peer**, so they are not isolated junk
  nodes — they are phantom *edges on a real node*, with nothing anomalous to
  notice.
- The phantom rate is a function of how much the format is discussed. This repo
  is the worst case for its own tool: dogfooding guarantees the transcript is
  full of text describing the format. "Eyeball the static output" was never a
  real mitigation.

### Fix, verified on both machines

Primary: trust `origin.kind == "peer"`. Fallback: require a well-formed
`from="bridge:..."` attribute. Never construct a message from a tag that cannot
be attributed — **drop, don't guess.**

Measured on one machine's transcript: 6 open tags found, exactly 1 kept, and it is the
real message. The 5 dropped carried attrs `' …'`, `''`, `'" in raw:\n81\t…'`,
`''`, `'[^'`. Perfect separation on the other machine's transcript as well.

## Event schema — SETTLED except where noted

Two tables. Events are append-only and immutable; sessions are mutable display
state re-sent on every poll.

```jsonc
// events[] — one per logical message
{"id":      "sha1[:12] of from_id|to_id|scrubbed_body|ordinal",
 "seq":     1417,              // monotonic, doubles as the poll cursor
 "kind":    "peer",            // "peer" | "human"
 "from_id": "01EXAMPLEVM1AAAAAAAAAAAA",   // prefix-stripped ULID core
 "to_id":   "01EXAMPLEVM2BBBBBBBBBBBB",
 "sent":     "2026-08-16T21:20:31.412Z",  // sender's clock — SendMessage call
 "enqueued": "2026-08-16T21:20:33.980Z",  // recipient's clock — message ARRIVED
 "delivered":"2026-08-16T21:21:42.402Z",  // recipient's clock — recipient CONSUMED it
 "sides":   "both",            // "out" | "in" | "both"
 "delivery":"midturn",         // "boundary" | "midturn" | null — null = sender side only
 "hops":    ["aaaa1111…"],     // opaque — semantics falsified, derive nothing from it
 "summary": "", "body": "<scrubbed>", "tags": [], "words": 0,
 "src_id":  "t3"}

// delta envelope
{"snapshot_id": "a91f3c", "seq": 1417, "events": [...], "sessions": {...},
 "sources": [...]}

// sessions{} — keyed by ULID core
{"01EXAMPLEVM1AAAAAAAAAAAA": {"name": "session-a",
                              "aka":  ["session-b"],
                              "uuid": null, "cwd": "…", "branch": "master",
                              "first": "…", "last": "…"}}
```

Each non-obvious choice and why:

- **No names on events.** Names are mutable (see above). Storing one per event
  makes all history stale on rename and grows a second graph node for a session
  that already exists. Normalized, a rename updates one row and history
  re-labels for free — the view handles a node *relabel*, not a node *merge*.
- **ULID cores, not uuids.** Uuid is unobtainable for a peer whose transcript we
  don't hold.
- **Three clocks, all nullable.** A single receiver-side timestamp conflates two
  genuinely different events. Measured gaps between `enqueue` and its matching
  `dequeue`/`remove` on one machine's transcript: boundary deliveries 0.010–0.018 s,
  mid-turn deliveries 32.4 s and 68.3 s. Same field, two meanings, and no way to
  tell afterwards which one a given row holds — the silent-drift signature again.
  Splitting them buys two independent measurements:

  ```
  sent      -> enqueued    transport / bridge latency
  enqueued  -> delivered   queue dwell — how long it sat before the recipient looked
  ```

  There is deliberately **no `seen` alias**. A compatibility alias for a conflated
  field is how the conflation survives.

- **Null in the receiver clocks is data, not absence.** Where we hold only the
  sender's transcript, `enqueued`, `delivered` and dwell are all null — and that
  *is* the delivery-confirmation signal. It must never render as a blank cell.
- **`sides`, not `dir`.** After dedup, direction is implied by from/to, so `dir`
  is redundant. What dedup destroys is *which sides we witnessed*, and that is
  the delivery-confirmation signal.
- **`src_id`, not a path.** A transcript path is `/home/<username>/…`. That is a
  real person's name, it would reach a published artifact, and `scrub()` does not
  catch it. Index into a `sources[]` table. Same class of mistake as the token
  that once reached a committed file.
- **Hash the *scrubbed* body.** If `SECRET` ever changes, an unscrubbed hash
  makes the same message hash differently on the two sides, and every historical
  message reappears as a duplicate.
- **`hops` is carried opaque.** Three readings of it were proposed and all three
  falsified; see below. Derive nothing from it.
- **`delivery`.** Boundary vs mid-turn is free (it is the `dequeue`/`remove`
  distinction the parser already reads to build the inbound set) and it is the
  only available measure of whether a *recipient* was saturated when a message
  landed. Mechanically observed, not inferred.

### `hopChain` — carry it opaque, build nothing on it

This field had three readings in one afternoon and **all three are falsified**.
The sequence is recorded because the field looks informative and the next person
will try to use it.

*Reading 1 — a relay path*, so `len > 1` means forwarded and `origin.from` is the
last hop rather than the originator. Falsified by elements repeating: in a
two-party exchange with no relay the chain runs `[A]`, `[A,B]`, `[A,B,A]`. A
delivery route does not revisit.

*Reading 2 — lineage, growing +1 per message.* *Reading 3 — `len(hopChain)`
is exchange depth.* Both falsified by the chain **stalling**. Reconstructed
across both transcripts:

```
machine A -> machine B #1   no hop-chain attr
machine B -> machine A #1   len 1   [A]
machine A -> machine B #2   len 2   [A,B]
machine B -> machine A #2   len 3   [A,B,A]
machine A -> machine B #3   len 4
machine B -> machine A #3   len 3   [A,B,A]      <-- growth stops
machine B -> machine A #4   len 3   [A,B,A]      <-- still 3, byte-identical
```

Eight-plus messages into the thread, one side's chain is pinned at 3 while the
conversation keeps growing. Depth is not length. The two sides also disagree at
the same point in the sequence, which is unexplained and may mean the value is
direction-dependent.

Only `len(set(hopChain)) == participants` survives, and it is fitted to a
two-participant sample — the weakest possible evidence for a claim about counting
participants. Treat `> 2` as a hint worth surfacing, never as authority over
`from_id`/`to_id`.

**Model rule: carry `hops` opaque. Do not derive threading, depth, or topology
from it.** Not for lack of coverage — coverage is complete, see below — but
because the semantics are *actively falsified* rather than merely unknown. A
threading key whose length means one thing at message 2 and another at message 6
yields a thread view that looks complete and is silently wrong, which is the
failure this project exists to prevent. Derive threading from
`from_id`/`to_id`/time, which we understand. Neither session can say what the
24-hex ids reference, and neither needs to.

A coverage objection was raised and withdrawn; the withdrawal is recorded because
it is the useful part. It appeared that `hopChain` rode on `origin` and so was
readable only on boundary deliveries — 2 messages of 5, gaps falling precisely on
the busiest sessions. That is **false**: the chain is also carried as a
`hop-chain` attribute on the `queue-operation` / `enqueue` tag, present on every
inbound message and agreeing with `origin.hopChain` element-for-element on all
four cross-checked. Coverage is complete.

Two process notes, since the same shape recurred at three levels today. The
coverage claim was drawn from the `origin` path alone — the same partial-source
mistake as the exclusion rule above, one level down. It was then *promoted* by the
other session from an unverified caveat to a settled disqualification without
being independently checked, which is worse: amplifying an unchecked finding is
how a wrong claim acquires the appearance of two-source confirmation.

### Events are mostly-append, not append-only — SETTLED

Two-pass pairing means an already-emitted event can change: `sides` mutates from
`"out"` to `"both"`, and — worse — an unpaired outbound and an unpaired inbound
are two events with two ids that **merge into one** when pass 2 succeeds. That is
an update plus a retraction. It is not an edge case: it is the normal path
whenever a transcript is added mid-run, which is the headline use case, and it is
the delivery-confirmation alert clearing itself.

Rather than invent a tombstone/merge protocol:

- Deltas carry **appends only** in the common case; `seq` semantics unchanged.
- Any revision to an already-emitted event bumps a top-level **`snapshot_id`**
  (pass-2 pairing, pass-3 reclassification, or a sessions-table rename that alters
  a resolved `to_id`).
- A client seeing a changed `snapshot_id` refetches the full merged set and resets
  its cursor. At a monitor's data volume this is milliseconds.

**Binding on both sides:** all derived state is a pure function of the full event
set, recomputed per poll, never accumulated. This is what makes retraction safe.
It is forced, not stylistic:

- Reply latency is not incrementally updatable. A→B at 10:00 and B→A at 10:05
  gives 5 min; backfill revealing B→A at 10:01 makes the answer 1 min. An
  accumulator keeps the wrong number forever with nothing to notice.
- Alerts are a derived **set**, never a log. Backfill turns absence into presence,
  so "sent, never landed" must be able to silently retract. Appending alerts to a
  log leaves permanent false alarms.
- Auto-scroll distinguishes tail from backfill by comparing `sent` against the max
  on screen, not by `seq`, so a backfilled event inserts in place instead of
  yanking the viewport.

### SETTLED — `id` ordinal and cross-side pairing

Repeated short bodies (`ack`, `ping`, `rerun it`) must not collide, but an
ordinal computed per transcript only agrees across sides if no message is lost —
and a lost message is exactly what `sides` exists to detect. Lose the second of
three `ack`s and the sender counts 0,1,2 while the receiver counts 0,1; the third
hashes differently on each side, lands as two events, and produces a false
"never landed" alert *plus* an orphan inbound. Both features fail together.

Proposed: ordinal disambiguates duplicates **within one transcript only** and is
never assumed to match across sides. Cross-side identity comes from pairing, not
hash equality:

```
pass 1  exact match on (from_id, to_id, sha1(scrubbed_body))
pass 2  leftovers: pair out/in within a time window, nearest first
pass 3  still unpaired -> genuine sides:"out" or sides:"in"
```

### SETTLED — `seq` ordering

`seq` is the poll cursor and must be stable across restarts, but it is assigned
at merge time, and a transcript added later wants low seq numbers already issued.
Resolution: **seq is monotonic in discovery order, not time order**, so the cursor
is never invalidated; the renderer sorts by `sent` (falling back to `enqueued`,
then `delivered`, since any of the three may be null) for display. The view
tolerates an event arriving with a `sent` older than everything on screen and
places it correctly rather than appending — confirmed by the view owner, and safe
because of the pure-function-of-full-event-set rule above.

## Architecture — SETTLED

**One model, two front-ends.** The existing rule is "only one parse"; at N
sessions that has to become "only one *model*". Parse, identity resolution, ULID
normalization, dedup and tailing are all shared. If normalization lived only in
the server, the static page would show 4 sessions while the live page showed 7 —
the same silent drift the original rule exists to prevent, moved up a layer.

**Polling, not SSE.** The live page fetches a delta endpoint at `?since=<seq>`;
the static page is *the same page* with data inlined and the cursor never
advancing. One renderer, two data sources. That keeps the archival artifact
working from `file://` with no server, and makes the live view a strict superset
instead of a second codebase of view logic that drifts. SSE stays available as a
later `--sse` flag if sub-second latency is ever wanted.

**The existing static builder's guarantees are frozen; its code is not.** Self-
contained, CSP-safe, scrubbed, findings never generated — those hold. But the
implementation currently hardcodes binary identity and renders phantoms, so
freezing the code would ship those forever.

## Views

Ranked by whether they tell you something you would act on.

**The `Built` column is not decoration.** This table began as a list of what to
build, and was read later as a description of what exists — the two drifted in
both directions without anyone asserting anything false. Tag drift stayed on the
list from the first message through every revision and reached the PR body as a
shipped feature; it was never written. Queue dwell went the other way: it came out
of the dwell-versus-depth argument, was built, and was never added here. A plan
item ages into a claim about the artifact unless the document says which it is.

| View | Built | Signal |
|---|---|---|
| Mesh graph | yes | Who talks to whom; node size = volume sent, edge weight = message count. The thing the pair-only page structurally cannot show. It does NOT pulse on a new message — that was claimed here and never built, the third time this table has described an intention as a fact. |
| Swimlanes | yes | One lane per session on a shared time axis — bursts, silences, who is waiting. Human turns render as marks here. |
| Queue dwell | yes | `enqueued → delivered` per message. Measures *interruptibility* — how long until the recipient's current tool call finished — not backlog. One long operation and a real backlog produce identical dwell. |
| Reply latency | yes | Per pair. A spike is how you notice a stuck or dead session. |
| Live stream | yes | The current message list, tailing, auto-scroll pausing on interaction. |
| Attention panel | yes | Structural signals only — silence, unanswered-after-N, delivery confirmation. Live state; never persisted into the static page. |
| Tag drift | **no** | Heuristic tags as a stacked area over time. Decided, never built. Tags currently exist only as filter chips on the stream. |
| Saturation | **no** | Queue depth per session over time — enqueued-but-unresolved at each instant. Depth > 1 is a backlog; depth 1 with high dwell is one long operation. Deliberately parked: depth cannot exceed 1 with fewer than three live sessions, so it reads flat on every dataset we can test. The fixture carries a depth-2 interval for when a real three-session mesh exists. |

## Timestamps — user requirement, SETTLED

Stated directly by the owner: *"plz make sure time stamps are included in the app
you end up building."* Treated as a hard requirement on the renderer, not a
nice-to-have. The schema already carries the data, so nothing changes in the model
contract — but two clocks that are each nullable make "show the time" less trivial
than it sounds, and the failure mode is showing a confident time that is quietly
the wrong one.

**Every view is a time surface**, not just the stream: the swimlane axis, the
latency chart, attention entries ("sent 8 min ago, never landed"), and
last-activity-per-node on the mesh. A monitor where only one panel is timestamped
is the one people complain about.

**Always say which clock — and there are three, not two.** `sent` (sender's),
`enqueued` (arrived at recipient) and `delivered` (recipient consumed it). The
honest answer to "when did this message happen" is three instants, and rendering
one unlabelled would be a choice made silently on the user's behalf. `sent` is the
primary display time; the others are shown wherever their delta is meaningful:

```
sent      -> enqueued    transport latency
enqueued  -> delivered   queue dwell
```

When only some exist that must be *visually* unambiguous rather than a bare
stamp. Where we hold only the sender's transcript, all three of
`enqueued`/`delivered`/dwell are null — and **that null is data, not absence**: it
is exactly the delivery-confirmation signal. It must not render as an empty cell.

**Absolute and relative, both.** Relative ("3m ago") is what a live monitor wants;
absolute is what you need to correlate against a commit or the other machine's
log. Relative labels **recompute on the poll tick** — a "2m ago" baked at render
and now ten minutes stale is worse than no timestamp at all.

**Timezone.** Transcript stamps are ISO-8601 UTC. Two machines need not share a
zone and the viewer is a third party to both, so render in the *viewer's* local
zone, keep UTC in the `title`, and label the zone visibly. Sessions rendering in
their own local zones would make the swimlane silently unalignable — the same
class of failure as the three-namespace join.

**Include the date.** `ts()` in the existing extractor formats `"%H:%M"` and drops
the date entirely. Fine for a one-day pair session, wrong for a multi-day mesh.
Since the live and static pages are now one renderer, fixing it fixes both.

**Precision.** Stamps carry milliseconds; keep ms internally because cross-machine
reply latency is where it matters, render to the second, and do not round-trip
through a lossy format.

## Attention panel — SETTLED, with a hard line

An alert panel is what makes this a monitor rather than a lava lamp, but it sits
next to the standing rule that the tool **never generates findings**. Two
constraints keep it clean:

1. **Alerts are ephemeral live state and are never persisted into the archival
   artifact.** The static page has no attention section, so nothing the tool
   inferred is ever published as judgment.
2. **Structural signals only, never lexical ones.**

Rule 2 rules out the obvious trigger. Firing on the `correction` tag
(`wrong|corrected|retract|refuted|supersede`) fails immediately: a healthy design
argument trips it on nearly every message — the exchange that produced this
document does, repeatedly — so it is a stuck horn rather than an alert. It is
also the tool asserting that a message mattered *because of its vocabulary*,
which is the exact thing the rule forbids. Same for `STOP|blocked|failed`.

Permitted triggers, all mechanically true rather than inferred:

- Silence duration per session.
- Outbound with no reply after N minutes.
- **Delivery confirmation** — outbound exists in the sender's transcript with no
  matching inbound in the recipient's. "Sent 8 minutes ago, never landed" is the
  most actionable signal in a mesh, it cannot be seen from either transcript
  alone, and it is free: it is the by-product of dedup, which is what `sides`
  records.

## Safety

`scrub()` and `esc()` remain necessary and are **not sufficient** for a live
page. The static page is generated by a human running a command and looking at
the output. The live page renders whatever a peer sent, seconds later, with
nobody reviewing — peer-controlled content reaching the DOM unreviewed is the
threat model.

- Everything through `esc()`, **including session names and tags**, not just
  message bodies.
- No `innerHTML` with interpolated peer data in any new view; bind via
  `textContent`.
- Session names are peer-controlled strings that flow into graph labels, the
  legend, chip filters and the attention panel — four surfaces, and someone will
  render one of them with `innerHTML` for convenience.
- The model is not trusted to have sanitized anything; the view sanitizes.
- `src_id` exists so local filesystem paths never reach the page at all.

## Ownership

Neither session edits the other's files. If you need a change on the other side,
send a message.

| Owner | Files |
|---|---|
| machine A | `chatter/model.py` — parse (origin primary + attributed fallback), ULID normalization, sessions table, two-pass pairing, tail/checkpoint, seq + snapshot_id, both human shapes, `hops`, queue-operation exclusion. Plus `examples/`. |
| machine B | Renderer, server, all views. Imports the model so the static builder resolves identically. |

### Fixture

`examples/` currently contains **zero** `origin` records — `make-sample-transcript.py`
hand-builds the legacy tag shape, so the fixture exercises only the fallback
path. Regenerating it is a real deliverable, not a detail: otherwise we build the
primary path and test the legacy one.

It must carry every case that has bitten us, because each one is a regression
someone would otherwise reintroduce:

- `origin`-shaped records (the primary path) **and** legacy tag-shaped ones.
- **At least one phantom** — a record whose text quotes the regex — so the
  discriminator stays under test.
- A rename mid-transcript, so the first-`agent-name` trap stays caught.
- A `queue-operation` / `enqueue` duplicating a peer message that is *also*
  delivered via `origin`, so intra-transcript double-counting stays caught.
- A mid-turn human injection with **no** `origin.kind == "human"` record.
- A normal typed human turn *with* one, so both shapes are exercised together.
- A `hopChain` with a repeated element, so nobody re-reads it as a relay path.
- An unpaired outbound, for the delivery alert.
- A pair that only resolves on the second pass, so the `snapshot_id` resync path
  is exercised rather than assumed.

## How every error here was actually caught

Two different things happened here and the section is weaker if they are merged.

**Traps found by inspection, before anything was built on them:** the
first-`agent-name` rename, and the regex that fabricates messages. Nobody
believed the wrong thing; measurement came first. These cost nothing.

**Claims that were asserted and then had to be withdrawn** — eight of them, and
these are the expensive kind:

| Claim | Held by | Killed by |
|---|---|---|
| `origin` is the complete inbound set | machine A | the other machine's 3-of-5 mid-turn sample |
| `hopChain` is a relay path | machine A | repetition, then the stall |
| `hopChain` grows +1 per message, so it is lineage | machine B | the stall at length 3 |
| `len(hopChain)` == exchange depth; viable threading key | both | the stall at length 3 |
| `hopChain` is unreadable on mid-turn deliveries | machine B, amplified by machine A | the `hop-chain` attribute on the enqueue copy |
| Delivery-mode regimes are cleanly separated | machine A | the other machine's `n=7` against one machine's `n=2` |
| The empty credential grep is a shell quirk | machine B | the NUL bytes in the template |
| `events[]` is append-only and immutable | machine A | argument, not data — see below |

Four were one machine's, three the other machine's, one both — and the counting matters, because this
table was written twice with the wrong tally. The first version merged in two
traps found by inspection, inflating the count. The correction then cited
`append-only` as a withdrawn claim while leaving it out of the table, so the table
undercounted by one and the prose and the rows disagreed. Both errors ran in the
generous direction, each time toward the *other* session — which is the bias the
table exists to remove, recurring inside the mechanism built to catch it. If a
record is meant to be checkable, the count has to reconcile with the rows.

The last row is the exception that keeps the rest honest. `append-only` was killed
by an **argument** — that two-pass pairing merges two emitted events into one, so
the wire cannot be append-only — and not by anyone's sample. It is the only row of
which that is true. Every other claim survived its holder's own review and died on
the other machine's *data*. That distinction is the whole reason "agree freely on
reasoning, never on measurements" is stated as the rule below: reasoning is the
one thing that did transfer between machines without needing to be re-run.

**In every case the wrong answer explained all the data that machine had.** Not a
sloppy answer — a *fitting* one. `origin` really was present on every message machine A
held. The chain really did grow by one per message across every sample machine B had.
The regimes really were three orders of magnitude apart in one machine's two observations.
A wrong explanation that fits the evidence ends the investigation, because there
is nothing left to be curious about.

The NUL bug is the sharpest instance. The mandated credential grep returned no
output; that was read as a shell-quoting quirk, a different tool was used to get
the number, and the check was recorded as passing. The explanation was plausible,
it fit, and it was produced *while actively verifying the credential path* — the
one place where suspicion should have been highest. Care was not the missing
ingredient. Neither was expertise.

What actually broke each of these was the same thing every time: **the other
machine held a sample that could produce a counterexample.** one machine's transcript had
zero mid-turn peer deliveries and structurally could not have refuted "origin is
complete". the other machine's had seven and refuted it immediately. Neither session was more
careful than the other; they had different data.

So the actionable rule is not "be careful", which nobody can act on. It is:

- **Verify against a sample that can falsify you.** If a claim cannot fail on the
  data in front of you, you have not tested it. Say `n=`, and say what the sample
  structurally cannot contain.
- **Agree freely on reasoning; never on measurements.** This is the operational
  form of the rule, and it is the one that would have caught the row with both
  names on it. Accepting the other session's *argument* without re-deriving it is
  fine — a design trade survives being taken on its merits. Accepting a *number*
  or a *sample* without re-running it on your own machine is not, even when you
  fully expect it to pass, because expecting it to pass is exactly the state in
  which nobody checks.
- **Do not promote another party's unverified finding.** One session's unchecked
  caveat was amplified into a settled rule without independent checking. That is
  worse than the original error: it gives a wrong claim the appearance of
  two-source confirmation, and the second source is what everyone trusts.
- **A check that can be skipped invisibly is worse than no check**, because it is
  trusted. Prefer checks that fail loudly over checks that go quiet.
- **State what remains unverified, in the handover** — but do not mistake stating
  it for handling it. See below.

### A caveat is not a check

For most of this work, "neither machine has a browser, so nothing about how the
page *looks* is verified by either of us" sat in this document as an honest
caveat, repeated in every handover. It was true, it was prominent, and it was
worth nothing.

Writing down that you cannot check something is not a mitigation. It makes the gap
legible while leaving it exactly as open as it was. We had already written the
rule one section up — verify against a sample that can falsify you — and then held
an entire category with **no sample at all**, and let a documented acknowledgement
stand in for one.

What it cost, concretely. While that caveat stood, a PR went up describing four
working views. One clipped its labels by up to 63px. One pushed the body into
horizontal scroll on any phone. The attention panel emitted one alert per unpaired
message — 1643px and 31 rows on a real transcript, two sentences repeated — which
made it unusable on the only kind of data anyone will actually point this at, and
buried the single actionable row underneath. Six rounds of arguing about the data
found none of them. Two rounds with a real browser found all three, plus two more.

The pattern is the same as every row in the table, one level up: **correct on the
input we built, wrong on the input people have.** The rendered page was not a
different *kind* of claim; it was a claim for which we had no input at all, so
nothing could falsify it and everything about it stayed true by default.

So the rule has a second half. If a category cannot be checked, the task is to get
the instrument, not to document its absence. Installing a headless browser took one
session a few minutes and immediately paid for itself several times over. The cost
of the gap was not the caveat being wrong — it was right — it was that being right
about a gap does nothing to close it.

This is also the argument for the tool itself. A commit records what was decided.
It does not record that the other session's data refuted yours, which is the only
reason any of this got fixed.

# Open work

Ordered by how much each one changes what someone can actually do with this.
Every item says what it costs and why it is worth it, because a task list that
only says *what* rots the moment someone disagrees with the *why*.

Items marked **verified** were measured, not assumed. Items marked *unverified*
are hypotheses and should be checked before anyone builds on them.

---

## 1. Cross-machine sync — the biggest gap

The product is cross-agent, and agents on different machines write transcripts on
those machines. Right now getting them together is "scp the files yourself", so
the far side shows as a dashed node and every message to it as unconfirmed.

**verified:** on this repo's own dashboard, `agent-chatter-vm2` has been a ghost
node for the entire life of the project, and 21 messages to it are permanently
unconfirmable — not because anything failed, but because its transcript is on
another host.

Options, cheapest first:

- document the `rsync` pattern into a watched directory and let `--watch` do the
  rest (an hour, no new code)
- `./agent-chatter --add host:~/.claude/projects/...` that pulls on a timer
- a tiny push agent on each machine

Do the first now regardless; it makes the tool honest about a workflow people
will otherwise invent badly.

## 2. Exchanges — group messages into threads

Currently the stream is flat and ordered by time, so six messages arguing one
point look identical to six unrelated ones.

Group by unordered pair, split on a silence gap (a multiple of the median
inter-message gap, or a flat 10–15 minutes). Purely structural, so it stays
inside the never-judge rule. Render an exchange header — participants, span,
message count, human turns — with older exchanges collapsed and the current one
open, and order **chronologically inside an exchange** (newest-first is right for
a five-row glance and wrong the moment someone expands it).

**verified:** the gap structure is visible in the activity chart already — the
bursts are the exchanges. Everything in items 3 and 5 hangs off this.

## 3. Work-state tiles replace failure counts

Three of the five headline tiles are failure counts. That is diagnostics, not
insight into the collaboration, and on a one-sided capture they scream red about
a structural fact that will never change.

Replace with: **Waiting on** (for each directed pair, the last message with no
subsequent reverse traffic, ticking — "vm2 owes vm1 a reply, 14m"), **current
exchange** (from item 2), and **pace** (recent versus overall, already computed
for the activity subtitle). Move the failure counts to Attention, which exists
for exactly that.

`Waiting on` is the design's own sanctioned-but-unbuilt alert trigger promoted to
the headline. It freezes honestly on a static page as "open at capture".

## 4. True reply latency

The Timing panel plots one message's `sent → delivered`, which is transport plus
queue wait. It is now titled *Delivery time* so it no longer claims otherwise,
but the number worth having — A messages B, B replies to A — does not exist yet.
Derivable from data already emitted, once exchanges (item 2) give it a boundary.
Ship it as a number, not a chart.

## 5. Human turns as plot points

`kind == "human"` is already in the data, and human turns are the beats that
produced most of this project's commits. They currently render with the same
weight as an `ack`. Distinct treatment in the stream, and a count per exchange
header. Nearly free.

## 6. "While you were away"

`seq` is monotonic in discovery order, which is exactly an unread cursor. Store
the last-seen value in `localStorage`, draw a divider in the stream, and add one
digest line: *"since 14:32 — 2 exchanges, 12 messages, 1 unanswered"*. Entirely
client-side.

## 7. Live feel

No new data needed: slide-in and brief highlight on new rows (honouring
`prefers-reduced-motion`), a ticking "last message 42s ago", an unread count in
`document.title`, and presence derived from clocks already held — an inbound that
is enqueued but not delivered means *"vm1 is mid-turn, 1 message waiting 3m"*.

## 8. Findings in the live view

**verified:** `serve-mesh.py` has no `--findings` flag, so curated findings only
appear in static builds and are invisible to anyone using the live dashboard.
Also worth rendering each finding inline at its anchor, not only as a card grid.

## 9. Packaging and naming

`agent-chatter` is the front door, but `serve-mesh.py`, `build-mesh-page.py` and
`extract-peer-conversation.py` sit beside it in the root with three different
vocabularies for one idea. Move them under `chatter/` and leave the single entry
point. A zipapp would give `uvx`-style one-line use without breaking the
stdlib-only rule.

## 10. Restart-to-see-changes

The server reads `dashboard.html` once at startup, so a renderer change needs a
restart while message data stays live. This has already produced one round of
"why am I looking at bugs you fixed". Either re-read the template when its mtime
changes, or stamp a build fingerprint in the footer so the page says which
version it is.

---

## Known-honest, deliberately not fixed

- **Saturation / queue-depth panel.** Depth cannot exceed 1 with fewer than three
  live sessions, so it reads flat on every dataset that can currently be tested.
  The fixture carries a depth-2 interval waiting for the case to become real.
- **`unidentified local peer`.** A message addressed to a raw unix socket
  identifies nobody. Guessing which session it was would be the kind of inference
  this parser refuses everywhere else. It could be paired by message body instead
  — a deliberate change, not a cleanup.
- **`hopChain`.** Carried opaque. Three separate readings of it have been
  falsified; deriving anything from it needs new evidence, not more thought.

## Standing rules that constrain all of the above

Stdlib only. One model, imported by both front-ends. Self-contained output that
survives a strict CSP and works from `file://`. Three theme states with every
token defined in all three. All peer-controlled content through `textContent`.
The tool never decides which exchange mattered — tags filter, alerts are
structural, and anything inferred never reaches a saved page.

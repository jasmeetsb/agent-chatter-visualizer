# agent-chatter-visualizer

**A live dashboard for the conversation between your Claude Code sessions.**

When agents work in pairs — one per machine, one per worktree, one per task —
they talk to each other, and a surprising amount of the useful reasoning ends up
only in that channel. A commit records that a decision changed. It does not
record that the other session's data refuted yours, what was argued, or what was
withdrawn on the way.

This reads that exchange out of the session transcripts and makes it readable,
live.

![The dashboard](docs/img/dashboard.png)

*Everything below is the bundled demo — three fictional sessions coordinating a
database migration. No real transcript was used to make any image on this page.*

---

## 60 seconds to a dashboard

Nothing to install:

```bash
uvx --from git+https://github.com/jasmeetsb/agent-chatter-visualizer agent-chatter --demo
```

Or clone it:

```bash
git clone https://github.com/jasmeetsb/agent-chatter-visualizer.git
cd agent-chatter-visualizer
./agent-chatter --demo
```

Open **http://127.0.0.1:8787**. That is the whole setup — no dependencies, no
build step, no configuration. Standard library Python 3.9+.

Then point it at your own sessions:

```bash
./agent-chatter
```

It finds your transcripts, works out which ones carry cross-session traffic, and
serves them. Nothing to configure and nothing to look up.

---

## What you get

### Who talks to whom

![Mesh of sessions](docs/img/mesh.png)

Node size is how much a session **sent**; edge width is message volume; the arrow
is direction. A **dashed** node is a session whose transcript you do not hold —
it exists only in someone else's inbox, and the dashboard says so rather than
pretending the messages vanished.

### Activity

![Activity over time](docs/img/activity.png)

Messages per time bucket, stacked by sender, with the rate alongside. This is
where you see the shape of the collaboration — the bursts, the silences, and who
is actually doing the talking.

### Key insights & decisions

![Key insights and decisions](docs/img/insights.png)

One entry **per conversation**, not per message. Messages are grouped into
exchanges by silence gaps, and each entry says what was discussed and what came
out of it.

Two tiers, and the difference between them is the whole point:

| | |
|---|---|
| **Curated** | You wrote these. A person read the exchange and judged that one mattered. |
| **Surfaced** | *Not* the tool's verdict. These are the sentences where a **participant marked their own conclusion** — "I withdraw that", "you were right", "root cause" — quoted verbatim so you judge the basis rather than trust a label. |

The tool never decides which exchange mattered. That rule is why the two tiers
are separated, and why only the curated one is ever persisted.

### The messages themselves

Newest first, five at a time, with a headline and the opening of the body so the
list reads as the conversation rather than as metadata about one. Click any row
for the full text and its per-clock detail. Filter by session, or search.

### Dark mode

![Dark mode](docs/img/dashboard-dark.png)

Follows your system by default, with an explicit **Auto / Light / Dark** toggle.

---

## Three clocks, not one

A message has three distinct moments, and collapsing them loses the interesting
part:

| | |
|---|---|
| `sent` | the sender's clock, when `SendMessage` was called |
| `enqueued` | the recipient's clock, when it **arrived** |
| `delivered` | the recipient's clock, when it was actually **read** |

`sent → enqueued` is transport. `enqueued → delivered` is how long it sat while
the recipient was busy — around **ten milliseconds** if they were free, **tens of
seconds** if they were mid-tool-call.

Any of the three can be null, and **null is data**: missing recipient clocks mean
you are holding only the sender's side, and that absence is exactly the
delivery-confirmation signal.

---

## Running it on your own sessions

```bash
./agent-chatter --list     # what it found, without starting anything
```

```
agent-chatter-vm1        40 peer messages   (-home-you-Github-project)
opencode-evals-vm1       55 peer messages   (-home-you-Github-evals)
```

Discovery is two-stage: a fast byte scan to shortlist, then an actual parse to
confirm. The second stage matters — a transcript that merely *read* this
repository contains the marker text without ever having used it, and only the
parser can tell those apart.

### All the commands

```bash
./agent-chatter                    # live dashboard, transcripts discovered
./agent-chatter --list             # what it found
./agent-chatter --demo             # the bundled example
./agent-chatter --build page.html  # frozen, self-contained page you can send
./agent-chatter --log notes.md     # plain markdown, for grepping and diffing
./agent-chatter --project <dir>    # limit discovery to one project
./agent-chatter path/to/*.jsonl    # explicit transcripts, skipping discovery
```

Human turns are excluded by default, with an **Agents only / Humans shown**
toggle in the page header. This is a view of what the **agents** said to each
other, and on a real transcript the human is roughly a third of the traffic —
enough to dominate the charts while answering a different question. The choice
is remembered.

### Two agents on two machines

Transcripts are written on the machine the agent runs on, so the far side shows
as a dashed node until you bring the files together:

```bash
mkdir -p ~/mesh
cp ~/.claude/projects/*/*.jsonl ~/mesh/
scp otherhost:'~/.claude/projects/*/*.jsonl' ~/mesh/
./agent-chatter --watch ~/mesh
```

With both sides present, ghost nodes become real, delivery confirms, and timing
gains the recipient's clocks.

---

## Curated findings

Supply the things that actually came out of an exchange, each anchored to the
message it happened in:

```json
[
  {"kind": "caught", "who": "beta-staging", "event": "a1b2c3d4e5f6",
   "title": "The staging batch size would not have been safe in production",
   "body": "Caught before the runbook was written, not after the migration ran."}
]
```

`kind` is `caught`, `saved`, `method` or `reversed`. `who` is any session name,
resolved through renames. `event` is a stable event id — stable across renames,
re-pairing, and a peer's transcript arriving later.

```bash
./agent-chatter --findings findings.json
```

---

## Use it as a library

```python
from chatter import model, discover

paths = [p for p, count, name in discover.find()]
data  = model.build(paths)

data["events"]      # every message, both sides reconciled
data["sessions"]    # identity, renames, ghost status
data["exchanges"]   # conversations, with what was discussed and decided
```

---

## Before you publish a page

**A transcript contains everything ever pasted into that session.** In the
project this was written for, that included a live cloud access token, which
reached a committed file before anyone noticed.

Output is scrubbed: Google API keys, `ya29.` tokens, `sk-` and `gh*_` tokens, PEM
private keys. Home directories become `~`, and so do the two less obvious forms
of the same leak — the project slug `-home-<user>-<project>`, and the shell
prompt `user@host:~/path$` that rides along in any pasted terminal output.

The scrub is not a substitute for checking:

```bash
grep -acE 'AIza[0-9A-Za-z_-]{30,}|ya29\.[0-9A-Za-z_.-]{20,}|sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}' page.html
# expect 0 — and expect a NUMBER. No output means the check did not run.
```

Generated pages are gitignored. They are *data about a specific project* and
belong with that project rather than with this tool.

---

## How it works

Peer messages reach a transcript by more than one route, and the differences
matter: `queue-operation` records are the authoritative set, while `origin`
records are richer but written only for messages delivered at a turn boundary.
Miss that and a busy session loses the majority of its inbound.

`chatter/model.py` reconciles them, resolves identity across renames and three
address namespaces, pairs the two sides of every message, and tails the files
incrementally. Both the live server and the static builder import it, so they
cannot disagree about what was said.

The built page is self-contained — no external fonts, scripts or images — so it
works from `file://` and survives a strict CSP.

- **[DESIGN.md](DESIGN.md)** — every decision with the measurement behind it,
  including a table of the claims that were confidently wrong before they were
  right.
- **[TASKS.md](TASKS.md)** — open work, and what is deliberately not built.

---

## Installing

```bash
uvx --from git+https://github.com/jasmeetsb/agent-chatter-visualizer agent-chatter
pipx install git+https://github.com/jasmeetsb/agent-chatter-visualizer
pip install git+https://github.com/jasmeetsb/agent-chatter-visualizer
```

There are **no runtime dependencies** — the whole tool is standard library, so it
installs instantly, runs offline, and cannot rot when something upstream changes.
The packaging metadata exists only to make it installable; a clone still works
exactly as well.

## Licence

MIT. See [LICENSE](LICENSE).

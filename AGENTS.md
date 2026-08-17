# AGENTS.md

Guidance for coding agents working in this repository. Read
[README.md](README.md) first for what the tool does; this file covers the things
that will bite you.

## Layout

```
agent-chatter        entry script for a git clone; the installed command runs the same code
chatter/             the package
  cli.py             argument parsing and dispatch
  model.py           the single model: parse, identity, pairing, tailing
  discover.py        finding transcripts worth reading
  scrub.py           the one credential scrubber
  summarize.py       the only module that talks to a network, opt-in
  render.py          fills the template
  dashboard.html     the entire front end
  server.py          live server
  page.py            static page builder
  mdlog.py           markdown log writer
  examples/          the synthetic fixture, shipped so --demo works when installed
tools/               fixture generator and verifier
docs/                TRANSCRIPT-FORMAT.md and the README images
```

## Rules that are not style preferences

**Never write output without scrubbing it.** A session transcript contains
everything ever pasted into that session. `chatter/scrub.py` holds the single
definition; import it rather than writing a second one. Home directories, the
flattened project directory name, and shell prompts in pasted terminal output are
all handled in `model.py`, and `build()` refuses to emit if one slips through.
That guard exists because the same leak arrived by five different routes, each
fix correct and blind to the next.

Before committing or publishing anything generated:

```bash
grep -acE 'AIza[0-9A-Za-z_-]{30,}|ya29\.[0-9A-Za-z_.-]{20,}|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}' out.html
# expect 0 — and expect a NUMBER. No output means the check did not run.
```

Keep this pattern and `SECRET` in `scrub.py` in step. They drifted once, in the
direction that matters: `sk-[A-Za-z0-9]{20,}` reads as covering every `sk-` key
and covers none of the dashed ones, because the run has to start immediately
after the prefix — so `sk-ant-api03-…` and `sk-proj-…` matched neither the
scrubber nor the check that exists to catch what the scrubber misses.

**Only one model.** The live server and the static builder both import
`chatter/model.py`. Two parsers drift, and so do two identity resolvers — the
failure is silent, and the page and the log end up disagreeing about what was
said.

**Never generate findings.** Heuristic tags exist for filtering. Deciding which
exchange mattered is a judgement and belongs to whoever supplies `--findings`,
which is why "What came out of it" takes an input file and has no generator
behind it. A tagger that also ranked importance would produce confident nonsense.

**Generated text is labelled, always.** The summaries panel is model-written
prose, which is exactly what the rule above exists to keep out of a published
page — it is allowed only because every card is captioned with the model that
wrote it. Drop that caption and the panel becomes the tool asserting what
mattered. It is also the only network call in the repo: keep the import lazy and
every other path offline.

**Nothing is summarised without being asked.** Either `--summarize`, or the
button, which POSTs to `/summarize`. A key existing is not permission to spend
it — `Summarizer.ready` means it is possible, `auto` means go ahead, and code
that conflates them bills someone for running `--build`. That is why `page.py`
gates on `auto`.

**The POST endpoint carries a token.** It spends money, and localhost is
reachable by every page in the user's browser: a cross-origin script can POST
here even though it cannot read the response. The token is per process, embedded
only in the page we served, and same-origin policy is what keeps it out of
anyone else's reach. Do not add CORS headers, and do not add a GET that spends.

**A cap is never silent.** `--summarize` spends the user's money, so it is capped
per run and per conversation — and every cap reports what it left out, on the
terminal and in the panel. An unreported limit is indistinguishable from the tool
deciding those conversations were not worth summarising, which is the judgement
it is not allowed to make. Same reason the estimate prints before the spend
rather than after.

**Alerts are structural only.** Silence, no reply, no delivery confirmation.
Never keyword-based: a pattern like `wrong|corrected|refuted` fires on every
message of a healthy design argument. Alerts are ephemeral and must never be
written into a saved page.

## The front end

**Self-contained, always.** No external fonts, scripts or images. The page is
published under a strict CSP and also has to work from `file://`.

**Three theme states, not two.** An explicit choice stamps `data-theme` on the
root; the default "system" setting stamps *nothing*, so most viewers hit the
un-stamped document where only `prefers-color-scheme` decides. Every colour token
is defined on bare `:root` and redefined in both
`@media (prefers-color-scheme: dark)` (guarded `:root:not([data-theme="light"])`)
and `:root[data-theme="dark"]`. Verify all three blocks carry the same token set
after any palette change.

**All peer-controlled content goes through `textContent`.** Session names and
message bodies are written by other agents and can contain anything. Never
`innerHTML` with data.

**A CSS rule beats a presentation attribute.** Setting `fill="#fff"` on SVG text
does nothing when the stylesheet carries `text{fill:…}`. Use an inline style.

**No raw control bytes in the template.** A NUL makes the file binary, which
silently disables the credential grep above. `render.py` refuses to emit one.

**`hidden` loses to any rule that sets `display`.** It is a presentation hint,
not a guarantee, so a `.theme` control with `hidden` set still laid out — the
project selector rendered as an empty pill in the header of every single-project
page. `[hidden]{display:none !important}` restores the attribute's meaning once,
globally; do not re-solve this per element.

## Testing

There is no test suite; the fixture is the test.

```bash
./tools/verify-fixture.py         # asserts every case is still present
./agent-chatter --demo            # the same fixture, rendered
```

The fixture deliberately contains a phantom, a rename, both delivery modes, an
unpaired message in each direction, a duplicated record, and two credentials in
different shapes. Add cases by editing `tools/make-fixture.py` and regenerating,
not by hand-editing the JSONL. If you change the fixture, run the verifier — it
exists so nobody quietly removes a case that looks like junk.

Changing a message body changes its event id, and an exchange's summary is
cached under a hash of its text — so regenerating the fixture invalidates
`chatter/examples/demo-summaries.json`. Re-key it rather than leaving the demo
with a tier that silently stopped appearing.

## Conventions

Standard library only. No build step, no runtime dependencies. Someone should be
able to clone this and run it, or `uvx` it without installing anything. The one
exception is `summarize.py`, which imports `anthropic` lazily inside the opt-in
path and is declared as an optional extra — the rule is that nothing you get by
default may require an install or a network.

Comments explain *why*. Several non-obvious choices here exist because the
alternative was tried and failed on real data, and that reasoning is worth more
than a description of what the line does. [docs/TRANSCRIPT-FORMAT.md](docs/TRANSCRIPT-FORMAT.md)
carries the measurements.

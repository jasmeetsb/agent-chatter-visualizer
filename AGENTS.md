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
  render.py          fills the template
  dashboard.html     the entire front end
  server.py          live server
  page.py            static page builder
  mdlog.py           markdown log writer
  examples/          the synthetic fixture, shipped so --demo works when installed
tools/               fixture generator and verifier
docs/                DESIGN.md and the README images
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
grep -acE 'AIza[0-9A-Za-z_-]{30,}|ya29\.[0-9A-Za-z_.-]{20,}|sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}' out.html
# expect 0 — and expect a NUMBER. No output means the check did not run.
```

**Only one model.** The live server and the static builder both import
`chatter/model.py`. Two parsers drift, and so do two identity resolvers — the
failure is silent, and the page and the log end up disagreeing about what was
said.

**Never generate findings.** Heuristic tags exist for filtering. Deciding which
exchange mattered is a judgement and belongs to whoever supplies `--findings`.
The insights panel may surface a sentence in which a participant marked their own
conclusion, quoted verbatim, and must keep that visually separate from curated
entries. A tagger that also ranked importance would produce confident nonsense.

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

## Testing

There is no test suite; the fixture is the test.

```bash
./tools/verify-fixture.py         # asserts every case is still present
./agent-chatter --demo            # the same fixture, rendered
```

The fixture deliberately contains a phantom, a rename, both delivery modes, an
unpaired message in each direction, a duplicated record, and a credential. Add
cases by editing `tools/make-fixture.py` and regenerating, not by hand-editing
the JSONL. If you change the fixture, run the verifier — it exists so nobody
quietly removes a case that looks like junk.

## Conventions

Standard library only. No build step, no runtime dependencies. Someone should be
able to clone this and run it, or `uvx` it without installing anything.

Comments explain *why*. Several non-obvious choices here exist because the
alternative was tried and failed on real data, and that reasoning is worth more
than a description of what the line does. [docs/DESIGN.md](docs/DESIGN.md)
carries the measurements.

"""The credential scrubber. ONE definition, and this is it.

A session transcript contains everything ever pasted into that session. In the
project this tooling was written for, that included a live cloud access token,
which reached a committed file before anyone noticed. Every tool here that writes
transcript-derived output imports this module; none of them define a second
pattern. Two scrubbers drift, and the one that drifts is the one that misses.

The pattern is deliberately broad. A false positive costs a redacted string in a
generated file. A miss costs a credential in a published artifact.

This lives in `chatter/` rather than beside a CLI on purpose. It used to live in
a command script, so the entire stack imported its most
safety-critical function out of a tool that was on its way to being deleted —
anyone retiring that file would have silently taken the scrubber with it.
Dependencies should point at the thing that is staying.

Adding a format: add it here, add a case to `examples/make-mesh-fixture.py` so the
fixture exercises it end to end, and re-run the verifier.
"""
import re

SECRET = re.compile(
    rb"(AIza[0-9A-Za-z_\-]{30,45}"                 # Google API keys
    rb"|ya29\.[0-9A-Za-z_\-\.]{20,}"               # Google OAuth access tokens
    # `sk-` keys, INCLUDING the dashed ones. This rule used to read
    # `sk-[A-Za-z0-9]{20,}`, which looks like it covers sk-ant-… and sk-proj-…
    # and does not: the run has to start immediately after `sk-`, so a real key
    # reading sk-ant-api03-<base64ish> stops dead at the first dash and was never
    # matched. Found while adding --summarize, which asks people to keep an
    # Anthropic key in a file — the shape the tool now most encourages you to
    # have lying around was the one shape it would have published. Twenty
    # characters is still long enough that prose about a key does not trip it.
    rb"|sk-[A-Za-z0-9_\-]{20,}"                    # Anthropic, OpenAI, and friends
    rb"|gh[pousr]_[A-Za-z0-9]{20,}"                # GitHub tokens
    rb"|xox[baprs]-[A-Za-z0-9\-]{10,}"             # Slack tokens
    rb"|-----BEGIN [A-Z ]*PRIVATE KEY-----)"       # PEM private keys
)

REDACTED = b"<REDACTED-CREDENTIAL>"


def scrub(text: str) -> str:
    """Replace anything matching SECRET. Safe on None and on empty input."""
    if not text:
        return text or ""
    return SECRET.sub(REDACTED, text.encode()).decode()


def has_secret(text: str) -> bool:
    """True if the text still contains something SECRET matches.

    For asserting on generated output before it is written. Note this is not a
    substitute for the `grep -a` check in AGENTS.md: this catches what the pattern
    knows about, and the point of that check is to catch what it does not.
    """
    return bool(text) and bool(SECRET.search(text.encode()))

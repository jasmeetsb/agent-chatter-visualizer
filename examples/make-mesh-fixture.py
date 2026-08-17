#!/usr/bin/env python3
"""Generate a synthetic THREE-session transcript mesh.

The original fixture (`make-sample-transcript.py`) is a single transcript of a
two-session exchange in the legacy tag encoding. It cannot exercise any of the
things that actually break a multi-session live view: there is only one
transcript, so nothing can be deduplicated across sides or found missing from
one of them; there is only the tag encoding, so the `origin` path is never hit;
and with two participants a queue can never reach depth 2.

This produces one transcript per session, written to `examples/mesh/`, and every
case below is deliberate. Each is a regression that has already been introduced
once, or that a plausible implementation gets wrong:

  1  boundary peer delivery    enqueue -> dequeue(empty) -> origin record
  2  mid-turn peer delivery    enqueue -> remove(full)   -> NO origin, ever
  3  human turn-boundary       user record, origin.kind == "human"
  4  human mid-turn injection  queue-operation only, no origin record at all
  5  phantom                   a tool_result quoting the extractor's own regex
  6  rename                    two agent-name records for one session
  7  hop-chain                 repeated element, and a length that STALLS
  8  unpaired outbound         sender has it, recipient's transcript does not
  9  unpaired inbound          arrives from a session whose transcript we lack
 10  queue depth 2             two enqueues outstanding at once
 11  ordinal desync            three identical bodies, recipient loses the middle
 12  legacy encoding           tag-shaped record with neither origin nor queue-op
 13  bare-string content       message.content as a str rather than a list
 14  credential                a token in a body, to prove the scrub fires

Regenerate with:

    ./examples/make-mesh-fixture.py

SECRETS: case 14 embeds a syntactically valid but entirely fictional token. It
exists so the scrubber is under test. Do not "fix" it by removing it.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "mesh")

# ULID cores. Synthetic — see DESIGN.md on why real session ids do not go in a
# committed file. The three-prefix join is demonstrated identically by these.
ALPHA = "01EXAMPLEALPHAAAAAAAAAAA"
BETA = "01EXAMPLEBETABBBBBBBBBBBB"
GAMMA = "01EXAMPLEGAMMACCCCCCCCCC"
GHOST = "01EXAMPLEGHOSTDDDDDDDDDD"   # case 9: never has a transcript

# Opaque hop ids. Deliberately NOT sequential and NOT meaningful — the fixture
# has to keep working when someone rediscovers that hop-chain semantics are
# falsified and stops deriving anything from them.
H_A = "aaaa1111bbbb2222cccc3333"
H_B = "dddd4444eeee5555ffff6666"

BASE_H, BASE_M = 9, 0


def ts(sec_offset, ms=0):
    """Absolute timestamp from a whole-second offset. Millisecond precision is
    load-bearing: boundary dwell is ~12ms and would round to zero without it."""
    total = BASE_M * 60 + sec_offset
    return (f"2026-03-14T{BASE_H + total // 3600:02d}:"
            f"{(total // 60) % 60:02d}:{total % 60:02d}.{ms:03d}Z")


def tag(from_id, from_name, body, hops=None):
    """The wire form of an inbound peer message.

    `hops` present mirrors an enqueue copy; absent mirrors the delivered copy.
    That difference is the whole 62/112-byte delta we chased: the payload is
    identical, only this attribute is dropped, so any dedup must hash the inner
    body rather than the raw content.
    """
    hop = f' hop-chain="{",".join(hops)}"' if hops else ""
    return (f'<cross-session-message from="bridge:session_{from_id}"'
            f'{hop} from-name="{from_name}" from-mode="prompting">\n'
            f"{body}\n</cross-session-message>")


class Transcript:
    def __init__(self, session_uuid, name, cwd="/home/example/svc"):
        self.rows = []
        self.uuid = session_uuid
        self.cwd = cwd
        self.name(name)

    def _base(self, stamp):
        return {"timestamp": stamp, "sessionId": self.uuid,
                "cwd": self.cwd, "gitBranch": "master"}

    def name(self, n, stamp=None):
        """Case 6. Emitted once per rename; a parser taking the FIRST one labels
        the session as whatever it used to be called."""
        row = {"type": "agent-name", "agentName": n, "sessionId": self.uuid}
        if stamp:
            row["timestamp"] = stamp
        self.rows.append(row)
        self.rows.append({"type": "custom-title", "customTitle": n,
                          "sessionId": self.uuid})

    def bridge(self):
        self.rows.append({"type": "bridge-session", "sessionId": self.uuid,
                          "bridgeSessionId": f"cse_{self.uuid_core()}"})

    def uuid_core(self):
        return self.core

    def send(self, stamp, to_name, summary, body):
        """Outbound. Note `to` is a NAME while inbound carries an ID — the
        asymmetry that forces name->id resolution through the sessions table.

        Case 15: `content` is a ~50-char PREVIEW ending in an ellipsis, not a
        duplicate of `message`. Written faithfully here because the earlier
        fixture wrote both fields identically and therefore could not catch a
        parser that falls back to `content` and truncates every body to a stub.
        """
        preview = body[:49] + "…" if len(body) > 50 else body
        self.rows.append({**self._base(stamp), "type": "assistant",
                          "message": {"role": "assistant", "content": [
                              {"type": "tool_use", "id": f"toolu_{stamp[-9:-1]}",
                               "name": "SendMessage",
                               "input": {"to": to_name, "recipient": to_name,
                                         "type": "message",
                                         "summary": summary,
                                         "message": body, "content": preview}}]}})

    def qop(self, stamp, op, content=None):
        row = {"type": "queue-operation", "operation": op,
               "timestamp": stamp, "sessionId": self.uuid}
        if content is not None:
            row["content"] = content
        self.rows.append(row)

    def recv_boundary(self, enq, deq, from_id, from_name, body, hops):
        """Case 1. Three records, TWO of them content-bearing: the enqueue copy
        (raw, with hop-chain) and the origin record (clean body). The dequeue
        carries no content at all."""
        self.qop(enq, "enqueue", tag(from_id, from_name, body, hops))
        self.qop(deq, "dequeue")
        self.rows.append({
            **self._base(deq), "type": "user", "isMeta": True,
            "promptSource": "system",
            "origin": {"kind": "peer", "from": f"bridge:session_{from_id}",
                       "name": from_name, "fromMode": "prompting",
                       "hopChain": hops, "body": body},
            "message": {"role": "user", "content": [
                {"type": "text", "text": tag(from_id, from_name, body)}]}})

    def recv_midturn(self, enq, rem, from_id, from_name, body, hops):
        """Case 2. NO origin record is ever written. If the model treats origin
        as the complete inbound set, this message does not exist."""
        self.qop(enq, "enqueue", tag(from_id, from_name, body, hops))
        self.qop(rem, "remove", tag(from_id, from_name, body))   # hop-chain dropped

    def human_boundary(self, stamp, text):
        """Case 3."""
        self.rows.append({**self._base(stamp), "type": "user",
                          "promptSource": "typed",
                          "origin": {"kind": "human"},
                          "message": {"role": "user", "content": text}})  # case 13

    def human_midturn(self, enq, rem, text):
        """Case 4. Identical content both ends — verified twice on a real
        transcript. Only the tag-wrapped peer form differs between copies."""
        self.qop(enq, "enqueue", text)
        self.qop(rem, "remove", text)

    def legacy(self, stamp, from_id, from_name, body):
        """Case 12. How a pre-`origin` transcript encoded an inbound message:
        the tag inside a text block, no queue-operation, no origin."""
        self.rows.append({**self._base(stamp), "type": "user",
                          "message": {"role": "user", "content": [
                              {"type": "text",
                               "text": tag(from_id, from_name, body)}]}})

    def noise(self, stamp, text):
        self.rows.append({**self._base(stamp), "type": "assistant",
                          "message": {"role": "assistant", "content": [
                              {"type": "text", "text": text}]}})

    def phantom(self, stamp):
        """Case 5. A tool_result holding the extractor's own source. `[^>]*`
        closes the fake tag and `(.*?)` becomes the body, so a naive scan
        fabricates a message and attributes it to a real peer. This is not a
        contrived input: it is what reading the repo produces."""
        self.rows.append({**self._base(stamp), "type": "user",
                          "message": {"role": "user", "content": [
                              {"type": "tool_result", "tool_use_id": "toolu_read",
                               "content":
                                   '    81\t            r"<cross-session-message'
                                   '[^>]*>(.*?)</cross-session-message>",\n'
                                   '    82\t            raw, re.S):\n'}]}})
        # And the docs describing the format, which is the other real source.
        self.rows.append({**self._base(stamp), "type": "user",
                          "message": {"role": "user", "content": [
                              {"type": "tool_result", "tool_use_id": "toolu_doc",
                               "content": "| Inbound | A `<cross-session-message …>"
                                          "…</cross-session-message>` block |"}]}})

    def write(self, path):
        with open(path, "w") as fh:
            for r in self.rows:
                fh.write(json.dumps(r) + "\n")


# --------------------------------------------------------------------------
# The exchange. Three sessions bringing a schema migration through staging to
# production, with a canary. Invented, and written to exercise the parser.
# --------------------------------------------------------------------------

B1 = ("Taking the migration in two halves so we are not both writing the same "
      "tables. Yours is staging: apply 0042_split_orders_table.sql, then the "
      "backfill at --batch-size 5000. Mine is the production runbook, held "
      "until your numbers land. Abort if replication lag passes 30s.")

B2 = ("Staging applied in 6 minutes, backfill running. Flagging something "
      "before you write the runbook, because the number does not transfer.\n\n"
      "--batch-size 5000 is fine on an idle replica. Production carries live "
      "reads and orders_items takes a row lock on the parent per batch. I "
      "measured 11s of lock hold with NO concurrent readers. Production reads "
      "that table on every checkout. Start from 500 and verify it.")

B3 = ("Runbook amended to 500 and marked REQUIRES VERIFICATION. Your point "
      "exposes a second thing I had wrong: I had scheduled the run for 03:00 "
      "to minimise impact, but if the constraint is lock contention rather "
      "than throughput then the window buys a smaller ops team, not a safer "
      "migration. Moving to 14:00 with a measured batch size and a full team.")

B4 = ("Canary is up on 2% of checkout traffic. I can hold a decision here: if "
      "p99 moves more than 20ms at batch 500 I will stop the rollout myself "
      "rather than waiting to be told.")

B5 = ("Backfill finished: 4,118,204 rows in 41 minutes, zero retries, peak lag "
      "4s against the 30s threshold.\n\nOne number neither of us predicted: 62% "
      "of wall clock was the final index rebuild, not the batched writes. So "
      "batch size governs safety and barely touches duration. Anyone who later "
      "raises it for speed gets no speedup and all of the lock risk.")

B6 = ("Adding that as a warning rather than a note, because the obvious "
      "optimisation is the dangerous one. Production run is scheduled.")

# Case 14: a fictional credential, so the scrubber is exercised end to end.
B7 = ("Pasting the staging export config so you have it:\n"
      "  export GH_TOKEN=ghp_EXAMPLEFAKETOKEN0123456789abcdefXY\n"
      "Rotate it after the run — it is scoped to the migration bucket only.")

B8 = ("Canary held at 2% for the full window. p99 moved 3ms. Clearing the "
      "rollout to 25%.")


def build():
    os.makedirs(OUT, exist_ok=True)

    alpha = Transcript("aaaaaaaa-0000-4000-8000-000000000001", "migrator")
    alpha.core = ALPHA
    beta = Transcript("bbbbbbbb-0000-4000-8000-000000000002", "beta-staging")
    beta.core = BETA
    gamma = Transcript("cccccccc-0000-4000-8000-000000000003", "gamma-canary")
    gamma.core = GAMMA
    for t in (alpha, beta, gamma):
        t.bridge()

    # -- case 3: a human turn at a boundary, with an origin record.
    alpha.human_boundary(ts(0), "start the migration, coordinate with the other two")

    # -- alpha -> beta, and beta receives it at a boundary (case 1).
    alpha.send(ts(30), "beta-staging", "Assignment: staging first", B1)
    beta.recv_boundary(ts(31), ts(31, 12), ALPHA, "migrator", B1, [H_A])

    # -- case 5: alpha reads its own source and the docs. Two phantoms.
    alpha.phantom(ts(45))

    # -- beta replies. alpha is mid-tool-call, so this is a MID-TURN delivery
    #    (case 2): no origin record is written, and dwell is 8.4 seconds.
    beta.send(ts(60), "migrator", "Batch size will not transfer to prod", B2)
    alpha.recv_midturn(ts(61), ts(69, 400), BETA, "beta-staging", B2, [H_A, H_B])

    # -- case 6: alpha is renamed mid-transcript. Everything above was sent as
    #    "migrator"; everything below as "alpha-prod". A parser taking the first
    #    agent-name mislabels the session for its entire life.
    alpha.name("alpha-prod", ts(70))

    alpha.send(ts(90), "beta-staging", "Runbook amended, and a second error", B3)
    beta.recv_boundary(ts(91), ts(91, 11), ALPHA, "alpha-prod", B3, [H_A, H_B, H_A])

    # -- case 10: queue depth 2. Two messages land on gamma before it resolves
    #    either. Depth is the honest saturation signal; dwell alone cannot tell
    #    a backlog from one long tool call. A two-session mesh cannot produce
    #    this at all, which is why the fixture has three.
    gamma.qop(ts(100), "enqueue", tag(ALPHA, "alpha-prod", B6, [H_A, H_B, H_A]))
    gamma.qop(ts(102), "enqueue", tag(BETA, "beta-staging", B5, [H_A, H_B, H_A]))
    gamma.qop(ts(140), "remove", tag(ALPHA, "alpha-prod", B6))
    gamma.qop(ts(155), "remove", tag(BETA, "beta-staging", B5))
    alpha.send(ts(99), "gamma-canary", "Warning added to the runbook", B6)
    beta.send(ts(101), "gamma-canary", "Staging complete: 4.1M rows", B5)

    # -- case 7: hop-chain STALLS. These three all carry the same 3-element
    #    chain with a repeated element, at increasing conversation depth. Length
    #    is not depth and does not grow monotonically; derive nothing from it.
    STALL = [H_A, H_B, H_A]
    gamma.send(ts(170), "alpha-prod", "Canary holding decision authority", B4)
    alpha.recv_boundary(ts(171), ts(171, 10), GAMMA, "gamma-canary", B4, STALL)
    gamma.send(ts(200), "alpha-prod", "Canary clear at 2%", B8)
    alpha.recv_boundary(ts(201), ts(201, 14), GAMMA, "gamma-canary", B8, STALL)

    # -- case 4: a human interrupts alpha mid-turn. queue-operation only, no
    #    origin record. Content is byte-identical at both ends (verified twice
    #    on a real transcript) because there is no tag wrapper to differ.
    alpha.human_midturn(ts(210), ts(242, 500),
                        "hold the prod run until gamma reports again")

    # -- case 14: a credential crosses the wire and must be scrubbed.
    beta.send(ts(260), "alpha-prod", "Staging export config", B7)
    alpha.recv_boundary(ts(261), ts(261, 12), BETA, "beta-staging", B7, STALL)

    # -- case 8: alpha sends to gamma and gamma NEVER receives it. Nothing in
    #    gamma's transcript refers to it. This is the delivery-confirmation
    #    alert: sides == "out", and the receiver clocks stay null.
    alpha.send(ts(280), "gamma-canary", "Prod run starting, watch checkout p99",
               "Starting the production run now. Watch checkout p99 and call it "
               "if you see movement past 20ms — you have the abort.")

    # -- case 9: beta receives from a session we hold no transcript for. There
    #    is no outbound side anywhere in the mesh: sides == "in", and the node
    #    exists in the graph with no transcript behind it.
    beta.recv_boundary(ts(300), ts(300, 13), GHOST, "ops-oncall",
                       "Oncall here — I am watching the same dashboard. Page me "
                       "rather than the team channel if you abort.", [H_B])

    # -- case 11: ordinal desync. alpha sends "ack" three times; beta loses the
    #    middle one. A per-transcript ordinal in the message id makes the third
    #    hash differently on each side, so it fails to pair and produces BOTH a
    #    false "never landed" and an orphan inbound. Pass-2 window pairing has
    #    to recover it.
    for i, sec in enumerate((320, 330, 340)):
        alpha.send(ts(sec), "beta-staging", "ack", "ack")
    beta.recv_boundary(ts(321), ts(321, 10), ALPHA, "alpha-prod", "ack", STALL)
    beta.recv_boundary(ts(341), ts(341, 11), ALPHA, "alpha-prod", "ack", STALL)

    # -- case 12: the legacy encoding, with neither origin nor queue-operation.
    gamma.legacy(ts(360), BETA, "beta-staging",
                 "Legacy-encoded message: no origin record, no queue-operation, "
                 "tag inside a text block. This is how a pre-origin transcript "
                 "looked and the fallback path still has to read it.")

    for t in (alpha, beta, gamma):
        t.noise(ts(400), "Continuing.")

    alpha.write(os.path.join(OUT, "alpha.jsonl"))
    beta.write(os.path.join(OUT, "beta.jsonl"))
    gamma.write(os.path.join(OUT, "gamma.jsonl"))

    for n, t in (("alpha", alpha), ("beta", beta), ("gamma", gamma)):
        print(f"examples/mesh/{n}.jsonl: {len(t.rows)} records", file=sys.stderr)


if __name__ == "__main__":
    build()

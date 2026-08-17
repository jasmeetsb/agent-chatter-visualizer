#!/usr/bin/env python3
"""Assert that the mesh fixture still contains every case it was built for.

This repo has no test suite; the fixture is the test. But a fixture is only a
test if something checks that the cases are still in it — otherwise a later
edit quietly removes the phantom, or flattens a mid-turn delivery into a
boundary one, and the case stops being covered with nothing to notice.

Deliberately independent of `chatter/model.py`: it reads the JSONL directly, so
it validates the FIXTURE rather than agreeing with the parser. A verifier that
imported the model could only tell you the two are consistent, which is not the
question.

    ./examples/verify-mesh-fixture.py        # exit 0 if every case holds
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MESH = os.path.join(HERE, os.pardir, "chatter", "examples", "mesh")

OPEN_TAG = re.compile(r"<cross-session-message([^>]*)>")
WELL_FORMED = re.compile(r'\bfrom="bridge:[^"]+"')

fails = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def load(sess):
    rows = []
    for line in open(os.path.join(MESH, f"{sess}.jsonl"), errors="replace"):
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def qops(rows, op=None):
    return [r for r in rows if r.get("type") == "queue-operation"
            and (op is None or r.get("operation") == op)]


def origins(rows, kind):
    return [r for r in rows if isinstance(r.get("origin"), dict)
            and r["origin"].get("kind") == kind]


def is_peer(content):
    return bool(content) and content.lstrip().startswith("<cross-session-message")


def secs(stamp):
    h, m, s = stamp[11:13], stamp[14:16], stamp[17:23]
    return int(h) * 3600 + int(m) * 60 + float(s)


def main():
    A, B, G = load("alpha"), load("beta"), load("gamma")
    print("Verifying examples/mesh/ ...\n")

    # 1 — boundary: enqueue -> dequeue(EMPTY) -> origin record
    deqs = qops(B, "dequeue")
    check("1  boundary delivery writes an origin record",
          len(origins(B, "peer")) > 0 and len(deqs) > 0,
          f"{len(origins(B,'peer'))} origin, {len(deqs)} dequeue")
    check("1b dequeue carries NO content",
          all("content" not in r for r in deqs))

    # 2 — mid-turn: enqueue -> remove, and NO origin for that message
    rem_peer = [r for r in qops(A, "remove") if is_peer(r.get("content"))]
    obodies = {o["origin"]["body"] for o in origins(A, "peer")}
    orphaned = []
    for r in rem_peer:
        m = re.search(r">\n(.*)\n</cross-session-message>", r["content"], re.S)
        if m and m.group(1) not in obodies:
            orphaned.append(m.group(1))
    check("2  mid-turn peer delivery exists with NO origin record",
          len(orphaned) > 0,
          f"{len(rem_peer)} peer remove, {len(orphaned)} without an origin")

    # 3 — human at a turn boundary, with an origin record
    check("3  human turn-boundary writes origin.kind == 'human'",
          len(origins(A, "human")) > 0, f"{len(origins(A,'human'))} found")

    # 4 — human mid-turn: queue-op only, byte-identical both ends
    hum = [r for r in qops(A) if r.get("content") and not is_peer(r["content"])]
    pairs = {}
    for r in hum:
        pairs.setdefault(r["content"], []).append(r["operation"])
    midturn_human = [c for c, ops in pairs.items()
                     if "enqueue" in ops and "remove" in ops]
    check("4  human mid-turn injection is queue-operation only",
          len(midturn_human) > 0, f"{len(midturn_human)} enqueue/remove pair(s)")
    check("4b its content is byte-identical at both ends",
          all(len(set(ops)) == len(ops) for ops in pairs.values()))

    # 5 — phantom: tags that CANNOT be attributed
    phantom = attributable = 0
    for rows in (A, B, G):
        for r in rows:
            c = (r.get("message") or {}).get("content")
            blocks = c if isinstance(c, list) else []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                raw = b.get("text") if b.get("type") == "text" else (
                    b.get("content") if b.get("type") == "tool_result" else "")
                if not isinstance(raw, str):
                    continue
                for m in OPEN_TAG.finditer(raw):
                    if WELL_FORMED.search(m.group(1)):
                        attributable += 1
                    else:
                        phantom += 1
    check("5  fixture contains unattributable phantom tags",
          phantom > 0, f"{phantom} phantom, {attributable} attributable")

    # 6 — rename: more than one agent-name for one session
    names = [r["agentName"] for r in A if r.get("type") == "agent-name"]
    check("6  a session is renamed mid-transcript",
          len(names) > 1 and len(set(names)) > 1, " -> ".join(names))
    check("6b taking the FIRST name would mislabel it",
          names[0] != names[-1], f"first={names[0]!r} last={names[-1]!r}")

    # 7 — hop-chain: a repeated element, and a length that stalls
    chains = []
    for rows in (A, B, G):
        for r in rows:
            o = r.get("origin")
            if isinstance(o, dict) and o.get("hopChain"):
                chains.append(o["hopChain"])
    repeated = [c for c in chains if len(set(c)) < len(c)]
    lens = [len(c) for c in chains]
    check("7  a hop-chain has a repeated element",
          len(repeated) > 0, f"e.g. {repeated[0] if repeated else None}")
    check("7b hop-chain length STALLS (not monotonic in depth)",
          len(lens) > 2 and lens.count(max(lens)) > 1,
          f"lengths seen: {lens}")

    # 7c — hop-chain present on the enqueue copy, absent on the delivered copy
    enq_hops = any("hop-chain=" in (r.get("content") or "")
                   for r in qops(A, "enqueue") if is_peer(r.get("content")))
    rem_nohops = all("hop-chain=" not in (r.get("content") or "")
                     for r in rem_peer)
    check("7c enqueue carries hop-chain, delivered copy drops it",
          enq_hops and rem_nohops)

    # 8 — unpaired outbound: alpha sent it, nobody received it
    sent_bodies = set()
    for r in A:
        for b in ((r.get("message") or {}).get("content") or []):
            if isinstance(b, dict) and b.get("name") == "SendMessage":
                sent_bodies.add(b["input"]["message"])
    recv_anywhere = set()
    for rows in (B, G):
        for r in rows:
            o = r.get("origin")
            if isinstance(o, dict) and o.get("body"):
                recv_anywhere.add(o["body"])
            if r.get("type") == "queue-operation" and is_peer(r.get("content")):
                m = re.search(r">\n(.*)\n</cross-session-message>",
                              r["content"], re.S)
                if m:
                    recv_anywhere.add(m.group(1))
    never_landed = sent_bodies - recv_anywhere
    check("8  an outbound message never lands anywhere",
          len(never_landed) > 0, f"{len(never_landed)} unpaired outbound")

    # 9 — unpaired inbound: from a session with no transcript
    known = {"01EXAMPLEALPHAAAAAAAAAAA", "01EXAMPLEBETABBBBBBBBBBBB",
             "01EXAMPLEGAMMACCCCCCCCCC"}
    ghosts = {o["origin"]["from"].split("_")[-1] for o in origins(B, "peer")} - known
    check("9  an inbound arrives from a session we hold no transcript for",
          len(ghosts) > 0, f"ghost senders: {sorted(ghosts)}")

    # 10 — queue depth reaches 2
    depth = mx = 0
    for r in sorted(qops(G), key=lambda r: r["timestamp"]):
        depth += 1 if r["operation"] == "enqueue" else -1
        mx = max(mx, depth)
    check("10 queue depth reaches 2 (needs 3 sessions to be possible)",
          mx >= 2, f"max depth {mx}")

    # 11 — ordinal desync: N identical bodies out, fewer in
    acks_out = sum(1 for b in sent_bodies if b == "ack")
    acks_out = sum(1 for r in A
                   for b in ((r.get("message") or {}).get("content") or [])
                   if isinstance(b, dict) and b.get("name") == "SendMessage"
                   and b["input"]["message"] == "ack")
    acks_in = sum(1 for o in origins(B, "peer") if o["origin"]["body"] == "ack")
    check("11 identical bodies desync across sides",
          acks_out > acks_in and acks_in > 0, f"{acks_out} sent, {acks_in} received")

    # 12 — legacy encoding, no origin and no queue-operation
    legacy = 0
    for r in G:
        if r.get("origin") or r.get("type") == "queue-operation":
            continue
        for b in ((r.get("message") or {}).get("content") or []):
            if isinstance(b, dict) and b.get("type") == "text" \
                    and "<cross-session-message" in (b.get("text") or ""):
                legacy += 1
    check("12 a legacy tag-encoded message exists", legacy > 0, f"{legacy} found")

    # 13 — message.content as a bare string
    bare = sum(1 for rows in (A, B, G) for r in rows
               if isinstance((r.get("message") or {}).get("content"), str))
    check("13 message.content appears as a bare string", bare > 0, f"{bare} found")

    # 14 — a credential is present for the scrubber to catch
    blob = ""
    for s in ("alpha", "beta", "gamma"):
        blob += open(os.path.join(MESH, f"{s}.jsonl"), errors="replace").read()
    check("14 a credential is present for scrub() to catch",
          bool(re.search(r"gh[pousr]_[A-Za-z0-9]{20,}", blob)))

    # dwell regimes — both must be present and must NOT be separable by threshold
    def dwell(rows):
        out = []
        pend = {}
        for r in sorted(qops(rows), key=lambda r: r["timestamp"]):
            if r["operation"] == "enqueue":
                pend.setdefault(r["timestamp"], r)
            else:
                if pend:
                    k = sorted(pend)[0]
                    out.append(secs(r["timestamp"]) - secs(k))
                    pend.pop(k)
        return out
    d = dwell(A) + dwell(B) + dwell(G)
    fast = [x for x in d if x < 1]
    slow = [x for x in d if x >= 1]
    check("15 both dwell regimes present (boundary ms, mid-turn seconds)",
          len(fast) > 0 and len(slow) > 0,
          f"{len(fast)} sub-second, {len(slow)} multi-second")

    # 16 — SendMessage.content is a truncated preview, NOT the body. A parser
    # that falls back to it truncates every outbound message to ~50 chars, and
    # the stub still reads as prose so nothing downstream flags it.
    diverged = same = 0
    for r in A + B + G:
        for b in ((r.get("message") or {}).get("content") or []):
            if isinstance(b, dict) and b.get("name") == "SendMessage":
                i = b.get("input") or {}
                m, c = i.get("message"), i.get("content")
                if m and c and len(m) > 50:
                    if m == c:
                        same += 1
                    elif c.endswith("…") and m.startswith(c[:-1]):
                        diverged += 1
    check("16 SendMessage content is a truncated preview, not the body",
          diverged > 0 and same == 0,
          f"{diverged} preview, {same} wrongly identical")

    print()
    if fails:
        print(f"{len(fails)} case(s) FAILED: {', '.join(fails)}")
        return 1
    print("All cases present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Generate a synthetic Claude Code transcript containing peer messages.

Real transcripts are large, private, and contain whatever was pasted into the
session. This produces a small fictional one so the tools can be run, tested and
demonstrated without any real conversation.

The exchange is invented — two sessions coordinating a database migration across
a staging and a production replica. It is written to exercise the parser rather
than to be interesting: both message directions, both encodings, messages long
enough to collapse, and enough vocabulary to trip most of the heuristic taggers.

    ./examples/make-sample-transcript.py > examples/sample-transcript.jsonl
"""
import json
import sys

# (minutes_offset, direction, summary, body)
# direction: "out" = this session -> peer, "in" = peer -> this session
EXCHANGE = [
    (0, "out", "Assignment: run the backfill on staging first",
     """Taking the migration in two halves so we are not both writing to the same tables.

YOURS: staging. Run migrations/0042_split_orders_table.sql against the staging replica, then
the backfill script with --batch-size 5000. Staging has ~4M rows so expect roughly 40 minutes.

MINE: I will prepare the production runbook and hold until your staging numbers land.

Report back: wall clock, peak replication lag, and whether any batch retried. If lag goes past
30s stop and tell me rather than pushing through — we agreed that is the abort threshold."""),

    (14, "in", "Migration applied, but the batch size is wrong for prod",
     """Staging migration applied cleanly in 6 minutes. Backfill is running.

But I want to flag something before you write the production runbook, because the number will
not transfer.

--batch-size 5000 is fine on staging, where the replica is idle. Production carries live read
traffic, and orders_items has a foreign key that makes each batch take a row lock on the
parent. At 5000 rows per batch I measured 11 seconds of lock hold on staging with NO
concurrent readers. Production reads that table on every checkout.

I would not copy the staging batch size into the production runbook. 500 is the number I would
start from, and even that wants verifying against production read volume rather than assumed.

Staging backfill ETA is 38 minutes, tracking your estimate. Replication lag peaked at 4s."""),

    (21, "out", "Good catch — runbook amended, and a second thing you found",
     """Amending the runbook to 500 and marking the batch size as REQUIRES VERIFICATION rather
than a fixed value. You are right that the staging number does not transfer, and I would have
copied it across without thinking about the lock.

Your point exposes a second thing I had wrong. My runbook said to run the backfill during the
low-traffic window at 03:00 to minimise impact. But if the constraint is lock contention with
readers rather than raw throughput, the window matters much less than the batch size does, and
03:00 buys us a smaller operations team rather than a safer migration. I would rather run it at
14:00 with a batch size we have measured and a full team watching than at 03:00 with a number
we guessed.

Changing the plan to: daylight run, batch size 500, verified against production read volume
first, abort threshold unchanged at 30s lag."""),

    (35, "in", "Staging complete: 4.1M rows, no retries",
     """Backfill finished. 4,118,204 rows in 41 minutes, zero batch retries, peak replication
lag 4s against the 30s threshold.

One number for your runbook that I do not think either of us predicted: the backfill spent 62%
of its wall clock waiting on the index rebuild at the end, not on the batched writes. So the
batch size governs safety but barely touches total duration. If someone later tries to make
this faster by raising the batch size they will get almost no speedup and all of the lock
risk."""),

    (44, "out", "Adding that to the runbook as an explicit warning",
     """That is the most useful thing to come out of the staging run, and it is exactly the
kind of thing that gets re-derived expensively later. Putting it in the runbook as a warning
rather than a note, because the obvious optimisation is the dangerous one:

  Raising --batch-size does NOT meaningfully reduce total duration. 62% of wall clock is the
  final index rebuild, which is unaffected. It only increases lock hold time against live
  readers. Do not tune this for speed.

Production run scheduled for 14:00 tomorrow with the team available. I will send the verified
batch size once I have measured against production read volume."""),

    (58, "in", "Verified 500 against production read volume — it holds",
     """Measured on the production replica with live traffic mirrored: at batch size 500 the
lock hold is 0.9s and p99 checkout latency moves by 3ms, which is inside normal variance. At
1000 it is 2.1s and p99 moves 40ms, which is visible to users.

So 500 is right and 1000 is already too much. The margin is narrower than I expected — worth
noting in the runbook that this is not a value to round up.

Ready when you are."""),
]

PROJECT = "-home-example-service-migration"


def main():
    out = []
    for mins, direction, summary, body in EXCHANGE:
        stamp = f"2026-03-14T{9 + mins // 60:02d}:{mins % 60:02d}:00.000Z"
        if direction == "out":
            out.append({
                "type": "assistant", "timestamp": stamp,
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": f"toolu_{mins:04d}",
                     "name": "SendMessage",
                     "input": {"to": "peer-session", "summary": summary,
                               "message": body}}]}})
        else:
            wrapped = (f'<cross-session-message from="bridge:session_example" '
                       f'from-name="staging-migrator" from-mode="prompting">\n'
                       f"{body}\n</cross-session-message>")
            out.append({
                "type": "user", "timestamp": stamp,
                "message": {"role": "user", "content": [
                    {"type": "text", "text": wrapped}]}})
        # A little unrelated traffic, so the extractor has to filter.
        out.append({"type": "assistant", "timestamp": stamp,
                    "message": {"role": "assistant", "content": [
                        {"type": "text", "text": "Continuing with the runbook."}]}})

    for row in out:
        sys.stdout.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main()

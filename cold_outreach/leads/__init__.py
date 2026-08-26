"""The ingested row — what comes through the pipe, and what the send path acts on.

`openoutreach find --json | outsend` hands this side one JSON record per line. The
models here are what those records become: a `Lead` (the person), a `Deal` (that
person under one campaign, carrying the finder's `reason`), a `Campaign` (the
config a message is written from), and the `Suppression` list — the one table
nothing may ever delete from.

The finder's own funnel ends at `RESOLVED`, which is where this one starts, so the
states are this side's to name and none of them is a copy of a finder state.
"""

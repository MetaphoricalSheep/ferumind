---
id: 1f2e3d4c-5b6a-7980-9fef-edcba9876543
type: document
project: ardwell-weather
created: 2026-03-20T15:00:00+00:00
updated: 2026-04-22T10:15:00+00:00
title: Firmware 1.6.2 rollout notes
description: "Working notes for the untested Kestrel-3 1.6.2 build: what the changelog claims to fix, a canary-first rollout order, and the packet-format and rollback questions still open."
status: gated
---

# Firmware 1.6.2 rollout notes

Working notes for the next Kestrel-3 build. Nothing here is approved for
field use until someone tests 1.6.2 on the bench unit and we agree a
rollout order. Most stations are still on 1.6.1; AWS-09 and AWS-10 are
on 1.5.0 and should probably jump straight to 1.6.2 rather than stepping
through 1.6.1 first, but that is still an open question.

## What 1.6.2 is meant to fix

The changelog mentions improved LoRa retransmit behaviour when the
collector is briefly offline, which would help AWS-10 more than anyone
else given the marginal link. There is also a fix for the rain gauge
debounce timer drifting after long cold spells, which might explain the
odd bucket counts at AWS-08 last February. Battery reporting granularity
improves slightly — not a reason to flash on its own, but nice for winter
rounds.

## Rollout order sketch

Bench test on the spare Rev C board in the shed, minimum one week of
logged readings compared against 1.6.1. Then AWS-06 as the canary — best
line of sight to the collector, easy drive. Then the ridge cluster AWS-01
through AWS-05 in a single weekend if the canary looks clean. Leave
AWS-09 and AWS-10 until last because if the radio changes misbehave we
want them failing near the end of the queue, not blocking the whole
network.

## Risks we have not closed

Nobody has confirmed whether the new firmware changes the over-the-air
packet format in a way the collector upload script rejects. The release
notes say backward compatible, but the notes also said that about 1.6.0
and we spent an afternoon fixing the parser. Flashing in the field still
needs the debug cable; budget twenty minutes per station plus travel.

## Open questions

Do we flash AWS-12 before or after AWS-11? Both are recent installs.
Should we standardise everyone on 1.6.2 before the next winter battery
round, or run mixed versions through one more season? Miriam wants a
written rollback plan; I have not written it yet.

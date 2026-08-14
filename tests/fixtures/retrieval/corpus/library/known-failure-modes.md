---
id: 8551141a-731d-4c52-80d1-0cd978162d5a
type: document
project: ardwell-weather
created: 2025-11-02T09:30:00+00:00
updated: 2026-01-20T17:45:00+00:00
title: Known failure modes
description: Reference for the recurring station faults - stuck rain gauge, flat winter battery, CRC-failing radio frames, drifting clocks and enclosure condensation - with the signature, underlying cause and field remedy for each.
status: active
---

# Known failure modes

This is the reference we reach for on a rota visit when a station is doing
something odd and nobody wants to start guessing from scratch. Each section
below covers one recurring failure: what it looks like from the collector
side or on site, what is actually going on, and what to do about it. If a
station is showing something not listed here, log it properly rather than
forcing it into the nearest category.

## Rain gauge reports nothing during rain (E-RAIN-STUCK)

The signature is a flat rainfall total from one station while its neighbours
are logging tips during the same window, sometimes for hours before anyone
notices. The tipping bucket mechanism has stopped registering tips even
though water is clearly falling on it. Almost always this is mechanical: a
spider has webbed across the pivot, a leaf or piece of grit has lodged
between the buckets, or in cold snaps the pivot has iced enough to bind. Less
often the reed switch itself has failed rather than the mechanism jamming,
which matters because clearing the bucket won't fix that case. On site, tip
the gauge gently by hand and watch whether both buckets swing freely; if they
do, run a magnet past the reed switch position and confirm a tip registers on
the logger before assuming the sensor itself is faulty. Clear any debris,
re-level the gauge, and watch the next hour of data before calling it fixed.
If the fault recurs at the same station repeatedly, treat it as a mounting
problem rather than bad luck and check the gauge is properly level.

## Batteries flat in winter (E-BATT-LOW)

A station on this fault reports intermittently, drops out overnight and
picks back up around midday, or goes dark for a stretch of days depending on
how far gone the battery is. Bench voltage measured on a visit sits below the
cutoff the logger is configured to flag. The underlying cause is nearly
always a mismatch between demand and midwinter charging: lead-acid capacity
falls off in the cold at the same time the panel is getting its least sun of
the year, and a spell of overcast days can draw a battery down faster than
the panel tops it back up even with the margin built into the sizing. A
battery that has been through a couple of winters and is no longer holding
its rated capacity will show this fault earlier in the season than a fresh
one. On a visit, check the panel is clean and not shaded by anything that has
grown up since the last check, then take a resting voltage reading rather
than one taken straight after the panel has been in sun. If the reading is
below threshold on two consecutive visits, swap the battery rather than keep
monitoring it and hoping the weather turns; a marginal battery left in place
tends to fail at the worst possible moment, usually a long cold snap.

## Corrupted radio frames (E-RADIO-CRC)

Readings arrive from the station but fail the checksum at the collector and
get dropped rather than logged, and the failure count for that station
climbs over the course of an hour or two before anything else looks wrong.
Wet foliage intruding on the hop path is the most common cause locally, since
leaves and wet branches attenuate and scatter the 868 MHz signal far more
than the same path does when dry, which is why this fault often turns up
during or just after heavy rain. A loose or lightly corroded antenna
connector produces the same symptom and is worth ruling out on any station
where the fault recurs outside wet weather. Before doing anything invasive,
watch the CRC failure rate over the following day rather than reacting to a
single bad hour, since a passing weather system can produce a burst of these
that clears on its own. If the rate stays elevated, check the path for new
obstruction first and the connector second.

## Clocks drifting (E-TIME-DRIFT)

Data keeps arriving, but the timestamp on it creeps away from collector time,
starting as a few seconds and becoming noticeable over a period of weeks.
Left long enough it makes exports built around exact times, particularly
anything near a day boundary, unreliable. The Kestrel-3's onboard clock is
not itself especially bad; the drift comes from missed time-sync frames
rather than the clock hardware, so a station on a weak or intermittent radio
link will drift faster than one with a clean link, because it is getting
fewer opportunities to resync. Forcing a resync on the next visit clears the
immediate problem, but a station that keeps drifting back is telling you
something about its link quality, not its clock, and the radio path is the
thing worth investigating.

## Water forming inside the enclosure

Symptom: condensation on the inside face of the lid and walls, occasionally
enough to pool at the base of the enclosure, with connector corrosion
appearing at stations where it has gone unnoticed for a while. The internal
humidity reading, where fitted, sits implausibly high relative to the
external sensor on the same station, which is usually the first sign
something is off before any visible moisture is found.

Mechanism: the enclosure has a lower thermal mass than the air trapped
inside it and cools faster overnight, so once the internal air temperature
drops below its dew point, moisture condenses on the coldest available
surface, which is the inside of the case itself. Sites in shade or over damp
ground reach that point sooner and hold it longer through the night than
sites in the open. The same sealing that keeps driven rain and dust out also
keeps any moisture that gets in — whether from a slightly imperfect
gasket, or simply from air trapped in at commissioning — from getting back
out again, which is what makes this fault persistent rather than one-off.

Remedy: the current mitigation is a desiccant sachet inside each enclosure,
replaced or oven-dried on a rota. It reduces the rate at which the problem
returns but does not remove the underlying mechanism, and this is not the
first time this fault has been written up; expect to see it again.

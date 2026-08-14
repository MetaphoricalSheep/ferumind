---
id: 3850d598-702c-45b9-ab2a-eaa4da57ab14
type: document
project: ardwell-weather
created: 2025-10-19T14:00:00+00:00
updated: 2025-12-02T09:30:00+00:00
title: Calibrating a rain gauge
description: "The poured-water check for a tipping-bucket gauge: 500 ml over five minutes, counting tips against the expected figure, the five per cent pass tolerance, and swapping a gauge that fails twice rather than adjusting it."
status: active
---

# Calibrating a rain gauge

Every tipping-bucket gauge in the network drifts a little over time, and
the only way to know whether a given station's rainfall figures can be
trusted is to check it against a known quantity of water rather than trust
the number it has been reporting. This procedure is the settled way to do
that check. It takes about twenty minutes per gauge once you've done it a
couple of times, and it should be run on every station at least once a
year, plus any time a station's rainfall totals look wrong compared to its
neighbours after a shared storm.

## Why a poured check and not just a visual one

Watching the bucket tip a few times by hand tells you the mechanism moves,
but it doesn't tell you the volume per tip is correct, and a gauge can look
perfectly healthy while quietly over- or under-reporting by ten or fifteen
per cent. The only test that actually catches that is pouring a measured
volume of water through the funnel and counting how many tips it produces,
then comparing that count against what the gauge's known tip volume
predicts.

## What you need

A graduated measuring jug accurate to at least ten millilitres, a funnel
that fits the gauge's collector cone without spilling round the edges, a
watch or phone timer, a notebook or the station card, and clean water —
rainwater from a butt is fine, tap water is fine, anything with grit in it
is not, since grit can jam the tipping mechanism and give you a false
result for a completely different reason than the one you're testing for.

## The procedure

1. **Level the gauge first.** Check the spirit level bubble before doing
   anything else. A gauge that isn't sitting level will give a biased tip
   count regardless of how carefully the rest of the test is done, and
   levelling it after the pour just means doing the whole test again.

2. **Measure out five hundred millilitres of water.** This is the standard
   volume used across the network because it produces enough tips to
   average out any single sticky tip, without taking so long to pour that
   the test becomes a chore nobody wants to repeat.

3. **Pour steadily over roughly five minutes, not all at once.** A gauge's
   tipping bucket is calibrated on the assumption of a rain-like flow
   rate. Dumping the whole jug in ten seconds can make the bucket
   overshoot and under-count, which produces a result that looks like a
   fault even when the gauge is fine, so the slow pour matters as much as
   the measured volume does.

4. **Count every tip as it happens.** Don't rely on reading the total back
   from the logger afterwards — count by eye or by ear during the pour, so
   you have an independent figure to check the logged count against, which
   also catches the rare case of a tip happening but not being registered
   electrically.

5. **Compare the tip count to the expected figure.** Each tip corresponds
   to 0.2 mm of rainfall over the gauge's known catchment area, so five
   hundred millilitres has a calculated expected tip count for a correctly
   calibrated gauge of this design. Write down both the counted tips and
   the logged tips, since a mismatch between those two numbers points at
   a wiring or logging fault rather than a mechanical calibration issue.

6. **Record the result on the station card with the date.** A single pass
   isn't much use on its own; what matters over time is the trend across
   repeated checks, and a card with only the most recent result on it
   loses that.

## What counts as a pass

A result within five per cent of the expected tip count is accepted
without further action, since the tipping mechanism has some inherent
variability from pour to pour and chasing anything tighter than that
wastes a Saturday for no real gain. Anything outside that tolerance is
treated as a fail, and the gauge is checked for the obvious causes first —
a partially blocked funnel, a bit of grit or a spider in the mechanism,
water not draining cleanly after each tip — before assuming the mechanism
itself has worn.

## What to do when it fails

The first failure gets a clean and a re-test on the same visit if time
allows, or on the next scheduled visit if it doesn't. Most first failures
turn out to be debris rather than a worn mechanism, and a clean and
re-pour resolves them without anything needing to be replaced. If a gauge
fails the check twice, on two separate visits, it is swapped for a spare
rather than adjusted or repaired in place. This is a deliberate rule
rather than an assumption of laziness: a bucket mechanism worn enough to
fail calibration twice tends to keep drifting rather than settling, and
adjusting the trigger point in the field has a poor track record compared
with simply fitting a known-good unit and taking the suspect one away to
be looked at properly on a bench.

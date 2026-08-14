---
id: af27f251-b51b-4ce0-87e9-bfcf0dcfe9db
type: document
project: ardwell-weather
created: 2026-03-08T11:05:00+00:00
updated: 2026-03-19T20:10:00+00:00
title: Enclosure redesign
description: "In-progress work on the condensation problem: trial vents, a breather membrane, a relocated desiccant pocket and a sloped lid, with four weeks of early-spring results and the temperature offset venting introduced."
status: gated
edit_policy: propose-first
---

# Enclosure redesign

Water forming inside the station cases has been the network's most
persistent problem since the first winter, and this document is where the
fix is being worked out rather than just patched again. Nothing here is
final. Anyone changing a figure should check it against what has actually
been tried at a real station before writing it down as settled, and the
gated status is there deliberately so a half-finished idea doesn't get
copied onto a live install by mistake.

## What's being tried

Three changes are on the table at once, which is not ideal but reflects how
the problem was actually approached this month. A pair of small vents is
being trialled on two enclosures, positioned low on opposite sides of the
case so air can move through without letting rain track in sideways during
a storm. A breather membrane, the kind used in outdoor electrical
enclosures, has been fitted over one of those vent pairs to see whether it
cuts down on driven rain getting through while still letting moisture out.
Separately, the desiccant sachet that used to sit loose in the bottom of
the case is being moved to a small mesh pocket mounted near the lid, on the
theory that condensation collects on the underside of the lid first and the
sachet does more good closer to where the moisture actually forms.

A sloped lid is the fourth idea and the least developed. The current lid is
flat, and standing water has been seen sitting on top of two enclosures
after heavy rain, which is suspected of forcing moisture through the seal
over time even where the seal itself looks intact. A wedge insert cut from
scrap plastic has been tried under one lid to test the principle, but
nobody has yet cut a proper sloped lid from scratch, and doing so would
mean giving up interchangeability with the existing flat-lid stock, which
is why it hasn't happened yet.

## What we've learned so far

The vented enclosures have gone four weeks without visible condensation,
which is promising but not long enough to call a result, since the driest
month of the year is not a fair test of a fix meant for winter. The
membrane-covered vent has stayed dry through two proper downpours that
soaked the unmembraned vent's enclosure slightly at the seam, which is the
first piece of evidence that the membrane is doing something rather than
just adding cost and a point of failure. Moving the desiccant sachet
produced no measurable difference over the same four weeks, though that may
simply mean four weeks wasn't long enough for the old position to show its
usual problem either.

One thing that wasn't expected: the vented cases run very slightly warmer
inside during the day than the sealed ones, presumably because outside air
is now moving through rather than the case acting as a small greenhouse,
and that has a knock-on effect on the temperature sensor reading that
hasn't been fully characterised yet. It isn't large, but it's there, and it
needs sorting out before vents go on every station.

## Still unknown

Whether the vents let in enough dust or insects over a full season to cause
a different problem is not yet known, since four weeks in early spring is a
poor test of what late summer will do. It also isn't clear whether the
membrane will hold up to a full winter of frost and thaw cycling, given
that the material wasn't originally chosen with that in mind. The sloped
lid idea hasn't been tested at all beyond the wedge mock-up, so there's no
real evidence yet either way, and the temperature offset introduced by
venting still needs a proper before-and-after comparison against a control
station rather than the rough impression logged here. Nobody has decided
whether the final design will use all four changes together or some
smaller combination, and that decision is being deliberately left open
until there's a full season of data to look at rather than four weeks of
an unusually dry March.

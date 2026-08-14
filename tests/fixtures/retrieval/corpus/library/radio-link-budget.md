---
id: e590bd51-dff7-4963-acbe-65309702a8e2
type: document
project: ardwell-weather
created: 2025-12-03T09:15:00+00:00
updated: 2026-03-18T16:40:00+00:00
title: Radio link budget
description: "Settled reference for the radio side: transmit power and antenna choice at each end, why real working range fell to less than half the free-space arithmetic, what terrain and wet foliage cost, and the 3.1 km longest working hop."
status: frozen
---

# Radio link budget

This is the settled reference for the radio side of the network. It gets
updated when something on the hardware changes, not when someone has an
opinion about it. Treat the numbers below as load-bearing.

## Band and transmit hardware

Every station talks to the collector over LoRa at 868 MHz, at the maximum
output the licence-free duty-cycle rules allow us here, which works out to
14 dBm at the radio and a little less by the time cable and connector losses
are taken off. Each station carries a quarter-wave omnidirectional whip on a
short mast bracket rather than anything directional, because the stations
don't know in advance which way the collector will end up sitting relative
to the hillside, and a whip is one less thing to get wrong when a volunteer
is up a ladder in the wind. The collector itself runs a small fibreglass
colinear on the roof of the shed, which buys a few extra dB over a whip and
is the one place in the network where a directional-ish gain antenna made
sense, since the collector's position doesn't move.

## What the arithmetic says versus what we get

A back-of-envelope free-space link budget at 868 MHz with our transmit power
and antenna gains suggests we should comfortably clear four or five
kilometres in open country, and for the first couple of installs that
matched what we saw. In practice, once stations went in on the actual valley
sides rather than the flat test field behind the collector shed, the working
range dropped by more than half. None of this is a fault in the radios; it
is the valley doing what valleys do to a signal that has to skim along
ground rather than travel through open air, and it is worth remembering that
every dB the spreadsheet promises is a dB the terrain is entitled to take
back.

## The longest hop that actually works

The furthest pair of stations that hold a reliable link sit 3.1 km apart,
and that link only works because there is a clean sightline along the
valley floor between them with nothing but grazing land in the way. Most
pairs on the network are much closer than that; station spacing across the
valley runs from as little as 400 m up to that 3.1 km outlier, and the
short-spacing sites are mostly short because the ground between them is
awkward rather than because closer was ever the plan. A gap of 400 m
between two stations on the same side of a fold in the hillside can be
harder to close reliably than the 3.1 km hop is, which surprised more than
one of us the first time it came up.

## Terrain and what eats the margin

Tree cover is the worst offender, followed by the fold of the hillside
itself, then hedgerows at mast height, then rain-loaded foliage in summer,
which quietly costs more than dry foliage does and has caught us out during
a wet August. A station with a clean line to the collector across open
pasture will run a comfortable link with margin to spare. A station with
even a modest rise or a stand of trees between it and the collector can sit
right at the edge of usefulness, and on a bad day a stretch of low cloud
sitting in the valley seems to make it worse still, though nobody has
measured that properly. Mast height helps more than transmit power does
once terrain is the limiting factor, because clearing the first obstruction
matters more than adding a couple of dB that the same obstruction would
just eat anyway.

## Notes for anyone siting a new station

Walk the proposed site with a handheld unit before committing a mast to the
ground, and do it in whatever weather the day gives you rather than waiting
for a dry one, because a marginal link on a dry day can fail outright once
the leaves are wet. Favour a sightline over a shorter straight-line
distance every time; a station 3 km away with clean ground between it and
the collector will out-perform a station 800 m away sitting behind a
hedge and a rise. Where a plot has genuinely no sightline, the honest answer
is usually that it isn't a site for this network, radio range on paper
notwithstanding.

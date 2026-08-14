---
id: f1a2b3c4-d5e6-4789-a012-3456789abcde
type: document
project: ardwell-weather
created: 2025-11-18T14:30:00+00:00
updated: 2026-03-02T09:45:00+00:00
title: Antenna mounting hardware
description: The standardised mast clamps, antenna brackets, guy-stay kits and coax strain relief used across the network, which three stations are guyed, and the improvisations that have caused trouble before.
status: active
---

# Antenna mounting hardware

Every station in the Ardwell Valley network carries a short whip antenna on a
shared mast with the rain gauge and solar panel, and the mounting hardware is
deliberately boring on purpose. Boring means fewer surprises when someone is
standing on a ladder in drizzle trying to remember which clamp goes where.
This page lists what we standardised on after the first few installs turned
into a mix of borrowed caravan brackets and whatever was in the shed.

## Mast clamps and bracket sets

The working height for most sites is a two-metre aluminium mast in three
sections, and each section joins with a stainless hose clamp rather than a
riveted joint so a bent segment can be swapped without cutting anything.
The LoRa antenna mounts on a fixed bracket roughly thirty centimetres below
the sensor head, bolted through a single hole drilled in the mast wall with
a rubber grommet on the inside so water does not track down the coax. We
bought the brackets in a batch of fifteen from a marine supplier; twelve are
in the field and three sit in the spare parts box at the collector house.

Bracket orientation matters more than people expect. The antenna should
point roughly toward the valley bottom where the collector sits, but a few
degrees off axis is fine on 868 MHz. What is not fine is mounting the whip
parallel to the mast tube, which happened once at AWS-04 before anyone
noticed the pattern in the packet loss logs.

## Guy wire and stay kits

Only three stations use guy wire because the ground is too soft or the mast
is taller than the usual two metres. AWS-04 at two-point-six metres and
AWS-09 at the same height both have a three-leg stay kit anchored with corkscrew
ground stakes, and AWS-10 uses a single backstay to a fence post the landowner
agreed we could tie to. The wire itself is 2 mm galvanised steel with
turnbuckles at the lower ends so tension can be checked without climbing.

Guy wire changes the climb procedure. Nobody tensions a stay from the top
of the ladder; you set the mast vertical first, fix the bracket and antenna,
then walk the stays out from the ground. The fieldwork safety rules cover
wind cut-offs, and a guyed mast in gusty weather is worse than an unguyed
one because the wires whip unpredictably.

## Coax routing and strain relief

The coax from the Kestrel-3 logger board exits the enclosure through a cable
gland and runs up the inside of the mast before emerging at the bracket.
Strain relief is a small plastic loop screwed to the mast wall about ten
centimetres below the bracket, and the coax is cable-tied with enough slack
that the enclosure lid can be opened without pulling on the connector.
Several early installs skipped the loop and paid for it when frost made the
coax stiff enough to tug the SMA joint loose.

If you are replacing an antenna or bracket on site, photograph the routing
before you take anything apart. The photos live in the shared album, not
here, but the rule is the same: put it back the way you found it unless you
have a specific reason to change it.

## What not to improvise

Do not use jubilee clips meant for garden hose on the mast joints; they
corrode and seize. Do not wrap coax around the mast in a spiral for neatness,
because that detunes the whip in ways the link budget spreadsheet does not
model. Do not mount a second antenna on the same bracket without checking
with whoever maintains the collector software, since duplicate transmitters
on overlapping stations have caused confusion in the upload logs before.

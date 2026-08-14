---
id: 98226ecf-f153-4269-96f8-36209e346278
type: document
project: ardwell-weather
created: 2025-12-11T13:20:00+00:00
updated: 2026-02-14T11:05:00+00:00
title: Debug cable pinout notes
description: Pin assignments for the two debug cables in the kit bag - the red-taped six-pin logger header cable and the blue-taped four-pin handheld adapter - and the field faults that grabbing the wrong one imitates.
status: active
---

# Debug cable pinout notes

Two different debug cables float around the kit bag and it's easy to grab
the wrong one in the dark at the base of a mast, so this is the reference
for which pins do what on each. Neither cable is labelled by the
manufacturer in a way that survives a season of being coiled up in a damp
bag, so we mark our own and refer to them by those marks rather than
anything printed on the connector.

## Cable A — logger debug header

Cable A is the one that plugs into the station logger's onboard debug
header, a six-pin JST connector recessed just inside the enclosure lid. It's
marked with a loop of red tape near the logger end so it can't be confused
with cable B by feel alone in poor light.

Pinout, numbered left to right with the connector's keyed notch facing up:

- Pin 1 — ground
- Pin 2 — logger power rail, present even when the station is otherwise
  asleep, so treat this pin as live at all times
- Pin 3 — serial transmit, logger to handheld
- Pin 4 — serial receive, handheld to logger
- Pin 5 — reset line, held low briefly to force a clean reboot without
  pulling the battery
- Pin 6 — unused on the current logger revision, present for a sensor
  expansion header that hasn't been fitted on any station yet

## Cable B — Kestrel handheld adapter

Cable B adapts the six-pin logger header to the four-pin port on the
handheld unit used for field checks, and is marked with a loop of blue tape
at the handheld end. It carries only ground, power, transmit, and receive
through to the handheld — the reset and expansion pins on cable A simply
aren't wired through this adapter, so a reset still has to be done with
cable A plugged straight into a laptop rather than through the handheld.

## Common mistakes

Plugging cable A in with the notch facing the wrong way is the single most
common cause of a "dead" logger reported from the field that turns out to
be nothing more than a reversed connector — the header isn't fully keyed
against it on the older enclosure batches, so it's worth checking the notch
before assuming a real fault. Using cable B's four pins where cable A's full
six are needed silently drops the reset line, which shows up as a handheld
session that can read status fine but can't force a reboot, another thing
that looks like a fault and isn't.

## Storage

Both cables live in the same zip pouch in the field bag, kept apart from the
spare desiccant sachets after one cable's connector corroded slightly from
sitting against a sachet that had gone damp. Neither cable is weatherproof
on its own, so neither gets left plugged into a station outside of an
active debug session.

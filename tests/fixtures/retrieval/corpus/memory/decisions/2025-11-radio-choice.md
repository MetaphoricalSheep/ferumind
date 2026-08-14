---
id: a68bbfb7-5e2d-45d7-89e8-e668862517a2
type: document
project: ardwell-weather
created: 2025-11-18T14:00:00+00:00
updated: 2025-11-20T09:15:00+00:00
title: Radio choice
description: Why readings travel by LoRa at 868 MHz rather than 433 MHz or a cellular modem per station, the antenna size, noise floor, running cost and coverage arguments behind it, and what would justify reopening the question.
status: active
---

# Radio choice

With four stations already in the ground on a temporary logging setup, we
needed to settle on how readings would actually get off the hill and down to
the collector before putting in the rest of the network. This records what
we decided, why, and what we are giving up by deciding it this way, so that
the reasoning doesn't have to be reconstructed from memory the next time
someone questions it.

## The shortlist

Three options were seriously considered: LoRa at 868 MHz, LoRa at 433 MHz,
and a cellular modem fitted to every station. A wired option was ruled out
early without much debate — running cable along a valley this size, over
ground none of us own outright, was never realistic for a volunteer group
working weekends.

## Why 868 MHz over 433 MHz

Both bands are licence-free and both would have worked at the ranges we
need, so the deciding factors were practical rather than about raw range.
An antenna tuned for 868 MHz is a good deal smaller than one tuned for 433
MHz, which matters when the antenna is riding on top of a 2 m mast that also
has to survive valley winds without adding a lot of extra windage. The 433
MHz band locally also carries more background traffic — the usual mix of
alarms, remote controls and other short-range devices — and a couple of
early bench tests showed a noisier noise floor there than on 868 MHz. Range
on paper was similar enough between the two that it wasn't the factor that
decided it.

## Why LoRa over a cellular modem

A cellular modem in every station was attractive on paper: no need to worry
about line of sight between stations, no hop planning, and a proven,
widely-used technology. It fell down on two points that matter more to us.
First, running cost: twelve stations each carrying a SIM and a data
contract is a recurring cost the group would be committed to indefinitely,
and this network is unfunded — everything gets paid for out of volunteers'
own pockets or not at all. Second, coverage: several of the sites we were
already planning sit in parts of the valley with patchy or no cellular
signal, which would have meant some stations simply couldn't have used it
regardless of cost. LoRa's licence-free duty cycle limits how much airtime
each station can use, but at the reporting interval we were planning that
limit was never going to bind, and it comes with no ongoing bill.

## What we are giving up

Choosing LoRa over a cellular modem means giving up a comfortable safety net
if a radio hop fails: there is no automatic fallback path, and a station cut
off from its neighbours by, say, storm damage or new foliage is cut off
until someone fixes the path or the vegetation. It also means payload sizes
stay small, so it was never going to be a way to send anything richer than
the readings themselves, which was already the plan. We accepted both of
those trade-offs going in.

## What would make us revisit this

If the group ever needed to report more often than the duty cycle allows
without also needing a repeater, that would be worth stopping and rethinking
in earnest. Equally, if cellular coverage improves across the valley to the
point where every planned site has a usable signal, or data costs for a
site network of this size come down enough that the running cost stops
mattering, the balance in this decision shifts and it would be worth
another look rather than sticking with LoRa out of habit.

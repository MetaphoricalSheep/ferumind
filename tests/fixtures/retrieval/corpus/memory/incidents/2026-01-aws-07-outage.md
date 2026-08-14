---
id: 0a0f6885-acdb-4f07-b4a4-e1d359f0c9d1
type: document
project: ardwell-weather
created: 2026-01-24T16:00:00+00:00
updated: 2026-01-24T16:00:00+00:00
title: AWS-07 eleven-day outage, January 2026
description: Eleven days of silence from AWS-07 in January 2026, most of it spent chasing a radio path that turned out to be fine before a failed battery was found, and the voltage-trend check the rota adopted afterwards.
status: active
---

# AWS-07 eleven-day outage, January 2026

AWS-07 dropped off the dashboard on the evening of the 6th of January and did not
report again until the 17th, eleven days with nothing coming through from that
station. It sits on the awkward eastern side of the valley, one of the harder
stations to reach in winter, which is part of why it took as long as it did before
anyone had eyes on the enclosure itself.

## What happened

The first missed upload wasn't remarked on at the time; stations miss the odd cycle
and catch up next time round without anyone noticing. It was the third or fourth
missed cycle in a row that got the volunteer on rota actually checking the log, by
which point AWS-07 had already been silent for the best part of two days. From there
the assumption in the group chat was immediate, and as it turned out wrong: the radio
path to AWS-07 crosses a stand of trees that had been flagged before as marginal, and
that's where everyone's first thought went.

## Chasing the radio theory

The site lead walked the hop from AWS-06 with a handheld the following weekend and
got a clean signal the whole way, which should have settled the radio question there
and then but didn't quite, because the walk was done in calm daylight and the outage
had started during a spell of wet, gusty weather. A second visit was pencilled in to
check the antenna mount on AWS-07 for movement, on the idea that a knocked antenna
could explain a clean path test alongside a silent station. That visit found the
mount solid and the antenna exactly where it should be.

Ruling out the radio path took most of the eleven days between the two visits, partly
because of the terrain and partly because nobody wanted to be the one who declared it
fine and turned out to be wrong later. Looking back, the group spent close to a week
on the wrong theory before anyone actually opened the enclosure.

## The actual fault

When the site lead finally got the lid off AWS-07, the battery voltage read barely
above four volts under load, well below what the logger needs to run the radio
reliably. The solar panel and its wiring both tested fine, so the fault wasn't in the
charging path; the battery itself had simply failed to hold charge through a run of
overcast days in early January, the kind of stretch the solar sizing is supposed to
cover but evidently didn't manage for this particular battery. It was a month on from
the pair swapped at AWS-02 and AWS-05 in December, and with hindsight this one had
probably been marginal for a while before it finally gave out.

## Getting it back up

A replacement battery went in on the 17th and the station reported again within the
hour, which was in itself fairly strong confirmation of what the actual problem had
been; a radio fault wouldn't have cleared itself the moment fresh power went in. The
old battery was carried back down for a look on the bench and wouldn't hold more than
a couple of volts, well past anything worth trying to nurse along.

## What changed afterwards

The clearest lesson out of this was that a battery running low looks, from the
dashboard, exactly like a radio problem: both show up as a station that has simply
stopped sending anything at all. The rota now checks battery voltage trend, not just
whether a signal is present, before anyone goes chasing a radio explanation, and a
station that has gone quiet gets that check done first rather than last. It's a
smaller change than eleven lost days might suggest, but it is the one that would have
caught this a week earlier.


---
id: d71c52be-2734-4365-ad11-a28e34c511bc
type: document
project: ardwell-weather
created: 2026-02-10T11:00:00+00:00
updated: 2026-02-14T18:20:00+00:00
title: Upload interval
description: "The move to a five-minute upload cycle from fifteen: the AWS-07 outage and coarse rainfall resolution that prompted it, the week-long single-station trial that cleared it, and the winter battery margin it eats into."
status: active
---

# Upload interval

Stations now push a reading to the collector every five minutes. This
replaces the fifteen-minute interval the network has run on since the first
stations went in, and this note is the current, correct answer to how often
data actually goes up — anything written before this date describing a
fifteen-minute schedule is describing a plan that no longer applies.

## What prompted it

Two things pushed this along at the same time. The AWS-07 outage in January
sat unnoticed for longer than it should have, and once it was resolved,
several of us kept coming back to how much sooner a gap like that would show
up if stations were checking in more often than once a quarter-hour. Ahead
of that, there had also been a running grumble that fifteen minutes is
coarse for rainfall in particular: with a 0.2 mm tipping bucket, a burst of
heavy rain can land several tips inside one interval and all you get out the
other end is a total, with no sense of how the rain was actually falling
across that window.

## What we measured first

Before changing the whole fleet, one station ran the five-minute interval on
its own for just over a week while the rest stayed on fifteen. The main
thing being watched was battery voltage overnight compared with a similar
station still on the old interval, alongside the CRC failure rate to check
the radio wasn't being pushed into a range where it was competing with
itself. Neither showed anything alarming over the trial, which is what
cleared it to go fleet-wide.

## What it costs

More frequent uploads mean more radio wake-ups, and each one draws current
whether or not there's anything interesting to report. The rough increase in
average draw from the trial was small enough to disappear into normal daily
variation through summer, but it eats into the margin the solar sizing
carries for a run of sunless days in midwinter, which was already tight
before this change. It is not expected to cause problems on its own, but it
is one more thing worth checking on a winter visit rather than being ignored
because it wasn't a factor before.

## Where this leaves the earlier schedule

The original fifteen-minute schedule was written when the first stations
went in and was a reasonable plan at the time, based on what the network
looked like then. It has not been updated to reflect this change and is not
going to be; it now sits in the archive as a record of what was decided at
the time, not as something anyone should follow. If a query about upload
frequency turns up that older document, this one is the one to trust.

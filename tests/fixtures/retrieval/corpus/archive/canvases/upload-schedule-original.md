---
id: 28103e2f-a480-4a0d-a59c-1965ffd7d683
type: document
project: ardwell-weather
created: 2025-09-22T14:30:00+00:00
updated: 2025-09-22T14:30:00+00:00
title: Upload schedule
description: The superseded fifteen-minute upload cycle agreed for the first four stations, and the airtime, battery and data-resolution reasoning behind that figure. Replaced by the five-minute interval.
status: archived
---

# Upload schedule

With the first four stations now in the ground and reporting, this sets out how often
each station uploads its readings to the collector and why that figure was chosen.
Every station uploads on a 15-minute cycle: readings are taken continuously by the
logger board but batched and sent every fifteen minutes rather than streamed in real
time.

Fifteen minutes is comfortably often enough for what the group actually wants out of
this data. Rainfall, temperature and humidity don't move fast enough at valley scale
for a five-minute or one-minute cycle to tell a meaningfully different story, and the
tipping-bucket gauge itself only registers in 0.2 mm steps, so a shorter interval
mostly just means more empty readings rather than more information.

The real argument for fifteen minutes is airtime and power. Every radio transmission
costs battery, and with the panels sized around the darkest stretch of midwinter
there isn't spare budget to spend on chatty stations. A fifteen-minute cycle keeps
each station's radio on air only a small fraction of the time, which matters both for
battery life and for keeping the shared 868 MHz band clear for everyone else using it
in the area. Going to a shorter interval would mean either accepting shorter battery
life through winter or resizing the solar panels, and neither looked worth it against
what a shorter interval would actually buy in data quality.

There's also a practical side to it: fifteen minutes is a comfortable interval to
work with when checking a station's status by hand. A missed upload or two is easy to
notice against a background of four an hour, without the noise of a much faster cycle
making a single dropped packet look more alarming than it is.

This interval was agreed for all four stations going in this autumn and is expected
to carry the network as it grows to the full set of twelve. Any station added later
should follow the same schedule unless there's a specific reason for that site to
differ.


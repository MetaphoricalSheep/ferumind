---
id: 9bf7b5a5-5eed-4f00-b433-fc5f6fde45a1
type: document
project: ardwell-weather
created: 2026-02-20T09:00:00+00:00
updated: 2026-03-28T16:40:00+00:00
title: Station inventory
description: Per-station table of install date, firmware, board revision, mast height and a one-line note for all twelve stations, updated on the day of a visit and treated as the tie-breaker when another document disagrees.
status: active
---

# Station inventory

Twelve stations are in the ground along the valley, each built around a
Kestrel-3 logger board, and this page is the single place to check what is
actually installed at each one rather than relying on memory or whoever did
the last visit. Firmware and mast height in particular are worth checking
here before a site visit, since not every station is running the same
build at the same time.

Install date is the day the station first went live on site, not the day it
was bought or bench-tested, and firmware is whatever the station is actually
running rather than whatever the latest release happens to be. Mast height
is measured from the ground to the sensor head, and the note column is
deliberately short — anything longer than a line belongs in a proper write-up
elsewhere, not squeezed into a table cell.

| Station | Installed | Firmware | Board | Mast height | Note |
|---|---|---|---|---|---|
| AWS-01 | 2025-09-14 | 1.6.1 | Rev B | 2.0 m | Top of the ridge, first one in, takes the worst of the westerlies. |
| AWS-02 | 2025-09-14 | 1.6.1 | Rev B | 2.0 m | Battery swapped Dec 2025; otherwise no history worth noting. |
| AWS-03 | 2025-09-21 | 1.6.1 | Rev B | 2.0 m | In the lee of the old barn; readings run a touch sheltered. |
| AWS-04 | 2025-09-21 | 1.6.1 | Rev B | 2.6 m | Hedge grew up faster than expected; mast raised at commissioning. |
| AWS-05 | 2025-10-05 | 1.6.1 | Rev B | 2.0 m | Battery swapped Dec 2025 alongside AWS-02. |
| AWS-06 | 2025-10-19 | 1.6.1 | Rev C | 2.0 m | Cleanest line of sight to the collector on the network. |
| AWS-07 | 2025-10-19 | 1.6.1 | Rev C | 2.0 m | Eleven-day outage in Jan 2026, traced to the battery; checked more often since. |
| AWS-08 | 2025-11-09 | 1.6.1 | Rev C | 2.0 m | Sits low in a fold of the valley; ground round the base holds water. |
| AWS-09 | 2025-11-23 | 1.5.0 | Rev C | 2.6 m | Reports intermittently; suspect the hedge line is clipping the radio path. |
| AWS-10 | 2025-12-07 | 1.5.0 | Rev C | 2.0 m | Furthest station from the collector, right up near the edge of its working hop. |
| AWS-11 | 2026-01-18 | 1.6.1 | Rev C | 2.0 m | Newest of the winter batch; no issues since commissioning. |
| AWS-12 | 2026-02-15 | 1.6.1 | Rev C | 2.0 m | Most recent install; still bedding in. |

AWS-09 and AWS-10 are the two still waiting on the 1.6.1 rollout; both are
awkward hop points to reach on a short visit, so they tend to be left for a
weekend when there is time to spend on getting up to them properly rather
than rushing a firmware push. Everything else on the list has been at 1.6.1
since the March rollout went through. Board revision is noted mainly because
Rev C carries a small connector change from Rev B, and it is worth knowing
which one is on a mast before turning up with the wrong spare in the bag.

This table is kept current by whoever does the visit updating it the same
day, not from memory a week later — a mast height, firmware version or
battery swap that only lives in someone's head is the same as it not being
recorded at all. When a station's firmware, board, or mounting changes on a
visit, the row gets updated before the rota sheet is put away, and the two
new sites surveyed in March will get rows of their own once they are
actually in the ground rather than before.

It is worth treating this page as the tie-breaker whenever another document
disagrees with it about what is currently on a mast: a canvas or a note
might describe a station as it was on the day it was written, but this page
is meant to describe it as it is now. If the two don't match, trust this one
and go check the station rather than the older document.

---
id: a7b8c9d0-e1f2-4365-a789-bcdef0123456
type: document
project: ardwell-weather
created: 2025-12-01T10:00:00+00:00
updated: 2026-04-10T17:20:00+00:00
title: Collector housekeeping
description: "How the collector PC in the valley-bottom shed is kept running: the daily upload and log checks, where firmware images and checksums live, disk and backup-drive routine, and when a stall is worth escalating."
status: active
---

# Collector housekeeping

The collector is an old desktop PC in a shed at the valley bottom, and it is
the only machine that sees readings from all twelve AWS stations at once.
Volunteers who maintain field sites still need to know how the collector side
works, because half the "my station is broken" reports turn out to be upload
or disk space problems here rather than radio faults on the ridge.

## Daily checks that actually matter

Nobody needs to babysit the collector every morning, but whoever is on
rota for the month should glance at three things once a day: whether the
last upload timestamp is within the expected window, whether the incoming
folder is growing, and whether the system log shows repeated reconnect
attempts from the same station. A single missed interval from AWS-09 is
worth noting; the same station missing six intervals in a row is worth a
phone call before someone drives up the hill.

Readings land in a dated subdirectory under the collector's data root, and
the upload script moves them into an archive after the weekly backup runs.
If the archive partition fills up, new files still arrive but the cleanup
job starts failing silently until someone notices the error mail.

## Firmware files and version tracking

Station firmware images live in a versioned folder on the collector, not on
individual laptops. When a volunteer flashes a logger in the field, they
should confirm the image checksum against the readme in that folder before
travelling, because a partial copy on a USB stick has happened twice and
both times the station came back reporting a version string that did not
match anything in the inventory.

The collector does not push firmware over the air. Updates are always a
physical visit with a debug cable, which means the collector's role is
storage and record-keeping rather than remote management. After a successful
flash, the volunteer updates the station inventory and notes the change in
whatever canvas document is tracking the rollout.

## Disk, backups, and the spare USB drive

The main disk is large enough that raw readings are not the problem; log
rotation and old diagnostic captures are. Once a quarter, someone deletes
archived debug bundles older than six months unless they are tied to an
open incident. A USB drive labelled for backups stays plugged in; the backup
script runs Sunday night and sends a short summary to the volunteer mailing
list.

If the backup drive is unplugged for any reason, put it back before leaving
the shed. Several volunteers have taken it home intending to copy files and
forgotten to return it, which is how we ended up with a week where the only
copy of March readings lived on one person's kitchen table.

## When to escalate

Restarting the upload service fixes most collector-side stalls. If readings
are arriving in the incoming folder but not appearing in the archive, that
is a script problem, not a radio problem. If no station has uploaded in
twelve hours, check power to the shed and the Ethernet cable to the router
before assuming the whole network failed.

Do not edit the collector's cron table without telling the group. One
well-meaning change to the upload interval in 2026 caused a week of
misaligned timestamps that took longer to untangle than the original
problem.

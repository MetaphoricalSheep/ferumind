---
id: 2a18f5c5-6b6a-42f3-b9a0-b1586cf4ed16
type: document
project: ardwell-weather
created: 2025-09-06T09:15:00+00:00
updated: 2026-03-14T16:40:00+00:00
title: Station rollout working notes
description: Running log of how the network actually got built - site survey method, mast height and bracket choices, solar sizing, radio siting, the commissioning sequence, and what went wrong on the first four stations.
status: active
---

# Station rollout working notes

This is the working document for putting stations in the ground. It has been
added to since the first weekend in September 2025 and it shows: some of it
is tidy, some of it is a paragraph typed on a phone at the roadside. Treat it
as the log of how the network actually got built rather than as a manual.
Nobody has gone back and smoothed it out, and nobody should, because the
rough edges are usually where the useful detail is.

## Why a valley network at all

The nearest official weather station sits eighteen kilometres away on flat
ground near the coast road, and its numbers have never matched what people
in the valley actually experience. Frost forms here on nights the coast
station records five degrees positive, and the bottom two fields flood on
rainfall totals that would barely wet the official gauge. A wet autumn in
2024 was the trigger: three separate households lost vegetable beds to a
flash flood that the regional forecast had rated as a minor shower, and the
conversation that followed at the village hall turned into a plan rather
than a grumble. Nobody involved had built a sensor network before, which
shows in some of the early choices, but the group agreed early on that
something imperfect and running beat something well-designed and still on
paper. The first four loggers went in on a single September weekend with
borrowed ladders and a lot of enthusiasm, and the rest of the network has
grown outward from that starting cluster ever since.

The aim was never to replace the official forecast, only to see what the
valley itself was doing at a resolution nobody else was bothering to record.
Rainfall, temperature, humidity and battery state are logged locally and
relayed by radio to a collector machine at the valley bottom, and the whole
setup runs without a budget: every pole, bracket and roll of cable has come
out of someone's shed or a weekend trip to the scrapyard. That constraint
has shaped almost every decision in this document, and it is worth saying
plainly here so later readers do not assume the choices below were made on
a clean sheet with proper funding behind them. They mostly weren't.

## Site survey method

A site visit starts with the map, not the ground. Whoever is doing the
survey marks candidate points at least a day before walking out, using the
existing station layout and a rough line-of-sight check against the terrain
model, so the day itself is spent confirming or rejecting rather than
discovering from scratch. On site, the checklist is deliberately short:
exposure to open sky for the rain gauge, a plausible mast anchor point that
isn't going to move in a gale, a sightline back towards the collector or
towards an existing station that can relay, and, not to be skipped, whoever
owns the field being asked and saying yes. Photographs are taken from four
compass points and logged with the candidate's initials and the date, partly
so a second opinion can be formed without a second visit, and partly because
memory of "that hedge over there" fades faster than anyone expects.

Distance from the collector matters more than it first appears to. Every
station between four hundred metres and just over three kilometres from its
nearest working neighbour has ended up on the network, and the group has
learned that the middle of that range survives weather far better than the
extremes. A site too close to another gives little extra coverage for the
effort of installing it; a site too far risks a hop that only works on the
clearest days. Landowner permission has never actually been the blocker
people expected it to be — every request so far has been granted, usually
over a cup of tea and a promise to keep the mast tidy — but it is treated as
a hard gate all the same, and no candidate site is surveyed a second time
until permission is confirmed in writing, even if that writing is a text
message.

## Mounting and mast height

The mast itself is almost always a length of galvanised scaffold pole,
sunk into a driven ground spike or, on softer ground, set in a bucket of
postmix that gets carried up in sections because a full bag on a valley
path is not something anyone wants to do twice. Three guy ropes at
roughly a hundred and twenty degrees apart are standard, tensioned by
hand and checked again a week later once the ground has settled around
the anchor. Corrosion has already claimed one set of mild-steel guy pegs
at an early site, replaced afterwards with stainless, and that swap is
now the default rather than an afterthought.

Height has mostly settled into a single answer, even though nobody wrote
it down as a rule at the start: across the network the mast is set at two
metres above ground at every site except two, where a mature hedge on the
sightline forced the crew to go up to two point six metres instead to
clear it, and both of those exceptions are noted on their respective
station cards so a future visit doesn't assume a fault when the numbers
just look a little different from the rest. Two metres was arrived at by
trial rather than calculation: the first mast went in taller, wobbled
alarmingly in a September gust, and got shortened the following weekend,
and every site since has copied the shorter figure unless the terrain
gave no choice. Anything taller than that starts to need proper guying
and a stiffer pole than a rucksack can carry in, and the group decided
early on that a slightly lower rain gauge beats a mast nobody trusts.

Bracket choice has changed twice since the first four sites. The original
brackets were off-the-shelf satellite dish mounts, which held the logger
enclosure fine but flexed enough in wind to throw the anemometer readings
off by a noticeable margin at two of the exposed sites. A stiffer
homemade bracket, cut from angle steel by one of the volunteers with
access to a workshop, replaced those at the affected sites in November
2025 and has since become the standard fitting for any new install.

## Power and solar sizing

Every station runs off a six volt twelve amp-hour sealed lead-acid
battery, topped up by a small solar panel through a basic charge
controller, and the group has been conservative about this from the
start rather than trying to shave weight or cost. The panel size was
chosen after a fair amount of back-and-forth about worst-case conditions,
and the answer the group settled on is that the solar panel is sized to
keep a station running through fourteen days with no meaningful sun in
midwinter, which is a deliberately pessimistic figure given how grey the
valley gets between December and February. That margin has already paid
for itself once: a run of overcast days in January left more than one
panel producing almost nothing for the better part of a week, and none of
the batteries dropped low enough to trip the low-voltage cutoff.

Battery voltage is checked at every routine visit and logged on the
station's card, not just read off the telemetry, because a battery that
is quietly failing can still report a plausible voltage under light load
right up until it can't. Two batteries were swapped on the same weekend
in December 2025 after their winter draw-down looked steeper than the
rest of the fleet, and that swap is recorded separately in the battery
log rather than repeated here. Cold has turned out to matter almost as
much as cloud cover: a sealed lead-acid battery loses a meaningful chunk
of its usable capacity as the temperature drops, so a battery that looks
fine on a mild fifty-fifty-charged autumn afternoon can behave quite
differently once the enclosure is sitting at close to freezing overnight,
and the fourteen-day margin exists partly to absorb that loss as well as
the lack of sun.

Charge controllers are the one part of the build the group buys rather
than improvises, on the basis that a wrongly wired charge circuit can
cook a battery in an afternoon and nobody wants to be the one explaining
that at the next work weekend. Even so, the controllers used are the
simplest PWM type rather than anything with maximum power point tracking,
because the panels are small enough that the efficiency gain wasn't
judged worth the extra cost or the extra thing that could fail out in
the field.

## Radio siting and line of sight

Every station talks over LoRa at 868 megahertz, and getting a usable link
back to the collector or to a neighbouring station is as much art as
measurement. The survey team carries a pair of handheld radios tuned
roughly to the same band during a site visit, less to prove a working
link than to rule out an obviously bad one before a mast ever goes up.
Hills, not trees, have turned out to be the dominant problem: a stand of
mature trees knocks a few decibels off a link but rarely kills it,
whereas a shoulder of high ground directly on the path can drop a signal
to nothing regardless of how tall the mast is at either end.

Where a direct link to the collector isn't achievable, the survey looks
for a station that already has one and can act as a relay, and this has
shaped the layout more than any original plan for it. Nobody sat down at
the start and designed a mesh; it grew site by site as each new candidate
either reached the collector directly or reached an existing station that
could pass its packets on. The longest single hop currently running
covers a little over three kilometres of open valley with nothing much in
the way, and that link is treated as the exception rather than something
to plan around, because it depends on a clear line across the widest part
of the valley that most candidate sites simply don't have.

Antenna orientation gets checked twice: once on the day of installation
and once again roughly a month later, because a mast that has settled
slightly in soft ground can rotate the antenna just enough to weaken a
link that tested fine on day one. This second check has caught two
marginal links early enough to fix them with a five-minute adjustment
rather than a diagnostic session weeks later wondering why a station's
packet loss has crept up.

## Commissioning checklist

A station is not considered live until it has gone through the same
sequence every time, partly so nothing gets forgotten and partly so a
fault found later can be traced back to a known-good starting point.
Power is applied first and the logger's own startup sequence watched on a
laptop over the debug cable before anything is buttoned up, because
chasing an intermittent fault through a sealed enclosure on a second
visit is far more annoying than catching it on the bench. Once the
logger boots cleanly, the rain gauge tipping mechanism is triggered by
hand a set number of times and the count checked against what arrives at
the collector, which catches a surprising number of wiring faults that
would otherwise only show up the next time it actually rained.

The station is then left running unattached to its final mounting for
roughly twenty minutes while readings are watched arrive at the
collector, giving the crew a chance to confirm temperature and humidity
look plausible against a handheld reference and that the radio link isn't
marginal before committing to the climb and the guying. Only after that
window does the enclosure go up onto its mast, and the final radio check
is repeated once everything is in its permanent position, since the
mounted orientation is not always identical to how it sat during the
bench test. A station card is filled in on the day, recording the serial
number, firmware version, mast height, and the date, and that card stays
the reference for anything unusual found on a later visit.

Firmware version is checked against the current release before a station
is signed off, and any station still carrying an older build gets flagged
for the next scheduled upgrade round rather than upgraded on the spot,
since a firmware change on a freshly commissioned unit adds a variable
nobody wants when they're already trying to confirm everything else is
sound.

## Sensor bring-up

The rain gauge is the sensor that causes the most first-time grief,
mostly because its tipping bucket mechanism needs to sit properly level
or the tip count starts to drift from the true rainfall figure in ways
that only show up over weeks rather than on the bench. A small spirit
level is now kept in the field kit specifically for this, added after the
second station's early readings looked suspiciously low and turned out to
be nothing more than an unlevel mount. Temperature and humidity sensing
has been more straightforward, though the first four stations all showed
a small daytime heating error before their enclosures were fitted with
better ventilation, since direct sun on the enclosure lid was warming the
sensor a degree or two above the true air temperature during the
afternoon.

Battery voltage sensing needed almost no attention once wired correctly,
but two early units had a loose connector that produced occasional zero
readings, traced eventually to a crimp that hadn't been fully seated
rather than any fault in the sensor itself. That particular failure mode
is now checked for explicitly during bring-up by gently tugging every
crimped connection before the enclosure is closed up, which takes thirty
seconds and has not let a loose crimp through since it became routine.
Bring-up finishes with a short burn-in period on the bench, generally
overnight, watching for anything erratic before the unit is ever taken
out to its site, and a station that shows anything odd during burn-in
simply doesn't go out that weekend.

## What we got wrong on the first four

Looking back at AWS-01 through AWS-04, the group got several things wrong
that later installs benefited from avoiding. The masts on those first
four sites went in noticeably taller than the two-metre figure that later
became standard, on the assumption that taller meant a better radio link,
and all four had to be shortened within the first couple of months once
it became clear the extra height bought little signal and cost a lot of
stability in wind. None of the first four enclosures had any ventilation
beyond the manufacturer's default, and all four subsequently developed
visible condensation inside the case well before anyone else on the
network hit the same problem, which in hindsight should have been
predicted given how exposed those sites are to daily temperature swings.

Cable runs on the first four were also left slightly too long and coiled
loosely inside the enclosure rather than properly dressed, which looked
fine on the bench but chafed against the case wall in wind over the
following months and damaged the insulation on one station's sensor
cable badly enough to need a full rewire. The solar panels on those
sites were mounted at a fixed angle chosen more by what looked
reasonable than by any calculation, and two of the four have since been
adjusted after a winter of noticeably slower charging than sites
installed later with a steeper, more deliberately chosen tilt.

None of this was disastrous, and all four stations were still reporting
data through their first winter, but every one of these early mistakes
fed directly into the checklist and the standard practices this document
now describes, and it felt worth writing them down properly rather than
just quietly fixing them and moving on.

## Open questions

A number of things about the rollout are still unsettled going into the
spring. The enclosure redesign that started in March 2026 is meant to
deal with the condensation problem properly rather than through the
partial fixes tried so far, but it isn't finished, and it isn't yet clear
whether the new lid profile will need a different bracket to fit. Two new
candidate sites were surveyed in March and both look promising on paper,
but neither has had a radio link confirmed in person yet, and the group
has learned not to trust a map-only assessment after at least one earlier
candidate that looked perfect and turned out to have a hill in exactly
the wrong place.

There is also an open question about whether the two-metre mast height
should simply become a written rule rather than the informal convention
it currently is, given that it has worked well everywhere except the two
hedge-blocked sites, and whether those two exceptions should get a
taller standard mount designed specifically for that situation rather
than a one-off fix each time. Nobody has yet worked out a good answer for
what happens if a station needs relocating after its landowner agreement
changes, since nothing in the survey process currently accounts for a
site becoming unavailable after installation rather than before it. And
finally, the group still hasn't decided whether the network should try
to standardise on a single mast and bracket kit that could be ordered in
bulk, or whether the current approach of building each mount from
whatever is on hand should simply continue, since it has been cheap and
has, so far, worked.

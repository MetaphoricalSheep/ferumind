---
id: a9d8c7b6-3e4f-4a5b-9c8d-7e6f5a4b3c2d
type: document
project: ardwell-weather
created: 2026-03-01T09:00:00+00:00
updated: 2026-03-28T16:20:00+00:00
title: Spring volunteer rota assignments
description: Who walks which stations from April to June after the March meeting, the access difficulty on each circuit, the battery voltage thresholds volunteers were given, and how spare batteries and tools are shared out.
status: active
---

# Spring volunteer rota assignments

The spring walking schedule for station maintenance has been sorted out after the March volunteers meeting, with coverage for all fourteen active stations through the April-June period. This covers the quarterly battery voltage checks, visual inspections, and any calibration work that got deferred from the winter months when some of the higher sites were inaccessible.

## Northern circuit (Jim and Sarah)

Jim takes the northern ridge walk covering AWS-01, AWS-02, and AWS-04 on the second Saturday of each month. This is the longest single walk at roughly 4 hours round trip, but the path is well-defined and the three stations are close enough together that battery replacement can be done for all three on the same visit if needed. AWS-01 has been running low voltage warnings since January and is first priority for a fresh battery.

Sarah covers AWS-03 and AWS-09 on the first weekend of the month, weather permitting. AWS-09 is the awkward one here, requiring a scramble up from the forestry track rather than a proper path, but it's also the station that's had the most enclosure problems with water ingress and needs the most frequent checks. She's asked for backup on any visit that involves carrying a replacement battery up to AWS-09, which is fair given the terrain.

## Central valley stations (Mike and Rachel)

Mike walks the easy central stations on the valley floor: AWS-05, AWS-06, and AWS-07. These are all road-accessible and the battery voltage has been stable through winter, so this is mostly visual inspections and checking that the rain gauge mechanisms are still free-moving after the spring melt. AWS-05 had spider webs in the gauge funnel last year around this time and Mike knows to check for that specifically.

Rachel has volunteered for AWS-08 and AWS-10, which are the two stations on the southern facing slopes that warm up earliest in spring. AWS-08 is a straightforward walk but AWS-10 requires crossing the beck at the narrow point and can't be reached safely when the water is high, so these visits are weather-dependent. Both stations have been solid performers and shouldn't need much beyond routine inspection.

## Eastern approach stations (David and Kate)

David covers AWS-11 and AWS-12, the two newest installations that went in last autumn. AWS-11 had the slow boot issue in February but has been stable since the firmware update, while AWS-12 has been the most reliable station in the entire network since commissioning. The eastern access track is rough but manageable in dry weather, and David has the 4WD that can make it most of the way to both sites.

Kate takes AWS-13 and AWS-14, which are the final pair on the eastern side but require a longer walk from where the track becomes impassable. AWS-13 sits in the old quarry site and gets good solar exposure, while AWS-14 is in the lee of the ridge and has needed more frequent battery attention. Kate has requested that someone else be available for backup if AWS-14 needs a battery replacement, since it's a steep carry from the nearest vehicle access.

## Battery voltage monitoring

All volunteers have been reminded to check the battery voltage on every visit and log it on the station card, not just when a low voltage alarm has already been triggered. The pattern from this winter suggests that batteries give very little warning before they drop below the usable threshold, and catching them early makes the difference between a planned replacement and an emergency callout.

The voltage readings to watch for are anything below 11.8 volts under load, which indicates a battery that will likely fail within the next month, and anything below 11.5 volts which means replacement should happen on the current visit if a spare battery is available. Fresh batteries typically read 12.6-12.8 volts when fully charged, and anything consistently above 12.2 volts indicates a battery in good condition.

## Radio signal strength checks

Each volunteer has been given a handheld radio programmed with the network frequencies to spot-check signal strength during their visits. This isn't a comprehensive test of the LoRa link budget, but it does catch obvious problems like a shifted antenna or a loose connection that might not show up as a complete station outage but could affect data reliability.

The check is simple: power on the handheld within sight of the station and attempt to raise the collector site. A clear response indicates the radio path is probably fine, while a weak or distorted response suggests something worth investigating further. No response at all doesn't necessarily indicate a fault, since the handheld runs much lower power than the station radios, but it's worth noting on the station card for correlation with any data upload issues.

## Spare equipment allocation

Each volunteer pair has been allocated two spare batteries and basic tools for routine maintenance. The spare batteries are to be returned to the equipment store after each rota period and replaced with freshly charged units, rather than kept by individuals long-term, to ensure they're always in known good condition when needed for an emergency replacement.

Jim and Sarah get the larger tool kit including the torque wrench for antenna work, since the northern circuit stations are the most exposed and most likely to need antenna realignment after winter weather. The other pairs get the basic electrical toolkit sufficient for battery replacement and simple troubleshooting, with the understanding that any major repairs get flagged for a dedicated maintenance visit rather than attempted in the field.
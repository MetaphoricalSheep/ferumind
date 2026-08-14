---
id: 7c4f9e12-89ab-4d3e-bfc2-a8e5d7f6c9b1
type: document
project: ardwell-weather
created: 2025-11-15T10:30:00+00:00
updated: 2026-01-22T14:45:00+00:00
title: LoRa duty cycle limits in the EU 868 MHz band
description: "What the ETSI one per cent duty cycle in the 868 MHz band permits: airtime per transmission, the headroom the current upload rate leaves, the randomised retry backoff, and the ceiling it sets on adding stations."
status: active
---

# LoRa duty cycle limits in the EU 868 MHz band

The European regulations for the 868 MHz ISM band impose strict duty cycle limits that directly affect how often our weather stations can transmit their data. Understanding these limits is essential for planning upload intervals and ensuring the network operates within legal bounds, particularly as we scale up the number of stations reporting to the same collector.

## The regulatory framework

European Telecommunications Standards Institute (ETSI) regulation EN 300.220 sets a maximum duty cycle of 1% for most devices operating in the 868.0-868.6 MHz portion of the band, which is where our LoRa radios operate. This translates to a maximum of 36 seconds of transmission time per hour for any given device, including both the data payload and all the protocol overhead that LoRa adds around it.

The duty cycle is calculated as a rolling average over any given hour, not as a fixed window, which means a device that has been quiet for several hours can transmit more frequently for a short period, but the average across any 60-minute window must not exceed the 1% limit.

## What this means for station transmissions

Each weather station transmit takes roughly 1.2 seconds of airtime when accounting for the LoRa spreading factor and bandwidth we use. This includes the time to transmit the actual sensor data plus all the radio protocol overhead. With the 1% duty cycle limit, each station could theoretically transmit up to 30 times per hour while staying within bounds, but that assumes no other stations are sharing the same frequency and no tolerance margin for regulatory safety.

In practice, we target a much more conservative transmission rate to avoid any risk of exceeding the limit during busy periods or when multiple stations attempt to transmit simultaneously. When this note was written the upload interval was fifteen minutes from each station, resulting in 4 transmissions per hour, consuming roughly 4.8 seconds of the available 36 seconds, leaving substantial headroom for network growth and unexpected retransmissions.

## Collision avoidance and backoff

LoRa itself has no collision detection, so when multiple stations transmit at the same time, their signals simply interfere with each other and potentially both transmissions are lost. The duty cycle regulation makes this problem worse by limiting how quickly a station can retry after a failed transmission. If AWS-03 and AWS-08 both attempt to upload at exactly 13:15:00 and neither packet gets through, both stations must wait before trying again, and that wait time counts against their duty cycle budget.

Our current approach uses a randomized retry delay of 30-90 seconds after a transmission failure, which spreads out the retries and reduces the chance of repeated collisions between the same pair of stations. The random window also ensures that stations don't fall into a permanent collision pattern where they keep trying at the same offset from each other.

## Scaling considerations

As the network grows beyond the current twelve active stations, the duty cycle constraint becomes the main limitation on how densely we can pack transmissions. Each new station we add reduces the available airtime for all the others, not because of any technical limitation of LoRa itself, but because of the regulatory ceiling.

The mathematics of this is straightforward: with 1% duty cycle shared across all stations using the same frequency, and 1.2 seconds per transmission, the absolute maximum number of stations that could transmit once per 15-minute window is 450 stations. In reality, the practical limit is much lower due to the need for retry capacity, tolerance margins, and the fact that transmission times can vary slightly depending on the exact payload size and atmospheric conditions.

## Future network planning

The long-term plan for valley-wide coverage will require either longer upload intervals as we add more stations, or splitting the network across multiple LoRa frequencies to distribute the duty cycle load. The 868 MHz band actually includes several sub-bands with different regulations, and we could potentially use 869.4-869.65 MHz which allows higher duty cycles but requires more expensive radio hardware.

Alternatively, moving to a 30-minute upload interval would double our station capacity under the current regulatory framework, at the cost of reduced temporal resolution in the weather data. Given that most meteorological phenomena develop over hours rather than minutes, this may be an acceptable trade-off for comprehensive coverage.

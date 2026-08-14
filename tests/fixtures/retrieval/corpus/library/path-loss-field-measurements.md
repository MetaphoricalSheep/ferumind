---
id: d4e5f6a7-8b9c-0d1e-2f3a-4b5c6d7e8f9a
type: document
project: ardwell-weather
created: 2025-09-22T16:15:00+00:00
updated: 2025-11-30T13:40:00+00:00
title: Radio path loss measurements across the valley
description: Measured attenuation on each station-to-collector path from the autumn survey, per-link figures against the calculated budget, the fog and rain sensitivity found, and the marginal AWS-06 route through the plantation.
status: active
---

# Radio path loss measurements across the valley

Field measurements of signal attenuation between weather stations and the collector site were carried out during September and October to validate the theoretical link budget calculations and identify any unexpected sources of path loss that could affect network reliability as more stations come online.

## Measurement methodology

Signal strength readings were taken using a calibrated spectrum analyzer at each station location while a test transmitter at the collector site transmitted continuous tone at 868.1 MHz. Measurements were made with the test equipment at the same height and orientation as the operational LoRa antennas to ensure results reflected real-world conditions rather than idealized free-space propagation.

Each measurement session included readings in different weather conditions where practical, since atmospheric moisture and barometric pressure can affect propagation at these frequencies over the distances involved. Morning fog, clear conditions, and light rain measurements were captured for most sites to establish the range of variation expected from weather effects alone.

## Direct line-of-sight paths

**AWS-01 to collector (2.8 km)**: Measured path loss 95-98 dB depending on conditions, closely matching the 94 dB predicted by free-space calculations with minor terrain shadowing. This is the cleanest radio path in the network and serves as the baseline for comparison with more challenging routes.

**AWS-02 to collector (1.9 km)**: Path loss 89-92 dB, slightly higher than the calculated 87 dB due to partial obstruction by the radio mast structure near the collector antenna. Still well within link budget margins and shows minimal weather sensitivity.

**AWS-04 to collector (3.4 km)**: Measured 101-105 dB against a calculated 96 dB, with the additional loss attributed to diffraction over the intermediate ridge that is not fully accounted for in simple line-of-sight models. Weather effects more pronounced at this distance, with up to 3 dB variation between clear and foggy conditions.

## Multi-hop and indirect paths

**AWS-03 via AWS-01 relay**: The two-hop path through AWS-01 shows cumulative loss of 108-115 dB, significantly better than attempting a direct 4.1 km path to the collector which would require cutting through the main ridge. The relay approach provides 8-12 dB improvement over direct transmission, justifying the additional complexity.

**AWS-09 through the forest gap**: This 2.1 km path includes 400 meters through mixed woodland and shows highly variable path loss depending on foliage conditions. Measurements ranged from 94 dB in late autumn after leaf fall to 108 dB during full summer foliage, suggesting this path may become unreliable during peak growing season without antenna height adjustments.

## Terrain and vegetation effects

The most significant finding was the severe impact of the pine plantation between AWS-06 and the collector site. Despite being only 1.6 km direct distance, path loss through the 600-meter forest section measured 112-118 dB, nearly 20 dB worse than equivalent open terrain. This path is currently marginal for reliable operation and will likely fail entirely as the plantation matures.

Conversely, the path across the open moorland to AWS-08 (2.5 km) measured within 2 dB of free-space predictions throughout all test conditions, demonstrating that terrain roughness alone has minimal impact on LoRa propagation compared to vegetation obstruction.

## Weather sensitivity analysis

Fog and light precipitation consistently added 2-4 dB of path loss across all measurements, with the effect most pronounced on the longer paths above 2.5 km. Heavy rain was not encountered during the measurement period, but extrapolation from light rain data suggests an additional 1-2 dB loss could be expected during severe weather.

Temperature inversion layers, common in the valley during calm morning conditions, occasionally produced anomalous results with signal strength 3-5 dB stronger than typical for the same path. This effect was irregular and could not be reliably predicted, but suggests that atmospheric ducting occasionally provides propagation enhancement that improves link margins beyond design calculations.

## Implications for network expansion

The measurements confirm that most existing station locations have adequate link budget margins for reliable operation, with AWS-06 being the notable exception requiring attention before winter weather further degrades the marginal path through the plantation.

Future station placement should prioritize clear line-of-sight paths over minimizing distance, as the path loss difference between 2 km through forest and 4 km across open terrain strongly favors the longer clear path. The relay capability demonstrated at AWS-03 provides a viable solution for stations that cannot achieve direct collector visibility.

## Recommendations for marginal paths

**AWS-06**: Immediate priority for antenna height increase or relocation to clear the forest canopy. Current 3-meter mast insufficient; recommend 6-meter minimum or site relocation 200 meters northeast to exploit the natural elevation advantage.

**AWS-09**: Acceptable for current summer operation but monitor closely during leaf-out period. Consider seasonal antenna adjustments or backup communication path through nearby AWS-08 if direct path reliability degrades significantly.

**Future eastern stations**: The planned expansion toward the eastern valley boundary should use the measured path loss data to optimize placement for reliable collector visibility, avoiding the need for additional relay sites unless absolutely necessary for coverage requirements.
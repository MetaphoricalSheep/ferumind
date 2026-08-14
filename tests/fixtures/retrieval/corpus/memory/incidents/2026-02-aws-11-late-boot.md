---
id: b2c3d4e5-6f7a-8b9c-d0e1-f2a3b4c5d6e7
type: document
project: ardwell-weather
created: 2026-02-18T11:30:00+00:00
updated: 2026-02-18T11:30:00+00:00
title: AWS-11 slow boot sequence after installation
description: The thirty-eight minute startup at AWS-11 on installation day, the same-day return visit and the checks made while waiting, and the LoRa join handshake blamed for it once the station settled into normal reporting.
status: active
---

# AWS-11 slow boot sequence after installation

AWS-11 was installed on the 15th of February but took nearly forty minutes to complete its startup sequence and begin reporting data, significantly longer than the typical 3-4 minute boot time seen with identical hardware at other stations. The delay was concerning enough to warrant a same-day return visit to verify the installation, though the station ultimately came online and has operated normally since.

## Timeline of events

The logger was powered on at 14:20 during the installation visit, with battery voltage reading 12.7 volts and all connections verified before closing the enclosure. By 14:45, when the installation team reached mobile signal coverage again, AWS-11 had not appeared on the dashboard despite being well past the expected boot time. A decision was made to monitor remotely for another hour before returning to site.

At 15:00, still no data from AWS-11. The team drove back to the installation site, arriving at 15:35, and opened the enclosure to find the logger's status LED cycling slowly through what appeared to be a radio initialization sequence rather than the usual steady operational state. Battery voltage had dropped to 12.4 volts but remained well within operating range.

The first data packet from AWS-11 was received at 15:58, thirty-eight minutes after initial power-on, followed by normal reporting at the configured 15-minute intervals from that point forward.

## Investigation during the extended boot

While waiting for the logger to complete its startup, the team verified all physical connections were secure and checked for obvious sources of interference that might disrupt the radio initialization process. The battery terminals were clean and tight, the antenna connection showed good continuity, and the enclosure was properly sealed with no visible ingress of moisture.

The solar panel output was tested and found to be within expected range for the overcast conditions on site. No loose components were found when the circuit board was visually inspected, and the SIM card was properly seated in its socket. The extended boot sequence appeared to be a software rather than hardware issue.

## Likely cause

Post-incident analysis suggests the delay was related to the LoRa radio's initial network join procedure, which can take significantly longer in areas with marginal signal conditions or when the network server is busy processing other join requests. AWS-11 sits in a slight depression relative to the collector site, and while the link budget analysis predicted adequate signal strength, the initial join handshake requires a higher signal-to-noise ratio than routine data transmission.

The timing coincided with several other stations reporting routine data, which may have created enough network traffic to delay the join acceptance from the server end. Once the initial join was completed successfully, subsequent transmissions used the established session credentials and proceeded normally.

## Outcome and follow-up

AWS-11 has operated without incident since completing its initial boot sequence, with battery voltage stable and data uploads arriving consistently at 15-minute intervals. The slow initial join does not appear to have affected the station's long-term operation or data quality.

No hardware changes were made, but the incident highlighted the importance of allowing adequate time during installations in marginal signal areas for the initial network join to complete. Future installations in similar locations will budget additional on-site time to verify successful startup before the installation team departs.

The extended boot time was noted on AWS-11's commissioning record for future reference, but no active troubleshooting is required unless similar delays are observed during routine maintenance visits or after power cycling events.
OctoPrint-TapoAutoShutdown v0.3.0

OctoPrint plugin for automatically controlling a Tapo P110 smart plug and managing Obico AI monitoring around your prints.

Features

* Automatically switches the Printer Tapo P110 off after a completed print.
* Configurable Printer Tapo shutdown delay in minutes.
* Automatically switches the OctoPi Tapo P110 off after a completed print, unless it fails on Obico.
* Preset OctoPi Tapo shutdown delay in minutes, after completed video rendering is saved.
* 
* Automatically disables Obico AI when a print starts.
* Configurable Obico AI monitoring delay.
* Enables Obico AI after the selected delay if the print is still running.
* Disables Obico AI when a print completes, is cancelled, or fails.
* Set the Obico delay to 0 to enable AI immediately.
* Helps reduce unnecessary Obico AI hour usage while a printer is idle or during the early part of a print.

Settings

The plugin provides settings for:

* Two unique Tapo P110 IP addresses
* Tapo username, Tapo password, both usually the same from the Tapo App
* Tapo shutdown delay
* Obico AI monitoring delay

Requirements

* OctoPrint installed on suitable Rasperry Pi 4
* Tapo P110 smart plug v1.0 - firmware 1.4.8 b260804 or later
* Tapo Python package 0.9.0 or newer
* Obico plugin for Obico AI monitoring features, with paid AI hours.

Installation

Install through OctoPrint’s Plugin Manager using the GitHub release package.

Development

This is an independently developed OctoPrint plugin by SpideRaY. To save power and the planet

GitHub: https://github.com/SpideRaY/OctoPrint-TapoAutoShutdown-ObicoDelay

Author

* SpideRaY

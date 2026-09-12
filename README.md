OctoPrint-TapoAutoShutdown
OctoPrint plugin for automatically controlling a Tapo P110 smart plug and managing Obico AI monitoring around your prints.

Features
Automatically switches the Tapo P110 off after a completed print.
Configurable Tapo shutdown delay in minutes.
Automatically disables Obico AI when a print starts.
Configurable Obico AI monitoring delay.
Enables Obico AI after the selected delay if the print is still running.
Disables Obico AI when a print completes, is cancelled, or fails.
Set the Obico delay to 0 to enable AI immediately.
Helps reduce unnecessary Obico AI hour usage while a printer is idle or during the early part of a print.

Settings
The plugin provides settings for:

Tapo P110 IP address
Tapo username
Tapo password
Tapo shutdown delay
Obico AI monitoring delay

Requirements
OctoPrint
Tapo P110 smart plug
tapo Python package 0.9.0 or newer
Obico plugin for Obico AI monitoring features

Installation
Install through OctoPrint's Plugin Manager using the GitHub release package.

Development
This is an independently developed OctoPrint plugin by SpideRaY.

GitHub:
https://github.com/SpideRaY/OctoPrint-TapoAutoShutdown-ObicoDelay

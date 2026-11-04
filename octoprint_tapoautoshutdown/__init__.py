import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import threading
import time
import urllib.error
import urllib.request
from importlib.metadata import version

import octoprint.plugin
from octoprint.systemcommands import system_command_manager
from tapo import ApiClient
from tapo.responses import PowerState
import octoprint_obico

__plugin_version__ = version("OctoPrint-TapoAutoShutdown")

# Raspberry Pi 4 thermal protection.
# Show a persistent OctoPrint warning from 60 C and request a clean Pi
# shutdown after two consecutive 10-second readings at/above 70 C.
SOC_WARNING_THRESHOLD_C = 60.0
SOC_SHUTDOWN_THRESHOLD_C = 70.0
SOC_SHUTDOWN_CONSECUTIVE_READINGS = 2
SOC_MONITOR_INTERVAL_SECONDS = 10


class TapoAutoShutdownPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
):

    def __init__(self):
        self._obico_timer = None
        self._obico_timer_lock = threading.Lock()
        self._successful_print = False
        self._timelapse_done = False
        self._timelapse_failed = False
        self._ender_plug_off = False
        self._octopi_shutdown_started = False
        self._auto_connect_handled = False
        self._auto_connect_lock = threading.Lock()
        self._bed_preheat_handled = False
        self._bed_preheat_complete_notified = False
        self._bed_cooling_monitor_lock = threading.Lock()
        self._bed_cooling_monitor_generation = 0      
        self._bed_40_notified = False
        self._bed_32_notified = False
        # Prevent the missing-configuration warning being written every second.
        self._ender_power_config_warning_logged = False
        # Throttle successful Ender power diagnostics independently of polling.
        self._ender_power_last_log_time = 0.0
        # Track the current ntfy configuration problem so each state is logged once.
        self._ntfy_config_warning_state = None

    def _send_ntfy_notification(self, message):
        """Send a push notification without disrupting printer control."""
        # Read the ntfy topic from OctoPrint settings so it can be changed
        # in Settings without editing the Python source.
        topic = (self._settings.get(["ntfy_topic"]) or "").strip()

        if not topic:
            if self._ntfy_config_warning_state != "missing":
                self._diag(
                    "warning",
                    "ntfy notification unavailable: topic is missing. "
                    "Enter your ntfy topic in the plugin settings."
                )
                self._ntfy_config_warning_state = "missing"
            return

        # Treat the topic as a single path component; don't allow a pasted URL
        # or slash/whitespace to change the destination.
        if "/" in topic or any(ch.isspace() for ch in topic):
            if self._ntfy_config_warning_state != "invalid":
                self._diag(
                    "warning",
                    "ntfy notification unavailable: topic is invalid. "
                    "Enter only the topic name, not a URL; spaces and '/' are not allowed."
                )
                self._ntfy_config_warning_state = "invalid"
            return

        # A valid configuration clears the warning state. A later problem
        # will therefore be reported again if the setting becomes invalid.
        self._ntfy_config_warning_state = None

        request = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": "Tapo Auto Shutdown", "Priority": "default"},
            method="POST",

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
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status < 200 or response.status >= 300:
                    self._diag(
                        "warning",
                        "ntfy notification failed: server returned HTTP status %s",
                        response.status,
                    )
        except Exception as e:
            self._diag("warning", "ntfy notification could not be sent: %s", e)

    def _init_dedicated_logger(self):
        """Write plugin-only diagnostics to OctoPrint's Logs directory."""
        try:
            logs_dir = octoprint.settings.settings().getBaseFolder("logs")
            os.makedirs(logs_dir, exist_ok=True)
            log_path = os.path.join(logs_dir, "plugin_tapoautoshutdown.log")

            self._tapo_file_logger = logging.getLogger(
                "octoprint.plugins.tapoautoshutdown.file"
            )
            self._tapo_file_logger.setLevel(logging.DEBUG)
            self._tapo_file_logger.propagate = False

            # Avoid adding duplicate handlers if the plugin is reloaded.
            for handler in self._tapo_file_logger.handlers:
                if isinstance(handler, RotatingFileHandler) and handler.baseFilename == os.path.abspath(log_path):
                    return

            handler = RotatingFileHandler(
                log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(logging.Formatter(
                "%(asctime)s - %(levelname)s - %(message)s"
            ))
            self._tapo_file_logger.addHandler(handler)
            self._tapo_file_logger.info("Dedicated Tapo Auto Shutdown log initialised")
        except Exception:
            # Logging setup must never prevent OctoPrint or the plugin starting.
            self._logger.exception("Could not initialise dedicated Tapo Auto Shutdown log")

    def _diag(self, level, message, *args, **kwargs):
        """Send diagnostics to the dedicated plugin-only log."""
        logger = getattr(self, "_tapo_file_logger", None)
        if logger is not None:
            getattr(logger, level)(message, *args, **kwargs)

    def on_after_startup(self):
        self._init_dedicated_logger()
        self._diag("info", "Tapo Auto Shutdown started")
        self._diag("info", "Tapo Auto Shutdown started")

        def check_for_obico():
            time.sleep(5)

            try:
                obico_plugin = self._plugin_manager.get_plugin("obico", False)
                if obico_plugin is not None:

                    self._diag(
                        "info",
                        "Obico plugin detected - ready for integration"
                    )
                else:
                    self._diag("warning", 
                        "Obico plugin not detected after startup"
                    )

            except Exception as e:
                self._diag("error", 
                    "Could not query Obico plugin: %s",
                    e,
                )

        threading.Thread(
            target=check_for_obico,
            daemon=True,
        ).start()

        # Start automatic printer connection monitoring
        threading.Thread(
            target=self._monitor_ender_power_for_connection,
            daemon=True,
        ).start()

    def _monitor_ender_power_for_connection(self):
        """Monitor printer power and request one automatic connection."""
        consecutive_high = 0
        connection_attempted = False
        armed = True

        while True:
            try:
                power = asyncio.run(self._read_ender_power())

                if power is None:
                    consecutive_high = 0
                elif power <= 6.0:
                    consecutive_high = 0
                    armed = True
                    connection_attempted = False
                elif power > 8.0:
                    consecutive_high += 1
                else:
                    consecutive_high = 0

                if (
                    armed
                    and not connection_attempted
                    and consecutive_high >= 3
                ):
                    armed = False
                    connection_attempted = True
                    consecutive_high = 0

                    if self._printer.is_operational():
                        self._diag("info", 
                            "Ender power is above 8 W, but printer is already connected"
                        )
                    else:
                        self._diag("info", 
                            "Ender power is above 8 W for three readings - requesting printer connection"
                        )
                        self._printer.connect()

            except Exception as e:
                self._diag("error", 
                    "Automatic printer connection monitor error: %s",
                    e,
                )

            # Poll every 10 seconds to reduce unnecessary Tapo API requests.
            # Three consecutive high readings are still required for connection.
            time.sleep(1)
    
    def on_shutdown(self):
        self._cancel_obico_timer()

    def on_event(self, event, payload):

        # Check that the Printer has connected
        if event == "Connected":
            self._maybe_preheat_bed()
        if event == "Disconnected":
            self._bed_preheat_handled = False

        # Start a new print and reset the event flags
        if event == "PrintStarted":
            self._successful_print = False
            self._timelapse_done = False
            self._timelapse_failed = False
            self._ender_plug_off = False
            self._octopi_shutdown_started = False
            with self._bed_cooling_monitor_lock:
                self._bed_cooling_monitor_generation += 1
                self._bed_40_notified = False
                self._bed_32_notified = False  
            self._disable_obico_monitoring()
            self._start_obico_timer()
           
        # Record successful timelapse rendering
        elif event == "MovieDone":
            self._timelapse_done = True
            self._diag("info", 
                "Timelapse rendering completed successfully"
            )
            self._maybe_start_octopi_shutdown()

        # Record failed timelapse rendering
        elif event == "MovieFailed":
            self._timelapse_failed = True
            self._diag("error", 
                "Timelapse rendering failed - automatic OctoPi shutdown cancelled"
            )    
        # Handle a successfully completed print
        elif event == "PrintDone":
            self._successful_print = True
            self._cancel_obico_timer()
            self._disable_obico_monitoring()

            # Start bed-temperature monitoring for printer shutdown.
            username = (self._settings.get(["username"]) or "").strip()
            password = self._settings.get(["password"])
            ip = (self._settings.get(["printer_tapo_ip"]) or "").strip()

            if username and password and ip:
                self._diag("info", 
                    "Print completed - starting bed-cooling monitor"
                )
                self._start_bed_cooling_monitor()
            else:
                self._diag("warning", 
                    "Bed-cooling monitor not started: Tapo credentials or IP missing"
                )

            # Check whether OctoPi shutdown should proceed
            if self._settings.get(["octopi_shutdown_enabled"]):
                self._diag("info", 
                    "Print completed - waiting for timelapse rendering"
                )
                self._maybe_start_octopi_shutdown()
        

        # Cancel Obico countdown if the print is cancelled or fails
        elif event in ("PrintCancelled", "PrintFailed"):
            self._successful_print = False
            self._timelapse_done = False
            self._timelapse_failed = False
            with self._bed_cooling_monitor_lock:
                self._bed_cooling_monitor_generation += 1
            self._cancel_obico_timer()
            self._disable_obico_monitoring()

    def _maybe_preheat_bed(self):
        """Preheat the bed after connection if enabled and the Tapo is on."""

        # Only proceed if automatic bed preheat is enabled
        if not self._settings.get(["bed_preheat_enabled"]):
            return
        if self._bed_preheat_handled:
            return         

        # Make sure the printer is connected and idle
        if not self._printer.is_operational():
            self._diag("info", 
                "Bed preheat skipped: printer is not operational"
            )
            return

        if self._printer.is_printing():
            self._diag("info", 
                "Bed preheat skipped: a print is already running"
            )
            return

        # Check the actual Ender Tapo on/off state
        try:
            username = (self._settings.get(["username"]) or "").strip()
            password = self._settings.get(["password"])
            ip = (self._settings.get(["printer_tapo_ip"]) or "").strip()

            client = ApiClient(username, password)
            plug = asyncio.run(client.p110(ip))
            info = asyncio.run(plug.get_device_info())

            if not info.device_on:
                self._diag("info", 
                    "Bed preheat skipped: Ender Tapo is switched off"
                )
                return

        except Exception as e:
            self._diag("error", 
                "Bed preheat skipped: could not check Tapo state: %s",
                e,
            )
            return

        # Use a fixed preheat target; this is intentionally not user-configurable.
        temperature = 50.0
   
        # Remind the user to switch on the light bar before preheating.
        self._plugin_manager.send_plugin_message(
            "tapoautoshutdown",
            {"type": "light_bar_reminder"},
        )

        # Ask the printer firmware to heat the bed
        try:
            self._bed_preheat_complete_notified = False
            self._bed_preheat_handled = True
            self._printer.set_temperature("bed", temperature)

            self._send_ntfy_notification(
                f" 🔥 Ender 3 Pro - Bed preheating set to {temperature:.0f}°C"
            )
            self._diag("info", 
                "Automatic bed preheat requested: %.1f C",
                temperature,
            )
            self._plugin_manager.send_plugin_message(
                "tapoautoshutdown",
                {"type": "bed_preheat", "temperature": temperature},
            )
            threading.Thread(
                target=self._monitor_bed_preheat_completion,
                args=(temperature,),
                daemon=True,
            ).start()

        except Exception as e:
            self._diag("error", 
                "Failed to start automatic bed preheat: %s",
                e,
            )

    def _monitor_bed_preheat_completion(self, target_temperature):
        """Monitor preheat, with timeout and cancellation checks."""
        timeout_seconds = 30 * 60
        start_time = time.monotonic()

        while time.monotonic() - start_time < timeout_seconds:
            time.sleep(5)

            if not self._printer.is_operational():
                self._diag("warning", 
                    "Bed preheat monitor stopped: printer disconnected"
                )
                return

            if self._printer.is_printing():
                self._diag("info", 
                    "Bed preheat monitor stopped: print started"
                )
                return

            try:
                temperatures = self._printer.get_current_temperatures()
                bed = (
                    temperatures.get("bed")
                    if isinstance(temperatures, dict)
                    else None
                )
                actual = (
                    bed.get("actual")
                    if isinstance(bed, dict)
                    else None
                )

                if not isinstance(actual, (int, float)):
                    self._diag("warning", 
                        "Bed preheat temperature unavailable; retrying"
                    )
                    continue
                    
                if actual >= target_temperature:
                    if not self._bed_preheat_complete_notified:
                        self._bed_preheat_complete_notified = True

                        # The fixed 50 C preheat target is the ready threshold.
                        if target_temperature >= 50.0 and actual >= 50.0:
                            self._plugin_manager.send_plugin_message(
                                "tapoautoshutdown",
                                {
                                    "type": "bed_preheat_complete",
                                    "temperature": round(actual, 1),
                                },
                            )

                            self._send_ntfy_notification(
                                f"✅ Ender 3 Pro - Bed preheated to {actual:.1f}°C."
                            )
                            self._diag(
                                "info",
                                "Bed reached print-ready temperature: %.1f C",
                                actual,
                            )
                        else:
                            self._diag(
                                "info",
                                "Bed reached preheat preset %.1f C; "
                                "Ready to print notification suppressed "
                                "because the target is below 50 C",
                                actual,
                            )
                    return

            except Exception as e:
                self._diag("warning", 
                    "Bed preheat monitor error; retrying: %s",
                    e,
                )

        self._diag("error", 
            "Bed preheat timed out after 30 minutes; target %.1f C not reached",
            target_temperature,
        )
        self._plugin_manager.send_plugin_message(
            "tapoautoshutdown",
            {
                "type": "bed_preheat_failed",
                "temperature": target_temperature,
                "message": (
                    "Preheating timed out. The bed did not reach "
                    f"{target_temperature:.1f}°C within 30 minutes. "
                    "Check the printer before printing."
                ),
            },
        )
            

    
    
    def _maybe_start_octopi_shutdown(self):
        if self._octopi_shutdown_started:
            return
        if not self._successful_print:
            return

        if self._timelapse_failed:
            self._diag("error", 
                "OctoPi shutdown cancelled because timelapse rendering failed"
            )
            return

        if not self._timelapse_done:
            self._diag("info", 
                "Waiting for timelapse rendering to finish"
            )
            return

        if not self._settings.get(["octopi_shutdown_enabled"]):
            return

        if not self._ender_plug_off:
            self._diag("info", 
                "Waiting for Ender plug to be confirmed off before starting OctoPi timer"
            )
            return

        self._octopi_shutdown_started = True
        self._diag("info", 
            "Timelapse complete and Ender plug confirmed off - starting OctoPi shutdown sequence"
        )

        threading.Thread(
            target=self._octopi_shutdown_sequence,
            daemon=True,
        ).start()

    def _start_obico_timer(self):
        self._cancel_obico_timer()

        try:
            delay_minutes = int(
                self._settings.get(["obico_monitor_delay"])
            )
        except (TypeError, ValueError):
            delay_minutes = 60

        self._diag("info", 
            "Obico AI monitoring timer started - %s minutes",
            delay_minutes,
        )

        self._obico_timer = threading.Timer(
            delay_minutes * 60,
            self._enable_obico_if_printing,
        )

        self._obico_timer.daemon = True
        self._obico_timer.start()

    def _cancel_obico_timer(self):
        with self._obico_timer_lock:
            if self._obico_timer is not None:
                self._obico_timer.cancel()
                self._obico_timer = None

    def _enable_obico_if_printing(self):

        with self._obico_timer_lock:
            self._obico_timer = None

        # Check that a print is actually still running
        try:
            if not self._printer.is_printing():
                self._diag("info", 
                    "Obico timer expired but no print is active - "
                    "AI monitoring not enabled"
                )
                return

        except Exception as e:
            self._diag("error", 
                "Could not determine printer state: %s",
                e,
            )
            return

        self._diag("info", 
            "Obico timer expired - enabling AI monitoring"
        )

        self._enable_obico_monitoring()

    def _enable_obico_monitoring(self):
        """
        Enable Obico AI monitoring for the linked printer.

        The exact Obico API call will be added once the
        authenticated endpoint construction is verified.
        """

        try:
            obico_module = self._plugin_manager.get_plugin(
                "obico",
                False,
            )

            if obico_module is None:
                self._diag("warning", 
                    "Obico plugin is not installed or enabled - "
                    "AI monitoring cannot be started"
                )
                return
            obico_plugin = getattr(obico_module, "__plugin_implementation__", None)

            self._diag("info", 
                "Obico plugin detected - ready to enable AI monitoring"
            )

            response = octoprint_obico.server_request(
                'PATCH',
                '/api/v1/octo/printer/',
                obico_plugin,
                headers=obico_plugin.auth_headers(auth_token=obico_plugin.auth_token()),
                json={'watching_enabled': True},
            )

            if response is not None and response.ok:
                self._diag("info", 
                    "Obico AI monitoring enabled successfully"
                )
            else:
                self._diag("error", 
                    "Failed to enable Obico AI monitoring: HTTP %s",
                    response.status_code if response is not None else "no response",
                )

        except Exception as e:
            self._diag("error", 
                "Failed to enable Obico AI monitoring: %s",
                e,
            )

    def _disable_obico_monitoring(self):
        try:
            obico_module = self._plugin_manager.get_plugin("obico", False)
            if obico_module is None:
                return

            obico_plugin = getattr(obico_module, "__plugin_implementation__", None)

            response = octoprint_obico.server_request(
                "PATCH",
                "/api/v1/octo/printer/",
                obico_plugin,
                headers=obico_plugin.auth_headers(auth_token=obico_plugin.auth_token()),
                json={"watching_enabled": False},
            )

            if response is not None and response.ok:
                self._diag("info", "Obico AI monitoring disabled successfully")
            else:
                self._diag("error", "Failed to disable Obico AI monitoring")

        except Exception as e:
            self._diag("error", 
                "Failed to disable Obico AI monitoring: %s", e
            )

    # Arm the OctoPi Tapo P110 shutdown timer
    async def _arm_octopi_tapo_timer(self):
        username = (self._settings.get(["username"]) or "").strip()
        password = self._settings.get(["password"])
        ip = (self._settings.get(["octopi_tapo_ip"]) or "").strip()

        if not username or not password or not ip:
            self._diag("error", 
                "OctoPi shutdown cancelled: Tapo credentials or IP are missing"
            )
            return False
            
        client = None
        plug = None
        self._diag("info", "OctoPi timer: starting setup for configured IP %s", ip)

        try:
            self._diag("debug", "OctoPi timer: creating Tapo API client")
            client = ApiClient(username, password)
            self._diag("debug", "OctoPi timer: requesting P110 device object")
            plug = await client.p110(ip)
            self._diag("debug", "OctoPi timer: P110 device object obtained")

            # Allow the configured time for Linux to shut down cleanly
            try:
                delay_minutes = int(
                    self._settings.get(["octopi_shutdown_delay"])
                )
                if not 1 <= delay_minutes <= 60:
                    raise ValueError("Delay must be between 1 and 60 minutes")
            except (TypeError, ValueError):
                delay_minutes = 5

            delay_seconds = delay_minutes * 60
            self._diag("info", "OctoPi timer: setting power-off timer for %s minutes", delay_minutes)
            await plug.set_timer(delay_seconds, PowerState.Off)
            self._diag("debug", "OctoPi timer: set_timer call completed")

            # Verify the timer was accepted
            self._diag("debug", "OctoPi timer: reading timer back for verification")
            timer = await plug.get_timer()
            self._diag("debug", "OctoPi timer: verification response received (response_present=%s)", timer is not None)

            if (
                timer is None
                or timer.desired_state != PowerState.Off
                or timer.remaining_s is None
                or timer.remaining_s <= 0
            ):
                self._diag("error", 
                    "OctoPi shutdown cancelled: Tapo timer verification failed"
                )
                self._diag("error", "OctoPi timer verification failed: response=%r", timer)
                try:
                    await plug.clear_timer()
                except Exception as clear_error:
                    self._diag("error", 
                        "Could not clear unverified Tapo timer: %s",
                        clear_error,
                    )
                return False
            self._diag("info", 
                "OctoPi Tapo shutdown timer verified: power off in approximately "
                "%s seconds",
                timer.remaining_s,
            )
            self._diag("info", "OctoPi timer verified: power off in approximately %s seconds", timer.remaining_s)
            return True

        except Exception as e:
            self._diag("error", 
                "Failed to arm or verify OctoPi Tapo shutdown timer: %s",
                e,
            )
            self._diag("exception", "OctoPi timer failed during setup/verification: %s", e)
            if plug is not None:
                try:
                    await plug.clear_timer()
                except Exception as clear_error:
                    self._diag("error", 
                        "Could not clear Tapo timer after error: %s",
                        clear_error,
                    )
                    self._diag("exception", "Could not clear Tapo timer after error: %s", clear_error)
            return False

    # Read current power from the Ender Tapo P110 (read-only)
    async def _read_ender_power(self):
        username = (self._settings.get(["username"]) or "").strip()
        password = self._settings.get(["password"])
        ip = (self._settings.get(["printer_tapo_ip"]) or "").strip()

        if not username or not password or not ip:
            # Log this once to the dedicated plugin log, not OctoPrint's main log.
            if not self._ender_power_config_warning_logged:
                self._diag(
                    "warning",
                    "Ender power reading skipped: Tapo credentials or IP are missing"
                )
                self._ender_power_config_warning_logged = True
            return None

        # If settings have been restored, allow a future missing-settings
        # condition to be recorded once again.
        self._ender_power_config_warning_logged = False

        try:
            client = ApiClient(username, password)
            plug = await client.p110(ip)
            result = await plug.get_current_power()

            # Extract a numeric power value if the result exposes one.
            # Polling remains every second; successful diagnostics are logged
            # at most once every 10 seconds to keep the dedicated log concise.
            for attribute in ("current_power", "power", "current_power_w"):
                value = getattr(result, attribute, None)
                if isinstance(value, (int, float)):
                    now = time.monotonic()
                    if now - self._ender_power_last_log_time >= 10:
                        self._diag(
                            "debug",
                            "Ender Tapo current power response: %r",
                            result,
                        )
                        self._diag(
                            "debug",
                            "Ender Tapo power reading: %.2f W",
                            value,
                        )
                        self._ender_power_last_log_time = now
                    return float(value)

            self._diag("warning", 
                "Ender power response received, but its numeric field "
                "is not yet recognised"
            )
            return None

        except Exception as e:
            self._diag("error", 
                "Failed to read Ender Tapo current power: %s",
                e,
            )
            return None
    
    # Monitor the real bed temperature after a successful print.
    def _start_bed_cooling_monitor(self):
        with self._bed_cooling_monitor_lock:
            self._bed_cooling_monitor_generation += 1
            generation = self._bed_cooling_monitor_generation
            self._bed_40_notified = False
            self._bed_32_notified = False  
        threading.Thread(
            target=self._bed_cooling_monitor_background,
            args=(generation,),
            daemon=True,
        ).start()

    def _bed_cooling_monitor_background(self, generation):
        """Notify at 40 C and switch off the printer plug at 30 C or below."""
        self._diag("info", 
            "Bed-cooling monitor started: reminder at 40 C, shutdown at 30 C"
        )

        while True:
            with self._bed_cooling_monitor_lock:
                if generation != self._bed_cooling_monitor_generation:
                    self._diag("info", "Bed-cooling monitor stopped")
                    return

            try:
                temperatures = self._printer.get_current_temperatures()
                bed = temperatures.get("bed") if isinstance(temperatures, dict) else None
                actual = bed.get("actual") if isinstance(bed, dict) else None

                if not isinstance(actual, (int, float)):
                    self._diag("warning", 
                        "Bed-cooling monitor: actual bed temperature unavailable; will retry"
                    )
                else:
                    actual = float(actual)
                    self._diag("info", 
                        "Bed-cooling monitor: bed actual temperature %.1f C",
                        actual,
                    )

                    if actual <= 30.0:
                        self._diag("info", 
                            "Bed reached %.1f C; switching off Ender Tapo plug",
                            actual,
                        )
                        switched_off = asyncio.run(self._switch_off_ender_tapo())
                      
                        if switched_off:
                            self._ender_plug_off = True
                            self._diag("info", 
                                "Ender Tapo plug switched off after bed cooled to 30 C or below; Ender-off flag set"
                            )
                            self._plugin_manager.send_plugin_message(
                                "tapoautoshutdown",
                                {
                                    "type": "bed_cooling_shutdown",
                                    "temperature": round(actual, 1),
                                },
                            )
                            # Start the OctoPi sequence only if timelapse has also finished.
                            self._maybe_start_octopi_shutdown()
                            return
                        else:
                            self._diag("error", 
                                "Could not switch off Ender Tapo plug; monitor will retry"
                            )
                            time.sleep(10)
                            continue 

                    if actual <= 40.0:
                        should_notify = False
                        with self._bed_cooling_monitor_lock:
                            if (
                                generation == self._bed_cooling_monitor_generation
                                and not self._bed_40_notified
                            ):
                                self._bed_40_notified = True
                                should_notify = True

                        if should_notify:
                            self._plugin_manager.send_plugin_message(
                                "tapoautoshutdown",
                                {
                                    "type": "bed_cooling_reminder",
                                    "temperature": round(actual, 1),
                                    "message": (
                                        "Bed is at 40°C or below — try removing your print. "
                                        "The printer will switch off automatically at 30°C."
                                    ),
                                },
                            )
                            self._diag("info", 
                                "Sent one-time bed-removal reminder at %.1f C",
                                actual,
                            )
                            self._send_ntfy_notification(
                                f" ❄️ Ender 3 Pro bed is at {actual:.1f}°C or below — you may be able to remove your print."
                            )

                    if actual <= 32.0:
                        should_notify_32 = False
                        with self._bed_cooling_monitor_lock:
                            if (
                                generation == self._bed_cooling_monitor_generation
                                and not self._bed_32_notified
                            ):
                                self._bed_32_notified = True
                                should_notify_32 = True

                        if should_notify_32:
                            self._plugin_manager.send_plugin_message(
                                "tapoautoshutdown",
                                {
                                    "type": "bed_cooling_shutdown_warning",
                                    "temperature": round(actual, 1),
                                    "message": (
                                        "The bed is at 32°C. "
                                        "The printer's Tapo plug will switch off "
                                        "automatically when the bed reaches 30°C."
                                    ),
                                },
                            )
                            self._diag("info", 
                                "Sent one-time Tapo shutdown warning at %.1f C",
                                actual,
                            )
                            self._send_ntfy_notification(
                                f"⚠️ Ender 3 Pro bed is at {actual:.1f}°C — the printer's Tapo plug will switch off automatically at 30°C."
                            )

            except Exception as e:
                self._diag("warning", 
                    "Bed-cooling monitor could not read temperature or process state; "
                    "it will retry: %s",
                    e,
                )

            time.sleep(10)

    async def _switch_off_ender_tapo(self):
        """Switch off the Ender plug directly; do not arm a fixed timer."""
        username = (self._settings.get(["username"]) or "").strip()
        password = self._settings.get(["password"])
        ip = (self._settings.get(["printer_tapo_ip"]) or "").strip()

        if not username or not password or not ip:
            self._diag("error", 
                "Cannot switch off Ender Tapo: credentials or IP are missing"
            )
            return False

        try:
            client = ApiClient(username, password)
            plug = await client.p110(ip)
            await plug.off()
            self._send_ntfy_notification(
                " 🔌Ender 3 Pro Tapo plug switched off after bed cooling."
            )
            return True
        except Exception as e:
            self._diag("error", "Failed to switch off Ender Tapo plug: %s", e)
            return False

    # Run the OctoPi shutdown sequence in the background
    def _octopi_shutdown_sequence(self):
        timer_armed = asyncio.run(
            self._arm_octopi_tapo_timer()
        )

        if timer_armed:
            self._shutdown_octopi()
        else:
            self._diag("error", 
                "OctoPi shutdown cancelled because the Tapo timer could not be armed"
            )
    
    # Cancel the independent hardware timer if OctoPrint rejects the shutdown request.
    async def _clear_octopi_tapo_timer(self):
        username = (self._settings.get(["username"]) or "").strip()
        password = self._settings.get(["password"])
        ip = (self._settings.get(["octopi_tapo_ip"]) or "").strip()

        if not username or not password or not ip:
            self._diag("error", 
                "Cannot cancel OctoPi Tapo timer after shutdown failure: "
                "Tapo credentials or IP are missing"
            )
            return False

        try:
            client = ApiClient(username, password)
            plug = await client.p110(ip)
            await plug.clear_timer()
            self._diag("warning", 
                "OctoPi Tapo shutdown timer cancelled after shutdown-request failure; "
                "Raspberry Pi power will remain on"
            )
            return True
        except Exception as e:
            self._diag("error", 
                "Failed to cancel OctoPi Tapo timer after shutdown failure: %s",
                e,
            )
            return False

    # Request a clean shutdown of OctoPi.
    # Important: this can catch a failure to initiate the shutdown request, but
    # cannot observe a later OS shutdown failure after OctoPrint has stopped.
    def _shutdown_octopi(self):
        try:
            self._diag("info", 
                "Requesting clean OctoPi shutdown; five-minute Tapo timer is armed"
            )
            system_command_manager().perform_system_shutdown()
            self._diag("info", 
                "OctoPrint shutdown request submitted; Tapo timer remains armed"
            )
            return True
        except Exception as e:
            self._diag("exception", 
                "OctoPrint shutdown request failed; cancelling OctoPi Tapo timer"
            )
            try:
                cancelled = asyncio.run(self._clear_octopi_tapo_timer())
                if not cancelled:
                    self._diag("critical", 
                        "Shutdown request failed and the OctoPi Tapo timer could not be "
                        "confirmed cancelled; check the Tapo app immediately"
                    )
            except Exception:
                self._diag("exception", 
                    "Unexpected error while cancelling the OctoPi Tapo timer"
                )
            return False
    
    def is_template_autoescaped(self):
        return True

    def get_assets(self):
        return {
            "js": ["js/tapoautoshutdown.js"]
        }
         
    def get_template_configs(self):
        return [
            {
                "type": "settings",
                "name": "Tapo Auto Shutdown",
                "template": "tapoautoshutdown_settings.jinja2",
                "custom_bindings": False,
            }
        ]

    def on_settings_initialized(self):
        """Migrate legacy printer IP settings and populate final-octet fields."""

        # Preserve settings from older versions.
        if not self._settings.get(["printer_tapo_ip"]):
            old_ip = self._settings.get(["ip"])
            if old_ip:
                self._settings.set(["printer_tapo_ip"], old_ip)

        if not self._settings.get(["printer_tapo_ip_last_octet"]):
            old_octet = self._settings.get(["ip_last_octet"])
            if old_octet:
                self._settings.set(["printer_tapo_ip_last_octet"], old_octet)

        for ip_key, octet_key in (
            ("printer_tapo_ip", "printer_tapo_ip_last_octet"),
            ("octopi_tapo_ip", "octopi_tapo_ip_last_octet"),
        ):
            current_octet = (self._settings.get([octet_key]) or "").strip()
            if current_octet:
                continue

            full_ip = (self._settings.get([ip_key]) or "").strip()
            prefix = "192.168.1."

            if full_ip.startswith(prefix):
                tail = full_ip[len(prefix):].strip()
                if tail.isdigit() and 1 <= int(tail) <= 254:
                    self._settings.set([octet_key], str(int(tail)))
                    continue

            if not full_ip:
                self._settings.set([octet_key], "100")

    def on_settings_save(self, data):
        """Preserve existing settings and safely update submitted values."""

        # Start with all existing settings, including defaults.
        existing = self._settings.get_all_data(merged=True) or {}
        merged_data = dict(existing)

        # Apply only values actually submitted by the settings page.
        merged_data.update(data or {})

        # Clean text fields without losing existing values.
        if "username" in merged_data:
            merged_data["username"] = str(
                merged_data.get("username") or ""
            ).strip()

        if "ntfy_topic" in merged_data:
            merged_data["ntfy_topic"] = str(
                merged_data.get("ntfy_topic") or ""
            ).strip()

        # Validate IP addresses only when their octet field was submitted.
        for ip_key, octet_key in (
            ("printer_tapo_ip", "printer_tapo_ip_last_octet"),
            ("octopi_tapo_ip", "octopi_tapo_ip_last_octet"),
        ):
            if octet_key not in (data or {}):
                continue

            raw = str(merged_data.get(octet_key) or "").strip()

            if raw.isdigit() and 1 <= int(raw) <= 254:
                octet = str(int(raw))
                merged_data[octet_key] = octet
                merged_data[ip_key] = "192.168.1." + octet
            else:
                merged_data[octet_key] = ""
                merged_data[ip_key] = ""

        # Save the complete merged settings dictionary.
        octoprint.plugin.SettingsPlugin.on_settings_save(
            self, merged_data
       )

    def get_settings_defaults(self):
        return {
            # Tapo Account
            "username": "",
            "password": "",
            
            # Printer Tapo P110
            "printer_tapo_ip": "",
            "printer_tapo_ip_last_octet": "",

            # Bed Preheat 
            "bed_preheat_enabled": False,

            # OctoPi Tapo P110
            "octopi_tapo_enabled": False,
            "octopi_tapo_ip": "",
            "octopi_tapo_ip_last_octet": "",

            # OctoPi shutdown
            "octopi_shutdown_enabled": False,
            "octopi_shutdown_delay": 5,

            # ntfy push notifications
            # Preserve the currently configured topic when upgrading.
            "ntfy_topic": "",

            # Obico AI Monitoring Delay
            "obico_monitor_delay": 30,
        }
        
    def get_settings_version(self):
        return 1
        
    def get_update_information(self):
        return {
            "tapoautoshutdown": {
                "displayName": "Tapo Auto Shutdown",
                "displayVersion": __plugin_version__,
                "type": "github_release",
                "user": "SpideRaY",
                "repo": "OctoPrint-TapoAutoShutdown-ObicoDelay",
                "current": __plugin_version__,
                "pip": "https://github.com/SpideRaY/OctoPrint-TapoAutoShutdown-ObicoDelay/archive/{target}.zip",
            }
        }
        
__plugin_name__ = "Tapo Auto Shutdown"
__plugin_identifier__ = "tapoautoshutdown"
__plugin_pythoncompat__ = ">=3.11,<4"

def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = TapoAutoShutdownPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
    }

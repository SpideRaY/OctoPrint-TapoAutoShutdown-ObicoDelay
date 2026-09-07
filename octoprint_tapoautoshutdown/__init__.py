import asyncio
import threading
import time

import octoprint.plugin
from tapo import ApiClient


class TapoAutoShutdownPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
):

    def __init__(self):
        self._obico_timer = None
        self._obico_timer_lock = threading.Lock()

    def on_after_startup(self):
        self._logger.info("Tapo Auto Shutdown started")

        def check_for_obico():
            time.sleep(5)

            try:
                obico = self._plugin_manager.get_plugin("obico", False)

                if obico is not None:
                    self._logger.info(
                        "Obico plugin detected - ready for integration"
                    )
                else:
                    self._logger.warning(
                        "Obico plugin not detected after startup"
                    )

            except Exception as e:
                self._logger.error(
                    "Could not query Obico plugin: %s",
                    e,
                )

        threading.Thread(
            target=check_for_obico,
            daemon=True,
        ).start()

    def on_shutdown(self):
        self._cancel_obico_timer()

    def on_event(self, event, payload):

        # Start Obico countdown when a print starts
        if event == "PrintStarted":
            self._start_obico_timer()

        # Cancel the Obico countdown if the print ends
        elif event in (
            "PrintDone",
            "PrintCancelled",
            "PrintFailed",
        ):
            self._cancel_obico_timer()

            # Existing Tapo shutdown behaviour
            if event == "PrintDone":
                self._logger.info(
                    "Print completed - starting shutdown timer"
                )

                threading.Thread(
                    target=self._delayed_shutdown,
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

        self._logger.info(
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
                self._logger.info(
                    "Obico timer expired but no print is active - "
                    "AI monitoring not enabled"
                )
                return

        except Exception as e:
            self._logger.error(
                "Could not determine printer state: %s",
                e,
            )
            return

        self._logger.info(
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
            obico = self._plugin_manager.get_plugin(
                "obico",
                False,
            )

            if obico is None:
                self._logger.warning(
                    "Obico plugin is not installed or enabled - "
                    "AI monitoring cannot be started"
                )
                return

            self._logger.info(
                "Obico plugin detected - ready to enable AI monitoring"
            )

            response = obico.server_request(
                'PATCH',
                '/api/v1/octo/printer/',
                obico,
                headers=obico.auth_headers(),
                json={'watching_enabled': True},
            )

            if response is not None and response.ok:
                self._logger.info(
                    "Obico AI monitoring enabled successfully"
                )
            else:
                self._logger.error(
                    "Failed to enable Obico AI monitoring: HTTP %s",
                    response.status_code if response is not None else "no response",
                )

        except Exception as e:
            self._logger.error(
                "Failed to enable Obico AI monitoring: %s",
                e,
            )

    def _delayed_shutdown(self):

        try:
            delay_minutes = int(
                self._settings.get(["tapo_shutdown_delay"])
            )
        except (TypeError, ValueError):
            delay_minutes = 5

        delay_seconds = delay_minutes * 60

        self._logger.info(
            "Waiting %s minutes before switching off the Tapo P110",
            delay_minutes,
        )

        time.sleep(delay_seconds)

        asyncio.run(self._switch_off())

    async def _switch_off(self):

        username = self._settings.get(["username"])
        password = self._settings.get(["password"])
        ip = self._settings.get(["ip"])

        try:
            client = ApiClient(username, password)
            plug = await client.p110(ip)

            await plug.off()

            self._logger.info(
                "Tapo P110 switched OFF successfully"
            )

        except Exception as e:
            self._logger.error(
                "Failed to switch off Tapo P110: %s",
                e,
            )

    def get_template_configs(self):
        return [
            {
                "type": "settings",
                "name": "Tapo Auto Shutdown",
                "template": "tapoautoshutdown_settings.jinja2",
                "custom_bindings": False,
            }
        ]

    def get_settings_defaults(self):
        return {
            "username": "",
            "password": "",
            "ip": "",
            "tapo_shutdown_delay": 5,
            "obico_monitor_delay": 60,
        }

    def get_settings_version(self):
        return 1


__plugin_name__ = "Tapo Auto Shutdown"
__plugin_pythoncompat__ = ">=3.9,<3.14"
__plugin_implementation__ = TapoAutoShutdownPlugin()

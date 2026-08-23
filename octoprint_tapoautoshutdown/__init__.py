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

    def on_after_startup(self):
        self._logger.info("Tapo Auto Shutdown started")

    def on_event(self, event, payload):
        if event == "PrintDone":
            self._logger.info(
                "Print completed - starting shutdown timer"
            )

            threading.Thread(
                target=self._delayed_shutdown,
                daemon=True,
            ).start()

    def _delayed_shutdown(self):
        delay = int(self._settings.get(["delay"]))

        self._logger.info(
            "Waiting %s seconds before switching off the Tapo P110",
            delay,
        )

        time.sleep(delay)

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
            "obico_monitor_delay": 30,
        }

    def get_settings_version(self):
        return 1


__plugin_name__ = "Tapo Auto Shutdown"
__plugin_pythoncompat__ = ">=3.9,<3.14"
__plugin_implementation__ = TapoAutoShutdownPlugin()

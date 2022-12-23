"""Support for recording details."""
import asyncio
import concurrent.futures
import logging
import queue
import threading
from typing import Any, Callable

import voluptuous as vol

from homeassistant.const import (
    EVENT_HOMEASSISTANT_START,
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
)
from homeassistant.core import CoreState, Event, HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entityfilter import (
    INCLUDE_EXCLUDE_BASE_FILTER_SCHEMA,
    convert_include_exclude_filter,
)
from homeassistant.helpers.typing import ConfigType

from .const import CONF_HOST, CONF_PORT, DOMAIN
from .event_handling import (
    finish_task_if_empty_event,
    get_event_from_queue,
    put_event_to_queue,
)
from .io import insert_event_data_into_questdb

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: INCLUDE_EXCLUDE_BASE_FILTER_SCHEMA.extend(
            {
                vol.Required(CONF_HOST): cv.string,
                vol.Required(CONF_PORT): cv.positive_int,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up qss."""
    conf = config[DOMAIN]

    db_host = conf.get(CONF_HOST)
    db_port = conf.get(CONF_PORT)
    entity_filter = convert_include_exclude_filter(conf)

    instance = QuestDB(
        hass=hass,
        host=db_host,
        port=db_port,
        entity_filter=entity_filter,
    )
    instance.async_initialize()
    instance.start()

    return await instance.qss_ready


class QuestDB(threading.Thread):  # pylint: disable = R0902
    """A threaded qss class."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        entity_filter: Callable[[str], bool],
    ) -> None:
        """Initialize qss."""
        threading.Thread.__init__(self, name="QSS")

        self.hass = hass
        self.host = host
        self.port = port
        self.entity_filter = entity_filter

        self.queue: Any = queue.Queue()
        self.qss_ready = asyncio.Future()

        self.engine: Any = None
        self.run_info: Any = None
        self.get_session = None

    @callback
    def async_initialize(self):
        """Initialize qss."""
        self.hass.bus.async_listen(EVENT_STATE_CHANGED, self.event_listener)

    def run(self):
        """Initialize qss and Insert data."""

        shutdown_task = object()
        hass_started = concurrent.futures.Future()

        @callback
        def register():
            """Register qss to Home Assistant."""
            self.qss_ready.set_result(True)

            def shutdown(event: Event):  # pylint: disable = W0613
                """Shut down the qss."""
                if not hass_started.done():
                    hass_started.set_result(shutdown_task)
                self.queue.put(None)
                self.join()

            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown)

            if self.hass.state == CoreState.running:
                hass_started.set_result(None)
            else:

                @callback
                def notify_hass_started(event: Event):  # pylint: disable = W0613
                    """Notify that hass has started."""
                    hass_started.set_result(None)

                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_START, notify_hass_started
                )

        self.hass.add_job(register)
        result = hass_started.result()

        if result is shutdown_task:
            _LOGGER.info(
                "Shutdown Task initialised: %s",
                result,
            )
            return

        while True:
            event = get_event_from_queue(self.queue)
            finish_task_if_empty_event(event, self.queue)
            insert_event_data_into_questdb(self.host, self.port, event, self.queue)

    @callback
    def event_listener(self, event: Event):
        """Listen for new events and put them in the process queue."""
        put_event_to_queue(event, self.entity_filter, self.queue)

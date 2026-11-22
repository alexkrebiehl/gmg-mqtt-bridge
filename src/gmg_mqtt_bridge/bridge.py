"""Poll a Green Mountain Grill over UDP and mirror its status onto MQTT for HA.

Read-only: the bridge sends the grill nothing but status and serial requests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiomqtt

from . import protocol
from .protocol import Status

log = logging.getLogger("gmg_mqtt_bridge")

# (host, message) -> reply bytes, or None on a timeout.
Transport = Callable[[str, bytes], Awaitable[bytes | None]]


@dataclass(frozen=True)
class Config:
    gmg_host: str
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    base_topic: str = "gmg"
    discovery_prefix: str = "homeassistant"
    device_name: str = "Pellet smoker"
    # While the grill is on or cooling down, every 10 s; while it's off, it
    # only needs to notice being lit.
    poll_active: float = 10
    poll_idle: float = 60
    # One poll is up to this many requests, for lost or short replies.
    attempts: int = 3
    timeout: float = 2
    # This many failed polls in a row and the grill shows as unavailable.
    misses_offline: int = 3

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ
        return cls(
            gmg_host=env["GMG_HOST"],
            mqtt_host=env.get("MQTT_HOST", ""),
            mqtt_port=int(env.get("MQTT_PORT", "1883")),
            mqtt_username=env.get("MQTT_USERNAME") or None,
            mqtt_password=env.get("MQTT_PASSWORD") or None,
            base_topic=env.get("BASE_TOPIC", "gmg"),
            discovery_prefix=env.get("DISCOVERY_PREFIX", "homeassistant"),
            device_name=env.get("DEVICE_NAME", "Pellet smoker"),
            poll_active=float(env.get("POLL_ACTIVE", "10")),
            poll_idle=float(env.get("POLL_IDLE", "60")),
        )


def udp_transport(timeout: float) -> Transport:
    """A fresh socket per request, so a late reply to an earlier request can't
    be taken for the answer to this one."""

    def exchange(host: str, message: bytes) -> bytes | None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(message, (host, protocol.UDP_PORT))
            try:
                data, _ = sock.recvfrom(1024)
            except TimeoutError:
                return None
            return data

    async def send(host: str, message: bytes) -> bytes | None:
        try:
            return await asyncio.to_thread(exchange, host, message)
        except OSError as err:
            log.debug("UDP to %s failed: %s", host, err)
            return None

    return send


class Grill:
    def __init__(self, host: str, transport: Transport, attempts: int = 3) -> None:
        self.host = host
        self.transport = transport
        self.attempts = attempts

    async def _ask(self, message: bytes, parse: Callable[[bytes], Any]) -> Any:
        for _ in range(self.attempts):
            raw = await self.transport(self.host, message)
            if raw is None:
                continue
            parsed = parse(raw)
            if parsed is not None:
                return parsed
            log.debug("Unusable reply to %r (%d bytes): %r", message, len(raw), raw)
        return None

    async def status(self) -> Status | None:
        return await self._ask(protocol.CODE_STATUS, protocol.decode)

    async def serial(self) -> str | None:
        return await self._ask(protocol.CODE_SERIAL, protocol.decode_serial)


class Bridge:
    def __init__(self, config: Config, transport: Transport | None = None) -> None:
        self.cfg = config
        t = config.base_topic
        self.t_bridge = f"{t}/bridge"
        self.t_grill = f"{t}/grill"
        self.t_state = f"{t}/state"
        self.t_ha_status = f"{config.discovery_prefix}/status"
        self.grill = Grill(config.gmg_host, transport or udp_transport(config.timeout), config.attempts)
        self.mqtt: aiomqtt.Client | None = None
        self.serial: str | None = None
        self.online = False
        self.misses = 0
        self.last: Status | None = None
        self._last_payload: str | None = None

    # ------------------------------------------------------------- discovery

    def discovery(self) -> dict[str, Any]:
        assert self.serial
        uid = f"gmg_{self.serial.lower()}"
        avail = {
            "availability": [{"topic": self.t_bridge}, {"topic": self.t_grill}],
            "availability_mode": "all",
        }

        def temp(key: str, name: str) -> dict[str, Any]:
            return {
                "p": "sensor", "name": name, "unique_id": f"{uid}_{key}",
                "default_entity_id": f"sensor.pellet_smoker_{key}",
                "device_class": "temperature", "unit_of_measurement": "°F",
                "state_class": "measurement", "state_topic": self.t_state,
                # null (an empty probe jack, no target set) renders "None",
                # which HA's MQTT sensor reads as unknown.
                "value_template": f"{{{{ value_json.{key} }}}}",
                **avail,
            }

        components = {
            "state": {
                "p": "sensor", "name": "State", "unique_id": f"{uid}_state",
                "default_entity_id": "sensor.pellet_smoker_state",
                "device_class": "enum", "options": protocol.STATES,
                "state_topic": self.t_state, "value_template": "{{ value_json.state }}",
                "json_attributes_topic": self.t_state,
                "json_attributes_template": "{{ {'fire_state': value_json.fire_state, "
                                            "'fire_progress': value_json.fire_progress, "
                                            "'warnings': value_json.warnings} | tojson }}",
                "icon": "mdi:grill",
                **avail,
            },
            "grill": temp("grill", "Grill"),
            "grill_target": temp("grill_target", "Grill target"),
            "probe_1": temp("probe_1", "Probe 1"),
            "probe_1_target": temp("probe_1_target", "Probe 1 target"),
            "probe_2": temp("probe_2", "Probe 2"),
            "probe_2_target": temp("probe_2_target", "Probe 2 target"),
            "warning": {
                "p": "binary_sensor", "name": "Warning", "unique_id": f"{uid}_warning",
                "default_entity_id": "binary_sensor.pellet_smoker_warning",
                "device_class": "problem", "state_topic": self.t_state,
                "value_template": "{{ 'ON' if value_json.warning else 'OFF' }}",
                "json_attributes_topic": self.t_state,
                "json_attributes_template": "{{ {'warnings': value_json.warnings} | tojson }}",
                **avail,
            },
        }
        return {
            "dev": {
                "identifiers": [uid],
                "name": self.cfg.device_name,
                "manufacturer": "Green Mountain Grills",
                "serial_number": self.serial,
            },
            "o": {"name": "gmg-mqtt-bridge"},
            "cmps": components,
            "qos": 1,
        }

    async def publish_discovery(self) -> None:
        if not (self.mqtt and self.serial):
            return
        topic = f"{self.cfg.discovery_prefix}/device/gmg_{self.serial.lower()}/config"
        await self.mqtt.publish(topic, json.dumps(self.discovery()), qos=1, retain=True)

    # ---------------------------------------------------------------- poll

    async def set_online(self, online: bool) -> None:
        if online == self.online:
            return
        self.online = online
        log.info("Grill %s", "answering" if online else "not answering")
        if self.mqtt:
            await self.mqtt.publish(self.t_grill, "online" if online else "offline", qos=1, retain=True)

    async def poll_once(self) -> Status | None:
        """One poll: update availability and state. Returns what the grill said."""
        if self.serial is None:
            self.serial = await self.grill.serial()
            if self.serial is None:
                await self._miss()
                return None
            log.info("Grill serial %s at %s", self.serial, self.cfg.gmg_host)
            await self.publish_discovery()
        status = await self.grill.status()
        if status is None:
            await self._miss()
            return None
        self.misses = 0
        self.last = status
        payload = json.dumps(status.as_json())
        # Publish the state before announcing the grill online, so HA never
        # shows it available with nothing to read.
        if payload != self._last_payload and self.mqtt:
            await self.mqtt.publish(self.t_state, payload, qos=1, retain=True)
            if self._last_payload is None or json.loads(self._last_payload)["state"] != status.state:
                log.info("Grill %s · %s° (set %s°)", status.state, status.grill, status.grill_target)
        self._last_payload = payload
        await self.set_online(True)
        return status

    async def _miss(self) -> None:
        self.misses += 1
        if self.misses >= self.cfg.misses_offline:
            await self.set_online(False)

    def interval(self) -> float:
        # A grill that was on and stops answering is checked at the fast rate,
        # so it goes unavailable quickly.
        return self.cfg.poll_active if self.last and self.last.active else self.cfg.poll_idle

    async def poll_loop(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.interval())

    # ---------------------------------------------------------------- run

    async def listen(self) -> None:
        assert self.mqtt
        async for message in self.mqtt.messages:
            payload = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
            if str(message.topic) == self.t_ha_status and payload == "online":
                # HA restarted: it reads retained discovery, but republishing
                # costs nothing and covers a broker that lost it.
                await self.publish_discovery()

    async def run(self) -> None:
        cfg = self.cfg
        will = aiomqtt.Will(self.t_bridge, "offline", qos=1, retain=True)
        async with aiomqtt.Client(
            hostname=cfg.mqtt_host, port=cfg.mqtt_port,
            username=cfg.mqtt_username, password=cfg.mqtt_password,
            identifier="gmg-mqtt-bridge", will=will,
        ) as mqtt:
            self.mqtt = mqtt
            await mqtt.publish(self.t_grill, "offline", qos=1, retain=True)
            await mqtt.publish(self.t_bridge, "online", qos=1, retain=True)
            await mqtt.subscribe(self.t_ha_status, qos=1)
            log.info("MQTT connected to %s:%s; polling %s", cfg.mqtt_host, cfg.mqtt_port, cfg.gmg_host)
            try:
                # Either task ending (a broker disconnect raises) ends the
                # process; the pod restarts it.
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self.poll_loop())
                    tg.create_task(self.listen())
            finally:
                await mqtt.publish(self.t_grill, "offline", qos=1, retain=True)

"""The Green Mountain Grill local protocol: just enough to read its status.

The grill listens on UDP 8080 and answers short ASCII commands. This module
only knows the two read-only ones: `UL!` (serial number) and `UR001!`
(status). It has no way to send power, temperature or probe commands, on
purpose: nothing behind this bridge can light or change the grill.

Status reply layout (byte offsets; temperatures are °F, two bytes, low first):

    0-1    "UR"                 header
    2-3    grill temperature
    4-5    probe 1 temperature  (an empty jack reads ~601)
    6-7    grill set point
    16-17  probe 2 temperature
    18-19  probe 2 target       (0 = none)
    24-27  warning flags        (32-bit, low byte first)
    28-29  probe 1 target       (0 = none)
    30     power                0 off, 1 on, 2 fan cooldown, 3 cold smoke
    32     fire state           1 off, 2 startup, 3 running, 4 cooldown, 198 cold smoke
    33     fire state progress  (percent)

Older grills (this one, a type-3 controller) send 36 bytes; newer ones send
52. The extra bytes aren't used here.

Reading only the low byte of a temperature, as the original HA integration
did, wraps everything above 255°F (350° reads as 94°). Both bytes are read.
"""

from __future__ import annotations

from dataclasses import dataclass

UDP_PORT = 8080
CODE_SERIAL = b"UL!"
CODE_STATUS = b"UR001!"

STATUS_HEADER = b"UR"
# The highest offset read is 33. Grills do send short datagrams now and then
# (18-31 bytes were seen by others), and ones that lost their first bytes; both
# are rejected and retried rather than read at the wrong offsets.
MIN_STATUS_BYTES = 34

# An empty probe jack reads about 601°F (it varies a little with the grill's
# calibration). Probes are rated to 257°F, so anything above this is no probe.
PROBE_UNPLUGGED_ABOVE = 300

POWER_OFF, POWER_ON, POWER_FAN, POWER_COLD_SMOKE = 0, 1, 2, 3
FIRE_STATES = {0: "default", 1: "off", 2: "startup", 3: "running", 4: "cooldown",
               5: "fail", 198: "cold_smoke"}
WARNINGS = ["fan_overload", "auger_overload", "ignitor_overload", "low_battery",
            "fan_disconnect", "auger_disconnect", "ignitor_disconnect", "low_pellet"]

# The headline state, for dashboards and automations.
STATES = ["off", "starting", "running", "cooldown", "cold_smoke"]


def _u16(raw: bytes, i: int) -> int:
    return raw[i] + (raw[i + 1] << 8)


@dataclass(frozen=True)
class Status:
    power: int
    fire: int
    fire_progress: int
    grill: int
    grill_target: int | None
    probe_1: int | None
    probe_1_target: int | None
    probe_2: int | None
    probe_2_target: int | None
    warning_code: int

    @property
    def fire_state(self) -> str:
        return FIRE_STATES.get(self.fire, f"unknown_{self.fire}")

    @property
    def warnings(self) -> list[str]:
        return [name for bit, name in enumerate(WARNINGS) if self.warning_code & (1 << bit)]

    @property
    def state(self) -> str:
        if self.power == POWER_OFF:
            return "off"
        if self.power == POWER_FAN:
            return "cooldown"
        if self.power == POWER_COLD_SMOKE:
            return "cold_smoke"
        # Power on (or a value nobody has decoded): the fire byte says whether
        # it's still lighting. It goes back to startup whenever the grill
        # drops below temperature and relights.
        if self.fire == 2:
            return "starting"
        if self.fire == 4:
            return "cooldown"
        return "running"

    @property
    def active(self) -> bool:
        return self.state != "off"

    def as_json(self) -> dict:
        return {
            "state": self.state,
            "grill": self.grill,
            "grill_target": self.grill_target,
            "probe_1": self.probe_1,
            "probe_1_target": self.probe_1_target,
            "probe_2": self.probe_2,
            "probe_2_target": self.probe_2_target,
            "fire_state": self.fire_state,
            "fire_progress": self.fire_progress,
            "warnings": self.warnings,
            "warning": bool(self.warnings),
        }


def _probe(raw: bytes, i: int) -> int | None:
    v = _u16(raw, i)
    return None if v > PROBE_UNPLUGGED_ABOVE else v


def _target(raw: bytes, i: int) -> int | None:
    v = _u16(raw, i)
    return v or None


def decode(raw: bytes) -> Status | None:
    """Decode a `UR001!` reply; None for anything that isn't a whole one."""
    if len(raw) < MIN_STATUS_BYTES or raw[:2] != STATUS_HEADER:
        return None
    return Status(
        power=raw[30],
        fire=raw[32],
        fire_progress=raw[33],
        grill=_u16(raw, 2),
        grill_target=_target(raw, 6),
        probe_1=_probe(raw, 4),
        probe_1_target=_target(raw, 28),
        probe_2=_probe(raw, 16),
        probe_2_target=_target(raw, 18),
        warning_code=int.from_bytes(raw[24:28], "little"),
    )


def decode_serial(raw: bytes) -> str | None:
    """`UL!` replies with the serial, e.g. b"GMG00000000"."""
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    return text if text.startswith("GMG") else None

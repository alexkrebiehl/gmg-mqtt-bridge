import asyncio
import logging

import pytest

from gmg_mqtt_bridge import protocol
from gmg_mqtt_bridge.bridge import Bridge, Config

from test_bridge import FakeMqtt
from test_protocol import packet


class Grill:
    """A fake grill that applies the commands it gets, like the real one would."""

    def __init__(self, power=1, fire=3, grill=225, target=225, answers=True, obeys=True):
        self.s = dict(power=power, fire=fire, grill=grill, target=target)
        self.answers = answers
        self.obeys = obeys
        self.commands = []

    async def __call__(self, host, message):
        if message == protocol.CODE_SERIAL:
            return b"GMG00000000" if self.answers else None
        if message == protocol.CODE_STATUS:
            if not self.answers:
                return None
            s = self.s
            return packet(power=s["power"], fire=s["fire"], grill=s["grill"], grill_target=s["target"])
        self.commands.append(message)
        if self.obeys:
            if message == protocol.CODE_POWER_ON:
                self.s.update(power=1, fire=2)
            elif message == protocol.CODE_POWER_OFF:
                self.s.update(power=2, fire=4)
            elif message.startswith(b"UT"):
                self.s["target"] = int(message[2:5])
        return b"OK"


def bridge(grill, allow=True):
    b = Bridge(Config(gmg_host="192.0.2.10", allow_control=allow, readback=(0, 0)), transport=grill)
    b.mqtt = FakeMqtt()
    return b


def cmd(b, topic, payload, retained=False):
    return asyncio.run(b.handle_command(f"gmg/set/{topic}", payload, retained))


# ------------------------------------------------------------------ bytes

def test_command_bytes():
    assert protocol.CODE_POWER_ON == b"UK001!"
    assert protocol.CODE_POWER_OFF == b"UK004!"
    assert protocol.set_temp_command(225) == b"UT225!"
    assert protocol.set_temp_command(150) == b"UT150!"
    assert protocol.set_temp_command(500) == b"UT500!"
    for bad in (149, 501, 0):
        with pytest.raises(ValueError):
            protocol.set_temp_command(bad)


# ------------------------------------------------------------------ sends

def test_set_point_sent_once_and_read_back():
    g = Grill()
    b = bridge(g)
    assert cmd(b, "grill_target", "250") is True
    assert g.commands == [b"UT250!"]
    assert b.last.grill_target == 250


def test_set_point_accepts_hass_float_payload():
    g = Grill()
    assert cmd(bridge(g), "grill_target", "230.0") is True
    assert g.commands == [b"UT230!"]


def test_power_on_from_off():
    g = Grill(power=0, fire=1, grill=60, target=0)
    b = bridge(g)
    assert cmd(b, "power", "ON") is True
    assert g.commands == [b"UK001!"]
    assert b.last.state == "starting"


def test_power_off_while_lit():
    g = Grill()
    b = bridge(g)
    assert cmd(b, "power", "OFF") is True
    assert g.commands == [b"UK004!"]
    assert b.last.state == "cooldown"


def test_a_write_the_grill_ignores_is_not_resent(caplog):
    g = Grill(obeys=False)
    with caplog.at_level(logging.WARNING):
        assert cmd(bridge(g), "grill_target", "250") is True
    assert g.commands == [b"UT250!"]
    assert "hasn't shown" in caplog.text


# --------------------------------------------------------------- refusals

@pytest.mark.parametrize("grill_kw, topic, payload", [
    (dict(), "power", "ON"),                                   # already lit
    (dict(power=2, fire=4), "power", "ON"),                    # cooling down
    (dict(power=0, fire=1), "power", "OFF"),                   # already off
    (dict(power=2, fire=4), "power", "OFF"),                   # cooling down
    (dict(power=0, fire=1), "grill_target", "250"),            # off
    (dict(power=2, fire=4), "grill_target", "250"),            # cooling down
    (dict(power=3, fire=198), "grill_target", "250"),          # cold smoke
    (dict(power=1, fire=2, grill=120), "grill_target", "250"),  # under 145°
    (dict(), "grill_target", "149"),                           # out of range
    (dict(), "grill_target", "501"),
    (dict(), "grill_target", "250.5"),
    (dict(), "grill_target", "hot"),
    (dict(), "power", "TOGGLE"),
    (dict(answers=False), "power", "OFF"),                     # offline
    (dict(answers=False), "grill_target", "250"),
])
def test_refusals_send_nothing(grill_kw, topic, payload, caplog):
    g = Grill(**grill_kw)
    with caplog.at_level(logging.WARNING):
        assert cmd(bridge(g), topic, payload) is False
    assert g.commands == []
    assert "Refused" in caplog.text


def test_unlocks_at_145():
    g = Grill(power=1, fire=2, grill=145, target=225)
    assert cmd(bridge(g), "grill_target", "200") is True
    assert g.commands == [b"UT200!"]


def test_retained_commands_are_ignored():
    g = Grill(power=0, fire=1)
    assert cmd(bridge(g), "power", "ON", retained=True) is False
    assert g.commands == []


# -------------------------------------------------------------- kill switch

def test_control_off_announces_no_controls():
    b = bridge(Grill(), allow=False)
    b.serial = "GMG00000000"
    cmps = b.discovery()["cmps"]
    assert "power" not in cmps and "set_temperature" not in cmps
    assert not any(k.endswith("command_topic") for c in cmps.values() for k in c)


def test_control_on_announces_switch_and_number():
    b = bridge(Grill())
    b.serial = "GMG00000000"
    cmps = b.discovery()["cmps"]
    assert cmps["power"]["default_entity_id"] == "switch.pellet_smoker"
    assert cmps["power"]["command_topic"] == "gmg/set/power"
    n = cmps["set_temperature"]
    assert n["default_entity_id"] == "number.pellet_smoker_set_temperature"
    assert (n["min"], n["max"], n["command_topic"]) == (150, 500, "gmg/set/grill_target")


class Msg:
    def __init__(self, topic, payload, retain=False):
        self.topic, self.payload, self.retain = topic, payload.encode(), retain


class Messages:
    def __init__(self, msgs):
        self.msgs = msgs

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self.msgs:
            yield m


def test_listen_ignores_commands_when_control_is_off():
    g = Grill(power=0, fire=1)
    b = bridge(g, allow=False)
    b.mqtt.messages = Messages([Msg("gmg/set/power", "ON")])
    asyncio.run(b.listen())
    assert g.commands == []


def test_listen_routes_commands_when_control_is_on():
    g = Grill(power=0, fire=1)
    b = bridge(g)
    b.mqtt.messages = Messages([Msg("gmg/set/power", "ON", retain=True), Msg("gmg/set/power", "ON")])
    asyncio.run(b.listen())
    assert g.commands == [b"UK001!"]

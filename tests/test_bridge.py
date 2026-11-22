import asyncio
import json

from gmg_mqtt_bridge import protocol
from gmg_mqtt_bridge.bridge import Bridge, Config

from test_protocol import REAL_ON, packet


class FakeGrill:
    """Answers like the grill; `replies` is a queue of status replies (None = timeout)."""

    def __init__(self, replies=None, serial=b"GMG00000000"):
        self.replies = list(replies or [])
        self.serial = serial
        self.sent = []

    async def __call__(self, host, message):
        self.sent.append(message)
        if message == protocol.CODE_SERIAL:
            return self.serial
        if message == protocol.CODE_STATUS:
            return self.replies.pop(0) if self.replies else None
        raise AssertionError(f"bridge sent {message!r}")


class FakeMqtt:
    def __init__(self):
        self.published = []

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    def last(self, topic):
        return next((p for t, p, _ in reversed(self.published) if t == topic), None)


def bridge(replies, **cfg):
    fake = FakeGrill(replies)
    b = Bridge(Config(gmg_host="192.0.2.10", **cfg), transport=fake)
    b.mqtt = FakeMqtt()
    return b, fake


def run(coro):
    return asyncio.run(coro)


def test_poll_publishes_state_then_online():
    b, _ = bridge([REAL_ON])
    run(b.poll_once())
    topics = [t for t, _, _ in b.mqtt.published]
    assert topics == ["homeassistant/device/gmg_gmg00000000/config", "gmg/state", "gmg/grill"]
    assert json.loads(b.mqtt.last("gmg/state"))["grill"] == 151
    assert b.mqtt.last("gmg/grill") == "online"
    assert all(retain for _, _, retain in b.mqtt.published)


def test_only_reads_are_ever_sent():
    b, fake = bridge([REAL_ON, None, None, None, REAL_ON])
    for _ in range(3):
        run(b.poll_once())
    assert set(fake.sent) <= {protocol.CODE_SERIAL, protocol.CODE_STATUS}


def test_bad_replies_are_retried_within_a_poll():
    b, _ = bridge([REAL_ON[:20], REAL_ON[5:], REAL_ON])
    assert run(b.poll_once()).grill == 151
    assert b.misses == 0


def test_offline_after_three_missed_polls():
    b, _ = bridge([REAL_ON])
    run(b.poll_once())
    for i in range(3):
        run(b.poll_once())
        assert b.mqtt.last("gmg/grill") == ("offline" if i == 2 else "online")
    b.grill.transport.replies.append(REAL_ON)
    run(b.poll_once())
    assert b.mqtt.last("gmg/grill") == "online"


def test_unchanged_state_is_not_republished():
    b, _ = bridge([REAL_ON, REAL_ON, packet(grill=152)])
    for _ in range(3):
        run(b.poll_once())
    assert [t for t, _, _ in b.mqtt.published].count("gmg/state") == 2


def test_poll_rate_follows_the_grill():
    b, _ = bridge([REAL_ON, packet(power=0, fire=1)])
    assert b.interval() == 60
    run(b.poll_once())
    assert b.interval() == 10
    run(b.poll_once())
    assert b.interval() == 60


def test_no_discovery_until_the_serial_is_known():
    b, fake = bridge([])
    fake.serial = None
    run(b.poll_once())
    assert b.mqtt.published == []


def test_discovery():
    b, _ = bridge([])
    b.serial = "GMG00000000"
    d = b.discovery()
    ids = {c["default_entity_id"] for c in d["cmps"].values()}
    assert ids == {
        "sensor.pellet_smoker_state", "sensor.pellet_smoker_grill", "sensor.pellet_smoker_grill_target",
        "sensor.pellet_smoker_probe_1", "sensor.pellet_smoker_probe_1_target",
        "sensor.pellet_smoker_probe_2", "sensor.pellet_smoker_probe_2_target",
        "binary_sensor.pellet_smoker_warning",
    }
    assert d["cmps"]["state"]["options"] == protocol.STATES
    assert d["cmps"]["probe_1"]["unit_of_measurement"] == "°F"
    for c in d["cmps"].values():
        assert c["availability_mode"] == "all"
        assert not any(k.endswith("command_topic") for k in c)

from gmg_mqtt_bridge import protocol

# A real UR001! reply from a type-3 controller (36 bytes),
# 2026-11-22: lit and holding 151° against a 150° set point, both probe jacks
# empty.
REAL_ON = bytes([85, 82, 151, 0, 89, 2, 150, 0, 5, 11, 20, 50, 25, 25, 25, 25, 89, 2, 0, 0,
                 255, 255, 255, 255, 0, 0, 0, 0, 0, 0, 1, 0, 3, 100, 0, 3])


def packet(**fields: int) -> bytes:
    """REAL_ON with fields replaced: name=value for the decoded fields."""
    b = bytearray(REAL_ON)
    u16 = {"grill": 2, "probe_1": 4, "grill_target": 6, "probe_2": 16, "probe_2_target": 18,
           "probe_1_target": 28}
    for k, v in fields.items():
        if k in u16:
            b[u16[k]], b[u16[k] + 1] = v & 0xFF, v >> 8
        elif k == "power":
            b[30] = v
        elif k == "fire":
            b[32] = v
        elif k == "warn":
            b[24:28] = v.to_bytes(4, "little")
        else:
            raise KeyError(k)
    return bytes(b)


def test_real_packet():
    s = protocol.decode(REAL_ON)
    assert s.as_json() == {
        "state": "running", "grill": 151, "grill_target": 150,
        "probe_1": None, "probe_1_target": None, "probe_2": None, "probe_2_target": None,
        "fire_state": "running", "fire_progress": 100, "warnings": [], "warning": False,
    }


def test_temps_above_255_use_both_bytes():
    s = protocol.decode(packet(grill=350, grill_target=375))
    assert (s.grill, s.grill_target) == (350, 375)
    # The bug this guards against: low byte only.
    assert 350 & 0xFF == 94


def test_inserted_probes_and_targets():
    s = protocol.decode(packet(probe_1=162, probe_1_target=203, probe_2=148, probe_2_target=0))
    assert (s.probe_1, s.probe_1_target) == (162, 203)
    assert (s.probe_2, s.probe_2_target) == (148, None)


def test_unplugged_sentinel_varies_with_calibration():
    for v in (584, 593, 601, 628):
        assert protocol.decode(packet(probe_1=v)).probe_1 is None
    assert protocol.decode(packet(probe_1=257)).probe_1 == 257


def test_states():
    assert protocol.decode(packet(power=0, fire=1)).state == "off"
    assert protocol.decode(packet(power=1, fire=2)).state == "starting"
    assert protocol.decode(packet(power=1, fire=3)).state == "running"
    assert protocol.decode(packet(power=2, fire=4)).state == "cooldown"
    assert protocol.decode(packet(power=3, fire=198)).state == "cold_smoke"
    assert not protocol.decode(packet(power=0, fire=1)).active


def test_grill_target_zero_is_none():
    assert protocol.decode(packet(power=0, grill_target=0)).grill_target is None


def test_warnings():
    s = protocol.decode(packet(warn=(1 << 7) | (1 << 1)))
    assert s.warnings == ["auger_overload", "low_pellet"]
    assert s.as_json()["warning"] is True


def test_52_byte_reply():
    assert protocol.decode(REAL_ON + bytes(16)).grill == 151


def test_short_and_headless_replies_are_rejected():
    assert protocol.decode(REAL_ON[:31]) is None
    assert protocol.decode(REAL_ON[6:]) is None
    assert protocol.decode(b"") is None


def test_serial():
    assert protocol.decode_serial(b"GMG00000000") == "GMG00000000"
    assert protocol.decode_serial(REAL_ON) is None
    assert protocol.decode_serial(b"\xff\xfe") is None

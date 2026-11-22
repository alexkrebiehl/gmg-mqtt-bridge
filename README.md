# gmg-mqtt-bridge

Reads a Green Mountain Grill pellet smoker's status over its local UDP protocol
and publishes it to an MQTT broker as Home Assistant entities (MQTT device
discovery).

Built for a type-3 WiFi controller, which answers with a
36-byte status. Newer controllers send 52 bytes; both are read.

## Why

- HA has no built-in GMG integration. The HACS one most people find reads only
  the low byte of each temperature, so anything above 255°F wraps (350° reads
  as 94°).
- A HACS integration has to broadcast to find the grill, so HA must share its
  network. This bridge polls one IP, so it runs anywhere that can reach the
  grill on UDP 8080, such as a cluster on another VLAN.
- **It is read-only.** It sends the grill only `UL!` (serial) and `UR001!`
  (status). It has no code for power, set-temperature or probe commands, so
  nothing behind it can light or change a grill.

## Entities

| Entity | Values |
|---|---|
| `sensor.pellet_smoker_state` | `off` `starting` `running` `cooldown` `cold_smoke`; attributes `fire_state`, `fire_progress`, `warnings` |
| `sensor.pellet_smoker_grill`, `sensor.pellet_smoker_grill_target` | °F; the target is unknown when the grill reports none |
| `sensor.pellet_smoker_probe_1`, `sensor.pellet_smoker_probe_2` | °F; unknown when nothing is plugged into the jack |
| `sensor.pellet_smoker_probe_1_target`, `sensor.pellet_smoker_probe_2_target` | °F; unknown when no target is set |
| `binary_sensor.pellet_smoker_warning` | on when the grill reports any warning; attribute `warnings` (`low_pellet`, `fan_overload`, `auger_disconnect`, ...) |

`starting` is the fire lighting. The grill drops back to it whenever it falls
below temperature and relights. `cooldown` is the fan running on after
power-off.

Every entity is unavailable unless the bridge is connected (MQTT last will) and
the grill answered one of the last three polls. A grill that's switched off but
plugged in still answers and reads `off`, so unavailable means it's unplugged
or unreachable.

## Protocol

UDP port 8080. Temperatures are two bytes, low first, in °F.

| Bytes | Field |
|---|---|
| 0–1 | `UR` header |
| 2–3 | grill temperature |
| 4–5 | probe 1 (an empty jack reads about 601) |
| 6–7 | grill set point |
| 16–17 / 18–19 | probe 2 / its target (0 = none) |
| 24–27 | warning flags, 32-bit |
| 28–29 | probe 1 target (0 = none) |
| 30 | power: 0 off, 1 on, 2 fan cooldown, 3 cold smoke |
| 32 / 33 | fire state (1 off, 2 startup, 3 running, 4 cooldown, 198 cold smoke) / its progress % |

A reply shorter than 34 bytes or without the `UR` header is retried, never
read. Grills do send short and headless datagrams now and then.

The layout was cross-checked against
[cbreger15/gmg_home_assistant](https://github.com/cbreger15/gmg_home_assistant) and
[gmg-local](https://pypi.org/project/gmg-local/), and against this grill.

## Configuration

| Env | |
|---|---|
| `GMG_HOST` | the grill's IP (reserve it in DHCP) |
| `MQTT_HOST`, `MQTT_PORT` (1883), `MQTT_USERNAME`, `MQTT_PASSWORD` | broker |
| `POLL_ACTIVE` (10), `POLL_IDLE` (60) | seconds between polls while the grill is on or cooling / while it's off |
| `BASE_TOPIC` (`gmg`), `DISCOVERY_PREFIX` (`homeassistant`), `DEVICE_NAME` (`Pellet smoker`), `LOG_LEVEL` | |

Topics: `gmg/state` (retained JSON), and `gmg/bridge` and `gmg/grill`
(`online`/`offline`, both retained).

The process exits on a broker disconnect and expects to be restarted. Run
exactly one instance.

## Development

```sh
uv run pytest
GMG_HOST=192.0.2.10 uv run gmg-mqtt-bridge --once   # read the grill once, no MQTT
```

The tests are built around a real status reply captured from the grill.

Images: `ghcr.io/alexkrebiehl/gmg-mqtt-bridge:sha-<commit>`, built by CI on
every push to `main`. Deployed by Flux from `flux-kustomizations-internal`.

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
- **Control is opt-in and guarded.** Without `ALLOW_CONTROL` the bridge only
  reads. With it, the bridge takes power on/off and a grill set point, and
  checks each one against a fresh status before sending it (see Control).
  Probe targets and cold smoke are left out.

## Entities

| Entity | Values |
|---|---|
| `sensor.pellet_smoker_state` | `off` `starting` `running` `cooldown` `cold_smoke`; attributes `fire_state`, `fire_progress`, `warnings` |
| `sensor.pellet_smoker_grill`, `sensor.pellet_smoker_grill_target` | °F; the target is unknown when the grill reports none |
| `sensor.pellet_smoker_probe_1`, `sensor.pellet_smoker_probe_2` | °F; unknown when nothing is plugged into the jack |
| `sensor.pellet_smoker_probe_1_target`, `sensor.pellet_smoker_probe_2_target` | °F; unknown when no target is set |
| `binary_sensor.pellet_smoker_warning` | on when the grill reports any warning; attribute `warnings` (`low_pellet`, `fan_overload`, `auger_disconnect`, ...) |
| `switch.pellet_smoker` | only with `ALLOW_CONTROL`: on while `starting`, `running` or `cold_smoke`; turning it on lights the grill, off shuts it down |
| `number.pellet_smoker_set_temperature` | only with `ALLOW_CONTROL`: the grill set point, 150–500°F |

`starting` is the fire lighting. The grill drops back to it whenever it falls
below temperature and relights. `cooldown` is the fan running on after
power-off.

Every entity is unavailable unless the bridge is connected (MQTT last will) and
the grill answered one of the last three polls. A grill that's switched off but
plugged in still answers and reads `off`, so unavailable means it's unplugged
or unreachable.

## Control

With `ALLOW_CONTROL=true`, the bridge subscribes to `gmg/set/power` (`ON`/`OFF`)
and `gmg/set/grill_target` (whole °F), and announces the switch and number
above. Every command is checked against a status read just before it:

| Command | Sent only when | Bytes |
|---|---|---|
| power on | the grill reads `off` (not lit, not cooling down, answering) | `UK001!` |
| power off | the grill is lit (`starting`, `running`, `cold_smoke`) | `UK004!` |
| set point | the grill is `starting` or `running`, is at least 145°F, and the value is a whole number from 150 to 500 | `UT<nnn>!` |

- A refused command is logged at WARNING and nothing is sent.
- **Retained command messages are ignored**, so a stray retained `ON` on the
  broker can't light the grill when the bridge restarts.
- A command is sent **once** and never retried. The bridge then re-polls at
  1 s and 3 s and publishes what the grill reports; if the change hasn't
  shown by then, it logs a warning.
- The 145°F unlock follows the GMG manual, which says not to change the set
  point until the grill has come up to temperature after lighting.

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

Writes: `UK001!` power on, `UK004!` power off, `UT<nnn>!` grill set point
(three digits, °F).

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
| `ALLOW_CONTROL` (off) | `true` to accept power and set-point commands (see Control) |
| `POLL_ACTIVE` (10), `POLL_IDLE` (60) | seconds between polls while the grill is on or cooling / while it's off |
| `BASE_TOPIC` (`gmg`), `DISCOVERY_PREFIX` (`homeassistant`), `DEVICE_NAME` (`Pellet smoker`), `LOG_LEVEL` | |

Topics: `gmg/state` (retained JSON), and `gmg/bridge` and `gmg/grill`
(`online`/`offline`, both retained). With control: `gmg/set/power`,
`gmg/set/grill_target`.

The process exits on a broker disconnect and expects to be restarted. Run
exactly one instance.

## Development

```sh
uv run pytest
GMG_HOST=192.0.2.10 uv run gmg-mqtt-bridge --once   # read the grill once, no MQTT
```

The tests are built around a real status reply captured from the grill.
Commands are tested against a fake grill only; `--once` never sends one.

Images: `ghcr.io/alexkrebiehl/gmg-mqtt-bridge:sha-<commit>`, built by CI on
every push to `main`. Deployed by Flux from `flux-kustomizations-internal`.

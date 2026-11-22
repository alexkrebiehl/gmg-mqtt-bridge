import argparse
import asyncio
import json
import logging
import os
import sys

from .bridge import Bridge, Config, Grill, udp_transport


async def once(host: str) -> int:
    """Read the grill once and print what the bridge would publish; no MQTT."""
    grill = Grill(host, udp_transport(2))
    serial = await grill.serial()
    status = await grill.status()
    if status is None:
        print(f"no usable status reply from {host}", file=sys.stderr)
        return 1
    print(json.dumps({"serial": serial, **status.as_json()}, indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="gmg-mqtt-bridge")
    parser.add_argument("--once", action="store_true",
                        help="read the grill at $GMG_HOST once, print it, and exit (no MQTT)")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.once:
        sys.exit(asyncio.run(once(os.environ["GMG_HOST"])))
    asyncio.run(Bridge(Config.from_env()).run())


if __name__ == "__main__":
    main()

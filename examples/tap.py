"""Show a short message in response to a tap on the reference firmware."""
import asyncio

from deskpet_platform.client import Client
from deskpet_platform.pairing import DEFAULT_CONFIG, DeviceConfig


async def main():
    async with Client(DeviceConfig.load(DEFAULT_CONFIG)) as board:
        print("Tap the reference firmware screen within 30 seconds.")
        event = await board.next_touch(timeout=30)
        print("Received:", event["action"], event["operation"])
        print(await board.show_text("Tap received"))


if __name__ == "__main__":
    asyncio.run(main())

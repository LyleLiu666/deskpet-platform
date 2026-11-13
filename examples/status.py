"""Run while no MCP instance owns the paired board."""
import asyncio

from deskpet_platform.client import Client
from deskpet_platform.mcp_server import status_view
from deskpet_platform.pairing import DEFAULT_CONFIG, DeviceConfig


async def main():
    async with Client(DeviceConfig.load(DEFAULT_CONFIG)) as client:
        print(status_view(await client.status(), client.channel.capabilities))


if __name__ == "__main__":
    asyncio.run(main())

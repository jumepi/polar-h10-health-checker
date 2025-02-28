import asyncio

from async_timeout import timeout
from bleak import BleakClient

POLAR_H10_ADDRESS = "7A8C6159-C50B-0651-3075-5411D72CA0E9"

async def main():
    print("=== Connecting to Polar H10 by address... ===")
    async with BleakClient(POLAR_H10_ADDRESS, timeout=30.0) as client:
        print("+++ Connected +++")
        services = await client.get_services()
        for s in services:
            print(s)
    print("+++ Done +++")

asyncio.run(main())

import asyncio
from bleak import BleakScanner

async def scan_polar_h10():
    print("Scanning for BLE devices...")
    devices = await BleakScanner.discover()
    for device in devices:
        # デバイス名に "Polar H10" が含まれているかチェック
        if device.name and "Polar H10" in device.name:
            print("Found Polar H10!")
            print("Address:", device.address)

if __name__ == "__main__":
    asyncio.run(scan_polar_h10())

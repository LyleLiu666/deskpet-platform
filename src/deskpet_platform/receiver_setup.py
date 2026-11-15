"""Pair a separate notification receiver without changing interactive defaults."""
import asyncio
from dataclasses import replace
from pathlib import Path

from .client import Client
from .device_registry import DeviceRegistry, selection_id
from .pairing import DeviceConfig
from .usb_link import UsbPort
from .usb_setup import probe_usb


async def add_usb_receiver(shared_config, port, expected_identity, credentials_file=None):
    shared_config=Path(shared_config).expanduser().resolve()
    DeviceConfig.load(shared_config)
    registry=DeviceRegistry(shared_config)
    if any(DeviceConfig.load(path).identity==expected_identity for path in registry.notification_paths()):
        raise ValueError('小板已经登记，请勿重复添加。')
    registration=UsbPort.register(port);registration.verify()
    config=await asyncio.to_thread(probe_usb,port,expected_identity,credentials_file)
    registration.verify()
    if not config.address:
        raise ValueError('小板尚未连接 Wi-Fi，请先在设备上联网。')
    async with Client(config) as board:
        await board.status()
        if board.device_id is not None:config=replace(config,device_id=board.device_id)
    path=registry.add_receiver(config)
    return {'device':config.identity,'id':selection_id(config),'config':str(path)}

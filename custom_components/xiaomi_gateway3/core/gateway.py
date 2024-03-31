import asyncio

from . import core_utils
from .const import GATEWAY, ZIGBEE, MESH, GROUP, MATTER
from .device import XDevice
from .gate.base import EVENT_MQTT_PUBLISH, EVENT_TIMER
from .gate.ble import BLEGateway
from .gate.lumi import LumiGateway
from .gate.matter import MatterGateway
from .gate.mesh import MeshGateway
from .gate.miot import MIoTGateway
from .gate.openmiio import OpenMiioGateway
from .gate.silabs import SilabsGateway
from .shell.session import Session


class MultiGateway(
    BLEGateway,
    LumiGateway,
    MatterGateway,
    MeshGateway,
    MIoTGateway,
    OpenMiioGateway,
    SilabsGateway,
):
    main_task: asyncio.Task | None = None

    def start(self):
        if self.main_task:
            return
        self.debug("start")
        self.main_task = asyncio.create_task(self.run_forever())

    async def stop(self):
        if not self.main_task:
            return

        self.debug("stop")
        # wait main task for finished: mqtt disconnect and gateway available false
        # updated for all devices
        while not self.main_task.cancelled():
            self.main_task.cancel()
            await asyncio.sleep(0.1)
        self.main_task = None

    async def run_forever(self):
        while True:
            # check if telnet port OK
            if not await core_utils.check_port(self.host, 23):
                if not await self.enable_telnet():
                    await asyncio.sleep(30)
                    continue

            if not await self.prepare_gateway():
                await asyncio.sleep(60)
                continue

            await self.handle_mqtt_messages()

    async def enable_telnet(self) -> bool:
        """Enable telnet with miio protocol."""
        if not (token := self.options.get("token")):
            return False
        try:
            resp = await core_utils.enable_telnet(
                self.host, token, self.options.get("key")
            )
            self.debug("enable_telnet", data=resp)
            return resp == "ok"
        except Exception as e:
            self.debug("enable_telnet", exc_info=e)
            return False

    async def prepare_gateway(self) -> bool:
        try:
            async with Session(self.host) as sh:
                if not await sh.only_one():
                    self.debug("Connection from a second Hass detected")
                    return False

                info = await sh.get_miio_info()
                info["version"] = await sh.get_version()

                if (
                    info["model"] == "lumi.gateway.mgl03"
                    and info["version"] < "1.4.7_0160"
                ):
                    self.error(f"Unsupported firmware: {info}")
                    return False

                # base and zigbee
                await self.base_read_device(info)
                await self.lumi_read_devices(sh)
                await self.silabs_read_device(sh)
                await self.openmiio_prepare_gateway(sh)

                # ble and mesh
                if hasattr(sh, "read_db_bluetooth"):
                    await self.ble_read_devices(sh)
                    await self.mesh_read_devices(sh)

                if info["model"] == "lumi.gateway.mgl001":
                    await self.matter_read_devices(sh)

            self.add_event_listner(EVENT_MQTT_PUBLISH, self.lumi_on_mqtt_publish)
            self.add_event_listner(EVENT_MQTT_PUBLISH, self.miot_on_mqtt_publish)
            self.add_event_listner(EVENT_MQTT_PUBLISH, self.openmiio_on_mqtt_publish)
            self.add_event_listner(EVENT_MQTT_PUBLISH, self.silabs_on_mqtt_publish)

            if hasattr(sh, "read_db_bluetooth"):
                self.add_event_listner(EVENT_MQTT_PUBLISH, self.ble_on_mqtt_publish)
                self.add_event_listner(EVENT_MQTT_PUBLISH, self.mesh_on_mqtt_publish)

            if info["model"] == "lumi.gateway.mgl001":
                self.add_event_listner(EVENT_MQTT_PUBLISH, self.matter_on_mqtt_publish)

            self.add_event_listner(EVENT_TIMER, self.openmiio_on_timer)
            self.add_event_listner(EVENT_TIMER, self.silabs_on_timer)

            return True
        except Exception as e:
            self.debug("Can't prepare gateway", exc_info=e)
            return False

    async def send(self, device: XDevice, data: dict):
        if device.type == GATEWAY:
            # support multispec in lumi and miot formats
            if "cmd" in data:
                lumi_data = (
                    {
                        "cmd": data["cmd"],
                        "did": "lumi.0",
                        "params": [i for i in data["params"] if "res_name" in i],
                    }
                    if "method" in data
                    else data
                )
                await self.lumi_send(device, lumi_data)

            if "method" in data:
                miot_data = (
                    {
                        "method": data["method"],
                        "params": [i for i in data["params"] if "siid" in i],
                    }
                    if "cmd" in data
                    else data
                )
                await self.miot_send(device, miot_data)

        elif device.type == ZIGBEE:
            # support multispec in lumi and silabs format
            if "cmd" in data:
                lumi_data = (
                    {"cmd": data["cmd"], "did": data["did"], "params": data["params"]}
                    if "commands" in data
                    else data
                )
                await self.lumi_send(device, lumi_data)

            if "commands" in data:
                silabs_data = {"commands": data["commands"]} if "cmd" in data else data
                await self.silabs_send(device, silabs_data)

        elif device.type in (MESH, GROUP):
            await self.miot_send(device, data)
        elif device.type == MATTER:
            await self.matter_send(device, data)

    async def telnet_command(self, cmd: str) -> bool | None:
        self.debug("telnet_command", data=cmd)
        try:
            async with Session(self.host) as sh:
                if cmd == "run_ftp":
                    await sh.run_ftp()
                    return True
                elif cmd == "reboot":
                    await sh.reboot()
                    return True
                elif cmd == "openmiio_restart":
                    await sh.exec("killall openmiio_agent")
                    await asyncio.sleep(1)
                    await self.openmiio_prepare_gateway(sh)
                    return True
                elif cmd == "check_firmware_lock":
                    return await sh.check_firmware_lock()
                elif cmd == "lock_firmware":
                    await sh.lock_firmware(True)
                    return await sh.check_firmware_lock() is True
                elif cmd == "unlock_firmware":
                    await sh.lock_firmware(False)
                    return await sh.check_firmware_lock() is False

        except Exception as e:
            self.error(f"Can't run telnet command: {cmd}", exc_info=e)

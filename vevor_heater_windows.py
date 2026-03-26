import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from enum import IntEnum
from datetime import datetime
from typing import Optional

from bleak import BleakClient, BleakScanner
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table


class HeaterMode(IntEnum):
    LEVEL = 0x01
    AUTOMATIC = 0x02


class HeaterPower(IntEnum):
    OFF = 0x00
    ON = 0x01


class HeaterCommand(IntEnum):
    STATUS = 0x01
    MODE = 0x02
    POWER = 0x03
    LEVEL_OR_TEMP = 0x04


@dataclass
class HeaterStatus:
    power: bool
    mode: HeaterMode
    target_temperature_level: int
    level: int
    running_state: int
    altitude: int
    voltage_battery: float
    temp_heating: int
    temp_room: int
    error_code: int


class VEVORHeaterWindows:
    HEADER = bytearray([0xAA, 0x55])
    SERVICE_UUID = "0000FFE0-0000-1000-8000-00805F9B34FB"
    CHAR_UUID = "0000FFE1-0000-1000-8000-00805F9B34FB"

    def __init__(self, device_identifier: str):
        self.device_identifier = device_identifier.strip()
        self.client: Optional[BleakClient] = None
        self.console = Console()
        self.logger = logging.getLogger("VEVORHeaterWindows")
        self.last_update = 0.0
        self._notify_ready = asyncio.Event()
        self._response_data: Optional[bytes] = None
        self._stop_polling = asyncio.Event()

    async def resolve_device(self):
        """
        Find device by exact MAC address or by name fragment.
        """
        self.console.print("[yellow]Scanning for BLE devices...[/yellow]")

        devices = await BleakScanner.discover(timeout=8.0)

        # Exact address match first
        for d in devices:
            if (d.address or "").lower() == self.device_identifier.lower():
                return d

        # Name contains match second
        needle = self.device_identifier.lower()
        for d in devices:
            name = (d.name or "").lower()
            if needle and needle in name:
                return d

        self.console.print("[red]Found devices:[/red]")
        for d in devices:
            self.console.print(f"  {d.address}   {d.name}")

        raise RuntimeError(
            f"Could not find device '{self.device_identifier}'. "
            "Use the heater MAC address or part of its BLE name."
        )

    async def connect(self):
        device = await self.resolve_device()
        self.client = BleakClient(device)

        await self.client.connect()
        if not self.client.is_connected:
            raise ConnectionError("BLE connection failed")

        await self.client.start_notify(self.CHAR_UUID, self.notification_handler)
        self.logger.info("Connected to heater")
        self.console.print(f"[green]Connected:[/green] {device.address} {device.name}")

    async def disconnect(self):
        if self.client:
            try:
                if self.client.is_connected:
                    try:
                        await self.client.stop_notify(self.CHAR_UUID)
                    except Exception:
                        pass
                    await self.client.disconnect()
            finally:
                self.client = None
                self.console.print("[green]Disconnected[/green]")

    def calculate_checksum(self, data: bytearray) -> int:
        return sum(data[2:]) % 256

    def create_command(self, command_type: int, value: int) -> bytearray:
        data = bytearray([
            *self.HEADER,
            0x0C,          # packet length
            0x22,          # packet type
            command_type,
            value,
            0x00,          # reserved
            0x00,          # checksum placeholder
        ])
        data[-1] = self.calculate_checksum(data)
        return data

    async def send_command(self, command: bytearray, wait_for_reply: float = 1.0):
        if not self.client or not self.client.is_connected:
            raise ConnectionError("Device not connected")

        self._response_data = None
        self._notify_ready.clear()

        await self.client.write_gatt_char(self.CHAR_UUID, command, response=True)

        try:
            await asyncio.wait_for(self._notify_ready.wait(), timeout=wait_for_reply)
        except asyncio.TimeoutError:
            return None

        return self._response_data

    def notification_handler(self, sender: int, data: bytearray):
        try:
            raw = bytes(data)
            self._response_data = raw
            self._notify_ready.set()

            if len(raw) >= 18 and raw[0] == 0xAA:
                status = HeaterStatus(
                    power=bool(raw[3]),
                    mode=HeaterMode.AUTOMATIC if raw[8] == 2 else HeaterMode.LEVEL,
                    target_temperature_level=raw[9],
                    level=raw[10],
                    running_state=raw[5],
                    altitude=raw[6] | (raw[7] << 8),
                    voltage_battery=(raw[11] | (raw[12] << 8)) / 10.0,
                    temp_heating=raw[13] | (raw[14] << 8),
                    temp_room=raw[15] | (raw[16] << 8),
                    error_code=raw[17],
                )
                self.display_status(status)

        except Exception as e:
            self.console.print(f"[red]Notification error:[/red] {e}")

    def display_status(self, status: HeaterStatus):
        now = asyncio.get_event_loop().time()
        if now - self.last_update < 0.5:
            return
        self.last_update = now

        running_state_map = {
            0x00: "Warmup",
            0x01: "Self test running",
            0x02: "Ignition",
            0x03: "Heating",
            0x04: "Shutting down",
        }
        running_state_description = running_state_map.get(status.running_state, "Unknown state")

        os.system("cls" if os.name == "nt" else "clear")

        layout = Layout()
        layout.split_column(
            Layout(name="status"),
            Layout(name="commands"),
        )

        status_table = Table(show_header=False, box=None)
        status_table.add_row(
            f"[bold blue]VEVOR Heater Control - {datetime.now().strftime('%H:%M:%S')}[/bold blue]"
        )
        status_table.add_row("Power", "[green]ON[/green]" if status.power else "[red]OFF[/red]")
        status_table.add_row(
            "Mode",
            "[yellow]AUTOMATIC[/yellow]" if status.mode == HeaterMode.AUTOMATIC else "[yellow]LEVEL[/yellow]",
        )
        status_table.add_row("Running State", f"[green]{running_state_description}[/green]")
        status_table.add_row("Room Temperature", f"[cyan]{status.temp_room}°C[/cyan]")
        status_table.add_row(
            "Target",
            f"[green]{status.target_temperature_level}{'°C' if status.mode == HeaterMode.AUTOMATIC else ' (level)'}[/green]",
        )
        status_table.add_row("Power Level", f"[white]{status.level}[/white]")
        status_table.add_row("Heating Temperature", f"[red]{status.temp_heating}°C[/red]")
        status_table.add_row("Battery", f"[magenta]{status.voltage_battery:.1f}V[/magenta]")
        status_table.add_row("Altitude", f"[white]{status.altitude} m[/white]")
        status_table.add_row("Error Code", f"[white]{status.error_code}[/white]")

        commands = (
            "[bold green]Available Commands:[/bold green]\n"
            "[white]P0[/white]  - Turn heater OFF\n"
            "[white]P1[/white]  - Turn heater ON\n"
            "[white]T8-T36[/white] - Set temperature (8-36°C)\n"
            "[white]L1-L10[/white] - Set power level (1-10)\n"
            "[white]status[/white] - Request fresh status\n"
            "[white]exit[/white] - Exit program\n\n"
            "Enter command:"
        )

        layout["status"].update(Panel(status_table, title="Status"))
        layout["commands"].update(Panel(commands, title="Commands"))
        self.console.print(layout)

    async def request_status(self):
        cmd = bytearray([0xAA, 0x55, 0x0C, 0x22, 0x01, 0x00, 0x00, 0x2F])
        return await self.send_command(cmd)

    async def set_mode(self, mode: HeaterMode):
        cmd = self.create_command(HeaterCommand.MODE, int(mode))
        await self.send_command(cmd)

    async def set_power(self, power: bool):
        value = HeaterPower.ON if power else HeaterPower.OFF
        cmd = self.create_command(HeaterCommand.POWER, int(value))
        await self.send_command(cmd)

    async def set_level(self, level: int):
        if not 1 <= level <= 10:
            raise ValueError("Level must be between 1 and 10")
        await self.set_mode(HeaterMode.LEVEL)
        cmd = self.create_command(HeaterCommand.LEVEL_OR_TEMP, level)
        await self.send_command(cmd)

    async def set_temperature(self, temp: int):
        if not 8 <= temp <= 36:
            raise ValueError("Temperature must be between 8°C and 36°C")
        await self.set_mode(HeaterMode.AUTOMATIC)
        cmd = self.create_command(HeaterCommand.LEVEL_OR_TEMP, temp)
        await self.send_command(cmd)

    async def polling_loop(self):
        while not self._stop_polling.is_set():
            try:
                await self.request_status()
            except Exception as e:
                self.console.print(f"[red]Polling error:[/red] {e}")
            await asyncio.sleep(1.0)

    async def command_loop(self):
        while True:
            cmd = await asyncio.to_thread(input, "> ")
            cmd = cmd.strip().lower()

            if cmd == "exit":
                self.console.print("[yellow]Shutting down...[/yellow]")
                self._stop_polling.set()
                break

            try:
                if cmd == "status":
                    await self.request_status()
                elif cmd in ("p0", "p1"):
                    await self.set_power(cmd == "p1")
                    self.console.print(f"[green]Power {'ON' if cmd == 'p1' else 'OFF'}[/green]")
                elif cmd.startswith("t"):
                    temp = int(cmd[1:])
                    await self.set_temperature(temp)
                    self.console.print(f"[green]Temperature set to {temp}°C[/green]")
                elif cmd.startswith("l"):
                    level = int(cmd[1:])
                    await self.set_level(level)
                    self.console.print(f"[green]Power level set to {level}[/green]")
                else:
                    self.console.print("[red]Invalid command[/red]")
            except ValueError as e:
                self.console.print(f"[red]{e}[/red]")
            except Exception as e:
                self.console.print(f"[red]Command error:[/red] {e}")


async def async_main():
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python vevor_heater_windows.py <BLE_MAC_or_name>")
        print("")
        print("Examples:")
        print("  python vevor_heater_windows.py 21:47:08:1A:B1:1E")
        print('  python vevor_heater_windows.py "AirHeaterBLE"')
        sys.exit(1)

    device_identifier = sys.argv[1]
    heater = VEVORHeaterWindows(device_identifier)

    try:
        await heater.connect()

        polling_task = asyncio.create_task(heater.polling_loop())
        await heater.command_loop()
        await polling_task

    except KeyboardInterrupt:
        heater.console.print("[yellow]Interrupted[/yellow]")
    finally:
        heater._stop_polling.set()
        await heater.disconnect()


if __name__ == "__main__":
    asyncio.run(async_main())

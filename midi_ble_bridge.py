#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import shutil
import subprocess
from pathlib import Path
import platform
import sys
from typing import Any

import mido

BLEAK_IMPORT_ERROR: Exception | None = None
try:
    from bleak import BleakClient, BleakScanner
except Exception as exc:  # pragma: no cover - depends on local environment
    BleakClient = None
    BleakScanner = None
    BLEAK_IMPORT_ERROR = exc

DBUS_IMPORT_ERROR: Exception | None = None
try:
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus
except Exception as exc:  # pragma: no cover - depends on local environment
    BusType = None
    MessageBus = None
    DBUS_IMPORT_ERROR = exc

BLE_DEVICE_IMPORT_ERROR: Exception | None = None
try:
    from bleak.backends.device import BLEDevice
except Exception as exc:  # pragma: no cover - depends on local environment
    BLEDevice = None
    BLE_DEVICE_IMPORT_ERROR = exc


CONFIG_PATH = Path.home() / ".config" / "midi-ble-bridge.json"
BLE_MIDI_SERVICE_UUID = "03b80e5a-ede8-4b33-a751-6ce34ec4c700"
BLE_MIDI_CHARACTERISTIC_UUID = "7772e5db-3868-4112-a1a9-f2669d106bf3"

DEFAULT_CONFIG = {
    "device_name": "",
    "device_address": "",
    "virtual_port_name": "Pad Magic BLE",
    "client_name": "Pad Magic Bridge",
    "service_uuid": BLE_MIDI_SERVICE_UUID,
    "characteristic_uuid": BLE_MIDI_CHARACTERISTIC_UUID,
    "scan_timeout": 8.0,
    "connect_timeout": 12.0,
    "reconnect_delay": 2.0,
    "verbose": False,
}


@dataclass(frozen=True)
class TargetDevice:
    address: str
    name: str
    handle: Any | None = None


def ensure_bleak_available() -> None:
    if BLEAK_IMPORT_ERROR is None:
        return
    raise SystemExit(
        "The 'bleak' package is required for Bluetooth MIDI bridging.\n"
        "Install it with: python3 -m pip install --user bleak"
    ) from BLEAK_IMPORT_ERROR


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"Bridge config not found: {CONFIG_PATH}\n"
            "Create it with:\n"
            "  python3 midi_ble_bridge.py init --device-name SMC-PAD"
        )

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        loaded = json.load(f)

    config = DEFAULT_CONFIG.copy()
    config.update(loaded)
    config["scan_timeout"] = float(config["scan_timeout"])
    config["connect_timeout"] = float(config.get("connect_timeout", DEFAULT_CONFIG["connect_timeout"]))
    config["reconnect_delay"] = float(config["reconnect_delay"])
    config["verbose"] = bool(config.get("verbose", False))
    config["service_uuid"] = str(config["service_uuid"]).lower()
    config["characteristic_uuid"] = str(config["characteristic_uuid"]).lower()

    if not config["device_name"] and not config["device_address"]:
        raise SystemExit(
            f"Bridge config must set device_name or device_address: {CONFIG_PATH}"
        )

    return config


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def bluez_available() -> bool:
    return (
        platform.system() == "Linux"
        and MessageBus is not None
        and BLEDevice is not None
    )


def create_virtual_output(config: dict[str, Any]):
    backend = mido.Backend("mido.backends.rtmidi")
    try:
        return backend.open_output(
            config["virtual_port_name"],
            virtual=True,
            client_name=config["client_name"],
        )
    except TypeError:
        return backend.open_output(config["virtual_port_name"], virtual=True)


def print_message(prefix: str, message: str) -> None:
    print(f"[{prefix}] {message}", flush=True)


def target_label(config: dict[str, Any]) -> str:
    if config.get("device_name"):
        return str(config["device_name"])
    return str(config.get("device_address", ""))


class BleMidiDecoder:
    def __init__(self) -> None:
        self._parser = mido.Parser()

    def decode(self, packet: bytes) -> list[mido.Message]:
        if len(packet) < 3:
            return []

        payload = bytearray(packet[2:])
        if not payload:
            return []

        cleaned = bytearray()
        for index, value in enumerate(payload):
            next_value = payload[index + 1] if index + 1 < len(payload) else None
            if (
                value >= 0x80
                and next_value is not None
                and next_value >= 0x80
                and next_value < 0xF8
            ):
                # BLE MIDI interleaves timestamp bytes before status bytes.
                continue
            cleaned.append(value)

        for value in cleaned:
            self._parser.feed_byte(value)

        messages: list[mido.Message] = []
        while True:
            message = self._parser.get_message()
            if message is None:
                break
            messages.append(message)
        return messages


async def get_bluez_devices() -> list[Any]:
    if not bluez_available():
        return []

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        introspection = await bus.introspect("org.bluez", "/")
        obj = bus.get_proxy_object("org.bluez", "/", introspection)
        manager = obj.get_interface("org.freedesktop.DBus.ObjectManager")
        managed = await manager.call_get_managed_objects()
    finally:
        bus.disconnect()

    devices: list[Any] = []
    for path, interfaces in managed.items():
        raw_props = interfaces.get("org.bluez.Device1")
        if raw_props is None:
            continue

        props = {key: value.value for key, value in raw_props.items()}
        alias = props.get("Alias") or props.get("Name") or props.get("Address")
        details = {
            "path": path,
            "props": props,
        }
        devices.append(BLEDevice(str(props["Address"]), alias, details))

    return devices


async def scan_devices(timeout: float) -> int:
    ensure_bleak_available()

    rows_by_address: dict[str, tuple[str, str]] = {}

    try:
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except TypeError:
        devices = await BleakScanner.discover(timeout=timeout)
        for device in devices:
            rows_by_address[str(device.address)] = (
                device.name or "(unnamed)",
                "device",
            )
    else:
        for _, (device, adv) in sorted(discovered.items()):
            service_uuids = {uuid.lower() for uuid in adv.service_uuids or []}
            rows_by_address[str(device.address)] = (
                device.name or adv.local_name or "(unnamed)",
                "BLE-MIDI" if BLE_MIDI_SERVICE_UUID in service_uuids else "device",
            )

    for device in await get_bluez_devices():
        props = device.details.get("props", {})
        service_uuids = {uuid.lower() for uuid in props.get("UUIDs", [])}
        label = "connected" if props.get("Connected") else "paired"
        if BLE_MIDI_SERVICE_UUID in service_uuids:
            label = f"{label}+BLE-MIDI"
        rows_by_address[str(device.address)] = (
            device.name or "(unnamed)",
            label,
        )

    if not rows_by_address:
        print("No BLE devices found.")
        return 0

    for address, (name, marker) in sorted(rows_by_address.items()):
        print(f"{address:20} {name:30} {marker}")
    return 0


def target_from_device(device: Any) -> TargetDevice:
    return TargetDevice(
        address=str(device.address),
        name=device.name or str(device.address),
        handle=device,
    )


def target_from_scan(device: Any, name: str) -> TargetDevice:
    return TargetDevice(
        address=str(device.address),
        name=name or str(device.address),
        handle=device,
    )


async def scan_live_devices(timeout: float) -> list[TargetDevice]:
    ensure_bleak_available()

    try:
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except TypeError:
        devices = await BleakScanner.discover(timeout=timeout)
        return [target_from_device(device) for device in devices]

    rows: list[TargetDevice] = []
    for _, (device, adv) in discovered.items():
        name = device.name or adv.local_name or str(device.address)
        rows.append(target_from_scan(device, name))
    return rows


async def find_target_device(config: dict[str, Any]) -> TargetDevice | None:
    ensure_bleak_available()

    target_name = str(config["device_name"]).strip()
    target_address = ""
    if not target_name:
        target_address = str(config["device_address"]).strip().lower()
    exact_matches: list[TargetDevice] = []
    partial_matches: list[TargetDevice] = []

    def consider(devices: list[Any], require_connected_name_match: bool = False) -> TargetDevice | None:
        exact_matches.clear()
        partial_matches.clear()

        for device in devices:
            address = str(device.address).lower()
            name = device.name or ""
            props = getattr(device, "details", {}).get("props", {})
            is_connected = bool(props.get("Connected"))
            if target_address and address == target_address:
                exact_matches.append(target_from_device(device))
                continue
            if require_connected_name_match and not is_connected:
                continue
            if target_name and name == target_name:
                exact_matches.append(target_from_device(device))
                continue
            if target_name and target_name.lower() in name.lower():
                partial_matches.append(target_from_device(device))

        if exact_matches:
            return exact_matches[0]
        if len(partial_matches) == 1:
            return partial_matches[0]
        return None

    # For name-based reconnects, only trust live scan results. Generic discover()
    # on BlueZ can hand back cached devices that are no longer advertising,
    # which leads to long connection timeouts after a power cycle.
    scanned_devices = await scan_live_devices(config["scan_timeout"])
    for device in scanned_devices:
        address = device.address.lower()
        name = device.name or ""
        if target_address and address == target_address:
            return device
        if target_name and name == target_name:
            return device
        if target_name and target_name.lower() in name.lower():
            partial_matches.append(device)

    if len(partial_matches) == 1:
        return partial_matches[0]

    bluez_devices = await get_bluez_devices()
    return consider(bluez_devices, require_connected_name_match=bool(target_name))


def refresh_stored_target(config: dict[str, Any], target: TargetDevice) -> None:
    updated = False

    if config.get("device_name"):
        # In name-based mode, keep the address blank so reconnects do not get
        # pulled back to a stale BLE address after a power cycle.
        if str(config.get("device_name", "")).strip() != target.name:
            config["device_name"] = target.name
            updated = True
        if str(config.get("device_address", "")).strip():
            config["device_address"] = ""
            updated = True
    else:
        if str(config.get("device_address", "")).strip() != target.address:
            config["device_address"] = target.address
            updated = True
        config["device_name"] = target.name

    if not updated:
        return

    save_config(config)
    if config.get("device_name") and not config.get("device_address"):
        print_message("ready", f"Using name-based reconnects for {target.name}")
    else:
        print_message("ready", f"Remembering {target.name} at {target.address}")


class MidiBleBridge:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.decoder = BleMidiDecoder()
        self.output = create_virtual_output(config)

    def handle_notification(self, _: Any, data: bytearray) -> None:
        messages = self.decoder.decode(bytes(data))
        if self.config["verbose"] and messages:
            print_message("midi", ", ".join(str(message) for message in messages))
        for message in messages:
            self.output.send(message)

    async def run_forever(self) -> None:
        reconnect_delay = self.config["reconnect_delay"]

        while True:
            target = await find_target_device(self.config)
            if target is None:
                print_message("wait", f"Could not find BLE device '{target_label(self.config)}'. Retrying.")
                await asyncio.sleep(reconnect_delay)
                continue

            refresh_stored_target(self.config, target)
            disconnected = asyncio.Event()
            loop = asyncio.get_running_loop()
            notify_started = False
            client = None

            def on_disconnect(_: Any) -> None:
                loop.call_soon_threadsafe(disconnected.set)

            try:
                client_target = target.handle if target.handle is not None else target.address
                async with BleakClient(
                    client_target,
                    disconnected_callback=on_disconnect,
                    timeout=self.config["connect_timeout"],
                ) as client:
                    print_message("ready", f"Connected to {target.name}")
                    await asyncio.sleep(0.5)
                    await client.start_notify(
                        self.config["characteristic_uuid"],
                        self.handle_notification,
                    )
                    notify_started = True
                    print_message(
                        "ready",
                        f"Publishing MIDI on virtual port '{self.config['virtual_port_name']}'",
                    )
                    await disconnected.wait()
                    print_message("wait", "Bluetooth device disconnected. Reconnecting.")
            except Exception as exc:
                print_message("wait", f"Bridge connection failed: {exc}")
            finally:
                if notify_started and client is not None and client.is_connected:
                    try:
                        await client.stop_notify(self.config["characteristic_uuid"])
                    except Exception:
                        pass

            wait_seconds = reconnect_delay
            if "NotPermitted" in str(exc) if 'exc' in locals() else False:
                wait_seconds = max(wait_seconds, 5.0)
            if "In Progress" in str(exc) if 'exc' in locals() else False:
                wait_seconds = max(wait_seconds, 5.0)
            await asyncio.sleep(wait_seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BLE MIDI bridge for Pad Magic.")
    subparsers = parser.add_subparsers(dest="command")

    scan_parser = subparsers.add_parser("scan", help="List nearby BLE devices.")
    scan_parser.add_argument("--timeout", type=float, default=8.0)

    init_parser = subparsers.add_parser("init", help="Write bridge config.")
    init_parser.add_argument("--device-name", default="")
    init_parser.add_argument("--device-address", default="")
    init_parser.add_argument("--port-name", default=DEFAULT_CONFIG["virtual_port_name"])
    init_parser.add_argument("--client-name", default=DEFAULT_CONFIG["client_name"])
    init_parser.add_argument("--verbose", action="store_true")

    subparsers.add_parser("run", help="Run the BLE MIDI bridge.")

    install_parser = subparsers.add_parser(
        "install",
        help="Copy runtime files into ~/.local/lib/pad-magic and (re)install systemd user units.",
    )
    install_parser.add_argument(
        "--prefix",
        default=str(Path.home() / ".local" / "lib" / "pad-magic"),
        help="Directory to copy runtime files into.",
    )
    install_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Skip daemon-reload and service (re)start.",
    )

    return parser


def command_init(args: argparse.Namespace) -> int:
    if not args.device_name and not args.device_address:
        raise SystemExit("init requires --device-name or --device-address")

    config = DEFAULT_CONFIG.copy()
    config.update(
        {
            "device_name": args.device_name.strip(),
            "device_address": args.device_address.strip(),
            "virtual_port_name": args.port_name.strip() or DEFAULT_CONFIG["virtual_port_name"],
            "client_name": args.client_name.strip() or DEFAULT_CONFIG["client_name"],
            "verbose": bool(args.verbose),
        }
    )
    save_config(config)
    print(f"Saved bridge config to: {CONFIG_PATH}")
    return 0


RUNTIME_FILES = (
    "midi_ble_bridge.py",
    "midi_execute.py",
    "midi_configure.py",
    "midi_triggers_common.py",
    "kitty-slot",
    "raise-or-launch",
)

GNOME_EXTENSION_DIR = "gnome-shell/pad-magic-window-activator@pad-magic"
GNOME_EXTENSION_UUID = "pad-magic-window-activator@pad-magic"

SERVICE_UNITS = {
    "midi-ble-bridge.service": """[Unit]
Description=Pad Magic BLE MIDI bridge

[Service]
Type=simple
WorkingDirectory={prefix}
ExecStart=/usr/bin/python3 {prefix}/midi_ble_bridge.py run
Restart=always
RestartSec=2
Environment=PYTHONUNBUFFERED=1
Environment=MIDO_BACKEND=mido.backends.rtmidi

[Install]
WantedBy=default.target
""",
    "midi-execute.service": """[Unit]
Description=MIDI trigger executor

[Service]
Type=simple
WorkingDirectory={prefix}
ExecStart=/usr/bin/python3 {prefix}/midi_execute.py
Restart=always
RestartSec=2
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
""",
    "kitty-midi-backend.service": """[Unit]
Description=Pad Magic kitty backend
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
ExecStartPre=/usr/bin/rm -f %t/kitty-midi.sock
ExecStart=/usr/bin/kitty -o allow_remote_control=yes --listen-on unix:%t/kitty-midi.sock --start-as=minimized --override remember_window_size=no --override initial_window_width=84c --override initial_window_height=25c --override confirm_os_window_close=0
Restart=always
RestartSec=2

[Install]
WantedBy=graphical-session.target
""",
}


def _systemctl_user(*args: str) -> int:
    result = subprocess.run(
        ["systemctl", "--user", *args],
        check=False,
    )
    return result.returncode


def command_install(args: argparse.Namespace) -> int:
    source_dir = Path(__file__).resolve().parent
    prefix = Path(args.prefix).expanduser().resolve()
    prefix.mkdir(parents=True, exist_ok=True)

    if prefix == source_dir:
        print(f"Source directory equals install prefix ({prefix}); skipping copy.")
    else:
        for name in RUNTIME_FILES:
            src = source_dir / name
            if not src.exists():
                print(f"warning: missing source file {src}, skipping")
                continue
            dst = prefix / name
            shutil.copy2(src, dst)
            if src.stat().st_mode & 0o111:
                dst.chmod(0o755)
            else:
                dst.chmod(0o644)
            print(f"installed {dst}")

    extension_src = source_dir / GNOME_EXTENSION_DIR
    if extension_src.is_dir():
        extension_dst = (
            Path.home()
            / ".local"
            / "share"
            / "gnome-shell"
            / "extensions"
            / GNOME_EXTENSION_UUID
        )
        extension_dst.mkdir(parents=True, exist_ok=True)
        for entry in extension_src.iterdir():
            if entry.is_file():
                target = extension_dst / entry.name
                shutil.copy2(entry, target)
                target.chmod(0o644)
                print(f"installed {target}")

    units_dir = Path.home() / ".config" / "systemd" / "user"
    units_dir.mkdir(parents=True, exist_ok=True)
    for unit_name, template in SERVICE_UNITS.items():
        unit_path = units_dir / unit_name
        unit_path.write_text(template.format(prefix=prefix))
        unit_path.chmod(0o644)
        print(f"wrote {unit_path}")

    if args.no_restart:
        print("Skipping daemon-reload and service restart (per --no-restart).")
        return 0

    if not shutil.which("systemctl"):
        print("systemctl not found; skipping daemon-reload and restart.")
        return 0

    _systemctl_user("daemon-reload")
    units_to_enable = [
        name for name in SERVICE_UNITS if not name.startswith("kitty-midi-backend")
    ]
    units_to_enable.append("kitty-midi-backend.service")
    _systemctl_user("enable", *units_to_enable)
    _systemctl_user("reset-failed", *units_to_enable)
    _systemctl_user("restart", *units_to_enable)

    print("\nInstall complete.")
    print(f"Runtime files: {prefix}")
    print(f"Unit files:    {units_dir}")
    return 0


def command_run() -> int:
    config = load_config()
    bridge = MidiBleBridge(config)
    try:
        asyncio.run(bridge.run_forever())
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    command = args.command or "run"

    if command == "scan":
        return asyncio.run(scan_devices(args.timeout))
    if command == "init":
        return command_init(args)
    if command == "install":
        return command_install(args)
    return command_run()


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
import sys
import mido

from midi_triggers_common import (
    ActivePadState,
    NormalizedEvent,
    canonicalize_port_name,
    default_cooldown_for_kind,
    describe_trigger,
    normalize_message,
    resolve_input_port_name,
)


CONFIG_PATH = Path.home() / ".config" / "midi-triggers.json"
RETRY_DELAY_SECONDS = 2.0
CONFIG_POLL_SECONDS = 1.0
GUI_ENV_REFRESH_SECONDS = 5.0
GUI_ENV_KEYS = (
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_CURRENT_DESKTOP",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_DESKTOP",
    "XDG_SESSION_TYPE",
)
GUI_ENV_REQUIRED_FOR_WINDOWS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
)

_cached_gui_env: dict[str, str] | None = None
_cached_gui_env_checked_at = 0.0
_last_gui_env_summary: str | None = None


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"Config file not found: {CONFIG_PATH}\n"
            f"Run ./midi_configure.py first."
        )

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = json.load(f)

    if "bindings" not in config:
        raise SystemExit(
            f"Config file uses the old broad-trigger format: {CONFIG_PATH}\n"
            f"Run ./midi_configure.py to create per-control bindings."
        )

    return config


def config_mtime_ns() -> int | None:
    try:
        return CONFIG_PATH.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def available_input_names() -> list[str]:
    try:
        return list(mido.get_input_names())
    except Exception:
        return []


def resolve_input_port_name_or_none(
    configured_name: str,
    available_names: list[str] | None = None,
) -> str | None:
    try:
        return resolve_input_port_name(configured_name, available_names)
    except LookupError:
        return None


def print_config_summary(config: dict) -> None:
    port_name = config["port"]
    bindings = config["bindings"]

    print(f"Listening on: {port_name}")
    print("Configured bindings:")
    if not bindings:
        print("  (none)")
        return

    for trigger_id in sorted(bindings):
        binding = bindings[trigger_id]
        label = describe_trigger(trigger_id, binding["kind"])
        print(f"  {label:20} [{binding['kind']}] -> {binding['command'] or '(not set)'}")


def read_systemd_user_environment() -> dict[str, str]:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return {}

    if result.returncode != 0:
        return {}

    env: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        if key in GUI_ENV_KEYS and value:
            env[key] = value

    return env


def launch_session_environment() -> dict[str, str]:
    global _cached_gui_env
    global _cached_gui_env_checked_at
    global _last_gui_env_summary

    now = time.monotonic()
    if _cached_gui_env is not None and now - _cached_gui_env_checked_at < GUI_ENV_REFRESH_SECONDS:
        return _cached_gui_env

    process_env = {
        key: value
        for key, value in os.environ.items()
        if key in GUI_ENV_KEYS and value
    }
    manager_env = read_systemd_user_environment()

    resolved_env = process_env.copy()
    resolved_env.update(manager_env)

    refreshed_keys = [
        key
        for key, value in manager_env.items()
        if process_env.get(key) != value
    ]
    missing_required = [key for key in GUI_ENV_REQUIRED_FOR_WINDOWS if not resolved_env.get(key)]

    summary_parts: list[str] = []
    if refreshed_keys:
        summary_parts.append(f"refreshed from systemd user env: {', '.join(refreshed_keys)}")
    if missing_required:
        summary_parts.append(f"still missing graphical vars: {', '.join(missing_required)}")

    summary = "; ".join(summary_parts)
    if summary and summary != _last_gui_env_summary:
        prefix = "[warn]" if missing_required else "[env]"
        print(f"{prefix} Command launch environment {summary}")
        _last_gui_env_summary = summary

    _cached_gui_env = resolved_env
    _cached_gui_env_checked_at = now
    return resolved_env


def run_command(command: str, event: NormalizedEvent) -> None:
    if not command:
        print(f"[skip] No command configured for {event.id}")
        return

    label = describe_trigger(event.id, event.kind)
    print(f"[run] {label} [{event.kind}] -> {command}")
    print(f"      raw: {event.raw}")

    env = {
        "MIDI_TRIGGER_KIND": event.kind,
        "MIDI_TRIGGER_ID": event.id,
        "MIDI_TRIGGER_NOTE": "" if event.note is None else str(event.note),
        "MIDI_TRIGGER_CONTROL": "" if event.control is None else str(event.control),
        "MIDI_TRIGGER_RAW": event.raw,
    }
    full_env = os.environ.copy()
    full_env.update(launch_session_environment())
    full_env.update(env)

    subprocess.Popen(
        command,
        shell=True,
        env=full_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def main() -> None:
    last_run: dict[str, float] = {}
    state = ActivePadState()
    loaded_config: dict | None = None
    loaded_mtime = config_mtime_ns()

    try:
        while True:
            if loaded_config is None:
                loaded_config = load_config()
                loaded_mtime = config_mtime_ns()
                print_config_summary(loaded_config)

            configured_port_name = loaded_config["port"]
            bindings = loaded_config["bindings"]
            cooldowns = loaded_config.get("cooldowns", {})

            if not configured_port_name:
                print("No MIDI port configured. Run ./midi_configure.py to choose one.")
                time.sleep(RETRY_DELAY_SECONDS)
                loaded_config = None
                continue

            try:
                port_name = resolve_input_port_name(configured_port_name)
                with mido.open_input(port_name) as port:
                    if canonicalize_port_name(port_name) == canonicalize_port_name(configured_port_name):
                        print(f"[ready] Connected to {port_name}")
                    else:
                        print(
                            f"[ready] Connected to {port_name}"
                            f" (configured as {configured_port_name})"
                        )
                    last_run.clear()
                    state = ActivePadState()
                    last_config_check = 0.0

                    while True:
                        now = time.monotonic()
                        if now - last_config_check >= CONFIG_POLL_SECONDS:
                            last_config_check = now
                            available_names = available_input_names()
                            resolved_port_name = resolve_input_port_name_or_none(
                                configured_port_name,
                                available_names,
                            )
                            if resolved_port_name is None:
                                print(
                                    f"[wait] MIDI input '{configured_port_name}' is no longer available; "
                                    "waiting for it to return."
                                )
                                loaded_config = None
                                break
                            if resolved_port_name != port_name:
                                print(
                                    f"[wait] MIDI input moved from {port_name} to {resolved_port_name}; "
                                    "reconnecting."
                                )
                                loaded_config = None
                                break

                            latest_mtime = config_mtime_ns()
                            if latest_mtime != loaded_mtime:
                                try:
                                    reloaded = load_config()
                                except Exception as exc:
                                    print(f"[warn] Config changed but could not be reloaded: {exc}")
                                else:
                                    loaded_mtime = latest_mtime
                                    if reloaded != loaded_config:
                                        print("[reload] Loaded updated config.")
                                        if canonicalize_port_name(reloaded["port"]) != canonicalize_port_name(
                                            configured_port_name
                                        ):
                                            print(
                                                f"[reload] Port changed from {configured_port_name} to "
                                                f"{reloaded['port']}; reconnecting."
                                            )
                                            loaded_config = reloaded
                                            print_config_summary(loaded_config)
                                            break

                                        loaded_config = reloaded
                                        bindings = loaded_config["bindings"]
                                        cooldowns = loaded_config.get("cooldowns", {})
                                        print_config_summary(loaded_config)

                        msg = port.poll()
                        if msg is None:
                            time.sleep(0.01)
                            continue

                        event = normalize_message(msg, state)
                        if event is None:
                            continue

                        binding = bindings.get(event.id)
                        if binding is None:
                            continue

                        cooldown = float(cooldowns.get(event.id, default_cooldown_for_kind(event.kind)))
                        elapsed = now - last_run.get(event.id, 0.0)

                        if elapsed < cooldown:
                            continue

                        last_run[event.id] = now
                        run_command(binding.get("command", ""), event)
            except Exception as exc:
                ports = available_input_names()
                print(f"[wait] Could not open MIDI input '{configured_port_name}': {exc}")
                if ports:
                    print(f"[wait] Available inputs: {', '.join(ports)}")
                else:
                    print("[wait] No MIDI inputs are currently available.")
                time.sleep(RETRY_DELAY_SECONDS)
                loaded_config = None

    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()

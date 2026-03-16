#!/usr/bin/env python3

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
import mido

from midi_triggers_common import (
    ActivePadState,
    choose_port_interactively,
    default_cooldown_for_kind,
    describe_event,
    describe_trigger,
    normalize_message,
)


CONFIG_PATH = Path.home() / ".config" / "midi-triggers.json"
SERVICE_NAME = "midi-execute.service"


def empty_config(port: str = "") -> dict:
    return {
        "port": port,
        "cooldowns": {},
        "bindings": {},
    }


def normalize_config(config: dict) -> tuple[dict, bool]:
    if "bindings" in config:
        return {
            "port": config.get("port", ""),
            "cooldowns": {
                str(trigger_id): float(value)
                for trigger_id, value in config.get("cooldowns", {}).items()
            },
            "bindings": {
                str(trigger_id): {
                    "kind": binding.get("kind", ""),
                    "command": binding.get("command", ""),
                }
                for trigger_id, binding in config.get("bindings", {}).items()
            },
        }, False

    return empty_config(port=config.get("port", "")), "commands" in config


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            config, migrated = normalize_config(json.load(f))
        if migrated:
            print("Existing config used the old broad-trigger format.")
            print("Starting from the same port with empty per-control bindings.")
        return config

    return empty_config()


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)


def restart_execute_service() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", SERVICE_NAME],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False, "systemctl not found; saved config without restarting the service."

    if result.returncode == 0:
        return True, f"Restarted {SERVICE_NAME}."

    details = (result.stderr or result.stdout).strip()
    if details:
        return False, f"Saved config, but could not restart {SERVICE_NAME}: {details}"
    return False, f"Saved config, but could not restart {SERVICE_NAME}."


def sorted_binding_ids(config: dict) -> list[str]:
    return sorted(
        config["bindings"],
        key=lambda trigger_id: (
            config["bindings"][trigger_id].get("kind", ""),
            describe_trigger(trigger_id, config["bindings"][trigger_id].get("kind", "")),
            trigger_id,
        ),
    )


def print_bindings(config: dict) -> None:
    bindings = sorted_binding_ids(config)
    if not bindings:
        print("  (none)")
        return

    for trigger_id in bindings:
        binding = config["bindings"][trigger_id]
        kind = binding["kind"]
        label = describe_trigger(trigger_id, kind)
        cooldown = float(config["cooldowns"].get(trigger_id, default_cooldown_for_kind(kind)))
        command = binding.get("command") or "(not set)"
        print(
            f"  {label} [{kind}] id={trigger_id} cooldown={cooldown:.2f}s command={command}"
        )


def recommended_one_shot_cooldown(kind: str) -> float:
    return {
        "pad_hit": 1.0,
        "button_pressed": 1.0,
        "knob_moved": 0.8,
        "pad_pressured": 0.8,
    }.get(kind, 1.0)


def choose_cooldown_for_binding(kind: str, existing: float | None = None) -> float:
    repeat_ready = default_cooldown_for_kind(kind)
    one_shot = recommended_one_shot_cooldown(kind)

    print("\nHow should this action behave if you trigger it again right away?")
    print(f"  1. Let it repeat freely ({repeat_ready:.2f}s cooldown)")
    print(f"  2. Treat it like a one-shot action ({one_shot:.2f}s cooldown)")
    print("  3. Enter a custom cooldown")
    if existing is not None:
        print(f"Press Enter to keep the current cooldown ({existing:.2f}s)")

    while True:
        choice = input("Cooldown choice: ").strip().lower()

        if choice == "" and existing is not None:
            return existing
        if choice == "1":
            return repeat_ready
        if choice == "2":
            return one_shot
        if choice == "3":
            raw = input("Custom cooldown in seconds: ").strip()
            try:
                return float(raw)
            except ValueError:
                print("Invalid number.")
                continue

        print("Choose 1, 2, or 3.")


def select_binding_id(config: dict, prompt: str) -> str | None:
    bindings = sorted_binding_ids(config)
    if not bindings:
        print("No bindings configured yet.")
        return None

    print()
    for idx, trigger_id in enumerate(bindings, start=1):
        binding = config["bindings"][trigger_id]
        label = describe_trigger(trigger_id, binding["kind"])
        command = binding.get("command") or "(not set)"
        print(f"  {idx}. {label} [{binding['kind']}] ({trigger_id}) - {command}")

    raw = input(prompt).strip()
    try:
        idx = int(raw)
    except ValueError:
        print("Invalid selection.")
        return None

    if not 1 <= idx <= len(bindings):
        print("Invalid selection.")
        return None

    return bindings[idx - 1]


def select_binding_ids(config: dict, prompt: str) -> list[str] | None:
    bindings = sorted_binding_ids(config)
    if not bindings:
        print("No bindings configured yet.")
        return None

    print()
    for idx, trigger_id in enumerate(bindings, start=1):
        binding = config["bindings"][trigger_id]
        label = describe_trigger(trigger_id, binding["kind"])
        command = binding.get("command") or "(not set)"
        print(f"  {idx}. {label} [{binding['kind']}] ({trigger_id}) - {command}")

    raw = input(prompt).strip()
    if not raw:
        print("Invalid selection.")
        return None

    selected_ids: list[str] = []
    seen_ids: set[str] = set()
    for part in raw.split(","):
        item = part.strip()
        try:
            idx = int(item)
        except ValueError:
            print("Invalid selection.")
            return None

        if not 1 <= idx <= len(bindings):
            print("Invalid selection.")
            return None

        trigger_id = bindings[idx - 1]
        if trigger_id not in seen_ids:
            selected_ids.append(trigger_id)
            seen_ids.add(trigger_id)

    return selected_ids


def capture_one_trigger(port_name: str) -> tuple[str, str]:
    print("\nWaiting for one supported gesture...")
    print("Do one of these:")
    print("- move a knob")
    print("- hit a pad")
    print("- press and add pressure to a held pad")
    print("- press a button")
    print()

    state = ActivePadState()
    with mido.open_input(port_name) as port:
        pending_pad_hit = None
        pending_deadline = 0.0

        while True:
            msg = port.poll()
            if msg is None:
                if pending_pad_hit is not None and time.monotonic() >= pending_deadline:
                    print(f"Detected: {describe_event(pending_pad_hit)}")
                    print(f"Raw MIDI:  {pending_pad_hit.raw}")
                    return pending_pad_hit.id, pending_pad_hit.kind
                time.sleep(0.01)
                continue

            if pending_pad_hit is not None:
                is_same_note_release = (
                    msg.type in {"note_off", "note_on"}
                    and getattr(msg, "note", None) == pending_pad_hit.note
                    and (msg.type == "note_off" or getattr(msg, "velocity", 0) == 0)
                )
                if is_same_note_release:
                    print(f"Detected: {describe_event(pending_pad_hit)}")
                    print(f"Raw MIDI:  {pending_pad_hit.raw}")
                    return pending_pad_hit.id, pending_pad_hit.kind

            event = normalize_message(msg, state)
            if event is None:
                continue

            if pending_pad_hit is not None and event.kind == "pad_pressured":
                print(f"Detected: {describe_event(event)}")
                print(f"Raw MIDI:  {event.raw}")
                return event.id, event.kind

            if pending_pad_hit is None and event.kind == "pad_hit":
                pending_pad_hit = event
                pending_deadline = time.monotonic() + 0.45
                continue

            print(f"Detected: {describe_event(event)}")
            print(f"Raw MIDI:  {event.raw}")
            return event.id, event.kind

    raise RuntimeError("Input stream ended unexpectedly.")


def main() -> None:
    config = load_config()

    if config.get("port"):
        print(f"Current MIDI port: {config['port']}")
    else:
        config["port"] = choose_port_interactively()
        print(f"Current MIDI port: {config['port']}")

    while True:
        print(f"\nMIDI port in use: {config['port']}")
        print("\nCurrent bindings:")
        print_bindings(config)

        print("\nOptions:")
        print("  1. Learn new binding")
        print("  2. List bindings")
        print("  3. Edit command for existing binding")
        print("  4. Remove binding")
        print("  5. Edit cooldown for existing binding")
        print("  6. Change MIDI port")
        print("  7. Save and quit")
        print("  8. Quit without saving")

        choice = input("Select: ").strip()

        if choice == "1":
            trigger_id, kind = capture_one_trigger(config["port"])
            label = describe_trigger(trigger_id, kind)
            existing = config["bindings"].get(trigger_id)
            print(f"\nEnter the shell command to run for: {label} [{trigger_id}]")
            print("Examples:")
            print("  notify-send 'MIDI' 'Pad hit'")
            print("  gnome-terminal")
            print("  xdg-open https://chatgpt.com")
            print("  playerctl play-pause")
            if existing and existing.get("command"):
                print(f"Current command: {existing['command']}")
            print()
            cmd = input("Command: ").strip()
            existing_cooldown = None
            if existing is not None:
                existing_cooldown = float(
                    config["cooldowns"].get(
                        trigger_id,
                        default_cooldown_for_kind(existing.get("kind", kind)),
                    )
                )
            cooldown = choose_cooldown_for_binding(kind, existing=existing_cooldown)
            config["bindings"][trigger_id] = {
                "kind": kind,
                "command": cmd,
            }
            config["cooldowns"][trigger_id] = cooldown
            print(f"Saved in memory: {label} -> {cmd}")
            print(f"Cooldown set to {cooldown:.2f}s")

        elif choice == "2":
            print("\nCurrent bindings:")
            print_bindings(config)

        elif choice == "3":
            trigger_id = select_binding_id(config, "Which binding number? ")
            if trigger_id is None:
                continue

            binding = config["bindings"][trigger_id]
            label = describe_trigger(trigger_id, binding["kind"])
            print(f"Current command for {label}: {binding['command'] or '(not set)'}")
            cmd = input("New command: ").strip()
            binding["command"] = cmd
            print(f"Updated {label}")

        elif choice == "4":
            trigger_ids = select_binding_ids(
                config,
                "Remove which binding number(s)? Use commas to remove multiple: ",
            )
            if trigger_ids is None:
                continue

            for trigger_id in trigger_ids:
                binding = config["bindings"].pop(trigger_id)
                config["cooldowns"].pop(trigger_id, None)
                print(f"Removed {describe_trigger(trigger_id, binding['kind'])}")

        elif choice == "5":
            trigger_id = select_binding_id(config, "Edit cooldown for which binding number? ")
            if trigger_id is None:
                continue

            binding = config["bindings"][trigger_id]
            label = describe_trigger(trigger_id, binding["kind"])
            current = float(
                config["cooldowns"].get(trigger_id, default_cooldown_for_kind(binding["kind"]))
            )
            print(f"Current cooldown for {label}: {current:.2f}s")
            raw = input("New cooldown in seconds (example 0.40): ").strip()
            try:
                config["cooldowns"][trigger_id] = float(raw)
                print(f"Updated {label} cooldown to {raw}")
            except ValueError:
                print("Invalid number.")

        elif choice == "6":
            config["port"] = choose_port_interactively()
            print(f"Now using MIDI port: {config['port']}")

        elif choice == "7":
            save_config(config)
            print(f"\nSaved config to: {CONFIG_PATH}")
            _, message = restart_execute_service()
            print(message)
            return

        elif choice == "8":
            print("Leaving without saving.")
            return

        else:
            print("Invalid option.")


if __name__ == "__main__":
    main()

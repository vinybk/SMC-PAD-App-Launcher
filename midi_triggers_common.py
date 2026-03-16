#!/usr/bin/env python3

from __future__ import annotations

from dataclasses import dataclass, field
import re
import mido


BUTTON_CCS = {25, 26, 27, 28, 29}
KNOB_CCS = {38, 39, 40, 41, 42, 43, 44, 45}
PORT_SUFFIX_RE = re.compile(r" \d+:\d+$")


@dataclass(frozen=True)
class NormalizedEvent:
    kind: str
    id: str
    raw: str
    note: int | None = None
    control: int | None = None


@dataclass
class ActivePadState:
    active_notes: list[int] = field(default_factory=list)

    def note_on(self, note: int) -> None:
        if note in self.active_notes:
            self.active_notes.remove(note)
        self.active_notes.append(note)

    def note_off(self, note: int) -> None:
        if note in self.active_notes:
            self.active_notes.remove(note)

    @property
    def current_note(self) -> int | None:
        if not self.active_notes:
            return None
        return self.active_notes[-1]


def default_cooldown_for_kind(kind: str) -> float:
    return {
        "pad_hit": 0.0,
        "button_pressed": 0.0,
        "knob_moved": 0.4,
        "pad_pressured": 0.5,
    }.get(kind, 0.0)


def describe_trigger(trigger_id: str, kind: str) -> str:
    if trigger_id.startswith("note:"):
        return f"pad {trigger_id.split(':', 1)[1]}"
    if trigger_id.startswith("aftertouch:"):
        return f"pressure on pad {trigger_id.split(':', 1)[1]}"
    if trigger_id.startswith("cc:"):
        control = trigger_id.split(":", 1)[1]
        if kind == "knob_moved":
            return f"knob {control}"
        if kind == "button_pressed":
            return f"button {control}"
        return f"CC {control}"
    return trigger_id


def describe_event(event: NormalizedEvent) -> str:
    if event.kind == "pad_hit" and event.note is not None:
        return f"pad hit on note {event.note}"
    if event.kind == "pad_pressured" and event.note is not None:
        return f"pressure on active pad {event.note}"
    if event.kind == "knob_moved" and event.control is not None:
        return f"knob movement on CC {event.control}"
    if event.kind == "button_pressed" and event.control is not None:
        return f"button press on CC {event.control}"
    return f"{event.kind} ({event.id})"


def normalize_message(
    msg: mido.Message,
    state: ActivePadState | None = None,
) -> NormalizedEvent | None:
    if msg.type == "control_change":
        control = getattr(msg, "control", None)
        value = getattr(msg, "value", 0)

        if control in KNOB_CCS:
            return NormalizedEvent(
                kind="knob_moved",
                id=f"cc:{control}",
                raw=str(msg),
                control=control,
            )

        if control in BUTTON_CCS and value > 0:
            return NormalizedEvent(
                kind="button_pressed",
                id=f"cc:{control}",
                raw=str(msg),
                control=control,
            )

        return None

    if msg.type == "note_on":
        note = getattr(msg, "note", None)
        velocity = getattr(msg, "velocity", 0)

        if note is None:
            return None

        if velocity > 0:
            if state is not None:
                state.note_on(note)
            return NormalizedEvent(
                kind="pad_hit",
                id=f"note:{note}",
                raw=str(msg),
                note=note,
            )

        if state is not None:
            state.note_off(note)
        return None

    if msg.type == "note_off":
        note = getattr(msg, "note", None)
        if state is not None and note is not None:
            state.note_off(note)
        return None

    if msg.type == "aftertouch":
        if state is None or state.current_note is None:
            return None
        return NormalizedEvent(
            kind="pad_pressured",
            id=f"aftertouch:{state.current_note}",
            raw=str(msg),
            note=state.current_note,
        )

    return None


def choose_port_interactively() -> str:
    ports = mido.get_input_names()
    if not ports:
        raise SystemExit("No MIDI input ports found.")

    print("Available MIDI input ports:")
    for idx, name in enumerate(ports, start=1):
        print(f"  {idx}. {name}")

    while True:
        choice = input("Choose port number: ").strip()
        try:
            idx = int(choice)
            if 1 <= idx <= len(ports):
                return canonicalize_port_name(ports[idx - 1])
        except ValueError:
            pass
        print("Invalid selection. Try again.")


def canonicalize_port_name(name: str) -> str:
    return PORT_SUFFIX_RE.sub("", name).strip()


def resolve_input_port_name(configured_name: str, available_names: list[str] | None = None) -> str:
    if available_names is None:
        available_names = list(mido.get_input_names())

    if configured_name in available_names:
        return configured_name

    wanted = canonicalize_port_name(configured_name)
    matches = [name for name in available_names if canonicalize_port_name(name) == wanted]
    if len(matches) == 1:
        return matches[0]

    raise LookupError(
        f"Configured MIDI input '{configured_name}' was not found."
        if not matches
        else f"Configured MIDI input '{configured_name}' matched multiple ports: {', '.join(matches)}"
    )

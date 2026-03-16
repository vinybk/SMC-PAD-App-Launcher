# BLE MIDI Bridge

This repo now includes a user-space BLE MIDI bridge in `midi_ble_bridge.py`.

## Why this exists

Some Linux installs do not expose Bluetooth MIDI devices as ALSA sequencer ports, even when the device advertises the BLE MIDI service. This bridge connects to the pad over BLE and publishes a stable virtual MIDI port locally for `midi_execute.py`.

## Install

```bash
python3 -m pip install --user bleak
```

`mido` and `python-rtmidi` are already required by the existing scripts.

## First-time setup

Scan for devices:

```bash
python3 midi_ble_bridge.py scan
```

Write the bridge config:

```bash
python3 midi_ble_bridge.py init --device-name SMC-PAD
```

That creates `~/.config/midi-ble-bridge.json`.

Using the device name is recommended. Many BLE devices rotate or reappear under
different addresses after reconnects or power cycles, so name-based configs
intentionally keep `device_address` empty and reconnect by name.

## Run it manually

```bash
python3 midi_ble_bridge.py run
```

The bridge creates a virtual MIDI output named `Pad Magic BLE`.

## Hook it into Pad Magic

Run `./midi_configure.py`, choose `Change MIDI port`, and select the `Pad Magic BLE` port. The configurator now saves stable port names without ALSA client numbers.

## Run it as a service

Copy `systemd/midi-ble-bridge.service` to:

```bash
~/.config/systemd/user/midi-ble-bridge.service
```

Then enable it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now midi-ble-bridge.service
journalctl --user -u midi-ble-bridge.service -f
```

Your existing `midi-execute.service` can stay as-is.

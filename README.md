# RetroBat Display Companion

RetroBat Display Companion provides auxiliary display support for RetroBat/EmulationStation arcade cabinets.

It listens for events from EmulationStation and uses [mpv](https://mpv.io/) to display artwork on additional cabinet screens such as a **backglass** and **DMD**.

Originally developed for a hybrid virtual pinball/arcade cabinet.

## Features

* Separate Backglass and DMD displays
* Displays media when navigating systems and games
* Supports fanart, backglass, marquee/logo and fallback artwork
* Responds to game start and end events
* Can blank displays when launching virtual pinball systems so VPX/Future Pinball can take control
* Handles rapid frontend navigation without queuing stale display updates
* Automatically starts and manages the required mpv instances
* Returns focus to EmulationStation after starting display windows

## Requirements

### Custom EmulationStation build

A **custom build of EmulationStation is required**.

RetroBat Display Companion uses a `NamedPipeEventBroadcaster` added to my [EmulationStation fork](https://github.com/elliot-forty-two/emulationstation/tree/feature/named-pipe-events). This publishes frontend events over a Windows named pipe, allowing the companion to respond directly to system selection, game selection, game start/end and other events.

Stock RetroBat/EmulationStation does not currently provide this interface.

The default event pipe is:

```text
\\.\pipe\EmulationStation.Events
```

### Python

Python 3 is required. The startup script uses the Windows Python launcher:

```text
py -3
```

### mpv

[mpv](https://mpv.io/) is used to display media.

`mpv.exe` is expected under the project's `mpv` directory:

```text
RetroBatDisplayCompanion/
├── display_companion.py
├── fallback.png
├── systems/
└── mpv/
    └── mpv.exe
```

## Running

The companion is intended to be launched automatically when EmulationStation starts.

For example:

```bat
@echo off
start "" /min py -3 "C:\RetroBat\plugins\retrobat-display-companion\display_companion.py"
```

Media modes, DMD dimensions, capture regions, and other user-facing options are
configured in `display_companion.ini`. Pipe names, display numbers, and advanced
developer settings remain constants near the top of `display_companion.py`.

## Status

This is a personal cabinet project and is currently tailored to my RetroBat setup. It requires the corresponding custom EmulationStation build and is not currently a drop-in plugin for a standard RetroBat installation.

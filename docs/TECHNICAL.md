# Technical notes

How the patch works, where it hooks, and how it was checked. For installing and
playing, see the [README](../README.md).

## Overview

The patcher appends one executable text section to `sys/main.dol`, at
`0x80001820` (in the OS's unused low memory, below the DOL's own code; the
section must end before `0x80003000`), and replaces a handful of instructions
with branches into it. No Gecko code handler is involved.

```
0x80001820  scratch: zeroed data (SI poller state, KPAD call counter, debug
            counters, per-channel feeder state)
0x800019A0  GameCube half: poller wrapper + poller, feeder wrapper + gc_feed()
            Classic Controller half: the six Vague Rant bodies
```

All three releases run the same game code and the same Wii SDK, only placed at
other addresses. The USA DOL was analysed in Ghidra; the other two were matched
against it with branch targets and immediates masked out (every match was
unique). `tools/regions.py` holds the few addresses the patch needs, and the
Classic Controller hook sites come from the per-region Gecko codes.

| | USA `R49E01` | Europe `R49P01` | Japan `R49J01` |
| --- | --- | --- | --- |
| `KPADReadEx` (after `KPADRead`'s two `li`s) | `0x8034DDE4` | `0x8034E4A4` | `0x8034CCA4` |
| KPAD pointer (DPD) routine | `0x8034CE50` | `0x8034D510` | `0x8034BD10` |
| `si::SIGetType` | `0x8031B940` | `0x8031C000` | `0x8031A800` |
| `si::` state (busy flag, SIPOLL shadow, types at `+0x18`) | `0x804770A0` | `0x8047ABA0` | `0x804763E0` |

## Classic Controller

Vague Rant and crediar's codes, unchanged: one `04` write that stops the
"insert a Nunchuk" error, and six `C2` insertions. The patcher parses
`codes/<disc id>.ini`, places each `C2` body in the new section, points the hook
site at it, and ends the body with a branch back to `site + 4` (what the code
handler would append). In short: the game treats an extension of type 2 as a
Nunchuk, takes the stick and acceleration from the Classic Controller, and the
three KPAD-library hooks turn Classic Controller buttons into Wii Remote
buttons and the clap/shake buttons into the acceleration the game expects.

## GameCube controller and DK Bongos

Jungle Beat links the SDK's `si::` library (`SIInit` runs at boot and probes
all four ports) but not the PAD library, so nothing ever polls a controller.
The patch adds two things.

### SI poller (`src/poller.s`)

Hooked at the start of `KPADReadEx`, which the game calls for every channel
every frame. It programs SI hardware auto-polling itself — the poll command into
each `SICnOUTBUF`, a `SISR` write to latch them, then `SIPOLL` with the enable
and copy-on-vblank bits for each channel whose cached `si::` type is a standard
GameCube device — and mirrors the enable byte into `si::`'s own `SIPOLL` shadow
so the retrace handler's rate refresh doesn't switch it off again. It re-probes
empty ports (`SIGetType`, at most every 0.25 s), turns a persistent `NOREP` into
the cached "no response" type so a replugged pad is noticed, and carries a
watchdog for `si::`'s single "transfer busy" flag, which nothing in the retail
binary ever times out. This is the poller from
[Barrel-Blast-Patch](https://github.com/quatric/Barrel-Blast-Patch) with this
game's addresses; that game links the same library and the logic was worked out
on a real console there. The wrapper also records the channel being read and
bumps a call counter in the scratch area.

### Feeder (`src/gc_feed.c`)

Hooked at the entry of the KPAD library's pointer routine, which `KPADReadEx`
calls once per Wii Remote sample after everything else for that sample is done.
When the channel's SI type is a GameCube device that is answering, it rewrites
the sample's `KPADStatus`:

- `hold`/`trig`/`release` (`+0x00..+0x08`) get the pad's buttons in Wii Remote
  bits. `trig`/`release` are worked out once per `KPADReadEx` call against the
  previous call, because the library gives every sample of a call the same
  edges and the game reads them from the newest one.
- `dev_type` (`+0x5C`) becomes 1 (Wii Remote + Nunchuk) and `err` (`+0x5D`)
  0, so the game's own Nunchuk check passes without any game patch.
- The Nunchuk stick (`+0x60`/`+0x64`) and acceleration (`+0x68..+0x70`), and
  the Wii Remote acceleration (`+0x0C..+0x14`).

Claps and shakes are the same square wave the Classic Controller hooks make:
three samples one way, one at rest, three the other way, because the game
counts a shake from the change between frames and the library delivers several
samples per frame. A single clap is one call's worth of the same value. In a
bubble the Wii Remote's tilt steers; the left stick stands in. The bubble state
is read from the same `EventDirector` data the Classic Controller hook uses
(`bubble_ptr` in `regions.py`).

DK Bongos answer the SI poll like a pad but with both stick bytes zero (a real
pad's rest near `0x80`), the right drum on A/X, the left on B/Y, and the clap
microphone as R. The feeder detects them by the zero stick bytes. A drum hit
runs DK for 170 ms, two drums within 50 ms are a 130 ms A press (jump, and
confirm in menus), R is a clap. The Wii Remote and Nunchuk are left alone
except for what the drums add, so a real remote can shake and steer a bubble.

Everything in the feeder is integer arithmetic: the hook sits before the hooked
function's prologue, so the floating-point registers are not saved, and the
blob has to build without relocations. Floats are written as IEEE-754 bit
patterns (`f32_64ths`).

Scratch `+0x28..+0x5F` is a debug block (last SI type, `INBUFH`, `INBUFL`, number
of feeder calls, drum-jump / left / right / clap counters) that can be read over
a debugger without touching the game.

## Building the blobs

`tools/build_blobs.py` assembles `poller.s` and `wrappers.s`, compiles
`gc_feed.c` with devkitPPC (`-msoft-float`, `-mno-sdata`, freestanding), links
them for each region at `0x800019A0`, and writes `tools/prebuilt/blobs.json`
with a hash of the sources. `tests/test_patch.py` fails if the hash is stale.

## How it was checked

No game files are in the repository, so the unit tests patch a skeleton DOL
that only holds the retail instructions at the hook sites.

The behaviour was checked in Dolphin with a scripted GameCube pad (Dolphin's
Pipe input; "Background Input" has to be on or it ignores the pipe) and the GDB
stub for reading memory:

- all three regions boot, reach the title screen, and are driven by the pad;
  the USA build was played through the file select, the "shake your
  controllers" prompt and into the first level;
- every pad button and stick position produces the expected `hold` bits and
  stick values, `X` produces the alternating ±3.4 acceleration, and `trig` is
  set on the newest sample;
- Dolphin's DK Bongos device is detected, and drum hits, both-drums, staggered
  hits inside and outside the join window, claps and the drum swap
  (`X`/`Y`) all count as designed (via the debug counters), and the bongos
  get through the menus and into the first level with a Dolphin Wii Remote
  doing the shake;
- the Classic Controller path plays through the same screens, on its own and
  chained with the GameCube feeder.

Not checked: a real console, a real pad being hot-plugged, real DK Bongos
hardware (the clap detection and which sensor is A/X versus B/Y follow Dolphin's
emulation and the Barrel-Blast-Patch notes).

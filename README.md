# VBRecomp Tools

Convert Virtual Boy ROM images from your own collection into native
PC executables (Linux and, optionally, Windows) using the
**vbrecomp** framework by **Matthew Stanley (mstan)**,
[RetroPortingToolkit](https://retroportingtoolkit.com).

The Virtual Boy had a tiny library (22 retail games), and this tool
is honest about how the framework works: conversion is
**profile-based**. A game converts only when a bundled per-game
integration profile — generated code plus host integration that
cannot be derived from the ROM alone — matches the ROM's exact
image (CRC32 + size). The bundled profiles are upstream's own
shipped titles:

| Game | Qualified image | CRC32 | Size |
|---|---|---|---|
| Mario's Tennis (Japan, USA) | retail | `7CE7460D` | 524,288 B |
| Zero Racers (prototype) | unreleased proto | `71553796` | 1,048,576 B |

Every other game — and every other revision of these two — is
listed as **needs review** with the reason shown, and will convert
if/when an upstream integration exists (the app tracks upstream).
Zero Racers is an unreleased prototype; if its ROM is not in your
collection, that profile simply never matches.

## Verified status (developer machine)

- **Mario's Tennis** converts end-to-end through this app and
  boots into real gameplay (launcher with box art → title →
  match), screenshots on file.
- The RetroAchievements hash for Mario's Tennis matches the
  official whole-file hash; the module logs in-attempts and its
  failure toast renders. A successful login and a real achievement
  unlock are verified on the user's machine (this build machine
  has no live route to retroachievements.org).
- The Windows build is cross-compiled and compile-verified;
  running it on real Windows is verified on the user's machine.
- The Zero Racers profile is carried but untested (its ROM is not
  in the test collection).

## Display

The game renders in the Virtual Boy's original red, one eye by
default (window 768×448, resizable). `--stereo` stacks both eyes
in one window. Esc opens the in-game settings menu. Mario's Tennis
packages include upstream's experimental full-color mod
(`.vbmod`); import it on the launcher's Mods page for color — it
is off by default and never modifies your ROM.

## RetroAchievements

Upstream ships no RetroAchievements support; VBRecomp Tools wires
it in itself: a vendored rcheevos module hooks the runtime's
per-frame tick, hashes the loaded ROM the official way (Virtual
Boy console ID 28, whole-file MD5), reads the game's WRAM and
cartridge RAM for achievement logic, logs in with the Connect
token from `retroachievements.toml` next to the game, and shows a
login toast plus achievement pop-ups through the game's own
runtime UI. Packages carry your filled
`retroachievements.toml` byte-identical when RA is on.

Note: RetroAchievements counts unlocks from recomp clients as
Casual (softcore) — the client is an unapproved emulator to the
service. This tool never spoofs another client's identity to
change that.

## Netplay

- **Mario's Tennis: none.** Single-system game; upstream wires no
  netplay for it, so the tool offers no netplay toggle rather than
  a fake one.
- **Zero Racers:** its upstream project ships a versus netplay
  host (local split-screen, LAN / Direct IP, online rooms) as part
  of that game's own integration, enabled via its multiplayer mod
  package. That profile is untested here — nothing is claimed
  beyond what upstream documents. (A connected match also cannot
  be proven from the developer's sandbox, whose network blocks
  UDP.)

## Notes

- Upstream's runtime always opens a localhost-only TCP debug
  server (127.0.0.1:4390, `--port N` to change) — that is how
  upstream's own tooling attaches, and their releases ship it
  too. Production builds here compile with
  `VBRECOMP_DEBUG_TOOLS=OFF`, which strips the trace/ring
  instrumentation; the debug server itself is upstream's design
  and is left as-is.

## BIOS

None — the Virtual Boy has no BIOS file. The runtime models the
hardware directly and no package ships one.

## Layout

- `vb-recomp-tools.sh` / `vb-recomp-tools.bat` — launchers. They
  create a local Python venv on first run and install PySide6 +
  Pillow from `requirements.txt`.
- `app/engine.py` — probe / scaffold / build / package engine and
  CLI (`toolbox`, `probe --rom`, `run --job`).
- `app/main.py` — the PySide6 GUI (Convert / Toolbox / Help tabs).
- `bundle/ra/` — the RetroAchievements module sources and the
  `retroachievements.toml.example` template.
- `framework-cache/` — fetched game-project templates (empty in
  the distribution zip; filled on first use).
- `art-cache/` — downloaded cover art (empty in the zip).

## Dependencies (Toolbox tab can install them)

CMake, Ninja, GCC/G++, git, curl, pkg-config, SDL2 development
files, OpenSSL headers (RetroAchievements), and MinGW-w64 for
Windows builds. The Windows SDL2 development kit is app-owned:
it downloads automatically into `tools/` on the first Windows
build.

## Credits

- **vbrecomp** framework and the **MarioTennisVirtualBoyRecomp** /
  **ZeroRacersVirtualBoyRecomp** game projects by **Matthew
  Stanley (mstan)**, RetroPortingToolkit — MIT licensed.
- Vendored components: SDL2, Dear ImGui, rcheevos (MIT,
  RetroAchievements project).
- Cover art from the libretro-thumbnails project
  (`Nintendo_-_Virtual_Boy`).

VBRecomp Tools is an unofficial fan tool. It is not affiliated
with Nintendo. Mario's Tennis, Zero Racers and Virtual Boy are
trademarks of Nintendo. No ROMs are included or distributed; you
supply dumps of your own cartridges.

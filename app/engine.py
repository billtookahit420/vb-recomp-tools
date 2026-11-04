#!/usr/bin/env python3
"""VBRecomp Tools - engine.

Converts Virtual Boy ROMs from the user's own collection into
native PC recompilations with the vbrecomp framework (Matthew
Stanley / RetroPortingToolkit), packaged the same way as the rest
of the Recomp Tools family.

How this framework differs from the cartridge siblings (GB/SNES):
a VB recomp is NOT produced from a ROM alone. The runtime executes
a per-game integration: generated C (committed in each upstream
game project) plus game-specific host code, and the runtime
CRC32-verifies the player's ROM at launch against the identity
baked in at build time. Each supported game lives in its own
upstream companion project. So this tool is PROFILE-based, like
the N64, Genesis and SMS/GG tools: it converts exactly the games
an integration profile exists for, and flags everything else
needs-review with the honest reason. The bundled profiles are
upstream's two shipped titles: Mario's Tennis (v0.2.3) and Zero
Racers (v0.1.2, an unreleased prototype game - its ROM is not in
Duane's collection, so that profile is carried for completeness
and is untested here).

Layout of a conversion (per profile):
  companion project (framework + recomp-ui pinned inside as
  submodules) -> stage a copy -> the generated code is already
  committed upstream, so no host recompiler step exists -> CMake/
  Ninja builds vb-runtime (renamed per game) -> package: exe +
  assets + play launcher + README + (optional) RA files. The ROM
  is never copied into the project or the package: the player
  supplies it at run time and the game verifies it.

RetroAchievements is fresh wiring in the family style (bundle/ra):
upstream ships none. The engine patches the STAGED framework
copies of vip.c (invoke vb_ra_frame_tick() at every guest
GAME_START frame tick, beside upstream's own mod frame tick) and
main.cpp (hand the module the host's runtime UI for toasts right
after the host creates it). RA off restores the staged files
byte-identical. Console id RC_CONSOLE_VIRTUAL_BOY = 28 (vendored
rc_consoles.h); the VB hash is the whole-file MD5.

Netplay: Mario's Tennis has none (single-system game; upstream
does not wire the launcher's optional netplay backend for it, and
this sandbox could not prove a connected match anyway - UDP is
blocked here). Zero Racers upstream ships a versus netplay host
(local split-screen, LAN/Direct IP, online rooms) at ITS framework
pin; that profile is untested here because the ROM is not in the
collection. No netplay toggle is offered for Mario's Tennis.

CLI:
  engine.py toolbox
  engine.py probe --rom PATH
  engine.py run --job FILE.json

Job JSON:
  {"roms": [paths], "output_dir": path,
   "ra": {"enabled": bool, "popups": bool, "toml": path-or-empty},
   "targets": {"linux": bool, "windows": bool}, "jobs": int,
   "force": bool}
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
import zlib
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
BUNDLE_RA = APP_DIR / "bundle" / "ra"
FRAMEWORK_CACHE = APP_DIR / "framework-cache"
ART_CACHE = APP_DIR / "art-cache"
TOOLS_DIR = APP_DIR / "tools"

MT_REPO_URL = "https://github.com/mstan/MarioTennisVirtualBoyRecomp.git"
ZR_REPO_URL = "https://github.com/mstan/ZeroRacersVirtualBoyRecomp.git"

ROM_EXTS = {".vb"}

# MarioTennis's .gitmodules points its vbrecomp submodule at an SSH
# URL (git@github.com:...), which fails on any machine without a
# GitHub SSH key - including a fresh clone by this app. Rewriting
# github SSH URLs to HTTPS for every git invocation the engine
# makes is the fix (the repos are public).
GIT_URL_REWRITE = ["-c", "url.https://github.com/.insteadOf=git@github.com:"]


class VbError(Exception):
    pass


def log_line(log, msg: str) -> None:
    print(msg, flush=True)
    if log:
        log(msg)


def run(cmd, *, cwd=None, env=None, log=None, timeout=None) -> subprocess.CompletedProcess:
    log_line(log, "+ " + " ".join(str(c) for c in cmd))
    proc = subprocess.run(
        [str(c) for c in cmd], cwd=cwd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if proc.stdout:
        for line in proc.stdout.splitlines():
            log_line(log, line)
    if proc.returncode != 0:
        raise VbError(f"command failed ({proc.returncode}): {' '.join(str(c) for c in cmd)}")
    return proc


def git_cmd(*args):
    return ["git"] + GIT_URL_REWRITE + list(args)


def camel(title: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[^A-Za-z0-9]+", title) if w)


# ---------------------------------------------------------------------------
# Integration profiles
#
# A profile is a qualified per-game integration: the exact ROM image
# (CRC32 + SHA-256 + size), the upstream companion project that
# carries its generated code + build glue, and the CMake targets
# that produce the game. Only add a profile when the framework (or
# a productised game project on it) actually ships the integration.
# ---------------------------------------------------------------------------

PROFILES = {
    "mariotennis": {
        "id": "mariotennis",
        "title": "Mario's Tennis",
        "camel": "MariosTennis",
        "crc32": 0x7CE7460D,
        "sha256": "5dc5e6b5d5f538f56b3b9727db1c7931d9dfe1bbd0743f698897e0fd90e70101",
        "size": 524288,
        "revision": "Japan, USA (512 KiB)",
        "template_repo": MT_REPO_URL,
        "template_dirname": "MarioTennisVirtualBoyRecomp",
        "module": "marios_tennis",
        "target": "vb-runtime",
        "mods_target": "mario-tennis-mods",
        "exe": "MarioTennisVirtualBoyRecomp",
        "mod_packages": ["marios-tennis-full-color-0.2.2.vbmod"],
        "players": 1,
        "netplay": False,
        "version": "0.2.3",
    },
    "zeroracers": {
        "id": "zeroracers",
        "title": "Zero Racers",
        "camel": "ZeroRacers",
        "crc32": 0x71553796,
        "sha256": "47421cd82dfd414d042d2f7f9db51e657d414d4ddfa46887efb544029fa11ce7",
        "size": 1048576,
        "revision": "Prototype (1 MiB)",
        "template_repo": ZR_REPO_URL,
        "template_dirname": "ZeroRacersVirtualBoyRecomp",
        "module": "zero_racers",
        "target": "vb-runtime",
        "mods_target": "zero-racers-mods",
        "exe": "ZeroRacersVirtualBoyRecomp",
        "mod_packages": ["zero-racers-full-color-0.2.1.vbmod",
                         "zero-racers-multiplayer-0.1.0.vbmod"],
        "players": 2,
        "netplay": True,
        "version": "0.1.2",
    },
}


def profile_for_probe(probe: dict):
    for prof in PROFILES.values():
        if probe.get("crc32") == prof["crc32"] and probe.get("size") == prof["size"]:
            return prof
    return None


# ---------------------------------------------------------------------------
# Probe: Virtual Boy cartridge header + image identity
#
# The VB header is 32 bytes at file offset (size - 0x220):
#   +0x00 title (20 bytes, ASCII, space/NUL padded)
#   +0x14 reserved (5 bytes)
#   +0x19 maker code (2 ASCII chars; "01" = Nintendo)
#   +0x1B game code (4 ASCII chars)
#   +0x1F game version (1 byte)
# Retail VB ROMs run 128 KiB to 2 MiB, always a power of two in
# practice. Identity for profile matching is CRC32 + size, exactly
# what the runtime itself verifies at launch.
# ---------------------------------------------------------------------------

MAKER_NAMES = {"01": "Nintendo"}


def probe_rom(path) -> dict:
    p = Path(path)
    result = {
        "path": str(p), "filename": p.name, "ok": False,
        "needs_review": False, "reasons": [], "info": [],
        "title": "", "system": "vb", "system_label": "Virtual Boy",
        "header_title": "", "maker_code": "", "maker": "",
        "game_code": "", "version": None,
        "size": 0, "file_size": 0,
        "crc32": None, "md5": "", "sha1": "", "sha256": "",
        "profile": None, "profile_title": "",
    }
    try:
        raw = p.read_bytes()
    except OSError as exc:
        result["reasons"].append(f"cannot read file: {exc}")
        return result
    result["file_size"] = len(raw)
    result["size"] = len(raw)
    result["crc32"] = zlib.crc32(raw) & 0xFFFFFFFF
    result["md5"] = hashlib.md5(raw).hexdigest()
    result["sha1"] = hashlib.sha1(raw).hexdigest()
    result["sha256"] = hashlib.sha256(raw).hexdigest()
    result["title"] = re.sub(r"\s*\[[^\]]*\]\s*$", "", p.stem).strip() or p.stem

    ext = p.suffix.lower()
    if ext not in ROM_EXTS:
        result["needs_review"] = True
        result["reasons"].append(
            f"unrecognised extension {ext!r} - not a Virtual Boy "
            "image this tool handles (.vb)")
        return result
    if len(raw) < 0x10000:
        result["needs_review"] = True
        result["reasons"].append(
            f"image is only {len(raw)} bytes - too small to be a "
            "Virtual Boy cartridge image")
        return result

    base = len(raw) - 0x220
    title_raw = raw[base:base + 20]
    try:
        header_title = title_raw.decode("ascii", errors="replace")
        header_title = header_title.replace("\x00", " ").strip()
        header_title = re.sub(r"\s+", " ", header_title)
    except Exception:
        header_title = ""
    # A plausible header has a printable title; headerless or
    # corrupt dumps still identify by CRC32 alone.
    if header_title and all(32 <= ord(c) < 127 for c in header_title):
        result["header_title"] = header_title
        result["maker_code"] = raw[base + 0x19:base + 0x1B].decode(
            "ascii", errors="replace")
        result["maker"] = MAKER_NAMES.get(result["maker_code"], "")
        result["game_code"] = raw[base + 0x1B:base + 0x1F].decode(
            "ascii", errors="replace")
        result["version"] = raw[base + 0x1F]
    else:
        result["info"].append(
            "no readable header title at size-0x220 - identity is "
            "by CRC32 only")

    prof = profile_for_probe(result)
    if prof:
        result["profile"] = prof["id"]
        result["profile_title"] = prof["title"]
        result["ok"] = True
        return result

    result["needs_review"] = True
    core = re.sub(r"\s*\([^)]*\)", "",
                  re.sub(r"\s*\[[^\]]*\]", "", p.stem)).strip().lower()
    for candidate in PROFILES.values():
        cand_titles = {candidate["title"].lower()}
        if core in cand_titles:
            result["reasons"].append(
                f"{candidate['title']} but not the qualified image: "
                f"the profile covers {candidate['revision']} (CRC32 "
                f"0x{candidate['crc32']:08X}, {candidate['size']} "
                f"bytes); this image is CRC32 "
                f"0x{result['crc32']:08X}, {len(raw)} bytes - "
                "different code, not yet qualified upstream")
            break
    else:
        if "(proto)" in p.name.lower():
            result["reasons"].append(
                "prototype dump with no integration profile: a VB "
                "recomp needs per-game generated code and host "
                "integration that cannot be derived from the ROM "
                "alone; the bundled profiles cover Mario's Tennis "
                "and Zero Racers, upstream's two shipped titles")
        else:
            result["reasons"].append(
                "no integration profile for this game: a VB recomp "
                "needs per-game generated code and host integration "
                "that cannot be derived from the ROM alone; the "
                "bundled profiles cover Mario's Tennis and Zero "
                "Racers, upstream's two shipped titles")
    return result


# ---------------------------------------------------------------------------
# Cover art: libretro-thumbnails, set Nintendo_-_Virtual_Boy.
# Dual-host (GitHub mirror first) + retries + name variants + the
# fuzzy listing fallback, ported from the family engines (the Sega
# sets' legacy naming taught the family this pattern; the VB set
# uses region-order variants like "(Japan, USA)").
# ---------------------------------------------------------------------------

COVER_SETS = {
    "vb": ("Nintendo_-_Virtual_Boy", "Nintendo%20-%20Virtual%20Boy"),
}
USER_AGENT = "VBRecompTools/1.0"
_ILLEGAL = dict.fromkeys(map(ord, '\\/:*?"<>|'), None)


def _lr_encode(name: str) -> str:
    return name.translate(_ILLEGAL).replace("&", "_")


def _http_get_once(url: str, timeout: int = 25):
    import urllib.error
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception:
        return None, None


def _http_get_retry(url: str, attempts: int = 3, timeout: int = 25):
    """Bytes or None. A 404 is definitive: never retried."""
    for i in range(attempts):
        data, status = _http_get_once(url, timeout=timeout)
        if data is not None:
            return data
        if status == 404:
            return None
        if i + 1 < attempts:
            time.sleep(2 * (i + 1))
    return None


def _valid_image(data) -> bool:
    if not data or len(data) < 4000:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) < 24:
            return False
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return w >= 100 and h >= 100
    return data[:3] == b"\xff\xd8\xff"


def _to_png(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        out = io.BytesIO()
        im.convert("RGB").save(out, "PNG")
        return out.getvalue()
    except Exception:
        return None


def cover_variants(title: str):
    title = (title or "").strip()
    if not title:
        return []
    out = []

    def add(n):
        n = (n or "").strip()
        if n and n not in out:
            out.append(n)

    add(title)
    no_brackets = re.sub(r"\s*\[[^\]]*\]", "", title).strip()
    add(no_brackets)
    norm = re.sub(r"\((JUE|JU|UE|JE|U|J|E)\)", "(World)", no_brackets)
    norm = re.sub(r"\(V(\d+)\.(\d+)\)", r"(Rev \2)", norm)
    add(norm)
    core = title.split(" (")[0].strip()
    add(core)
    for base in (core, norm.split(" (")[0].strip()):
        for suffix in (" (USA)", " (USA, Europe)", " (World)",
                       " (Europe)", " (Japan, USA)", " (USA, Japan)",
                       " (Japan)"):
            add(base + suffix)
    return out


def _cover_try_name(system: str, name: str, log=None):
    import urllib.parse
    gh, canon = COVER_SETS[system]
    enc = urllib.parse.quote(_lr_encode(name), safe="")
    cache = ART_CACHE / system / (enc + ".png")
    if cache.is_file():
        data = cache.read_bytes()
        if _valid_image(data):
            return data, f"cache ({name})"
    urls = [
        f"https://raw.githubusercontent.com/libretro-thumbnails/{gh}/master/Named_Boxarts/{enc}.png",
        f"https://thumbnails.libretro.com/{canon}/Named_Boxarts/{enc}.png",
    ]
    for url in urls:
        data = _http_get_retry(url)
        if data and _valid_image(data):
            png = _to_png(data)
            if png:
                try:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_bytes(png)
                except OSError:
                    pass
                host = "GitHub mirror" if "githubusercontent" in url else "libretro"
                return png, f"{host} ({name})"
    return None, None


_REGION_WORDS = {
    "usa", "europe", "japan", "world", "australia", "brazil", "korea",
    "canada", "france", "germany", "spain", "italy", "netherlands",
    "sweden", "denmark", "finland", "norway", "asia", "taiwan",
    "china", "russia", "mexico", "portugal", "greece", "india",
    "israel", "uk", "scandinavia", "latin america", "new zealand",
}


def _cover_listing(system: str, log=None):
    gh, _canon = COVER_SETS[system]
    dest = ART_CACHE / "listings" / f"{system}.json"
    stale = []
    try:
        blob = json.loads(dest.read_text())
        stale = blob.get("names", [])
        if time.time() - blob.get("ts", 0) < 7 * 24 * 3600 and stale:
            return stale
    except Exception:
        pass
    if not shutil.which("git"):
        return stale
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="vb-list-"))
    try:
        repo = f"https://github.com/libretro-thumbnails/{gh}"
        subprocess.run(git_cmd("clone", "--quiet", "--depth", "1",
                               "--filter=tree:0", "--no-checkout", repo,
                               str(tmp / "repo")),
                       check=True, capture_output=True, timeout=300)
        out = subprocess.run(
            git_cmd("-C", str(tmp / "repo"), "ls-tree", "-r",
                    "--name-only", "HEAD"),
            check=True, capture_output=True, timeout=300).stdout
        names = [line[len("Named_Boxarts/"):-len(".png")]
                 for line in out.decode(errors="replace").splitlines()
                 if line.startswith("Named_Boxarts/")
                 and line.endswith(".png")]
        if names:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps({"ts": time.time(), "names": names}))
            return names
    except Exception as exc:
        log_line(log, f"cover: set listing failed ({exc})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return stale


def _regions_of(name: str):
    found = set()
    for group in re.findall(r"\(([^)]*)\)", name):
        for tok in re.split(r"[,+]", group):
            tok = tok.strip().lower()
            if tok in _REGION_WORDS:
                found.add(tok)
    return found


def fetch_cover(title: str, system: str = "vb", log=None):
    """(png_bytes, desc) or (None, None): exact variants first, then a
    fuzzy match against the set's real file listing."""
    if system not in COVER_SETS:
        system = "vb"
    for name in cover_variants(title):
        data, desc = _cover_try_name(system, name, log)
        if data:
            return data, desc
    core = _lr_encode(title).split(" (")[0].strip().lower()
    want = _regions_of(title)
    scored, partial = [], []
    for n in _cover_listing(system, log):
        if _lr_encode(n).split(" (")[0].strip().lower() != core:
            continue
        regs = _regions_of(n)
        low = n.lower()
        score = 100 - 4 * len(regs - want) - len(n)
        if any(t in low for t in ("(proto)", "(beta)", "[hack", "(sample)",
                                  "(demo)", "(unl)")):
            score -= 25
        if want and not want.issubset(regs):
            partial.append((score + 12 * len(want & regs), n))
            continue
        scored.append((score, n))
    pool = scored or partial
    pool.sort(key=lambda t: -t[0])
    for _, cand in pool[:6]:
        data, desc = _cover_try_name(system, cand, log)
        if data:
            return data, f"listing match ({cand})"
    return None, None


# ---------------------------------------------------------------------------
# Windows build support files (app-owned, self-provisioning from
# birth - the GB tool's field lesson: never depend on a folder the
# user happens to have). The VB MinGW build needs only the SDL2
# MinGW kit: the framework's vendored SDL2 pack is MSVC-shaped and
# is skipped for MinGW by upstream's own CMake.
# ---------------------------------------------------------------------------

WIN_SDL2_DIR = TOOLS_DIR / "SDL2-2.30.0"
WIN_PROVISIONED_FILES = [
    ("SDL2 MinGW config (sdl2-config.cmake)",
     WIN_SDL2_DIR / "x86_64-w64-mingw32/lib/cmake/SDL2/sdl2-config.cmake"),
    ("SDL2.dll (MinGW runtime)",
     WIN_SDL2_DIR / "x86_64-w64-mingw32/bin/SDL2.dll"),
]
WIN_DOWNLOADS = [
    ("SDL2-devel-2.30.0-mingw.tar.gz",
     "https://github.com/libsdl-org/SDL/releases/download/release-2.30.0/SDL2-devel-2.30.0-mingw.tar.gz",
     "dir"),
]


def provisioned_ok() -> bool:
    return all(p.is_file() and p.stat().st_size > 0
               for _, p in WIN_PROVISIONED_FILES)


def _download_atomic(url: str, dest: Path, log=None) -> None:
    tmp = dest.with_name(dest.name + ".part")
    log_line(log, f"Downloading {url}")
    curl = shutil.which("curl")
    try:
        if curl:
            proc = subprocess.run(
                [curl, "-fL", "--retry", "3", "-o", str(tmp), url],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if proc.stdout:
                for line in proc.stdout.splitlines():
                    log_line(log, line)
            if proc.returncode != 0:
                raise VbError(f"curl failed ({proc.returncode}) for {url}")
        else:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=300) as r, \
                    open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def ensure_win_tools(log=None) -> None:
    if provisioned_ok():
        return
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    downloads = TOOLS_DIR / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    import tarfile
    for fname, url, kind in WIN_DOWNLOADS:
        archive = downloads / fname
        if not archive.is_file():
            _download_atomic(url, archive, log)
        log_line(log, f"Extracting {fname}")
        with tarfile.open(archive) as tf:
            tf.extractall(TOOLS_DIR)
    if not provisioned_ok():
        missing = [label for label, p in WIN_PROVISIONED_FILES if not p.is_file()]
        raise VbError(
            "Windows support files are incomplete after provisioning: "
            + ", ".join(missing))


# ---------------------------------------------------------------------------
# Framework templates: the upstream companion projects (framework +
# recomp-ui pinned inside each as submodules; the generated game
# code is committed, so there is no host recompiler step).
# ---------------------------------------------------------------------------

def _template_markers(t: Path, prof: dict) -> bool:
    return all([
        (t / "CMakeLists.txt").is_file(),
        (t / "game-build.json").is_file(),
        (t / "vbrecomp" / "runtime" / "src" / "vip.c").is_file(),
        (t / "vbrecomp" / "runtime" / "src" / "main.cpp").is_file(),
        (t / "vbrecomp" / "runtime" / "include" / "memory.h").is_file(),
        (t / "recomp-ui" / "recomp_ui.cmake").is_file(),
        (t / "assets" / "boxart.tga").is_file(),
    ])


def find_template(prof: dict):
    env = os.environ.get("VB_TEMPLATE_DIR")
    if env and Path(env).name == prof["template_dirname"] and \
            _template_markers(Path(env), prof):
        return Path(env)
    cached = FRAMEWORK_CACHE / prof["template_dirname"]
    if _template_markers(cached, prof):
        return cached
    dev = Path.home() / "workspace" / "vb-probe" / (
        "MarioTennis" if prof["id"] == "mariotennis" else "ZeroRacers")
    if _template_markers(dev, prof):
        return dev
    return None


def ensure_frameworks(log=None) -> dict:
    """Returns {"templates": {profile_id: Path}}. Clones the
    companion projects (recursive submodules, with the SSH->HTTPS
    URL rewrite MarioTennis's .gitmodules needs) into the app's
    framework-cache on first run."""
    templates = {}
    for prof in PROFILES.values():
        template = find_template(prof)
        if template is None:
            FRAMEWORK_CACHE.mkdir(parents=True, exist_ok=True)
            dest = FRAMEWORK_CACHE / prof["template_dirname"]
            if dest.exists():
                shutil.rmtree(dest)
            run(git_cmd("clone", "--recursive", prof["template_repo"],
                        str(dest)), log=log)
            template = dest
        if not _template_markers(template, prof):
            raise VbError(
                f"framework template at {template} is incomplete "
                "(missing project/framework files) - delete it and "
                "re-run so the app can fetch a fresh copy")
        templates[prof["id"]] = template
    return {"templates": templates}


# ---------------------------------------------------------------------------
# Scaffold: stage the companion project, patch (optionally) RA
# ---------------------------------------------------------------------------

_ORIG_SUFFIX = ".vb-orig"
_STAGE_MARKER = ".vb-tools-stage.json"


def _backup_once(path: Path) -> Path:
    orig = path.with_name(path.name + _ORIG_SUFFIX)
    if not orig.exists():
        shutil.copy2(path, orig)
    return orig


def _restore_orig(path: Path) -> None:
    orig = path.with_name(path.name + _ORIG_SUFFIX)
    if orig.exists():
        shutil.copy2(orig, path)
        orig.unlink()


def _template_fingerprint(template: Path) -> str:
    h = hashlib.sha256()
    for rel in ("CMakeLists.txt", "game-build.json",
                "vbrecomp/runtime/src/vip.c",
                "vbrecomp/runtime/src/main.cpp"):
        try:
            h.update((template / rel).read_bytes())
        except OSError:
            pass
    # The RA bundle is staged INTO the project, so its content is
    # part of the stage identity too: an app update to the module
    # must restage, never silently reuse the old copy (the SMS/GG
    # tool's stale-stage lesson).
    for rel in ("vb_ra.cpp", "vb_ra.h", "ra_http.c", "ra_http.h"):
        try:
            h.update((BUNDLE_RA / rel).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:16]


def _stage_template(template: Path, proj: Path, prof: dict, log=None) -> None:
    if proj.exists():
        shutil.rmtree(proj)
    skip_names = {".git", "__pycache__", "beetle-vb"}

    def ignore(dirpath, names):
        out = []
        for n in names:
            full = Path(dirpath) / n
            if n in skip_names:
                out.append(n)
            elif n.startswith("build") and full.is_dir():
                out.append(n)
            elif n.lower().endswith(".vb"):
                out.append(n)          # never stage a ROM
            elif n.endswith(_ORIG_SUFFIX) or n.endswith(".AppImage"):
                out.append(n)
        return out

    shutil.copytree(template, proj, ignore=ignore)
    log_line(log, f"Staged template project at {proj}")


VIP_DECL_ANCHOR = '#include "vip_phase.h"'
VIP_DECL_BLOCK = """#include "vip_phase.h"
/* === VBRecomp Tools: RA frame hook (begin) === */
#ifdef VB_RA
void vb_ra_frame_tick(void);
#endif
/* === VBRecomp Tools: RA frame hook (end) === */"""

VIP_CALL_ANCHOR = "vb_mod_runtime_frame_tick_c();"
VIP_CALL_BLOCK = """vb_mod_runtime_frame_tick_c();
#ifdef VB_RA
                vb_ra_frame_tick();
#endif"""


def _patch_vip_ra(proj: Path, log=None) -> None:
    """Invoke vb_ra_frame_tick() at every guest GAME_START frame
    tick in the STAGED vip.c, beside upstream's own mod frame
    tick. Marker-guarded; RA off restores the pristine file."""
    vip = proj / "vbrecomp" / "runtime" / "src" / "vip.c"
    _restore_orig(vip)   # always patch from pristine: a stale patch
    _backup_once(vip)    # from an older engine must not persist
    text = vip.read_text()
    if "vb_ra_frame_tick" not in text:
        if VIP_DECL_ANCHOR not in text or VIP_CALL_ANCHOR not in text:
            raise VbError(
                "RA patch: frame-tick anchors not found in vip.c "
                "(upstream layout changed?)")
        text = text.replace(VIP_DECL_ANCHOR, VIP_DECL_BLOCK, 1)
        text = text.replace(VIP_CALL_ANCHOR, VIP_CALL_BLOCK, 1)
    vip.write_text(text)
    log_line(log, "RA: vip.c frame tick patched (staged copy)")


MAIN_BIND_ANCHOR = "s_runtime_ui = recomp_runtime_ui_create_standard(&runtime_ui_config);"
# NOTE: the declaration must come from vb_ra.h at file scope. An
# inline `extern void vb_ra_bind_ui(void*);` inside the function
# body declares a C++-linkage function in this C++ file and the
# link fails (the Genesis main was C, where that pattern worked).
MAIN_BIND_BLOCK = MAIN_BIND_ANCHOR + """
#ifdef VB_RA
    vb_ra_bind_ui(s_runtime_ui);
#endif"""
MAIN_INCLUDE_ANCHOR = '#include "fntrace.h"'
MAIN_INCLUDE_BLOCK = MAIN_INCLUDE_ANCHOR + """
#ifdef VB_RA
#include "vb_ra.h"
#endif"""


def _patch_main_ra(proj: Path, log=None) -> None:
    """Hand the RA module the host's runtime UI (its toast channel)
    right after the host creates it in the STAGED main.cpp."""
    main_cpp = proj / "vbrecomp" / "runtime" / "src" / "main.cpp"
    _restore_orig(main_cpp)   # always patch from pristine (see vip.c)
    _backup_once(main_cpp)
    text = main_cpp.read_text()
    if "vb_ra_bind_ui" not in text:
        if MAIN_BIND_ANCHOR not in text:
            raise VbError(
                "RA patch: runtime-UI creation anchor not found in "
                "main.cpp (upstream layout changed?)")
        if MAIN_INCLUDE_ANCHOR not in text:
            raise VbError(
                "RA patch: include-list anchor not found in "
                "main.cpp (upstream layout changed?)")
        text = text.replace(MAIN_INCLUDE_ANCHOR, MAIN_INCLUDE_BLOCK, 1)
        text = text.replace(MAIN_BIND_ANCHOR, MAIN_BIND_BLOCK, 1)
    main_cpp.write_text(text)
    log_line(log, "RA: main.cpp UI bind patched (staged copy)")


RA_CMAKE_BEGIN = "# === VBRecomp Tools: RetroAchievements (begin) ==="
RA_CMAKE_END = "# === VBRecomp Tools: RetroAchievements (end) ==="


def _ra_cmake_block(popups: bool) -> str:
    lines = [
        RA_CMAKE_BEGIN,
        'if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/ra/vb_ra.cpp")',
        '  file(GLOB_RECURSE VB_RA_RCHEEVOS_SOURCES',
        '       "${CMAKE_CURRENT_SOURCE_DIR}/ra/ra-rcheevos/src/*.c")',
        '  list(FILTER VB_RA_RCHEEVOS_SOURCES EXCLUDE REGEX "rc_libretro\\\\.c$")',
        '  list(FILTER VB_RA_RCHEEVOS_SOURCES EXCLUDE REGEX "rc_client_raintegration\\\\.c$")',
        '  target_sources(vb-runtime PRIVATE',
        '    "${CMAKE_CURRENT_SOURCE_DIR}/ra/vb_ra.cpp"',
        '    "${CMAKE_CURRENT_SOURCE_DIR}/ra/ra_http.c"',
        '    ${VB_RA_RCHEEVOS_SOURCES})',
        '  target_include_directories(vb-runtime PRIVATE',
        '    "${CMAKE_CURRENT_SOURCE_DIR}/ra"',
        '    "${CMAKE_CURRENT_SOURCE_DIR}/ra/ra-rcheevos/include"',
        '    "${CMAKE_CURRENT_SOURCE_DIR}/vbrecomp/runtime/include"',
        '    "${CMAKE_CURRENT_SOURCE_DIR}/recomp-ui/src")',
        '  target_compile_definitions(vb-runtime PRIVATE VB_RA=1)',
    ]
    if not popups:
        lines.append(
            '  target_compile_definitions(vb-runtime PRIVATE RA_DISABLE_POPUPS=1)')
    lines += [
        '  find_package(Threads REQUIRED)',
        '  target_link_libraries(vb-runtime PRIVATE Threads::Threads)',
        '  if(WIN32)',
        '    target_link_libraries(vb-runtime PRIVATE winhttp)',
        '  else()',
        '    find_package(OpenSSL REQUIRED)',
        '    target_link_libraries(vb-runtime PRIVATE OpenSSL::SSL OpenSSL::Crypto m)',
        '  endif()',
        'endif()',
        RA_CMAKE_END,
        '',
    ]
    return "\n".join(lines)


def _apply_ra(proj: Path, popups: bool, log=None) -> None:
    ra_dir = proj / "ra"
    if ra_dir.exists():
        shutil.rmtree(ra_dir)
    shutil.copytree(BUNDLE_RA, ra_dir)
    _patch_vip_ra(proj, log)
    _patch_main_ra(proj, log)
    cmakelists = proj / "CMakeLists.txt"
    text = cmakelists.read_text()
    if RA_CMAKE_BEGIN in text:
        text = re.sub(re.escape(RA_CMAKE_BEGIN) + r".*?" +
                      re.escape(RA_CMAKE_END) + r"\n?", "",
                      text, flags=re.S)
    cmakelists.write_text(text.rstrip() + "\n\n" +
                          _ra_cmake_block(popups))
    log_line(log, "RA: module staged into the project")


def _strip_ra(proj: Path) -> None:
    ra_dir = proj / "ra"
    if ra_dir.exists():
        shutil.rmtree(ra_dir)
    _restore_orig(proj / "vbrecomp" / "runtime" / "src" / "vip.c")
    _restore_orig(proj / "vbrecomp" / "runtime" / "src" / "main.cpp")
    cmakelists = proj / "CMakeLists.txt"
    if cmakelists.is_file():
        text = cmakelists.read_text()
        if RA_CMAKE_BEGIN in text:
            text = re.sub(re.escape(RA_CMAKE_BEGIN) + r".*?" +
                          re.escape(RA_CMAKE_END) + r"\n?", "",
                          text, flags=re.S)
            cmakelists.write_text(text)


def scaffold_project(job: dict, probe: dict, template: Path,
                     log=None) -> Path:
    prof = PROFILES[probe["profile"]]
    out_dir = Path(job["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    proj = out_dir / f"{prof['camel']}-src"
    ra_cfg = job.get("ra", {}) or {}
    ra_on = bool(ra_cfg.get("enabled", True))
    fingerprint = _template_fingerprint(template)
    want = {"profile": prof["id"], "crc32": probe["crc32"],
            "ra": ra_on, "popups": bool(ra_cfg.get("popups", True)),
            "fingerprint": fingerprint}
    have = None
    marker = proj / _STAGE_MARKER
    if marker.is_file():
        try:
            have = json.loads(marker.read_text())
        except Exception:
            have = None
    if have != want or not (proj / "CMakeLists.txt").is_file():
        _stage_template(template, proj, prof, log)
    else:
        log_line(log, f"Reusing staged project at {proj}")

    # Cover art for the package (artwork/<title>/front.png).
    png, desc = fetch_cover(probe["title"], "vb", log)
    if png:
        art_dir = ART_CACHE / "package-art"
        art_dir.mkdir(parents=True, exist_ok=True)
        (art_dir / f"{prof['camel']}.png").write_bytes(png)
        log_line(log, f"Cover art: {desc}")
    else:
        log_line(log, "Cover art: none found online for this title")

    if ra_on:
        _apply_ra(proj, bool(ra_cfg.get("popups", True)), log)
    else:
        _strip_ra(proj)
    marker.write_text(json.dumps(want, indent=1))
    return proj


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _cmake_env() -> dict:
    """Subprocess environment for CMake/Ninja. PATH is rebuilt to
    exclude the app tools dir: SDL2's MinGW files living there have
    poisoned Linux SDL2 discovery in sibling tools."""
    env = dict(os.environ)
    parts = []
    tools = str(TOOLS_DIR)
    for part in env.get("PATH", "").split(os.pathsep):
        if part and os.path.abspath(part) != os.path.abspath(tools):
            parts.append(part)
    env["PATH"] = os.pathsep.join(parts)
    return env


def _sdl2_cmake_dir() -> str:
    candidates = sorted(glob.glob("/usr/lib/*/cmake/SDL2")) + \
        sorted(glob.glob("/usr/lib/cmake/SDL2")) + \
        sorted(glob.glob("/usr/local/lib/cmake/SDL2"))
    for c in candidates:
        if (Path(c) / "sdl2-config.cmake").is_file() or \
                (Path(c) / "SDL2Config.cmake").is_file():
            return c
    return ""


def _write_mingw_toolchain(proj: Path) -> Path:
    tc = proj / "tc-mingw.cmake"
    sdl2_cmake = (WIN_SDL2_DIR / "x86_64-w64-mingw32" / "lib" / "cmake"
                  / "SDL2").as_posix()
    lines = [
        "set(CMAKE_SYSTEM_NAME Windows)",
        "set(CMAKE_SYSTEM_PROCESSOR x86_64)",
        "set(CMAKE_C_COMPILER x86_64-w64-mingw32-gcc)",
        "set(CMAKE_CXX_COMPILER x86_64-w64-mingw32-g++)",
        "set(CMAKE_RC_COMPILER x86_64-w64-mingw32-windres)",
        f"set(CMAKE_FIND_ROOT_PATH {TOOLS_DIR.as_posix()})",
        "set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)",
        "set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)",
        "set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)",
        f'set(SDL2_DIR "{sdl2_cmake}")',
    ]
    tc.write_text("\n".join(lines) + "\n")
    return tc


def build_target(job: dict, proj: Path, target_os: str, log=None) -> Path:
    prof = PROFILES[job["_profile_id"]]
    jobs = str(job.get("jobs", 2))
    env = _cmake_env()
    targets = [prof["target"]]
    if prof.get("mods_target"):
        targets.append(prof["mods_target"])
    if target_os == "linux":
        bdir = proj / "build-linux"
        cmd = ["cmake", "-S", str(proj), "-B", str(bdir), "-G", "Ninja",
               "-DCMAKE_BUILD_TYPE=Release",
               "-DVBRECOMP_DEBUG_TOOLS=OFF"]
        sdl2_dir = _sdl2_cmake_dir()
        if sdl2_dir:
            cmd.append(f"-DSDL2_DIR={sdl2_dir}")
        run(cmd, env=env, log=log)
        run(["cmake", "--build", str(bdir), "--target"] + targets +
            ["-j", jobs], env=env, log=log)
        exe = bdir / "vbrecomp" / "runtime" / prof["exe"]
    else:
        ensure_win_tools(log)
        tc = _write_mingw_toolchain(proj)
        bdir = proj / "build-win"
        cmd = ["cmake", "-S", str(proj), "-B", str(bdir), "-G", "Ninja",
               f"-DCMAKE_TOOLCHAIN_FILE={tc}",
               "-DCMAKE_BUILD_TYPE=Release",
               "-DVBRECOMP_DEBUG_TOOLS=OFF"]
        run(cmd, env=env, log=log)
        run(["cmake", "--build", str(bdir), "--target"] + targets +
            ["-j", jobs], env=env, log=log)
        exe = bdir / "vbrecomp" / "runtime" / (prof["exe"] + ".exe")
    if not exe.is_file() or exe.stat().st_size < 200_000:
        raise VbError(
            f"{target_os} build finished but {exe.name} is missing or "
            f"suspiciously small - treating as failed")
    return exe


# ---------------------------------------------------------------------------
# Package
# ---------------------------------------------------------------------------

PLAY_SH = """#!/bin/sh
# Launch {title} (Virtual Boy) - native PC recompilation
# (VBRecomp Tools). The game ROM is NOT included: put your own
# dump in this folder first (see README.txt).
cd "$(dirname "$0")" || exit 1
ROM=""
for f in *.vb *.VB; do
    if [ -f "$f" ]; then ROM="$f"; break; fi
done
if [ -z "$ROM" ]; then
    echo "No game ROM found in this folder."
    echo "Copy your own dump of {title} ({rom_hint}) into this folder"
    echo "and run play.sh again - or start {exe} directly and pick"
    echo "the ROM in the launcher. The game verifies the ROM's CRC32"
    echo "(expected {crc32:08X}) before it starts."
    exit 1
fi
exec "./{exe}" --rom "$ROM"
"""

PLAY_BAT = """@echo off
rem Launch {title} (Virtual Boy) - native PC recompilation
rem (VBRecomp Tools). The game ROM is NOT included: put your own
rem dump in this folder first (see README.txt).
cd /d "%~dp0"
set ROM=
for %%f in (*.vb) do if not defined ROM set "ROM=%%f"
if not defined ROM (
    echo No game ROM found in this folder.
    echo Copy your own dump of {title} ({rom_hint}) into this folder
    echo and run play.bat again - or start {exe}.exe directly and
    echo pick the ROM in the launcher. The game verifies the ROM's
    echo CRC32 (expected {crc32:08X}) before it starts.
    pause
    exit /b 1
)
"{exe}.exe" --rom "%ROM%"
"""

PACKAGE_README = """{title} (Virtual Boy) - native PC recompilation (VBRecomp Tools)

Built with VBRecomp Tools from the player's own "{rom_name}" image
(CRC32 {crc32:08X}, SHA-256 {sha256}, {size} bytes). This package
contains NO ROM and NO BIOS - the Virtual Boy has no BIOS; the
runtime models the hardware directly.

To play:
  1. Copy your own dump of {title} into this folder (any file name
     ending in .vb). Only the qualified image ({revision}) is
     accepted - the game verifies the ROM's CRC32 at launch and
     refuses other revisions, by design: the recompiled code was
     generated from that exact image.
  2. Run:
       Linux:   ./play.sh
       Windows: play.bat
     (or run {exe} yourself and pick the ROM in the launcher, or
     pass it directly: {exe} --rom yourdump.vb)

Display: the game renders in the Virtual Boy's original red, one
eye by default (the window opens at 2x scale, 768x448; resize
freely). --stereo shows both eyes stacked. Esc opens the in-game
settings menu.
{mod_block}
Controls (keyboard):
  Left D-pad: arrow keys     Right D-pad: W/A/S/D
  A / B: X / Z               L / R: Q / E
  Start: Enter               Select: Right Shift
  Turbo (skip frame pacing): TAB
An Xbox-style controller works too (rebindable in the launcher).
{ra_block}
Credits: vbrecomp framework and the {project} game project by
Matthew Stanley (mstan), RetroPortingToolkit - MIT licensed.
Vendored components: SDL2, Dear ImGui, rcheevos (MIT,
RetroAchievements). VBRecomp Tools is an unofficial fan tool; it
is not affiliated with Nintendo. {title} is a trademark of
Nintendo.
"""

MOD_BLOCK_MT = """
Optional full-color mod: marios-tennis-full-color-0.2.2.vbmod is
upstream's experimental color package. Import it from the
launcher's Mods page and enable "Full-color renderer" to play in
color instead of the original red. It is off by default and never
modifies your ROM.
"""

MOD_BLOCK_ZR = """
Optional mod packages: zero-racers-full-color-0.2.1.vbmod
(experimental color) and zero-racers-multiplayer-0.1.0.vbmod
(the versus multiplayer feature set). Import them from the
launcher's Mods page. They are off by default and never modify
your ROM.
"""

RA_PACKAGE_BLOCK = """
RetroAchievements: enabled. retroachievements.toml sits next to the
executable with your username and Connect token; a login toast shows
at start and achievements pop up in game on unlock. Note:
RetroAchievements counts unlocks from recomp clients as Casual
(softcore) - the client is an unapproved emulator to the service,
and that cannot be changed without spoofing another client, which
this tool never does.
"""


def _mingw_runtime_dlls(exe: Path):
    """Runtime DLLs the Windows exe actually imports (from the MinGW
    installation), so the package carries exactly what it needs."""
    wanted = {"libwinpthread-1.dll", "libgcc_s_seh-1.dll",
              "libstdc++-6.dll", "libgcc_s_dw2-1.dll"}
    try:
        out = subprocess.run(
            ["x86_64-w64-mingw32-objdump", "-p", str(exe)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=60).stdout
    except Exception:
        return []
    imported = {m.group(1) for m in
                re.finditer(r"DLL Name:\s*(\S+)", out)} & wanted
    found = []
    search = [Path("/usr/x86_64-w64-mingw32/bin"),
              Path("/usr/lib/gcc/x86_64-w64-mingw32")]
    for name in sorted(imported):
        for root in search:
            hits = sorted(root.rglob(name)) if root.is_dir() else []
            if hits:
                found.append(hits[0])
                break
    return found


def package_built(job: dict, probe: dict, proj: Path, target_os: str,
                  exe: Path, log=None) -> Path:
    prof = PROFILES[probe["profile"]]
    out_dir = Path(job["output_dir"])
    ra_cfg = job.get("ra", {}) or {}
    ra_on = bool(ra_cfg.get("enabled", True))
    suffix = "Linux" if target_os == "linux" else "Windows"
    stage = out_dir / ".vb-packages" / f"{prof['camel']}-{target_os}"
    if stage.exists():
        shutil.rmtree(stage)
    payload = stage / f"{prof['title']} (Virtual Boy)"
    payload.mkdir(parents=True)
    shutil.copy2(exe, payload / exe.name)

    # The launcher's assets (box art + notice) are staged beside the
    # exe by the build's own assets target; ship them as built.
    built_assets = exe.parent / "assets"
    if built_assets.is_dir():
        shutil.copytree(built_assets, payload / "assets")
    else:
        log_line(log, "package: WARNING - no assets/ beside the built "
                      "exe; the launcher will lack its box art")

    # Upstream licenses.
    lic_dir = payload / "licenses"
    lic_dir.mkdir(exist_ok=True)
    for src, name in ((proj / "LICENSE.md", "game-MIT.txt"),
                      (proj / "vbrecomp" / "LICENSE", "vbrecomp-MIT.txt")):
        if src.is_file():
            shutil.copy2(src, lic_dir / name)

    # Built mod packages (.vbmod), when the build produced them.
    bdir = exe.parent.parent.parent  # build-<os>/ (exe nests in vbrecomp/runtime/)
    for mod_name in prof.get("mod_packages", []):
        cand = bdir / "mod-packages" / mod_name
        if cand.is_file():
            shutil.copy2(cand, payload / mod_name)
        else:
            log_line(log, f"package: note - mod package {mod_name} "
                          "was not produced by this build")

    rom_hint = "any name, keep the .vb extension"
    if target_os == "linux":
        play = payload / "play.sh"
        play.write_text(PLAY_SH.format(
            title=prof["title"], rom_hint=rom_hint,
            crc32=prof["crc32"], exe=prof["exe"]))
        play.chmod(0o755)
    else:
        (payload / "play.bat").write_text(PLAY_BAT.format(
            title=prof["title"], rom_hint=rom_hint,
            crc32=prof["crc32"], exe=prof["exe"]))
        dlls = []
        sdl2_dll = exe.parent / "SDL2.dll"
        if not sdl2_dll.is_file():
            sdl2_dll = (WIN_SDL2_DIR / "x86_64-w64-mingw32" / "bin"
                        / "SDL2.dll")
        if sdl2_dll.is_file():
            dlls.append(sdl2_dll)
        dlls += _mingw_runtime_dlls(exe)
        for dll in dlls:
            dest = payload / dll.name
            shutil.copy2(dll, dest)
            if dll.name != "SDL2.dll":
                run(["x86_64-w64-mingw32-strip", str(dest)], log=log)

    mod_block = ""
    if prof["id"] == "mariotennis":
        mod_block = MOD_BLOCK_MT
    elif prof["id"] == "zeroracers":
        mod_block = MOD_BLOCK_ZR

    ra_block = ""
    if ra_on:
        example = BUNDLE_RA / "retroachievements.toml.example"
        if example.is_file():
            shutil.copy2(example, payload / "retroachievements.toml.example")
        toml_src = ra_cfg.get("toml") or ""
        if toml_src and Path(toml_src).is_file():
            shutil.copy2(toml_src, payload / "retroachievements.toml")
            ra_block = RA_PACKAGE_BLOCK
        else:
            ra_block = ("\nRetroAchievements: compiled in. To enable, "
                        "copy retroachievements.toml.example to\n"
                        "retroachievements.toml next to the executable "
                        "and fill in your username\nand Connect token.\n")

    (payload / "README.txt").write_text(PACKAGE_README.format(
        title=prof["title"], rom_name=Path(probe["path"]).name,
        crc32=probe["crc32"], sha256=probe["sha256"],
        size=probe["size"], revision=prof["revision"],
        exe=prof["exe"], mod_block=mod_block, ra_block=ra_block,
        project=prof["template_dirname"]))

    art = ART_CACHE / "package-art" / f"{prof['camel']}.png"
    if art.is_file():
        adir = payload / "artwork" / prof["title"]
        adir.mkdir(parents=True)
        shutil.copy2(art, adir / "front.png")

    zip_path = out_dir / f"{prof['camel']}-{suffix}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(stage.rglob("*")):
            if f.is_file():
                info = zipfile.ZipInfo(
                    f.relative_to(stage).as_posix(),
                    date_time=time.localtime(f.stat().st_mtime)[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = 0o755 if (f.suffix in (".sh",)
                                 or f.name == prof["exe"]) else 0o644
                info.external_attr = (mode & 0xFFFF) << 16
                z.writestr(info, f.read_bytes())
    log_line(log, f"Packaged {zip_path} ({zip_path.stat().st_size} bytes)")
    return zip_path


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

def run_job(job: dict, log=None) -> dict:
    roms = job.get("roms") or []
    if not roms:
        raise VbError("job has no ROMs")
    targets = [t for t, on in (job.get("targets") or {}).items() if on]
    if not targets:
        targets = ["linux"]
    fw = ensure_frameworks(log)
    summary = {"packages": [], "errors": []}
    for rom in roms:
        probe = probe_rom(rom)
        name = Path(rom).name
        if probe["profile"] is None:
            reasons = "; ".join(probe["reasons"]) or "no profile"
            raise VbError(f"{name}: cannot convert - {reasons}")
        log_line(log, f"{name}: profile {probe['profile']} "
                      f"({probe['profile_title']}, CRC32 "
                      f"0x{probe['crc32']:08X})")
        job["_profile_id"] = probe["profile"]
        prof = PROFILES[probe["profile"]]
        proj = scaffold_project(job, probe,
                                fw["templates"][prof["id"]], log)
        failed_targets = []
        for target_os in targets:
            try:
                exe = build_target(job, proj, target_os, log)
            except Exception as exc:
                failed_targets.append(target_os)
                log_line(log, f"{target_os} build FAILED: {exc}")
                summary["errors"].append(f"{name} [{target_os}]: {exc}")
                continue
            zip_path = package_built(job, probe, proj, target_os, exe, log)
            entry = {"rom": rom, "target": target_os,
                     "zip": str(zip_path)}
            if failed_targets:
                entry["note"] = ("partial: failed targets: "
                                  + ", ".join(failed_targets))
            summary["packages"].append(entry)
        if len(failed_targets) == len(targets):
            raise VbError(
                f"{name}: all target builds failed "
                f"({'; '.join(summary['errors'])})")
    return summary


# ---------------------------------------------------------------------------
# Toolbox
# ---------------------------------------------------------------------------

def _check(ok, label, detail, fix=None):
    return {"ok": bool(ok), "label": label, "detail": detail, "fix": fix}


def toolbox_status() -> list:
    import importlib.util
    checks = []
    py_ok = sys.version_info >= (3, 10)
    checks.append(_check(py_ok, "Python 3.10+", sys.version.split()[0],
                         "Install Python 3.10 or newer"))
    for mod, label in (("PySide6", "PySide6 (GUI)"),
                       ("PIL", "Pillow (cover art)")):
        checks.append(_check(importlib.util.find_spec(mod) is not None,
                             label, "importable" if
                             importlib.util.find_spec(mod) else "missing",
                             "pip install -r requirements.txt"))
    for exe, label, fix in (
            ("cmake", "CMake", "sudo apt install -y cmake"),
            ("ninja", "Ninja", "sudo apt install -y ninja-build"),
            ("gcc", "GCC (host C)", "sudo apt install -y build-essential"),
            ("g++", "G++ (host C++)", "sudo apt install -y build-essential"),
            ("git", "git", "sudo apt install -y git"),
            ("curl", "curl", "sudo apt install -y curl"),
            ("x86_64-w64-mingw32-gcc", "MinGW-w64 GCC (Windows builds)",
             "sudo apt install -y gcc-mingw-w64-x86-64 g++-mingw-w64-x86-64"),
            ("x86_64-w64-mingw32-strip", "MinGW strip",
             "sudo apt install -y binutils-mingw-w64-x86-64")):
        path = shutil.which(exe)
        detail = ""
        if path:
            try:
                out = subprocess.run([exe, "--version"], text=True,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, timeout=10)
                detail = (out.stdout or "").splitlines()[0][:60]
            except Exception:
                detail = path
        checks.append(_check(path is not None, label,
                             detail or "not found", fix))
    sdl2 = subprocess.run(["pkg-config", "--modversion", "sdl2"],
                          text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)
    checks.append(_check(sdl2.returncode == 0, "SDL2 development files",
                         (sdl2.stdout or "").strip() or "pkg-config failed",
                         "sudo apt install -y libsdl2-dev"))
    checks.append(_check(Path("/usr/include/openssl/ssl.h").is_file(),
                         "OpenSSL headers (RetroAchievements)",
                         "present" if Path("/usr/include/openssl/ssl.h").is_file()
                         else "missing",
                         "sudo apt install -y libssl-dev"))
    checks.append(_check(Path("/usr/include/freetype2/freetype/freetype.h").is_file(),
                         "FreeType headers (launcher text rendering)",
                         "present" if Path("/usr/include/freetype2/freetype/freetype.h").is_file()
                         else "missing - launcher falls back to its basic font path",
                         "sudo apt install -y libfreetype-dev"))
    xcb = subprocess.run(["dpkg", "-s", "libxcb-cursor0"], text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    checks.append(_check(xcb.returncode == 0,
                         "libxcb-cursor0 (Qt GUI on minimal systems)",
                         "installed" if xcb.returncode == 0 else "missing",
                         "sudo apt install -y libxcb-cursor0"))
    checks.append(_check(provisioned_ok(),
                         "Windows support files (SDL2 MinGW kit, app-owned)",
                         "provisioned" if provisioned_ok() else
                         "downloaded automatically on the first Windows build",
                         None))
    for prof in PROFILES.values():
        template = find_template(prof)
        checks.append(_check(
            template is not None,
            f"Framework template ({prof['template_dirname']})",
            str(template) if template else
            "fetched automatically on first conversion",
            None))
    return checks


INSTALL_PACKAGES = [
    "build-essential", "cmake", "ninja-build", "git", "curl",
    "pkg-config", "libsdl2-dev", "libssl-dev", "libfreetype-dev",
    "gcc-mingw-w64-x86-64", "g++-mingw-w64-x86-64",
    "binutils-mingw-w64-x86-64", "libxcb-cursor0", "python3-venv",
]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vb-engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("toolbox")
    pp = sub.add_parser("probe")
    pp.add_argument("--rom", required=True)
    rp = sub.add_parser("run")
    rp.add_argument("--job", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "toolbox":
        for c in toolbox_status():
            mark = "OK " if c["ok"] else "MISS"
            print(f"[{mark}] {c['label']}: {c['detail']}")
        return 0
    if args.cmd == "probe":
        print(json.dumps(probe_rom(args.rom), indent=1))
        return 0
    if args.cmd == "run":
        job = json.loads(Path(args.job).read_text())
        try:
            summary = run_job(job)
        except VbError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(summary, indent=1))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

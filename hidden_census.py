#!/usr/bin/env python3
"""Census driver: probe every ROM in Duane's Virtual Boy collection
through the shipped engine probe and write the needs-review list.
Not part of the shipped app."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
import engine  # noqa: E402

ROM_DIR = Path.home() / "workspace" / "virtualboy-collection" / "roms" / "virtualboy"
OUT = Path.home() / "workspace" / "virtualboy-collection" / "needs-review-list.md"

roms = sorted(ROM_DIR.glob("*.vb"))
probes = [engine.probe_rom(str(r)) for r in roms]
qualified = [p for p in probes if p["profile"]]
review = [p for p in probes if not p["profile"]]
total_bytes = sum(p["size"] for p in probes)

lines = []
lines.append("# Virtual Boy collection - needs-review census")
lines.append("")
lines.append(f"Total files probed: {len(probes)}")
lines.append(f"Total bytes (extracted files): {total_bytes}")
lines.append("By extension: {'.vb': %d}" % len(probes))
lines.append(f"Qualified for conversion (profile match): {len(qualified)}")
for p in qualified:
    lines.append(
        f"- QUALIFIED: {p['filename']} - {p['profile_title']} "
        f"CRC32={p['crc32']:08X} size={p['size']} "
        f"header='{p['header_title']}' game_code={p['game_code']}")
lines.append("")
lines.append("## Needs review (no profile / not qualified)")
lines.append("")
lines.append(
    "vbrecomp is profile-based: only Mario's Tennis (CRC32 7CE7460D, "
    "512 KiB) and Zero Racers (CRC32 71553796, 1 MiB, an unreleased "
    "prototype not present in this collection) have bundled profiles. "
    "Everything else is needs-review until an upstream per-game "
    "integration exists.")
lines.append("")
for p in review:
    reason = "; ".join(p["reasons"]) or "no profile"
    lines.append(
        f"- {p['filename']} - header title: {p['header_title'] or '?'} | "
        f"size={p['size']} CRC32={p['crc32']:08X} "
        f"game_code={p['game_code'] or '?'} maker={p['maker_code'] or '?'} | "
        f"{reason}")
lines.append("")
OUT.write_text("\n".join(lines))
print(f"probed {len(probes)}: {len(qualified)} qualified, "
      f"{len(review)} needs-review -> {OUT}")

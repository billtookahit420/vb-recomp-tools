#!/usr/bin/env python3
"""Offscreen GUI smoke + churn stress for VBRecomp Tools.

Rapid selection changes while REAL cover fetches are in flight
(the QThread-lifetime crash pattern), plus add/remove/clear
churn. Exits non-zero on any exception or hang.
Usage: hidden_gui_stress.py <app_dir> <rom_dir>
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
app_dir, rom_dir = sys.argv[1], sys.argv[2]
sys.path.insert(0, app_dir)

import main as app_main  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)
w = app_main.MainWindow()
w.show()

roms = sorted(str(p) for p in Path(rom_dir).glob("*.vb"))
assert len(roms) >= 8, roms
print(f"stress: {len(roms)} ROMs", flush=True)

# Add all ROMs one at a time, selecting each (starts cover fetches).
for p in roms:
    w._add_path(p)
    w.table.selectRow(w.table.rowCount() - 1)
    app.processEvents()
print(f"stress: added, rows={w.table.rowCount()}", flush=True)

# Rapid selection churn, several sweeps both directions.
for _ in range(4):
    for r in range(w.table.rowCount()):
        w.table.selectRow(r)
        app.processEvents()
    for r in range(w.table.rowCount() - 1, -1, -1):
        w.table.selectRow(r)
        app.processEvents()
print("stress: churn sweeps done", flush=True)

# Remove + clear + re-add while fetches may still be in flight.
w.table.selectRow(0)
app.processEvents()
w.remove_selected()
w.clear_all()
for p in roms[:3]:
    w._add_path(p)
    w.table.selectRow(w.table.rowCount() - 1)
    app.processEvents()
print("stress: remove/clear/re-add done", flush=True)

# Drain: wait until all cover workers finish (bounded).
deadline = time.time() + 60
while time.time() < deadline:
    app.processEvents()
    if not w.cover_workers and not w.cover_inflight:
        break
    time.sleep(0.05)
print(f"stress: drained workers={len(w.cover_workers)} "
      f"inflight={len(w.cover_inflight)} covers={len(w.covers)}",
      flush=True)

# Toolbox tab refresh path (async worker).
w.refresh_toolbox()
deadline = time.time() + 30
while time.time() < deadline:
    app.processEvents()
    if w.toolbox_table.rowCount() > 0:
        break
    time.sleep(0.05)
print(f"stress: toolbox rows={w.toolbox_table.rowCount()}", flush=True)
assert w.toolbox_table.rowCount() > 0

print("GUI STRESS OK", flush=True)

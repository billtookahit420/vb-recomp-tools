#!/usr/bin/env python3
"""VBRecomp Tools - GUI.

Convert Virtual Boy ROM images from your own collection into
native PC executables (Linux and Windows) using the vbrecomp
framework (Matthew Stanley / RetroPortingToolkit). The framework
is profile-based: a ROM converts only when a bundled per-game
integration profile matches its exact image (CRC32). Today the
two qualified profiles are Mario's Tennis and Zero Racers.
RetroAchievements is wired through a vendored rcheevos module the
tool adds itself; Mario's Tennis has no netplay (Zero Racers'
upstream netplay is part of that game's own profile).
"""
import json
import subprocess
import sys
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import engine  # noqa: E402

from PySide6.QtCore import Qt, QThread, Signal  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QFileDialog, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

HELP_TEXT = """VBRecomp Tools - help

WHAT THIS IS
A converter that turns Virtual Boy cartridge images from your own
collection into native PC executables (Linux and, optionally,
Windows) using the vbrecomp framework by Matthew Stanley (mstan),
RetroPortingToolkit. The Virtual Boy library is tiny (22 retail
games), and the conversion model reflects how the framework
works: per-game integrations, not a generic converter.

HOW CONVERSION WORKS - AND ITS LIMIT
This framework is PROFILE-BASED (like the N64, Genesis and SMS/GG
tools): a game converts only when a bundled per-game integration
profile matches the ROM's exact image (CRC32 + size). The profile
carries generated code and host integration that cannot be
derived from the ROM alone. The two profiles bundled today are
upstream's own shipped titles:
  - Mario's Tennis (Japan, USA) - CRC32 7CE7460D
  - Zero Racers (prototype) - CRC32 71553796
Other revisions of those games are different code and are
refused, with the reason shown in the details pane. Every other
game is listed as 'needs review': it will convert the day an
upstream integration exists for it (the app tracks upstream).
Zero Racers is an unreleased prototype game; if its ROM is not
in your collection, that profile simply never matches.

PLAYING A PACKAGE
Each package zip contains the executable, the launcher assets
(box art), a play script, a README, and (when found) cover art.
The ROM is NOT included: copy your own dump into the folder and
run play.sh (Linux) or play.bat (Windows), or start the game and
pick the ROM in the launcher. The game verifies the ROM's CRC32
before starting. Display is the original Virtual Boy red, one
eye by default; --stereo shows both eyes stacked; Esc opens the
in-game settings menu. Mario's Tennis packages also include
upstream's experimental full-color mod (.vbmod) - import it on
the launcher's Mods page if you want color; it is off by
default.

KEYBOARD CONTROLS
Left D-pad: arrow keys. Right D-pad: W/A/S/D. A/B: X/Z.
L/R: Q/E. Start: Enter. Select: Right Shift. TAB: turbo.
An Xbox-style controller works too (rebindable in the launcher).

BIOS
None. The Virtual Boy has no BIOS file; the runtime models the
hardware directly, and this tool never ships one. (Upstream's
runtime always opens a localhost-only TCP debug server on
127.0.0.1:4390 - their own releases ship it too; --port changes
it. Production builds here strip the trace instrumentation.)

RETROACHIEVEMENTS
Wired in by this tool (upstream ships none): a vendored rcheevos
module hooks the runtime's per-frame tick, hashes the loaded ROM
the official way (whole-file MD5), reads the game's WRAM and
cartridge RAM for achievement logic, logs in with the Connect
token from retroachievements.toml next to the game, and shows a
login toast plus achievement pop-ups through the game's own
runtime UI. Leave the box ticked and point it at your filled
retroachievements.toml; packages carry your file byte-identical.
Note: RetroAchievements counts unlocks from recomp clients as
Casual (softcore) - the client is an unapproved emulator to the
service, and the tool never spoofs another client to change
that. Verification labels: the module builds into every package;
the hash + login attempt + failure toast were exercised on the
developer's machine with Mario's Tennis. A successful login and
a real unlock need a live route to retroachievements.org and are
verified on the user's machine.

NETPLAY
Mario's Tennis: none. It is a single-system game and upstream
does not wire netplay for it, so the tool offers no netplay
toggle rather than a fake one. Zero Racers is different: its
upstream project ships a versus netplay host (local
split-screen, LAN / Direct IP and online rooms) as part of that
game's own integration, enabled via its multiplayer mod package.
That profile is untested by this tool's developer (the ROM is
not in his test collection); nothing about it is claimed here
beyond what upstream documents.

DISPLAY
The Virtual Boy's stereo pair renders as a single eye on a
normal monitor (2x scale, 768x448 window, resizable). True
stereoscopy needs a VR/stereo display path the framework does
not provide; --stereo stacks both eyes in one window.

COVER ART
Fetched from the libretro-thumbnails project
(Nintendo_-_Virtual_Boy Named_Boxarts) and bundled into the
package under artwork/. The fetcher tries name variants and a
fuzzy catalog match. The launcher's own box art ships with each
game project and is included in packages as built.

CREDITS
vbrecomp framework and the MarioTennisVirtualBoyRecomp /
ZeroRacersVirtualBoyRecomp game projects by Matthew Stanley
(mstan), RetroPortingToolkit - MIT licensed. Vendored
components: SDL2, Dear ImGui, rcheevos (MIT, RetroAchievements).
VBRecomp Tools is an unofficial fan tool, not affiliated with
Nintendo."""

COLUMNS = ["Title", "Game code", "Maker", "Profile", "ROM", "Status"]


class CoverWorker(QThread):
    """Fetches cover thumbnails one title at a time. Kept alive by
    the window (a set of running workers) until finished - letting
    the last Python reference drop mid-flight destroyed the
    QThread and could take the whole GUI down (a crash class
    shared with the Genesis build of this GUI)."""
    done = Signal(object, object, object)   # key, png bytes, error text

    def __init__(self, key, system: str, title: str):
        super().__init__()
        self.key = key
        self.system = system
        self.title = title

    def run(self):
        png, err = None, ""
        try:
            png, err = engine.fetch_cover(self.title, self.system)
        except Exception as exc:            # never kill the GUI
            err = str(exc)
        self.done.emit(self.key, png, err)


class ConvertWorker(QThread):
    log = Signal(str)
    finished_job = Signal(object)

    def __init__(self, job: dict):
        super().__init__()
        self.job = job

    def run(self):
        try:
            summary = engine.run_job(
                self.job, log=lambda m: self.log.emit(str(m)))
            self.finished_job.emit({"ok": True, "summary": summary})
        except Exception as exc:
            self.finished_job.emit({"ok": False, "error": str(exc)})


class ToolboxWorker(QThread):
    done = Signal(object)

    def run(self):
        try:
            self.done.emit(engine.toolbox_status())
        except Exception as exc:
            self.done.emit([{"ok": False, "label": "toolbox",
                             "detail": str(exc), "fix": None}])


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VBRecomp Tools")
        self.resize(980, 680)
        self.probes: dict[str, dict] = {}
        self.order: list[str] = []
        self.covers: dict[str, bytes] = {}
        self.cover_inflight: set = set()
        self.cover_workers: set = set()
        self.worker = None

        tabs = QTabWidget()
        tabs.addTab(self._build_convert_tab(), "Convert")
        tabs.addTab(self._build_toolbox_tab(), "Toolbox")
        help_edit = QPlainTextEdit(HELP_TEXT)
        help_edit.setReadOnly(True)
        tabs.addTab(help_edit, "Help")
        self.setCentralWidget(tabs)

    # -- Convert tab -------------------------------------------------
    def _build_convert_tab(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)

        btn_row = QHBoxLayout()
        self.add_btn = QPushButton("Add ROMs…")
        self.add_btn.clicked.connect(self.add_roms)
        self.add_folder_btn = QPushButton("Add folder…")
        self.add_folder_btn.clicked.connect(self.add_folder)
        self.remove_btn = QPushButton("Remove selected")
        self.remove_btn.clicked.connect(self.remove_selected)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_all)
        for b in (self.add_btn, self.add_folder_btn, self.remove_btn,
                  self.clear_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        splitter = QSplitter(Qt.Horizontal)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        splitter.addWidget(self.table)

        side = QWidget()
        side_layout = QVBoxLayout(side)
        self.cover_label = QLabel("No cover")
        self.cover_label.setAlignment(Qt.AlignCenter)
        self.cover_label.setMinimumSize(180, 220)
        side_layout.addWidget(self.cover_label)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        side_layout.addWidget(self.details)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

        opt = QHBoxLayout()
        self.linux_cb = QCheckBox("Linux")
        self.linux_cb.setChecked(True)
        self.win_cb = QCheckBox("Windows")
        self.win_cb.setChecked(True)
        self.ra_cb = QCheckBox("RetroAchievements")
        self.ra_cb.setChecked(True)
        self.popups_cb = QCheckBox("Achievement pop-ups")
        self.popups_cb.setChecked(True)
        self.ra_cb.toggled.connect(self.popups_cb.setEnabled)
        opt.addWidget(self.linux_cb)
        opt.addWidget(self.win_cb)
        opt.addWidget(self.ra_cb)
        opt.addWidget(self.popups_cb)
        opt.addWidget(QLabel("Jobs"))
        self.jobs_spin = QSpinBox()
        self.jobs_spin.setRange(1, 4)
        self.jobs_spin.setValue(2)
        opt.addWidget(self.jobs_spin)
        opt.addStretch(1)
        root.addLayout(opt)

        toml_row = QHBoxLayout()
        toml_row.addWidget(QLabel("retroachievements.toml:"))
        self.toml_label = QLabel(self._default_toml() or "(none chosen)")
        toml_row.addWidget(self.toml_label, 1)
        toml_btn = QPushButton("Choose…")
        toml_btn.clicked.connect(self.choose_toml)
        toml_row.addWidget(toml_btn)
        root.addLayout(toml_row)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output folder:"))
        self.out_label = QLabel(str(Path.home() / "vb-output"))
        out_row.addWidget(self.out_label, 1)
        out_btn = QPushButton("Choose…")
        out_btn.clicked.connect(self.choose_output)
        out_row.addWidget(out_btn)
        root.addLayout(out_row)

        run_row = QHBoxLayout()
        self.convert_btn = QPushButton("Convert")
        self.convert_btn.clicked.connect(self.start_convert)
        run_row.addWidget(self.convert_btn)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        run_row.addWidget(self.progress, 1)
        root.addLayout(run_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        root.addWidget(self.log_view, 1)
        return w

    def _default_toml(self) -> str:
        cand = Path.home() / "workspace" / "retroachievements-hardcore.toml"
        return str(cand) if cand.is_file() else ""

    # -- Toolbox tab ---------------------------------------------------
    def _build_toolbox_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self.toolbox_table = QTableWidget(0, 3)
        self.toolbox_table.setHorizontalHeaderLabels(
            ["Check", "Status", "Detail / fix"])
        layout.addWidget(self.toolbox_table, 1)
        row = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_toolbox)
        row.addWidget(refresh)
        install = QPushButton("Install missing (apt)")
        install.clicked.connect(self.install_missing)
        row.addWidget(install)
        row.addStretch(1)
        layout.addLayout(row)
        self.refresh_toolbox()
        return w

    def refresh_toolbox(self):
        worker = ToolboxWorker(self)

        def apply(checks):
            self.toolbox_table.setRowCount(len(checks))
            for i, c in enumerate(checks):
                self.toolbox_table.setItem(
                    i, 0, QTableWidgetItem(c["label"]))
                self.toolbox_table.setItem(
                    i, 1, QTableWidgetItem("OK" if c["ok"] else "MISSING"))
                detail = c["detail"] or ""
                if not c["ok"] and c.get("fix"):
                    detail = (detail + "  |  fix: " + c["fix"]).strip(" |")
                self.toolbox_table.setItem(i, 2, QTableWidgetItem(detail))
            self.toolbox_table.resizeColumnsToContents()

        worker.done.connect(apply)
        worker.start()
        self._toolbox_worker = worker

    def install_missing(self):
        pkgs = " ".join(engine.INSTALL_PACKAGES)
        cmd = f"sudo apt-get install -y {pkgs}"
        QMessageBox.information(
            self, "Install missing dependencies",
            "Run this in a terminal:\n\n" + cmd +
            "\n\nThen press Refresh. (Windows support files download "
            "automatically on the first Windows build.)")

    # -- ROM list ------------------------------------------------------
    def add_roms(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Choose Virtual Boy ROMs", "",
            "Virtual Boy images (*.vb);;All files (*)")
        for f in files:
            self._add_path(f)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose a folder of ROMs")
        if not folder:
            return
        for f in sorted(Path(folder).rglob("*.vb")):
            self._add_path(str(f))

    def _add_path(self, path: str):
        if path in self.probes:
            return
        try:
            probe = engine.probe_rom(path)
        except Exception as exc:
            probe = {"path": path, "title": Path(path).stem,
                     "system_label": "?", "game_code": "",
                     "profile": None, "ok": False, "needs_review": True,
                     "reasons": [str(exc)], "info": []}
        self.probes[path] = probe
        self.order.append(path)
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._fill_row(row, path, probe)
        self.table.resizeColumnsToContents()

    def _fill_row(self, row: int, path: str, probe: dict):
        profile = probe.get("profile_title") or "-"
        status = "Convertible" if probe.get("profile") else "Needs review"
        maker = probe.get("maker") or probe.get("maker_code") or "-"
        values = [probe.get("title", Path(path).stem),
                  probe.get("game_code") or "-",
                  maker,
                  profile,
                  Path(path).name,
                  status]
        for col, v in enumerate(values):
            item = QTableWidgetItem(str(v))
            item.setData(Qt.UserRole, path)
            self.table.setItem(row, col, item)

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedItems()},
                      reverse=True)
        for row in rows:
            item = self.table.item(row, 0)
            path = item.data(Qt.UserRole) if item else None
            self.table.removeRow(row)
            if path in self.probes:
                del self.probes[path]
                self.order.remove(path)

    def clear_all(self):
        self.table.setRowCount(0)
        self.probes.clear()
        self.order.clear()
        self.details.clear()
        self.cover_label.setText("No cover")
        self.cover_label.setPixmap(QPixmap())

    # -- Details + cover ------------------------------------------------
    def selection_changed(self):
        items = self.table.selectedItems()
        if not items:
            return
        path = items[0].data(Qt.UserRole)
        probe = self.probes.get(path)
        if not probe:
            return
        lines = [f"Title: {probe.get('title')}",
                 f"File: {path}",
                 f"System: {probe.get('system_label')}"]
        if probe.get("header_title"):
            lines.append(f"Header title: {probe['header_title']}")
        if probe.get("game_code"):
            lines.append(f"Game code: {probe['game_code']}")
        if probe.get("maker_code"):
            maker = probe.get("maker") or "unknown maker"
            lines.append(f"Maker code: {probe['maker_code']} ({maker})")
        if probe.get("version") is not None:
            lines.append(f"ROM version: {probe['version']}")
        lines.append(f"Size: {probe.get('size')} bytes")
        lines.append(f"CRC32: {probe.get('crc32', 0):08X}")
        lines.append(f"MD5: {probe.get('md5', '')}")
        lines.append(f"SHA-256: {probe.get('sha256', '')}")
        if probe.get("profile"):
            lines.append(f"Profile: {probe['profile_title']} "
                         f"(bundled - convertible)")
        for r in probe.get("reasons", []):
            lines.append(f"Needs review: {r}")
        for i in probe.get("info", []):
            lines.append(f"Note: {i}")
        self.details.setPlainText("\n".join(lines))
        self._request_cover(probe)

    def _request_cover(self, probe: dict):
        key = (probe.get("system"), probe.get("title"))
        png = self.covers.get(key)
        if png:
            self._show_cover(png)
            return
        self.cover_label.setText("Fetching cover…")
        self.cover_label.setPixmap(QPixmap())
        if key in self.cover_inflight:
            return
        self.cover_inflight.add(key)
        worker = CoverWorker(key, probe.get("system", "vb"),
                             probe.get("title", ""))
        worker.done.connect(self._cover_done)
        # The window holds the only strong reference until the
        # worker finishes; dropping it earlier can destroy the
        # QThread mid-flight and crash the whole GUI (the Genesis
        # tool's churn bug - fixed there, kept fixed here).
        self.cover_workers.add(worker)
        worker.finished.connect(lambda w=worker: self._cover_reap(w))
        worker.start()

    def _cover_reap(self, worker):
        self.cover_workers.discard(worker)

    def _cover_done(self, key, png, err):
        self.cover_inflight.discard(key)
        if png:
            self.covers[key] = png
        items = self.table.selectedItems()
        if not items:
            return
        probe = self.probes.get(items[0].data(Qt.UserRole))
        if not probe:
            return
        if (probe.get("system"), probe.get("title")) != key:
            return                      # stale: selection moved on
        if png:
            self._show_cover(png)
        else:
            self.cover_label.setPixmap(QPixmap())
            self.cover_label.setText("No cover found")

    def _show_cover(self, png: bytes):
        pix = QPixmap()
        if pix.loadFromData(png):
            self.cover_label.setText("")
            self.cover_label.setPixmap(pix.scaled(
                220, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # -- Convert ---------------------------------------------------------
    def choose_toml(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Choose retroachievements.toml", "",
            "TOML files (*.toml);;All files (*)")
        if f:
            self.toml_label.setText(f)

    def choose_output(self):
        d = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if d:
            self.out_label.setText(d)

    def start_convert(self):
        convertible = [p for p in self.order
                       if self.probes[p].get("profile")]
        if not convertible:
            QMessageBox.warning(
                self, "Nothing to convert",
                "No selected ROM matches a bundled integration "
                "profile. The details pane explains each ROM's "
                "status; only Mario's Tennis and Zero Racers "
                "convert today.")
            return
        skipped = len(self.order) - len(convertible)
        targets = {}
        if self.linux_cb.isChecked():
            targets["linux"] = True
        if self.win_cb.isChecked():
            targets["windows"] = True
        if not targets:
            QMessageBox.warning(self, "No target",
                                "Tick at least one of Linux / Windows.")
            return
        toml = self.toml_label.text()
        job = {
            "roms": convertible,
            "output_dir": self.out_label.text(),
            "ra": {"enabled": self.ra_cb.isChecked(),
                   "popups": self.popups_cb.isChecked(),
                   "toml": toml if toml and Path(toml).is_file() else ""},
            "targets": targets,
            "jobs": self.jobs_spin.value(),
            "force": False,
        }
        if skipped:
            self.log(f"Skipping {skipped} ROM(s) with no bundled "
                     "profile (needs review).")
        self.convert_btn.setEnabled(False)
        self.progress.show()
        self.worker = ConvertWorker(job)
        self.worker.log.connect(self.log)
        self.worker.finished_job.connect(self.convert_done)
        self.worker.start()

    def convert_done(self, result: dict):
        self.progress.hide()
        self.convert_btn.setEnabled(True)
        if result.get("ok"):
            summary = result["summary"]
            for pkg in summary.get("packages", []):
                note = pkg.get("note", "")
                self.log(f"Package: {pkg['zip']}"
                         + (f" ({note})" if note else ""))
            for err in summary.get("errors", []):
                self.log(f"Error: {err}")
            QMessageBox.information(
                self, "Conversion finished",
                f"Produced {len(summary.get('packages', []))} "
                f"package(s) in {self.out_label.text()}")
        else:
            self.log("FAILED: " + result.get("error", "unknown error"))
            QMessageBox.critical(self, "Conversion failed",
                                 result.get("error", "unknown error"))

    def log(self, msg: str):
        self.log_view.appendPlainText(msg)


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

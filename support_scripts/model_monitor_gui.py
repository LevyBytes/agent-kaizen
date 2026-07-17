#!/usr/bin/env python3
"""Agent Kaizen backend monitor -- native desktop window (PySide6).

1-click via ``model-monitor.cmd`` (pythonw, no console). The live window provides a GPU header
(temp / fan / util / VRAM bar / power), a "Models running now" panel, and a "Recent Kaizen model
calls" feed. It reuses the B6 data layer (``kaizen_components.model_monitor.collect``), so the
window and ``python kaizen.py B6`` share one observation path.

Automatic polling is read-only and never runs inference: it uses nvidia-smi, Ollama ``/api/ps``,
a torch-free config reflection, and a DB read. The explicit emergency-stop action can unload models
and terminate the displayed GPU processes after user confirmation.

Run:
    pythonw model_monitor_gui.py             # the window (what model-monitor.cmd launches)
    python  model_monitor_gui.py --once      # one JSON snapshot, no Qt (headless self-test)
    python  model_monitor_gui.py --interval 2.0 --limit 8
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

# support_scripts/ is one level under the repo root; put the root on sys.path so the shared B6 data
# layer imports cleanly whether launched by pythonw (detached) or `python -m`.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from kaizen_components.model_monitor import collect, stop_gpu_models  # noqa: E402


class Source:
    """Thin read-only wrapper over the B6 aggregator. The monitor never loads models -- it observes
    what is already on the GPU (any project) + Ollama, so ``snapshot()`` is pure observation."""

    def __init__(self, limit: int = 8) -> None:
        self.limit = limit

    def snapshot(self) -> dict:
        """Return one read-only B6 snapshot without probing model devices."""
        return collect(SimpleNamespace(limit=self.limit, probe=False))


# --------------------------------------------------------------------------------- headless paths

def _run_once(source: Source) -> int:
    """Print one indented JSON snapshot and return success for headless verification."""
    print(json.dumps(source.snapshot(), indent=2, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------------------- Qt window

def _build_gui(source: Source, interval_ms: int):
    """Lazily build/start the PySide6 poller and return its application and window."""
    from PySide6.QtCore import QThread, Signal
    from PySide6.QtGui import QColor, QFont, QPalette
    from PySide6.QtWidgets import (
        QApplication, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit,
        QProgressBar, QPushButton, QVBoxLayout, QWidget,
    )

    class StopWorker(QThread):
        """Runs the emergency stop off the UI thread (HTTP unloads + taskkills), then reports."""
        done = Signal(dict)

        def run(self) -> None:
            try:
                self.done.emit(stop_gpu_models(kill_processes=True))
            except Exception as exc:  # noqa: BLE001 -- never let the emergency path crash the UI
                self.done.emit({"status": "ERROR", "errors": [f"{type(exc).__name__}: {exc}"]})

    class Poller(QThread):
        snap = Signal(dict)
        fail = Signal(str)  # a read NEVER fails silently -> surfaced to the header, never a frozen UI

        def __init__(self, src: Source, every_ms: int) -> None:
            super().__init__()
            self.src, self.every_ms, self._run = src, every_ms, True

        def run(self) -> None:
            while self._run:
                try:
                    self.snap.emit(self.src.snapshot())
                except Exception as exc:  # noqa: BLE001 -- tell the user reads are failing
                    self.fail.emit(f"{type(exc).__name__}: {exc}")
                slept = 0
                while self._run and slept < self.every_ms:
                    step = min(100, self.every_ms - slept)
                    self.msleep(step)
                    slept += step

        def halt(self) -> None:
            self._run = False

    class MainWindow(QMainWindow):
        _FONT_FAMILY = "Consolas"
        _MIN_PT, _MAX_PT = 12, 22       # readable floor .. comfortable ceiling
        _MIN_W, _REF_W = 760, 1600      # width -> _MIN_PT at _MIN_W, _MAX_PT at >= _REF_W
        _RECENT_MAX = 50                # accumulated-activity rows kept (matches the widget block cap)

        def __init__(self, src: Source, interval_ms: int) -> None:
            super().__init__()
            self.src = src
            self._interval_s = interval_ms / 1000
            self.writes = 0
            self._last_fail = None
            self._recent_log = []       # accumulated activity, newest-first -- survives is_test/K7 purges
            self._recent_seen = set()   # trace keys already logged (dedup)
            self._font_pt = None            # last-applied point size (None => force first apply)
            self._applying_font = False     # reentrancy guard
            self.setWindowTitle("Agent Kaizen - backend monitor")
            self.setMinimumWidth(self._MIN_W)
            self.resize(900, 680)
            central = QWidget()
            self.setCentralWidget(central)
            root = QVBoxLayout(central)
            root.setContentsMargins(12, 10, 12, 12)
            root.setSpacing(8)

            self.gpu_lbl = QLabel("GPU: ...")
            self.gpu_lbl.setStyleSheet("font-weight:bold;")
            root.addWidget(self.gpu_lbl)
            self.vram = QProgressBar()
            self.vram.setTextVisible(True)
            self.vram.setFixedHeight(20)
            root.addWidget(self.vram)

            self.fail_lbl = QLabel("")
            self.fail_lbl.setStyleSheet("color:#f85149;font-weight:bold;")
            self.fail_lbl.setWordWrap(True)
            self.fail_lbl.hide()
            root.addWidget(self.fail_lbl)

            # THE point of the monitor: what is actually on the GPU now -- every project's model process
            # (with real per-process VRAM/util) + every Ollama-resident model -- read from the driver.
            hdr = QLabel("Models running now  (system-wide):")
            hdr.setStyleSheet("font-weight:bold;color:#3fb950;")
            root.addWidget(hdr)
            self.running = QPlainTextEdit(readOnly=True)
            self.running.setMinimumHeight(140)                    # was fixed; now grows with the window
            self.running.setStyleSheet("background:#0d1117;border:1px solid #21262d;color:#c9d1d9;")
            root.addWidget(self.running, 3)                       # stretch 3: takes most extra height

            root.addWidget(QLabel("Recent Kaizen model calls:"))
            self.recent = QPlainTextEdit(readOnly=True)
            self.recent.setMaximumBlockCount(self._RECENT_MAX)
            self.recent.setMinimumHeight(80)
            self.recent.setStyleSheet("background:#0d1117;border:1px solid #21262d;color:#8b949e;")
            root.addWidget(self.recent, 2)

            foot = QHBoxLayout()
            self._stop_worker = None
            self.stop_btn = QPushButton("EMERGENCY STOP  (unload GPU models)")
            self.stop_btn.setStyleSheet(
                "background:#4a1414;color:#ff6b6b;font-weight:bold;padding:5px 12px;"
                "border:1px solid #f85149;border-radius:6px;"
            )
            self.stop_btn.setToolTip(
                "Immediately unload every Ollama-resident model and terminate the GPU "
                "AI processes (python / torch / comfy / llama-server). Frees VRAM now."
            )
            self.stop_btn.clicked.connect(self._on_emergency_click)
            foot.addWidget(self.stop_btn)
            foot.addStretch(1)
            self.status_lbl = QLabel("starting...")
            self.status_lbl.setStyleSheet("color:#6e7681;")  # neutral liveness indicator, no config framing
            foot.addWidget(self.status_lbl)
            root.addLayout(foot)

            self.poller = Poller(src, interval_ms)
            self.poller.snap.connect(self._on_snap)
            self.poller.fail.connect(self._on_fail)
            self._apply_font(self._pt_for_width(self.width()))   # size correctly before first show
            self.poller.start()

        def _pt_for_width(self, w: int) -> int:
            """Map window width -> integer point size, clamped to [_MIN_PT, _MAX_PT] (never below floor)."""
            span = max(1, self._REF_W - self._MIN_W)
            frac = (w - self._MIN_W) / span
            frac = 0.0 if frac < 0.0 else 1.0 if frac > 1.0 else frac
            return round(self._MIN_PT + frac * (self._MAX_PT - self._MIN_PT))

        def _apply_font(self, pt: int) -> None:
            if pt == self._font_pt:      # anti-thrash gate: no-op unless the integer size truly changed
                return
            self._font_pt = pt
            font = QFont(self._FONT_FAMILY, pt)
            font.setStyleHint(QFont.Monospace)                    # graceful mono fallback if Consolas absent
            self._applying_font = True
            try:
                self.centralWidget().setFont(font)                # QLabels / QProgressBar inherit
                for widget in (self.running, self.recent):
                    widget.setFont(font)                          # stylesheet'd widgets: set explicitly
            finally:
                self._applying_font = False

        def resizeEvent(self, event) -> None:  # noqa: N802 -- Qt override
            super().resizeEvent(event)
            if self._applying_font:                               # ignore any event our own setFont caused
                return
            self._apply_font(self._pt_for_width(self.width()))

        def closeEvent(self, event) -> None:  # noqa: N802 -- Qt override
            self.poller.halt()
            self.poller.wait(1500)
            if self._stop_worker is not None and self._stop_worker.isRunning():
                self._stop_worker.wait()
            super().closeEvent(event)

        def _on_emergency_click(self) -> None:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Emergency stop")
            box.setText("Free the GPU now?")
            box.setInformativeText(
                "Unloads ALL Ollama-resident models and TERMINATES the GPU AI processes shown "
                "(python / torch / comfy / llama-server). In-flight work in those processes is lost."
            )
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            box.setDefaultButton(QMessageBox.No)
            if box.exec() != QMessageBox.Yes:
                return
            self.stop_btn.setEnabled(False)
            self.stop_btn.setText("stopping...")
            self._stop_worker = StopWorker()
            self._stop_worker.done.connect(self._on_stop_done)
            self._stop_worker.start()

        def _on_stop_done(self, result: dict) -> None:
            self.stop_btn.setEnabled(True)
            self.stop_btn.setText("EMERGENCY STOP  (unload GPU models)")
            unloaded = result.get("ollama_unloaded", [])
            killed = result.get("processes_killed", [])
            errors = result.get("errors", [])
            msg = f"Unloaded {len(unloaded)} Ollama model(s); terminated {len(killed)} GPU process(es)."
            if errors:
                msg += "\nIssues: " + "; ".join(str(e) for e in errors)
            QMessageBox.information(self, "Emergency stop", msg)
            self.writes += 1

        def _on_fail(self, text: str) -> None:
            self._last_fail = text
            self.fail_lbl.setText(f"read failed: {text}")
            self.fail_lbl.show()
            self.writes += 1

        @staticmethod
        def _recent_key(r: dict) -> str:
            """Stable identity for a trace so the activity log dedups across polls. Prefers the
            trace id; falls back to its fields if a snapshot omits it (e.g. the smoke harness)."""
            return r.get("id") or (
                f"{r.get('created_at')}|{r.get('kind')}|{r.get('model')}|{r.get('latency_ms')}"
            )

        def _on_snap(self, snap: dict) -> None:
            self.fail_lbl.hide()
            gpu = snap.get("gpu", {})
            devices = gpu.get("devices") or []
            if not gpu.get("available") or not devices:
                self.gpu_lbl.setText(f"GPU: nvidia-smi unavailable ({gpu.get('reason', 'not found')})")
                self.vram.setValue(0)
                self.vram.setFormat("no GPU")
            else:
                dev = devices[0]
                self.gpu_lbl.setText(
                    f"GPU{dev.get('index', 0)} {dev.get('name', '?')}   "
                    f"{_u(dev.get('temp_c'), 'C')}   fan {_u(dev.get('fan_pct'), '%')}   "
                    f"util {_u(dev.get('util_pct'), '%')}   {_u(dev.get('power_w'), 'W')}/"
                    f"{_u(dev.get('power_limit_w'), 'W')}"
                )
                used, total = dev.get("mem_used_mb") or 0, dev.get("mem_total_mb") or 0
                self.vram.setMaximum(max(1, int(total)))
                self.vram.setValue(int(used))
                self.vram.setFormat(f"VRAM {used}/{total} MB")

            ol = snap.get("ollama", {})
            loaded = ol.get("loaded", []) if ol.get("reachable") else []

            # "Models running now": GPU AI processes (any project, with real per-process VRAM/util from
            # Windows perf counters) + Ollama-resident models.
            rows = []
            procs = snap.get("gpu_processes", {})
            if not procs.get("available"):
                rows.append("GPU processes: nvidia-smi unavailable")
            else:
                for p in procs.get("procs", []):
                    mem = p.get("gpu_mem_mb")
                    if mem is None:
                        mem = p.get("vram_mb")
                    mem_s = f"{mem} MB" if mem is not None else "mem n/a"
                    util = p.get("gpu_util_pct")
                    util_s = f"{util}%" if util is not None else "-"
                    rows.append(f"[{str(p.get('kind', 'gpu')):<12}] pid {str(p.get('pid', '?')):<7} "
                                f"{str(p.get('name', '?')):<22} {mem_s:>10}  util {util_s}")
            if not ol.get("reachable"):
                rows.append(f"[ollama      ] unreachable at {ol.get('endpoint', '?')}")
            else:
                for m in loaded:
                    vram = f"{m.get('size_vram_mb', '?')} MB"
                    rows.append(f"[ollama      ] {str(m.get('model', '?')):<40} {vram:<9} "
                                f"resident, expires in {m.get('keep_alive', '?')}")
            running_text = "\n".join(rows) or "(nothing running - no python/ollama/comfyui model on the GPU)"
            if running_text != getattr(self, "_last_running", None):
                self._last_running = running_text
                self.running.setPlainText(running_text)

            # "Recent Kaizen model calls" is an ACCUMULATING session log, not a live DB mirror.
            # recent_activity() reads trace_events, but a backend-test run writes is_test traces that
            # K7 purges (per-class teardown + end-of-run cleanup) -- so mirroring the query would blank
            # the panel ("reset") on every purge. Instead we retain every trace we ever observe, keyed
            # by id, so the feed is a stable record of what actually ran this session.
            new_rows = False
            for r in snap.get("recent", []):
                key = self._recent_key(r)
                if key in self._recent_seen:
                    continue
                self._recent_seen.add(key)
                self._recent_log.append(r)
                new_rows = True
            if new_rows:
                self._recent_log.sort(key=lambda r: (r.get("created_at") or ""), reverse=True)
                del self._recent_log[self._RECENT_MAX:]                     # cap to the newest N
                self._recent_seen = {self._recent_key(r) for r in self._recent_log}  # bound the dedup set
                recent_text = "\n".join(
                    f"{(r.get('created_at') or '')[11:19]}  {str(r.get('lane') or r.get('kind', '?')):<11} "
                    f"{str(r.get('model', '?')):<26} "
                    f"{(str(r.get('latency_ms')) + 'ms') if r.get('latency_ms') is not None else '-'}"
                    for r in self._recent_log
                ) or "(none recorded)"
                self.recent.setPlainText(recent_text)  # only rewrite on genuinely new activity (no flicker)

            updated = datetime.now().strftime("%H:%M:%S")
            self.status_lbl.setText(f"auto-refresh {self._interval_s:g}s  -  updated {updated}")
            self.writes += 1

    def _u(value, unit: str) -> str:
        """Format a value and unit, using a question mark when the value is absent."""
        return f"{value}{unit}" if value is not None else f"?{unit}"

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(True)
    # Font SIZE is driven by MainWindow.resizeEvent (readable min + scale-with-window); only the
    # family baseline is set here so early widgets are monospace before the first resize.
    app.setFont(QFont("Consolas"))
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor("#010409"))
    pal.setColor(QPalette.WindowText, QColor("#c9d1d9"))
    pal.setColor(QPalette.Base, QColor("#0d1117"))
    pal.setColor(QPalette.Text, QColor("#c9d1d9"))
    pal.setColor(QPalette.Button, QColor("#21262d"))
    pal.setColor(QPalette.ButtonText, QColor("#c9d1d9"))
    pal.setColor(QPalette.Highlight, QColor("#1f6feb"))
    app.setPalette(pal)

    win = MainWindow(source, interval_ms)
    win.show()
    return app, win


def main(argv=None) -> int:
    """Parse arguments and run one headless snapshot or the native Qt event loop."""
    ap = argparse.ArgumentParser(description="Agent Kaizen backend monitor (PySide6 native window)")
    ap.add_argument("--once", action="store_true", help="print one JSON snapshot and exit (no Qt)")
    ap.add_argument("--interval", type=float, default=2.0, help="poll seconds (default 2.0, min 0.5)")
    ap.add_argument("--limit", type=int, default=8, help="recent-activity rows (default 8)")
    args = ap.parse_args(argv)

    source = Source(limit=max(1, args.limit))
    if args.once:
        return _run_once(source)
    interval_ms = int(max(0.5, args.interval) * 1000)
    app, _window = _build_gui(source, interval_ms)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

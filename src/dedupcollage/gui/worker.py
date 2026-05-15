"""Background QThread that drives the pipeline.

The worker emits Qt signals so the GUI thread can update widgets without
touching DB connections directly. It owns its own SQLite connection and
governor.
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from dedupcollage import analyze as analyze_mod
from dedupcollage import cluster as cluster_mod
from dedupcollage import fingerprint as fp
from dedupcollage import organize as org_mod
from dedupcollage import scan as scan_mod
from dedupcollage import select as select_mod
from dedupcollage.db import connect
from dedupcollage.governor import PRESETS, Governor

log = logging.getLogger(__name__)


class PipelineWorker(QThread):
    """Runs all six stages in sequence. Emits signals as it goes."""

    stage_started = Signal(str)               # stage label
    stage_progress = Signal(int, int)         # done, total
    stage_finished = Signal(str, dict)        # stage label, summary dict
    pipeline_finished = Signal()
    pipeline_error = Signal(str)
    resource_sample = Signal(dict)            # cpu/ram/io snapshot from governor

    def __init__(
        self,
        db_path: Path,
        source: Path,
        output: Path,
        *,
        throttle: str = "balanced",
        hamming: int = 8,
        label: str | None = None,
    ) -> None:
        super().__init__()
        self._db_path = db_path
        self._source = source
        self._output = output
        self._throttle = throttle
        self._hamming = hamming
        self._label = label
        self._stop_requested = False

    def request_stop(self) -> None:
        """Cooperative stop — pipeline checks this between stages."""
        self._stop_requested = True

    def run(self) -> None:  # noqa: PLR0915 - long but linear and easy to read
        try:
            conn = connect(self._db_path)
            gov = Governor(PRESETS.get(self._throttle, PRESETS["balanced"]))
            gov.start()

            def progress(done: int, total: int) -> None:
                self.stage_progress.emit(done, total)
                self.resource_sample.emit(gov.snapshot())

            stages = [
                ("scan", lambda: scan_mod.scan(
                    conn, self._source, label=self._label, on_progress=progress
                )),
                ("quickhash", lambda: fp.run_quickhash_stage(
                    conn, governor=gov, on_progress=progress
                )),
                ("fullhash", lambda: fp.run_fullhash_stage(
                    conn, governor=gov, on_progress=progress
                )),
                ("analyze", lambda: analyze_mod.run_analyze_stage(
                    conn, governor=gov, on_progress=progress
                )),
                ("cluster", lambda: cluster_mod.run_cluster_stage(
                    conn, hamming_threshold=self._hamming
                )),
                ("select", lambda: select_mod.run_select_stage(conn)),
                ("organize", lambda: org_mod.run_organize_stage(
                    conn, self._output, on_progress=progress
                )),
            ]

            for label, fn in stages:
                if self._stop_requested:
                    log.info("worker stop requested; exiting before stage %s", label)
                    break
                self.stage_started.emit(label)
                summary = fn() or {}
                self.stage_finished.emit(label, summary)

            gov.stop()
            conn.close()
            self.pipeline_finished.emit()
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc()
            log.error("pipeline failed: %s\n%s", e, tb)
            self.pipeline_error.emit(f"{type(e).__name__}: {e}")

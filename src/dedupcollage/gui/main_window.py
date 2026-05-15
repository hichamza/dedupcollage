"""Main window — source/output pickers, progress, cluster tree, preview pane.

This is a deliberately compact PySide6 layout for v0:
  * Top strip: source + output paths with Browse buttons.
  * Throttle dropdown.
  * Start / Stop button.
  * Progress: stage label + bar + tiny resource readout.
  * Cluster tree: top-level rows are clusters, children are file members.
  * Preview pane on the right: thumbnail + metadata key/value.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dedupcollage import __app_name__, __version__
from dedupcollage._paths import default_db_path
from dedupcollage.db import connect
from dedupcollage.gui.worker import PipelineWorker
from dedupcollage.utils import format_bytes

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{__app_name__} {__version__}")
        self.resize(1180, 720)

        self._worker: PipelineWorker | None = None
        self._db_path: Path = default_db_path()

        self._build_ui()
        self._refresh_clusters_timer = QTimer(self)
        self._refresh_clusters_timer.setInterval(5000)
        self._refresh_clusters_timer.timeout.connect(self.refresh_clusters)

    # ---------- UI construction ----------

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ----- Inputs row -----
        inputs_row = QHBoxLayout()
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Source folder (e.g. D:\\Recovery)")
        self.source_browse = QPushButton("Browse…")
        self.source_browse.clicked.connect(lambda: self._pick_folder(self.source_edit, "Pick source"))

        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Output folder (e.g. E:\\Clean)")
        self.output_browse = QPushButton("Browse…")
        self.output_browse.clicked.connect(lambda: self._pick_folder(self.output_edit, "Pick output"))

        inputs_row.addWidget(QLabel("Source:"))
        inputs_row.addWidget(self.source_edit, stretch=1)
        inputs_row.addWidget(self.source_browse)
        inputs_row.addSpacing(12)
        inputs_row.addWidget(QLabel("Output:"))
        inputs_row.addWidget(self.output_edit, stretch=1)
        inputs_row.addWidget(self.output_browse)
        layout.addLayout(inputs_row)

        # ----- Controls row -----
        controls_row = QHBoxLayout()
        controls_row.addWidget(QLabel("Throttle:"))
        self.throttle_combo = QComboBox()
        self.throttle_combo.addItem("Balanced (90% CPU)", "balanced")
        self.throttle_combo.addItem("Background (50% CPU)", "background")
        self.throttle_combo.addItem("Full speed", "fullspeed")
        controls_row.addWidget(self.throttle_combo)

        controls_row.addWidget(QLabel("Match:"))
        self.match_combo = QComboBox()
        self.match_combo.addItem("Strict (Hamming ≤ 4)", 4)
        self.match_combo.addItem("Normal (≤ 8)", 8)
        self.match_combo.addItem("Loose (≤ 12)", 12)
        self.match_combo.setCurrentIndex(1)
        controls_row.addWidget(self.match_combo)

        controls_row.addStretch(1)

        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.start_pipeline)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_pipeline)
        self.stop_btn.setEnabled(False)
        controls_row.addWidget(self.start_btn)
        controls_row.addWidget(self.stop_btn)
        layout.addLayout(controls_row)

        # ----- Progress row -----
        progress_row = QHBoxLayout()
        self.stage_label = QLabel("idle")
        self.stage_label.setMinimumWidth(140)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.resource_label = QLabel("CPU 0%  RAM 0%  I/O 0 MB/s")
        self.resource_label.setMinimumWidth(220)
        self.resource_label.setStyleSheet("color: #666;")
        progress_row.addWidget(self.stage_label)
        progress_row.addWidget(self.progress, stretch=1)
        progress_row.addWidget(self.resource_label)
        layout.addLayout(progress_row)

        # ----- Splitter: cluster tree + preview pane -----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.cluster_tree = QTreeWidget()
        self.cluster_tree.setColumnCount(5)
        self.cluster_tree.setHeaderLabels(["Cluster / file", "Size", "Score", "Decode", "Path"])
        self.cluster_tree.setUniformRowHeights(True)
        self.cluster_tree.itemSelectionChanged.connect(self._on_selection_changed)
        splitter.addWidget(self.cluster_tree)

        preview_widget = QWidget()
        preview_layout = QVBoxLayout(preview_widget)
        self.preview_image = QLabel()
        self.preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_image.setMinimumSize(220, 220)
        self.preview_image.setStyleSheet("background: #f3f3f3; border: 1px solid #ddd;")
        self.preview_image.setText("(select a file to preview)")
        self.preview_details = QTextBrowser()
        self.preview_details.setMinimumWidth(260)
        preview_layout.addWidget(self.preview_image)
        preview_layout.addWidget(self.preview_details, stretch=1)
        splitter.addWidget(preview_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, stretch=1)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage(f"db: {self._db_path}")

    # ---------- helpers ----------

    def _pick_folder(self, line_edit: QLineEdit, caption: str) -> None:
        folder = QFileDialog.getExistingDirectory(self, caption, line_edit.text() or "")
        if folder:
            line_edit.setText(folder)

    # ---------- pipeline lifecycle ----------

    def start_pipeline(self) -> None:
        source = self.source_edit.text().strip()
        output = self.output_edit.text().strip()
        if not source or not output:
            self.statusBar().showMessage("Both Source and Output are required.", 5000)
            return
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.stage_label.setText("starting…")

        self._worker = PipelineWorker(
            db_path=self._db_path,
            source=Path(source),
            output=Path(output),
            throttle=self.throttle_combo.currentData(),
            hamming=self.match_combo.currentData(),
        )
        self._worker.stage_started.connect(self._on_stage_started)
        self._worker.stage_progress.connect(self._on_stage_progress)
        self._worker.stage_finished.connect(self._on_stage_finished)
        self._worker.pipeline_finished.connect(self._on_pipeline_finished)
        self._worker.pipeline_error.connect(self._on_pipeline_error)
        self._worker.resource_sample.connect(self._on_resource_sample)
        self._worker.start()
        self._refresh_clusters_timer.start()

    def stop_pipeline(self) -> None:
        if self._worker is not None:
            self._worker.request_stop()
            self.statusBar().showMessage("Stop requested — finishing current stage…", 5000)

    def _on_stage_started(self, label: str) -> None:
        self.stage_label.setText(f"{label}…")
        self.progress.setRange(0, 0)  # busy indicator until first progress event

    def _on_stage_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        self.progress.setRange(0, total)
        self.progress.setValue(done)

    def _on_stage_finished(self, label: str, summary: dict) -> None:
        bits = ", ".join(f"{k}={v}" for k, v in summary.items() if not isinstance(v, dict))
        self.statusBar().showMessage(f"{label}: {bits}", 8000)

    def _on_pipeline_finished(self) -> None:
        self.stage_label.setText("done")
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._refresh_clusters_timer.stop()
        self.refresh_clusters()

    def _on_pipeline_error(self, msg: str) -> None:
        self.stage_label.setText("error")
        self.statusBar().showMessage(msg, 0)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._refresh_clusters_timer.stop()

    def _on_resource_sample(self, snapshot: dict) -> None:
        cpu = snapshot.get("cpu_pct", 0)
        ram = snapshot.get("ram_pct", 0)
        io = snapshot.get("io_mbps", 0)
        self.resource_label.setText(f"CPU {cpu:.0f}%  RAM {ram:.0f}%  I/O {io:.0f} MB/s")

    # ---------- cluster tree ----------

    def refresh_clusters(self) -> None:
        try:
            conn = connect(self._db_path)
        except Exception as e:  # noqa: BLE001
            log.warning("could not open db for cluster refresh: %s", e)
            return
        try:
            cluster_rows = list(conn.execute("""
                SELECT c.id, c.member_count, c.effective_date, c.effective_date_source,
                       c.winner_id, c.donor_id, c.target_path
                FROM clusters c
                WHERE c.member_count > 1
                ORDER BY c.member_count DESC, c.effective_date DESC
                LIMIT 500
            """))
            self.cluster_tree.clear()
            for c in cluster_rows:
                cid = int(c["id"])
                members = list(conn.execute(
                    "SELECT id, path, size, quality_score, decode_ok, is_winner, sha256 "
                    "FROM files WHERE cluster_id = ? ORDER BY quality_score DESC", (cid,)
                ))
                label = f"#{cid} · {c['effective_date'] or 'unknown date'} · {c['member_count']} copies"
                if c["effective_date_source"] == "mtime":
                    label += " (save date)"
                top = QTreeWidgetItem([label, "", "", "", c["target_path"] or ""])
                top.setData(0, Qt.ItemDataRole.UserRole, {"type": "cluster", "id": cid})
                for m in members:
                    decode = "ok" if m["decode_ok"] == 1 else ("fail" if m["decode_ok"] == 0 else "?")
                    name = Path(m["path"]).name
                    if m["is_winner"]:
                        name = "★ " + name
                    child = QTreeWidgetItem([
                        name,
                        format_bytes(int(m["size"] or 0)),
                        f"{float(m['quality_score'] or 0):.0f}",
                        decode,
                        m["path"],
                    ])
                    child.setData(0, Qt.ItemDataRole.UserRole, {"type": "file", "id": int(m["id"]), "path": m["path"]})
                    top.addChild(child)
                self.cluster_tree.addTopLevelItem(top)
            for col in range(self.cluster_tree.columnCount()):
                self.cluster_tree.resizeColumnToContents(col)
        finally:
            conn.close()

    def _on_selection_changed(self) -> None:
        items = self.cluster_tree.selectedItems()
        if not items:
            return
        payload = items[0].data(0, Qt.ItemDataRole.UserRole) or {}
        if payload.get("type") != "file":
            return
        path = payload.get("path")
        if not path:
            return
        self._preview_file(Path(path), int(payload["id"]))

    def _preview_file(self, path: Path, file_id: int) -> None:
        if path.exists() and path.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp", ".gif"):
            pix = QPixmap(str(path))
            if not pix.isNull():
                self.preview_image.setPixmap(pix.scaled(
                    self.preview_image.width(), self.preview_image.height(),
                    Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
                ))
            else:
                self.preview_image.setText("(could not load image)")
        else:
            self.preview_image.setPixmap(QPixmap())
            self.preview_image.setText(f"(no preview for {path.suffix})")

        try:
            conn = connect(self._db_path)
        except Exception as e:  # noqa: BLE001
            self.preview_details.setPlainText(f"could not open db: {e}")
            return
        try:
            row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            self.preview_details.setPlainText("(no record)")
            return
        lines = [
            f"file       : {Path(row['path']).name}",
            f"path       : {row['path']}",
            f"size       : {format_bytes(int(row['size'] or 0))}",
            f"dim        : {row['width'] or '?'} × {row['height'] or '?'}",
            f"taken      : {row['effective_date'] or '?'}  ({row['date_source'] or '?'})",
            f"cam        : {row['camera_make'] or ''} {row['camera_model'] or ''}".strip(),
            f"serial     : {row['camera_serial'] or '?'}",
            f"lens       : {row['lens_model'] or '?'}",
            f"sha256     : {row['sha256'] or '?'}",
            f"decode_ok  : {row['decode_ok']}",
            f"valid rows : {row['valid_pixel_rows']}/{row['height'] or '?'}",
            f"quality    : {row['quality_score']:.0f}" if row['quality_score'] is not None else "quality    : ?",
        ]
        self.preview_details.setPlainText("\n".join(lines))

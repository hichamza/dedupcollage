"""QApplication bootstrap."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from dedupcollage import __app_name__
from dedupcollage.gui.main_window import MainWindow
from dedupcollage.utils import setup_logging


def main() -> int:
    setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setOrganizationName("DedupCollage")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

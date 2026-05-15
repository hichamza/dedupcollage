"""Shared pytest fixtures."""

from __future__ import annotations

import io
import os
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def image_factory(tmp_path: Path):
    """Returns a factory function that creates JPEG fixtures with controlled corruption."""

    def make(
        name: str,
        *,
        color: tuple[int, int, int] = (200, 150, 100),
        size: tuple[int, int] = (200, 200),
        truncate_bytes: int = 0,
        no_exif: bool = False,
    ) -> Path:
        img = Image.new("RGB", size, color)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        data = buf.getvalue()
        if truncate_bytes:
            data = data[:-truncate_bytes]
        p = tmp_path / name
        p.write_bytes(data)
        os.utime(p, (datetime(2023, 8, 14, 14, 23).timestamp(),) * 2)
        return p

    return make

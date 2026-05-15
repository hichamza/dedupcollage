"""Perceptual hashing — pHash and dHash for near-duplicate clustering.

A 32x32 grayscale thumbnail is derived once from each decoded image; from it
we compute a DCT-based pHash and a gradient-based dHash, both 64-bit.

Storing 16 bytes per file means even 1 M files fit in 16 MB during clustering.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from dedupcollage._paths import ffmpeg_path

log = logging.getLogger(__name__)


def _imagehash_to_bytes(h) -> bytes:
    """Convert an imagehash.ImageHash (64-bit) into a packed 8-byte big-endian blob."""
    bits = h.hash.flatten()
    out = 0
    for bit in bits:
        out = (out << 1) | (1 if bit else 0)
    return out.to_bytes(8, byteorder="big", signed=False)


def hash_image(img: Image.Image) -> tuple[bytes, bytes]:
    """Return (phash_bytes, dhash_bytes) for a PIL Image."""
    import imagehash  # imported lazily so unit tests not needing it stay fast

    # Both hashes work on a small grayscale thumbnail; size 8 -> 64-bit hash.
    p = imagehash.phash(img, hash_size=8)
    d = imagehash.dhash(img, hash_size=8)
    return _imagehash_to_bytes(p), _imagehash_to_bytes(d)


def hash_video(path: Path) -> tuple[bytes, bytes] | None:
    """Hash a video by sampling five evenly-spaced frames via ffmpeg.

    Each frame is hashed individually; the per-bit majority across frames is
    the cluster-stable hash. Returns None if ffmpeg is unavailable or the
    file produces no decodable frames.
    """
    ff = ffmpeg_path()
    if ff is None:
        log.warning("ffmpeg not available; skipping perceptual hash for %s", path)
        return None

    frames: list[Image.Image] = []
    with tempfile.TemporaryDirectory(prefix="dedupcollage_") as tmp:
        out_pattern = str(Path(tmp) / "frame_%02d.jpg")
        cmd = [
            str(ff), "-v", "error", "-y",
            "-i", str(path),
            "-vf", "select='not(mod(n\\,30))',scale=256:-1",
            "-frames:v", "5",
            "-vsync", "0",
            out_pattern,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.warning("ffmpeg frame extraction failed for %s: %s", path, e)
            return None
        for f in sorted(Path(tmp).glob("frame_*.jpg")):
            try:
                frames.append(Image.open(f).convert("L"))
            except OSError:
                continue

    if not frames:
        return None

    phash_bits = [0] * 64
    dhash_bits = [0] * 64
    for frame in frames:
        p, d = hash_image(frame)
        for i in range(64):
            if (p[i // 8] >> (7 - i % 8)) & 1:
                phash_bits[i] += 1
            if (d[i // 8] >> (7 - i % 8)) & 1:
                dhash_bits[i] += 1
    threshold = len(frames) / 2
    p_int = 0
    d_int = 0
    for i in range(64):
        p_int = (p_int << 1) | (1 if phash_bits[i] > threshold else 0)
        d_int = (d_int << 1) | (1 if dhash_bits[i] > threshold else 0)
    return p_int.to_bytes(8, "big"), d_int.to_bytes(8, "big")


def hamming_distance(a: bytes, b: bytes) -> int:
    """Bit-level Hamming distance between two equal-length byte sequences."""
    return sum(bin(x ^ y).count("1") for x, y in zip(a, b, strict=True))

"""Local image cache: download post images once, serve them ourselves.

Security posture: only http(s) URLs, magic-byte verified, size-capped, and
the HTTP route serves ONLY files this module wrote (hash-named, fixed ext).
"""
from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path

_dir: Path | None = None
_lock = threading.Lock()

MAX_BYTES = 5_000_000
_per_post = 4


def set_per_post(n) -> int:
    """Clamp/store the per-article image budget."""
    global _per_post
    try:
        _per_post = max(1, min(8, int(n)))
    except (TypeError, ValueError):
        pass
    return _per_post


def per_post() -> int:
    return _per_post
_NAME_RE = re.compile(r"^[0-9a-f]{24}\.(jpg|png|webp|gif)$")

# magic bytes -> extension
_MAGIC = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def init(path) -> None:
    """Point the cache at a directory (created if missing). Always absolute."""
    global _dir
    p = Path(path).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    _dir = p


def directory() -> Path | None:
    return _dir


def _sniff(data: bytes) -> str | None:
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:12].startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return None


def has(url: str) -> str | None:
    """Filename if this URL is already cached (no download)."""
    if not _dir or not url or not url.startswith(("http://", "https://")):
        return None
    stem = hashlib.sha256(url.encode()).hexdigest()[:24]
    for ext in ("jpg", "png", "webp", "gif"):
        if (_dir / f"{stem}.{ext}").exists():
            return f"{stem}.{ext}"
    return None


def store(url: str, timeout=20) -> str | None:
    """Fetch an image and cache it. Returns the stored filename or None.
    Never raises: images are decorative and must never break digesting."""
    try:
        if not _dir or not url or not url.startswith(("http://", "https://")):
            return None
        existing = has(url)
        if existing:
            return existing
        import requests
        from .fetcher import UA
        with requests.get(url, headers={"user-agent": UA}, timeout=timeout,
                          stream=True, allow_redirects=True) as r:
            if not r.ok:
                return None
            ctype = (r.headers.get("content-type") or "").lower()
            if ctype and not ctype.startswith("image/"):
                return None
            data = b""
            for chunk in r.iter_content(65536):
                data += chunk
                if len(data) > MAX_BYTES:
                    return None
        ext = _sniff(data)
        if not ext:
            return None
        fname = hashlib.sha256(url.encode()).hexdigest()[:24] + "." + ext
        tmp = _dir / (fname + ".part")
        with _lock:
            tmp.write_bytes(data)
            tmp.replace(_dir / fname)
        return fname
    except Exception:  # noqa: BLE001
        return None


def safe_path(name: str) -> Path | None:
    """Resolve a route-provided filename, or None if not a cache file."""
    if not _dir or not _NAME_RE.fullmatch(name or ""):
        return None
    p = _dir / name
    return p if p.is_file() else None


def ahash(name: str, bits: int = 8) -> int | None:
    """Average perceptual hash (grayscale, mean-threshold, 8x8): robust
    to resize AND crop variants. Flat images return None (nothing to
    distinguish them)."""
    if not _dir:
        return None
    try:
        from PIL import Image
        with Image.open(_dir / name) as im:
            im = im.convert("L")
            # centre crop: og variants differ mostly at the edges
            w, h = im.size
            cw, ch = int(w * 0.6), int(h * 0.6)
            im = im.crop(((w - cw) // 2, (h - ch) // 2,
                          (w + cw) // 2, (h + ch) // 2))
            im = im.resize((bits, bits), Image.BILINEAR)
            px = list(im.tobytes())
        avg = sum(px) / len(px)
        var = sum((p - avg) ** 2 for p in px) / len(px)
        if var < 64:          # near-flat: mean-threshold is meaningless
            return None
        mask = 0
        for p in px:
            mask = (mask << 1) | (1 if p > avg else 0)
        return mask
    except Exception:  # noqa: BLE01 - decorative dedupe
        return None


def cache_mb() -> float:
    """Live image-cache size in MB (scandir sum; admin panel truth)."""
    import os
    if _dir is None:
        return 0.0
    total = 0
    try:
        with os.scandir(_dir) as it:
            for e in it:
                try:
                    if e.is_file():
                        total += e.stat().st_size
                except OSError:
                    pass
    except OSError:
        return 0.0
    return round(total / 1e6, 1)

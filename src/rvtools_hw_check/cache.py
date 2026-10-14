"""On-disk cache for catalog lookups (spec FR-4.6)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_TTL = 7 * 24 * 3600


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "rvtools-hw-check"


class Cache:
    def __init__(
        self,
        directory: str | Path | None = None,
        ttl: float = DEFAULT_TTL,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
    ):
        self.directory = Path(directory) if directory else default_cache_dir()
        self.ttl = ttl
        self.enabled = enabled
        self._clock = clock

    def _path(self, key: str) -> Path:
        return self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def get(self, key: str):
        if not self.enabled:
            return None
        path = self._path(key)
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if entry.get("key") != key or self._clock() - entry.get("stored_at", 0) > self.ttl:
            return None
        return entry.get("value")

    def set(self, key: str, value) -> None:
        if not self.enabled:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self._path(key)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"key": key, "stored_at": self._clock(), "value": value}))
            tmp.replace(path)
        except OSError as exc:
            logger.warning("Could not write cache entry: %s", exc)

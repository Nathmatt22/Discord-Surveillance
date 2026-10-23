"""Mémorisation du dernier message traité, pour ne pas réanalyser au redémarrage."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class StateStore:
    """Petit fichier JSON : ``{"channels": {"<id salon>": <dernier id message>}}``."""

    def __init__(self, path: str | Path, save_interval_seconds: float = 2.0) -> None:
        self._path = Path(path)
        self._save_interval = save_interval_seconds
        self._last: dict[int, int] = {}
        self._dirty = False
        self._last_save = 0.0

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            data: Any = json.loads(self._path.read_text(encoding="utf-8"))
            channels = data.get("channels", {}) if isinstance(data, dict) else {}
            self._last = {
                int(channel_id): int(message_id)
                for channel_id, message_id in channels.items()
            }
            log.debug("État chargé depuis %s (%d salon(s)).", self._path, len(self._last))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            log.warning("État illisible dans %s, on repart de zéro : %s", self._path, exc)
            self._last = {}

    def last_message_id(self, channel_id: int) -> int | None:
        return self._last.get(channel_id)

    def set_last(self, channel_id: int, message_id: int) -> None:
        current = self._last.get(channel_id)
        # Discord peut livrer les messages dans le désordre : on garde le plus récent.
        if current is not None and message_id <= current:
            return
        self._last[channel_id] = message_id
        self._dirty = True
        self._maybe_save()

    def flush(self) -> None:
        if not self._dirty:
            return
        payload = {
            "version": 1,
            "channels": {str(channel_id): message_id for channel_id, message_id in self._last.items()},
        }
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            os.replace(tmp, self._path)
            self._dirty = False
            self._last_save = _now()
        except OSError as exc:
            log.warning("Impossible d'écrire %s : %s", self._path, exc)

    def _maybe_save(self) -> None:
        if _now() - self._last_save >= self._save_interval:
            self.flush()


def _now() -> float:
    return time.monotonic()

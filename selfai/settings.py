"""Réglages modifiables pendant l'exécution.

Le bot lit ces valeurs à chaque message : les changer dans l'interface prend effet
immédiatement, et la sauvegarde les réécrit dans ``config.json`` pour le prochain
démarrage.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

log = logging.getLogger(__name__)

MIN_CONFIDENCE_FLOOR = 0.05
MIN_CONFIDENCE_STEP = 0.05


@dataclass
class LiveSettings:
    min_confidence: float = 0.5
    sound: bool = True
    ignore_bots: bool = True
    telegram_enabled: bool = False
    telegram_chat_id: str = ""
    webhook_url: str = ""

    @classmethod
    def from_config(
        cls, config: object, environ: Mapping[str, str] | None = None
    ) -> "LiveSettings":
        analysis = getattr(config, "analysis", None)
        # Accepte aussi directement un NotifyConfig (tests, appels ciblés).
        notify = getattr(config, "notify", config)
        source = environ if environ is not None else os.environ
        webhook_variable = str(getattr(notify, "webhook_url_env", "") or "")
        return cls(
            min_confidence=float(getattr(analysis, "min_confidence", 0.5)),
            sound=bool(getattr(notify, "sound", True)),
            ignore_bots=bool(getattr(analysis, "ignore_bots", True)),
            telegram_enabled=bool(getattr(notify, "telegram_enabled", False)),
            telegram_chat_id=str(getattr(notify, "telegram_chat_id", "") or ""),
            webhook_url=(source.get(webhook_variable, "") if webhook_variable else "").strip(),
        )

    def copy(self) -> "LiveSettings":
        return LiveSettings(
            self.min_confidence,
            self.sound,
            self.ignore_bots,
            self.telegram_enabled,
            self.telegram_chat_id,
            self.webhook_url,
        )

    def adjust_confidence(self, direction: int) -> None:
        value = self.min_confidence + MIN_CONFIDENCE_STEP * direction
        self.min_confidence = round(
            min(1.0, max(MIN_CONFIDENCE_FLOOR, value)), 2
        )


def save_settings(path: str | Path, settings: LiveSettings) -> None:
    """Réécrit les réglages dans le fichier de configuration, sans le reformater."""
    config_path = Path(path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Sauvegarde des réglages impossible (%s) : %s", config_path, exc)
        return

    data.setdefault("analysis", {})["min_confidence"] = settings.min_confidence
    data["analysis"]["ignore_bots"] = settings.ignore_bots
    notify = data.setdefault("notify", {})
    notify["sound"] = settings.sound
    telegram = notify.setdefault("telegram", {})
    telegram["enabled"] = settings.telegram_enabled
    telegram["chat_id"] = settings.telegram_chat_id

    temporary = config_path.with_name(config_path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(temporary, config_path)
    except OSError as exc:
        log.warning("Écriture de %s impossible : %s", config_path, exc)

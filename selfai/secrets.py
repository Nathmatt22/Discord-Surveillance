"""Jeton du compte, clé IA et jeton Telegram : lecture et écriture dans ``.env``.

Les valeurs ne sortent jamais de ce module pour l'affichage : l'interface ne reçoit
que l'état « défini » ou « absent », et la saisie est masquée.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values, set_key

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecretField:
    """Un secret de l'interface, rangé dans ``.env``."""

    key: str  # nom logique, utilisé par l'interface
    variable: str  # nom de la variable dans .env
    label: str


class SecretStore:
    """Lit et met à jour les secrets, sans jamais les journaliser."""

    def __init__(
        self,
        path: str | Path,
        fields: list[SecretField],
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._path = Path(path)
        self._fields = list(fields)
        self._environ = environ if environ is not None else os.environ

    @property
    def path(self) -> Path:
        return self._path

    def fields(self) -> list[SecretField]:
        return list(self._fields)

    def field(self, key: str) -> SecretField | None:
        return next((field for field in self._fields if field.key == key), None)

    def _file_values(self) -> dict[str, str]:
        try:
            raw = dotenv_values(self._path) if self._path.exists() else {}
        except Exception:
            log.exception("Lecture de %s impossible", self._path)
            return {}
        return {name: value for name, value in raw.items() if value is not None}

    def values(self) -> dict[str, str]:
        """Valeur effective de chaque secret (environnement prioritaire)."""
        file_values = self._file_values()
        return {
            field.key: (
                self._environ.get(field.variable)
                or file_values.get(field.variable)
                or ""
            ).strip()
            for field in self._fields
        }

    def status(self) -> dict[str, bool]:
        """« défini » ou « absent », sans dévoiler la valeur."""
        return {key: bool(value) for key, value in self.values().items()}

    def set(self, key: str, value: str) -> bool:
        """Écrit la valeur dans ``.env`` (chaîne vide = efface)."""
        field = self.field(key)
        if field is None:
            return False
        cleaned = value.strip()
        try:
            self._path.touch(exist_ok=True)
            set_key(str(self._path), field.variable, cleaned, quote_mode="always")
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass  # Windows ne gère pas vraiment les droits POSIX
        except Exception:
            log.exception("Écriture de %s impossible", self._path)
            return False

        # Le reste du programme lit os.environ : on le met à jour tout de suite.
        self._environ[field.variable] = cleaned
        return True

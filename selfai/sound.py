"""Son d'alerte.

Sur Windows, utilise ``winsound`` (bibliothèque standard) : le bip système ou un
fichier .wav. Ailleurs, joue le fichier avec un lecteur présent sur la machine,
sinon émet la cloche du terminal.

Aucune dépendance externe.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

# Joueurs audio usuels sous macOS / Linux, par ordre de préférence.
POSIX_PLAYERS = ("afplay", "paplay", "aplay", "ffplay")


def play_alert(path: str | None = None, *, platform: str | None = None) -> None:
    """Joue le son d'alerte. Ne lève jamais d'exception."""
    system = platform or os.name
    try:
        if system == "nt":
            _play_windows(path)
        else:
            _play_posix(path)
    except Exception as exc:  # un son raté ne doit pas casser une alerte
        log.debug("Lecture du son impossible : %s", exc)


def _play_windows(path: str | None) -> None:
    import winsound

    if path:
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        except RuntimeError:
            log.warning("Fichier son introuvable ou illisible : %s", path)

    try:
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except RuntimeError:
        winsound.Beep(880, 180)


def _play_posix(path: str | None) -> None:
    if path and os.path.exists(path):
        for player in POSIX_PLAYERS:
            executable = shutil.which(player)
            if executable is None:
                continue
            command = [executable, path]
            if player == "ffplay":
                command = [executable, "-nodisp", "-autoexit", "-loglevel", "quiet", path]
            subprocess.Popen(  # noqa: S603 - commande fixe, chemin fourni par l'utilisateur
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        log.warning("Aucun lecteur audio trouvé pour %s.", path)

    _bell()


def _bell() -> None:
    """Cloche du terminal (peut être muette selon la configuration)."""
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass

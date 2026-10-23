"""Cœur du filtre : décider quels messages partiront à l'IA et quoi notifier.

Volontairement indépendant de discord.py (objets « canards »), pour être testable
sans connexion réseau.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from .classifier import ClassifierError, Verdict
from .config import Config
from .settings import LiveSettings
from .state import StateStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IncomingMessage:
    """Un message Discord réduit à ce dont l'analyse a besoin."""

    id: int
    channel_id: int
    channel_name: str
    author_name: str
    content: str
    author_id: int = 0
    author_is_bot: bool = False
    jump_url: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True)
class Alert:
    """Un message jugé important, prêt à être notifié."""

    message: IncomingMessage
    verdict: Verdict


class Notifier(Protocol):
    async def notify(self, alert: Alert) -> None: ...


class MessageAnalyzer:
    """Décide puis interroge l'IA pour chaque message surveillé."""

    def __init__(
        self,
        config: Config,
        classifier: object,
        notifier: Notifier,
        state: StateStore,
        on_verdict: Callable[[IncomingMessage, Verdict, bool], None] | None = None,
        settings: LiveSettings | None = None,
    ) -> None:
        self._config = config
        self._classifier = classifier
        self._notifier = notifier
        self._state = state
        self._settings = settings or LiveSettings.from_config(config)
        self._on_verdict = on_verdict
        self._recent: dict[int, deque[str]] = {}

    # -- filtres locaux (gratuits) ----------------------------------------

    def should_ignore(self, message: IncomingMessage) -> str | None:
        """Renvoie la raison d'ignorer le message, ou ``None`` s'il est à analyser."""
        if self._config.channel(message.channel_id) is None:
            return "salon non surveillé"
        if message.author_id in self._config.analysis.ignore_user_ids:
            return "compte ignoré"
        if self._settings.ignore_bots and message.author_is_bot:
            return "message d'un bot"
        if not message.content.strip():
            return "message sans texte"
        return None

    # -- analyse -----------------------------------------------------------

    async def analyze(self, message: IncomingMessage) -> Alert | None:
        """Analyse un message ; renvoie l'alerte si elle a été notifiée."""
        last_id = self._state.last_message_id(message.channel_id)
        if last_id is not None and message.id <= last_id:
            log.debug("Message %s déjà traité, ignoré.", message.id)
            return None

        reason = self.should_ignore(message)
        if reason is not None:
            log.debug("Message %s ignoré (%s).", message.id, reason)
            return None

        channel_config = self._config.channel(message.channel_id)
        channel_name = message.channel_name or (channel_config.name if channel_config else "")
        context = list(self._recent.get(message.channel_id, ()))
        text = message.content[: self._config.analysis.max_message_chars]

        prompt = f"Salon : #{channel_name or 'inconnu'} | {message.author_name} : {text}"
        if channel_config and channel_config.context:
            prompt = f"Contexte du salon : {channel_config.context}\n{prompt}"

        try:
            verdict: Verdict = await asyncio.to_thread(
                self._classifier.classify,
                text=prompt,
                channel_name=channel_name,
                author=message.author_name,
                context=tuple(context),
            )
        except ClassifierError as exc:
            log.warning("Analyse impossible du message %s : %s", message.id, exc)
            self._remember(message)
            self._state.set_last(message.channel_id, message.id)
            return None

        log.debug(
            "Message %s → important=%s (%.2f) : %s",
            message.id,
            verdict.important,
            verdict.confidence,
            verdict.reason,
        )
        self._remember(message)
        self._state.set_last(message.channel_id, message.id)

        threshold = self._settings.min_confidence
        alert: Alert | None = None
        if verdict.important:
            if verdict.confidence < threshold:
                log.info(
                    "Message %s important mais confiance trop basse (%.2f < %.2f), "
                    "pas d'alerte.",
                    message.id,
                    verdict.confidence,
                    threshold,
                )
            else:
                alert = Alert(message=message, verdict=verdict)
                try:
                    await self._notifier.notify(alert)
                except Exception:  # une notification ratée ne doit pas arrêter le bot
                    log.exception("Échec de la notification du message %s.", message.id)

        if self._on_verdict is not None:
            try:
                self._on_verdict(message, verdict, alert is not None)
            except Exception:
                log.exception("Erreur dans le retour d'affichage.")
        return alert

    # -- mémoire courte par salon -----------------------------------------

    def _remember(self, message: IncomingMessage) -> None:
        limit = self._config.analysis.context_messages
        if limit <= 0:
            return
        buffer = self._recent.setdefault(message.channel_id, deque(maxlen=limit))
        buffer.append(f"{message.author_name} : {message.content.strip()[:300]}")

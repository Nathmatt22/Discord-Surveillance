"""Connexion au compte Discord (jeton utilisateur) et envoi des alertes.

Aucun bot à ajouter : le script se connecte avec le jeton du compte lui-même et
reçoit les messages des salons auxquels ce compte a accès.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import discord
import requests

from .analyzer import Alert, IncomingMessage, MessageAnalyzer
from .classifier import Classifier
from .config import Config, NotifyConfig
from .settings import LiveSettings
from .sound import play_alert
from .state import StateStore
from .telegram import TelegramNotifier

log = logging.getLogger(__name__)

QUEUE_SIZE = 1000
WEBHOOK_TIMEOUT = 15
SOUND_COOLDOWN = 2.0


class AccountNotifier:
    """Envoie les alertes : MP, salon dédié, webhook et/ou Telegram."""

    def __init__(
        self,
        client: discord.Client,
        config: NotifyConfig,
        settings: LiveSettings | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._settings = settings or LiveSettings.from_config(config)
        self._last_sound = 0.0
        self._telegram: TelegramNotifier | None = None
        self._telegram_key: tuple[str, str] | None = None

    # -- cibles relues à chaque alerte : les réglages s'appliquent à chaud

    @property
    def webhook_url(self) -> str:
        return self._settings.webhook_url.strip()

    def telegram_notifier(self) -> TelegramNotifier | None:
        """Notifier Telegram courant, reconstruit si le chat ou le jeton change."""
        if not (self._settings.telegram_enabled and self._settings.telegram_chat_id):
            return None
        token = os.environ.get(self._config.telegram_token_env, "").strip()
        if not token:
            return None
        key = (self._settings.telegram_chat_id, token)
        if self._telegram is None or self._telegram_key != key:
            self._telegram = TelegramNotifier(token, self._settings.telegram_chat_id)
            self._telegram_key = key
        return self._telegram

    async def notify(self, alert: Alert) -> None:
        verdict = alert.verdict
        message = alert.message
        log.info(
            "ALERTE #%s — %s : %s (confiance %.0f %%)",
            message.channel_name or message.channel_id,
            message.author_name,
            verdict.reason,
            verdict.confidence * 100,
        )

        await self._maybe_play_sound()

        text = format_alert(alert)
        mentions = discord.AllowedMentions.none()

        if self._config.dm_user_id:
            try:
                user = self._client.get_user(self._config.dm_user_id)
                if user is None:
                    user = await self._client.fetch_user(self._config.dm_user_id)
                await user.send(text, allowed_mentions=mentions)
            except Exception:
                log.exception("Impossible d'envoyer le message privé d'alerte.")

        if self._config.log_channel_id:
            try:
                channel = self._client.get_channel(self._config.log_channel_id)
                if channel is None:
                    channel = await self._client.fetch_channel(self._config.log_channel_id)
                await channel.send(text, allowed_mentions=mentions)
            except Exception:
                log.exception("Impossible de publier l'alerte dans le salon de journal.")

        if self.webhook_url:
            try:
                await asyncio.to_thread(self._post_webhook, text)
            except Exception:
                log.exception("Impossible d'envoyer l'alerte au webhook.")

        telegram = self.telegram_notifier()
        if telegram is not None:
            try:
                await asyncio.to_thread(telegram.send, alert)
            except Exception:
                log.exception("Impossible d'envoyer l'alerte sur Telegram.")

    async def _maybe_play_sound(self) -> None:
        """Bip à chaque alerte, avec un délai mini pour éviter la mitraille."""
        if not self._settings.sound:
            return
        now = time.monotonic()
        if now - self._last_sound < SOUND_COOLDOWN:
            return
        self._last_sound = now
        try:
            await asyncio.to_thread(play_alert, self._config.sound_file)
        except Exception:
            log.debug("Lecture du son impossible.", exc_info=True)

    def _post_webhook(self, text: str) -> None:
        response = requests.post(
            self.webhook_url,
            json={
                "content": text,
                "username": "Filtre IA",
                # Empêche un @everyone présent dans le message d'origine de pinguer.
                "allowed_mentions": {"parse": []},
            },
            timeout=WEBHOOK_TIMEOUT,
        )
        if response.status_code >= 300:
            raise RuntimeError(
                f"le webhook a répondu HTTP {response.status_code} : {response.text[:200]}"
            )


def format_alert(alert: Alert, max_content: int = 1200) -> str:
    """Texte de l'alerte envoyée sur Discord."""
    message = alert.message
    verdict = alert.verdict
    content = message.content.strip()
    if len(content) > max_content:
        content = content[:max_content] + "…"
    quoted = "\n".join(f"> {line}" for line in content.splitlines()) or "> (vide)"

    when = ""
    if message.created_at is not None:
        when = message.created_at.astimezone().strftime("%d/%m/%Y %H:%M")

    lines = [
        "🚨 **Message important**",
        f"**Salon :** #{message.channel_name or message.channel_id}"
        + (f" · {when}" if when else ""),
        f"**Auteur :** {message.author_name}",
        quoted,
        f"**Raison :** {verdict.reason}",
        f"**Confiance :** {verdict.confidence * 100:.0f} %",
    ]
    if message.jump_url:
        lines.append(f"**Lien :** {message.jump_url}")
    return "\n".join(lines)[:1990]


class SelfAIBot(discord.Client):
    """Client Discord connecté au compte personnel (jeton utilisateur)."""

    def __init__(
        self,
        config: Config,
        classifier: Classifier,
        state: StateStore,
        dashboard: object | None = None,
        settings: LiveSettings | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.state = state
        self.dashboard = dashboard
        self.live_settings = settings or LiveSettings.from_config(config)
        self.notifier = AccountNotifier(self, config.notify, self.live_settings)
        self.analyzer = MessageAnalyzer(
            config,
            classifier,
            self.notifier,
            state,
            on_verdict=getattr(dashboard, "record", None),
            settings=self.live_settings,
        )
        self.queue: asyncio.Queue[IncomingMessage] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._worker_task: asyncio.Task[None] | None = None

    # -- cycle de vie ------------------------------------------------------

    async def setup_hook(self) -> None:
        self._worker_task = asyncio.create_task(self._worker(), name="selfai-worker")

    async def close(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        self.state.flush()
        await super().close()

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Connecté au compte %s (id %s).", self.user, self.user.id)
        for channel in self.config.channels:
            log.info("Salon surveillé : #%s (%s)", channel.name or "sans nom", channel.id)
        log.info("Analyse des nouveaux messages uniquement (aucun historique relu).")
        if self.dashboard is not None:
            self.dashboard.connected(  # type: ignore[attr-defined]
                str(self.user),
                [f"#{c.name}" if c.name else str(c.id) for c in self.config.channels],
            )

    # -- réception ---------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id not in self.config.channel_ids:
            return
        if self.user is not None and message.author.id == self.user.id:
            return  # pas d'alerte sur tes propres messages
        self._enqueue(self._to_incoming(message))

    def _enqueue(self, message: IncomingMessage) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            log.warning(
                "File d'analyse pleine, message %s ignoré (l'IA est plus lente que le trafic).",
                message.id,
            )

    def _to_incoming(self, message: discord.Message) -> IncomingMessage:
        channel_config = self.config.channel(message.channel.id)
        author_name = getattr(message.author, "display_name", None) or message.author.name
        return IncomingMessage(
            id=message.id,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", "")
            or (channel_config.name if channel_config else ""),
            author_name=author_name,
            content=message.content,
            author_id=message.author.id,
            author_is_bot=bool(message.author.bot),
            jump_url=getattr(message, "jump_url", ""),
            created_at=getattr(message, "created_at", None),
        )

    # -- travail en arrière-plan ------------------------------------------

    async def _worker(self) -> None:
        while True:
            message = await self.queue.get()
            try:
                await self.analyzer.analyze(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Erreur pendant l'analyse du message %s.", message.id)
            finally:
                self.queue.task_done()


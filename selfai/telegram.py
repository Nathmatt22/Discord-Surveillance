"""Alertes Telegram, sans dépendance supplémentaire.

On utilise l'API Bot de Telegram en HTTPS avec ``requests`` : il suffit d'un bot
créé chez BotFather et de l'identifiant de la conversation à prévenir.

Rien n'est jamais écouté côté Telegram : le bot ne fait que poster les alertes.
"""

from __future__ import annotations

import html
import logging
from typing import Any

import requests

from .analyzer import Alert

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
TIMEOUT = 15
MAX_CONTENT = 3000


class TelegramNotifier:
    """Poste une alerte dans une conversation Telegram (privée, groupe ou canal)."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        session: Any = None,
        timeout: float = TIMEOUT,
    ) -> None:
        self._token = token.strip()
        self._chat_id = str(chat_id).strip()
        self._session = session or requests
        self._timeout = timeout

    def send(self, alert: Alert) -> None:
        try:
            response = self._session.post(
                f"{API_ROOT}/bot{self._token}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": format_alert(alert),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=self._timeout,
            )
        except Exception as exc:
            # Le message d'une exception réseau contient l'URL, donc le jeton :
            # on ne garde que le type d'erreur pour ne rien écrire dans le journal.
            raise RuntimeError(f"Telegram injoignable ({type(exc).__name__})") from None
        if response.status_code >= 300:
            raise RuntimeError(
                f"Telegram a répondu HTTP {response.status_code} : {response.text[:200]}"
            )


def format_alert(alert: Alert, max_content: int = 1200) -> str:
    """Texte HTML de l'alerte envoyée sur Telegram."""
    message = alert.message
    verdict = alert.verdict
    channel = message.channel_name or str(message.channel_id)

    content = message.content.strip()
    if len(content) > max_content:
        content = content[:max_content] + "…"
    quoted = html.escape(content)
    if len(quoted) > MAX_CONTENT:
        # Échapper « < » ou « & » fait gonfler le texte : on garde une marge sous
        # la limite de 4096 caractères imposée par Telegram.
        quoted = quoted[:MAX_CONTENT] + "…"

    when = ""
    if message.created_at is not None:
        when = message.created_at.astimezone().strftime("%d/%m/%Y %H:%M")

    lines = [f"🚨 <b>Message important</b> · #{html.escape(channel)}"]
    meta = html.escape(message.author_name)
    if when:
        meta += f" · {when}"
    lines.append(f"{meta} · confiance {verdict.confidence:.0%}")
    lines.append(f"<b>Raison :</b> {html.escape(verdict.reason)}")
    if quoted:
        lines.append(f"<i>{quoted}</i>")
    if message.jump_url:
        lines.append(f'<a href="{html.escape(message.jump_url)}">Voir sur Discord</a>')
    return "\n".join(lines)

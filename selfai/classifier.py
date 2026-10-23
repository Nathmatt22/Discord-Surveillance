"""Envoi d'un message Discord à une IA et lecture du verdict.

Fonctionne avec toute API de chat compatible OpenAI : il suffit de changer
``ai.base_url`` et ``ai.model`` dans ``config.json``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Sequence

import requests

from .config import AIConfig, AnalysisConfig

log = logging.getLogger(__name__)

SYSTEM_PROMPT_TEMPLATE = """Tu es un filtre de notifications pour un serveur Discord.
On te donne un message et, parfois, les messages qui le précèdent. Tu dois dire
s'il mérite une notification immédiate à l'utilisateur.

Important : une annonce importante, une question ou une demande qui me concerne
directement, une mention, une urgence, un changement de date ou de planning, une
décision, une échéance, une alerte.
Pas important : bavardage, blague, meme, réaction sociale sans enjeu, spam,
message trop vague pour être actionnable.

Règles supplémentaires choisies par l'utilisateur :
{instructions}

Réponds UNIQUEMENT avec un objet JSON, sans texte autour :
{{"important": true, "confidence": 0.0, "reason": "une phrase courte en français"}}

- "important" : true ou false.
- "confidence" : ta certitude, entre 0 et 1.
- "reason" : une phrase maximum, en français.
"""

DEFAULT_INSTRUCTIONS = (
    "Aucune règle particulière : applique les critères généraux ci-dessus."
)


@dataclass(frozen=True)
class Verdict:
    """Le jugement de l'IA sur un message."""

    important: bool
    confidence: float
    reason: str


class ClassifierError(RuntimeError):
    """L'IA n'a pas pu être interrogée ou sa réponse est inutilisable."""


class Classifier:
    """Interroge l'API IA et renvoie un :class:`Verdict`."""

    def __init__(
        self,
        ai: AIConfig,
        analysis: AnalysisConfig,
        api_key: str,
        session: requests.Session | None = None,
    ) -> None:
        self._ai = ai
        self._analysis = analysis
        self._api_key = api_key
        self._session = session or requests.Session()

    def set_api_key(self, api_key: str) -> None:
        """Remplace la clé à chaud (modifiée depuis l'interface)."""
        self._api_key = api_key.strip()

    # -- prompt ------------------------------------------------------------

    def build_messages(
        self,
        *,
        text: str,
        channel_name: str = "",
        author: str = "",
        context: Sequence[str] = (),
    ) -> list[dict[str, str]]:
        instructions = self._analysis.instructions or DEFAULT_INSTRUCTIONS
        system = SYSTEM_PROMPT_TEMPLATE.format(instructions=instructions)

        lines: list[str] = []
        if channel_name:
            lines.append(f"Salon : #{channel_name}")
        if author:
            lines.append(f"Auteur : {author}")
        if context:
            lines.append("Messages précédents :")
            lines.extend(f"- {item}" for item in context)
        lines.append("Message à analyser :")
        lines.append('"""')
        lines.append(text)
        lines.append('"""')

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n".join(lines)},
        ]

    # -- appel réseau ------------------------------------------------------

    def classify(
        self,
        *,
        text: str,
        channel_name: str = "",
        author: str = "",
        context: Sequence[str] = (),
    ) -> Verdict:
        """Classe un message. Lève :class:`ClassifierError` en cas d'échec."""
        payload: dict[str, Any] = {
            "model": self._ai.model,
            "messages": self.build_messages(
                text=text, channel_name=channel_name, author=author, context=context
            ),
            "temperature": self._ai.temperature,
        }
        if self._ai.json_mode:
            payload["response_format"] = {"type": "json_object"}

        url = f"{self._ai.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        attempt = 0
        while True:
            try:
                response = self._session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=self._ai.timeout_seconds,
                )
            except requests.RequestException as exc:
                if attempt >= self._ai.retries:
                    raise ClassifierError(f"Impossible de joindre l'API IA : {exc}") from exc
                attempt += 1
                self._wait(attempt, f"erreur réseau ({exc.__class__.__name__})")
                continue

            # Certains fournisseurs n'acceptent pas response_format : on réessaie
            # une fois sans ce champ plutôt que d'échouer.
            if (
                response.status_code == 400
                and "response_format" in payload
                and "response_format" in response.text
            ):
                log.warning("Le fournisseur refuse response_format, nouvel essai sans ce champ.")
                payload.pop("response_format", None)
                continue

            if response.status_code in (401, 403):
                raise ClassifierError(
                    "Clé API refusée par le fournisseur "
                    f"(HTTP {response.status_code}). Vérifie la variable d'environnement "
                    f"{self._ai.api_key_env}."
                )

            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._ai.retries:
                    raise ClassifierError(
                        f"API IA indisponible (HTTP {response.status_code}) après "
                        f"{self._ai.retries + 1} tentative(s)."
                    )
                attempt += 1
                self._wait(
                    attempt,
                    f"HTTP {response.status_code}",
                    retry_after=response.headers.get("Retry-After"),
                )
                continue

            if response.status_code >= 400:
                raise ClassifierError(
                    f"API IA a renvoyé une erreur HTTP {response.status_code} : "
                    f"{response.text[:300]}"
                )

            try:
                content = _extract_content(response.json())
                return _parse_verdict(content)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                if attempt >= self._ai.retries:
                    raise ClassifierError(
                        f"Réponse de l'IA inexploitable : {exc}"
                    ) from exc
                attempt += 1
                self._wait(attempt, f"réponse inexploitable ({exc})")
                continue

    def _wait(self, attempt: int, cause: str, retry_after: str | None = None) -> None:
        delay = self._ai.retry_backoff_seconds * (2 ** (attempt - 1))
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        delay = min(delay, 30.0)
        log.info("Nouvel essai dans %.1f s (%s).", delay, cause)
        if delay > 0:
            time.sleep(delay)


# --------------------------------------------------------------------------
# Parsing tolérant
# --------------------------------------------------------------------------


def _extract_content(data: Any) -> str:
    """Récupère le texte de la réponse, quel que soit le format des blocs."""
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("réponse sans champ « choices »") from exc

    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content

    # Certains fournisseurs renvoient une liste de blocs de texte.
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        joined = "".join(parts).strip()
        if joined:
            return joined

    raise ValueError("la réponse de l'IA est vide")


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"true", "vrai", "oui", "yes", "1"}:
            return True
        if cleaned in {"false", "faux", "non", "no", "0"}:
            return False
    return None


def _parse_verdict(raw: str) -> Verdict:
    """Extrait le verdict du texte renvoyé par l'IA (JSON strict ou balisé)."""
    text = raw.strip()

    # Retire un éventuel bloc Markdown ```json ... ```.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("aucun objet JSON dans la réponse")

    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("le JSON renvoyé n'est pas un objet")

    important = _as_bool(data.get("important"))
    if important is None:
        raise ValueError("champ « important » absent ou invalide")

    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = min(max(confidence, 0.0), 1.0)

    reason = str(data.get("reason") or "").strip()
    reason = reason[:300] if reason else "aucune raison fournie"

    return Verdict(important=important, confidence=confidence, reason=reason)

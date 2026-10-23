"""Chargement et validation du fichier ``config.json``.

Toutes les valeurs sont validées au démarrage pour éviter de découvrir une
faute de frappe après plusieurs minutes de fonctionnement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Le fichier de configuration est absent, illisible ou invalide."""


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelConfig:
    """Un salon Discord à surveiller."""

    id: int
    name: str = ""
    context: str = ""


# Fournisseurs connus : « ai.provider » remplit base_url, model et le nom de
# la variable d'environnement. Chaque champ reste surchargeable dans config.json.
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "vyceai": {
        "base_url": "https://vyceai.com/v1",
        "model": "deepseek-v4-flash",
        "api_key_env": "VYCEAI_API_KEY",
    },
    "zai": {
        "base_url": "https://api.z.ai/api/paas/v4",
        "model": "glm-4.6",
        "api_key_env": "AI_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "api_key_env": "GROQ_API_KEY",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "llama3.2",
        "api_key_env": "OLLAMA_API_KEY",
    },
}


@dataclass(frozen=True)
class AIConfig:
    """Connexion à une API de chat compatible OpenAI."""

    base_url: str
    model: str
    api_key_env: str = "AI_API_KEY"
    provider: str = ""
    json_mode: bool = True
    temperature: float = 0.0
    timeout_seconds: float = 30.0
    retries: int = 2
    retry_backoff_seconds: float = 1.0


@dataclass(frozen=True)
class AnalysisConfig:
    """Comment juger un message."""

    instructions: str = ""
    ignore_bots: bool = True
    ignore_user_ids: frozenset[int] = frozenset()
    context_messages: int = 3
    max_message_chars: int = 2000
    min_confidence: float = 0.5


@dataclass(frozen=True)
class NotifyConfig:
    """Où envoyer les alertes (en plus de la console)."""

    dm_user_id: int | None = None
    log_channel_id: int | None = None
    webhook_url_env: str = "DISCORD_WEBHOOK_URL"
    sound: bool = True
    sound_file: str | None = None
    telegram_enabled: bool = False
    telegram_chat_id: str = ""
    telegram_token_env: str = "TELEGRAM_BOT_TOKEN"


@dataclass(frozen=True)
class RuntimeConfig:
    """Réglages d'exécution."""

    state_file: str = "state.json"
    token_env: str = "DISCORD_USER_TOKEN"


@dataclass(frozen=True)
class Config:
    channels: list[ChannelConfig]
    ai: AIConfig
    analysis: AnalysisConfig
    notify: NotifyConfig
    runtime: RuntimeConfig

    @property
    def channel_ids(self) -> set[int]:
        return {channel.id for channel in self.channels}

    def channel(self, channel_id: int) -> ChannelConfig | None:
        for channel in self.channels:
            if channel.id == channel_id:
                return channel
        return None


# --------------------------------------------------------------------------
# Helpers de validation
# --------------------------------------------------------------------------


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"« {path} » doit être un objet JSON.")
    return value


def _snowflake(value: Any, path: str) -> int:
    """Accepte un identifiant Discord écrit en nombre ou en chaîne."""
    if isinstance(value, int) and not isinstance(value, bool):
        if value > 0:
            return value
    elif isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit() and int(cleaned) > 0:
            return int(cleaned)
    raise ConfigError(
        f"« {path} » doit être un identifiant Discord valide "
        "(clic droit sur le salon > Copier l'identifiant)."
    )


def _optional_snowflake(value: Any, path: str) -> int | None:
    if value is None or value == "":
        return None
    return _snowflake(value, path)


def _snowflake_list(value: Any, path: str) -> frozenset[int]:
    """Liste d'identifiants, écrite en nombres ou en chaînes."""
    if value is None or value == "":
        return frozenset()
    if not isinstance(value, list):
        raise ConfigError(f"« {path} » doit être une liste d'identifiants Discord.")
    return frozenset(
        _snowflake(item, f"{path}[{index}]") for index, item in enumerate(value)
    )


def _chat_id(value: Any, path: str) -> str:
    """Identifiant de conversation Telegram : nombre ou texte (« -100123… », « @canal »)."""
    if value is None:
        return ""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    raise ConfigError(f"« {path} » doit être un identifiant (nombre) ou un texte.")


def _text(value: Any, path: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"« {path} » doit être un texte.")
    return value.strip()


def _boolean(value: Any, path: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"« {path} » doit être true ou false.")
    return value


def _number(
    value: Any,
    path: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"« {path} » doit être un nombre.")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ConfigError(f"« {path} » doit être supérieur ou égal à {minimum}.")
    if maximum is not None and number > maximum:
        raise ConfigError(f"« {path} » doit être inférieur ou égal à {maximum}.")
    return number


def _preset(provider: str) -> dict[str, str]:
    if not provider:
        return {}
    try:
        return PROVIDER_PRESETS[provider]
    except KeyError:
        connus = ", ".join(sorted(PROVIDER_PRESETS))
        raise ConfigError(
            f"« ai.provider » vaut « {provider} », qui est inconnu. "
            f"Valeurs possibles : {connus}."
        ) from None


def _integer(
    value: Any,
    path: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = 10**9,
) -> int:
    number = _number(value, path, float(default), minimum=minimum, maximum=maximum)
    if number != int(number):
        raise ConfigError(f"« {path} » doit être un nombre entier.")
    return int(number)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_config(data: Any) -> Config:
    """Construit une :class:`Config` depuis un dictionnaire déjà décodé."""
    root = _mapping(data, "config")

    discord_section = _mapping(root.get("discord", {}), "discord")
    raw_channels = discord_section.get("channels")
    if not isinstance(raw_channels, list) or not raw_channels:
        raise ConfigError(
            "« discord.channels » doit contenir au moins un salon à surveiller."
        )

    channels: list[ChannelConfig] = []
    seen: set[int] = set()
    for index, raw in enumerate(raw_channels):
        path = f"discord.channels[{index}]"
        channel_data = _mapping(raw, path)
        channel_id = _snowflake(channel_data.get("id"), f"{path}.id")
        if channel_id in seen:
            raise ConfigError(f"Le salon {channel_id} est configuré deux fois.")
        seen.add(channel_id)
        channels.append(
            ChannelConfig(
                id=channel_id,
                name=_text(channel_data.get("name"), f"{path}.name"),
                context=_text(channel_data.get("context"), f"{path}.context"),
            )
        )

    ai_data = _mapping(root.get("ai"), "ai")
    provider = _text(ai_data.get("provider"), "ai.provider").lower()
    preset = _preset(provider)

    base_url = _text(ai_data.get("base_url"), "ai.base_url") or preset.get("base_url", "")
    model = _text(ai_data.get("model"), "ai.model") or preset.get("model", "")
    api_key_env = (
        _text(ai_data.get("api_key_env"), "ai.api_key_env")
        or preset.get("api_key_env", "AI_API_KEY")
    )

    if not base_url:
        raise ConfigError(
            "« ai.base_url » est obligatoire (ou choisis un « ai.provider »)."
        )
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError("« ai.base_url » doit commencer par http:// ou https://.")
    if not model:
        raise ConfigError("« ai.model » est obligatoire (ou choisis un « ai.provider »).")

    ai = AIConfig(
        base_url=base_url.rstrip("/"),
        model=model,
        provider=provider,
        api_key_env=api_key_env,
        json_mode=_boolean(ai_data.get("json_mode"), "ai.json_mode", True),
        temperature=_number(
            ai_data.get("temperature"), "ai.temperature", 0.0, minimum=0.0, maximum=2.0
        ),
        timeout_seconds=_number(
            ai_data.get("timeout_seconds"),
            "ai.timeout_seconds",
            30.0,
            minimum=1.0,
            maximum=300.0,
        ),
        retries=_integer(ai_data.get("retries"), "ai.retries", 2, minimum=0, maximum=5),
        retry_backoff_seconds=_number(
            ai_data.get("retry_backoff_seconds"),
            "ai.retry_backoff_seconds",
            1.0,
            minimum=0.0,
            maximum=30.0,
        ),
    )

    analysis_data = _mapping(root.get("analysis", {}), "analysis")
    analysis = AnalysisConfig(
        instructions=_text(analysis_data.get("instructions"), "analysis.instructions"),
        ignore_bots=_boolean(
            analysis_data.get("ignore_bots"), "analysis.ignore_bots", True
        ),
        ignore_user_ids=_snowflake_list(
            analysis_data.get("ignore_user_ids"), "analysis.ignore_user_ids"
        ),
        context_messages=_integer(
            analysis_data.get("context_messages"),
            "analysis.context_messages",
            3,
            minimum=0,
            maximum=20,
        ),
        max_message_chars=_integer(
            analysis_data.get("max_message_chars"),
            "analysis.max_message_chars",
            2000,
            minimum=100,
            maximum=10000,
        ),
        min_confidence=_number(
            analysis_data.get("min_confidence"),
            "analysis.min_confidence",
            0.5,
            minimum=0.0,
            maximum=1.0,
        ),
    )

    notify_data = _mapping(root.get("notify", {}), "notify")
    telegram_data = _mapping(notify_data.get("telegram", {}), "notify.telegram")
    telegram_enabled = _boolean(
        telegram_data.get("enabled"), "notify.telegram.enabled", False
    )
    telegram_chat_id = _chat_id(telegram_data.get("chat_id"), "notify.telegram.chat_id")
    if telegram_enabled and not telegram_chat_id:
        raise ConfigError(
            "« notify.telegram.chat_id » est obligatoire quand « notify.telegram.enabled » "
            "vaut true."
        )

    notify = NotifyConfig(
        dm_user_id=_optional_snowflake(notify_data.get("dm_user_id"), "notify.dm_user_id"),
        log_channel_id=_optional_snowflake(
            notify_data.get("log_channel_id"), "notify.log_channel_id"
        ),
        webhook_url_env=_text(
            notify_data.get("webhook_url_env"),
            "notify.webhook_url_env",
            "DISCORD_WEBHOOK_URL",
        ),
        sound=_boolean(notify_data.get("sound"), "notify.sound", True),
        sound_file=_text(notify_data.get("sound_file"), "notify.sound_file") or None,
        telegram_enabled=telegram_enabled,
        telegram_chat_id=telegram_chat_id,
        telegram_token_env=_text(
            telegram_data.get("token_env"),
            "notify.telegram.token_env",
            "TELEGRAM_BOT_TOKEN",
        )
        or "TELEGRAM_BOT_TOKEN",
    )

    runtime_data = _mapping(root.get("runtime", {}), "runtime")
    runtime = RuntimeConfig(
        state_file=_text(runtime_data.get("state_file"), "runtime.state_file", "state.json")
        or "state.json",
        token_env=_text(
            runtime_data.get("token_env"), "runtime.token_env", "DISCORD_USER_TOKEN"
        )
        or "DISCORD_USER_TOKEN",
    )

    return Config(
        channels=channels,
        ai=ai,
        analysis=analysis,
        notify=notify,
        runtime=runtime,
    )


def load_config(path: str | Path) -> Config:
    """Lit ``config.json`` et renvoie la configuration validée."""
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(
            f"Fichier de configuration introuvable : {config_path}\n"
            "Copie « config.example.json » en « config.json » puis remplis-le."
        )
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{config_path} n'est pas un JSON valide (ligne {exc.lineno}, "
            f"colonne {exc.colno}) : {exc.msg}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"Impossible de lire {config_path} : {exc}") from exc

    return parse_config(raw)

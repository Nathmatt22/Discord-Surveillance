"""Point d'entrée : connecte le compte Discord et filtre les messages via une IA.

Exemples :
    python main.py --check
    python main.py --test "La réunion est avancée à 15h"
    python main.py
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

import discord

from selfai.bot import SelfAIBot
from selfai.classifier import Classifier, ClassifierError
from selfai.config import Config, ConfigError, load_config
from selfai.dashboard import Dashboard, DashboardLogHandler
from selfai.settings import LiveSettings
from selfai.state import StateStore

log = logging.getLogger("selfai")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyse les messages de salons Discord avec une IA et signale les importants."
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Chemin du fichier de configuration (défaut : config.json).",
    )
    parser.add_argument(
        "--test",
        metavar="TEXTE",
        help="Classe un texte sans se connecter à Discord (teste la clé IA).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Valide la configuration et l'environnement, puis quitte.",
    )
    parser.add_argument("--verbose", action="store_true", help="Journal détaillé.")
    parser.add_argument("--log-file", metavar="CHEMIN", help="Écrit aussi les logs dans ce fichier.")
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Désactive l'interface plein écran et affiche les logs dans le terminal.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_output()
    load_dotenv(override=False)
    _configure_logging(args.verbose, args.log_file)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    api_key = os.environ.get(config.ai.api_key_env, "").strip()
    token = os.environ.get(config.runtime.token_env, "").strip()

    if args.check:
        print_summary(config, api_key, token)
        return 0

    if not api_key:
        log.error(
            "Clé IA manquante : renseigne %s dans le fichier .env (voir .env.example).",
            config.ai.api_key_env,
        )
        return 2

    classifier = Classifier(config.ai, config.analysis, api_key)

    if args.test is not None:
        return _run_test(config, classifier, args.test)

    if not token:
        log.error(
            "Jeton Discord manquant : renseigne %s dans le fichier .env "
            "(jeton de TON compte, pas d'un bot). Voir la section « Obtenir le jeton » du README.",
            config.runtime.token_env,
        )
        return 2

    state_path = Path(args.config).parent / config.runtime.state_file
    state = StateStore(state_path)
    state.load()

    # Interface plein écran : les logs partent dans un fichier pour ne pas
    # casser l'affichage.
    settings = LiveSettings.from_config(config)
    dashboard: Dashboard | None = None
    log_path = args.log_file
    if not args.no_ui and sys.stdout.isatty():
        log_path = args.log_file or "selfai.log"
        dashboard = Dashboard(
            log_path=log_path,
            settings=settings,
            config_path=Path(args.config),
            # Les jetons se saisissent dans Paramètres et s'écrivent ici.
            env_path=Path(args.config).parent / ".env",
            secret_variables={
                "discord_token": config.runtime.token_env,
                "ai_key": config.ai.api_key_env,
                "telegram_token": config.notify.telegram_token_env,
                "webhook": config.notify.webhook_url_env,
            },
            classifier=classifier,
        )
        _configure_logging(args.verbose, log_path, dashboard)

    log.warning(
        "Rappel : automatiser un compte Discord est contraire aux CGU de Discord "
        "et peut entraîner un bannissement. Utilise de préférence un compte secondaire."
    )

    bot = SelfAIBot(
        config, classifier, state, dashboard=dashboard, settings=settings
    )

    if dashboard is not None:
        dashboard.start()

    code = 0
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        log.error(
            "Connexion refusée : le jeton %s est invalide ou a expiré. "
            "Rouvre Discord dans le navigateur pour récupérer un jeton à jour.",
            config.runtime.token_env,
        )
        code = 1
    except KeyboardInterrupt:
        log.info("Arrêt demandé.")
    finally:
        if dashboard is not None:
            dashboard.stop()
        state.flush()

    if dashboard is not None:
        analyzed, alerts = dashboard.stats()
        print(
            f"Session terminée : {analyzed} message(s) analysé(s), "
            f"{alerts} alerte(s). Journal : {log_path}"
        )
        if code != 0:
            print("Connexion refusée : jeton invalide ou expiré (détail dans le journal).")
    return code


def _run_test(config: Config, classifier: Classifier, text: str) -> int:
    log.info("Test de l'IA sur un message factice…")
    try:
        verdict = classifier.classify(text=text, channel_name="test", author="toi")
    except ClassifierError as exc:
        log.error("Échec de l'appel à l'IA : %s", exc)
        return 1

    notified = verdict.important and verdict.confidence >= config.analysis.min_confidence
    print()
    print(f"Important   : {'oui' if verdict.important else 'non'}")
    print(f"Confiance   : {verdict.confidence:.0%}")
    print(f"Raison      : {verdict.reason}")
    print(
        f"Notification: {'oui' if notified else 'non'} "
        f"(seuil {config.analysis.min_confidence:.0%})"
    )
    return 0


def print_summary(config: Config, api_key: str, token: str) -> None:
    print("Configuration valide.")
    print(f"  Salons surveillés ({len(config.channels)}) :")
    for channel in config.channels:
        label = f"#{channel.name}" if channel.name else "(sans nom)"
        print(f"    - {label} ({channel.id})")
    provider = f" ({config.ai.provider})" if config.ai.provider else ""
    print(f"  IA          : {config.ai.model} via {config.ai.base_url}{provider}")
    print(f"  Clé IA      : {'présente' if api_key else 'MANQUANTE'} ({config.ai.api_key_env})")
    print(
        f"  Jeton Discord: {'présent' if token else 'MANQUANT'} "
        f"({config.runtime.token_env})"
    )
    cibles = []
    if config.notify.dm_user_id:
        cibles.append(f"MP à {config.notify.dm_user_id}")
    if config.notify.log_channel_id:
        cibles.append(f"salon {config.notify.log_channel_id}")
    if config.notify.webhook_url_env and os.environ.get(config.notify.webhook_url_env):
        cibles.append("webhook")
    print(
        "  Alertes Discord : "
        + (" + ".join(cibles) if cibles else "aucune cible (affichage local)")
    )
    if config.notify.telegram_enabled:
        token = os.environ.get(config.notify.telegram_token_env, "").strip()
        telegram = (
            f"actif (conversation {config.notify.telegram_chat_id})"
            if token
            else f"clé {config.notify.telegram_token_env} MANQUANTE"
        )
    else:
        telegram = "désactivé"
    print(f"  Telegram        : {telegram}")
    son = (config.notify.sound_file or "bip système") if config.notify.sound else "désactivé"
    print(f"  Son             : {son}")
    print(f"  Seuil       : {config.analysis.min_confidence:.0%}")
    manquants = []
    if not api_key:
        manquants.append(config.ai.api_key_env)
    if not token:
        manquants.append(config.runtime.token_env)
    if manquants:
        print(f"\nIl reste à renseigner dans .env : {', '.join(manquants)}.")


def _configure_logging(
    verbose: bool, log_file: str | None, dashboard: Dashboard | None = None
) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = []
    if dashboard is None:
        handlers.append(logging.StreamHandler(sys.stdout))
    if log_file:
        handlers.append(
            RotatingFileHandler(
                log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
        )
    if dashboard is not None:
        handlers.append(DashboardLogHandler(dashboard))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("discord").setLevel(logging.DEBUG if verbose else logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _configure_output() -> None:
    """Évite un crash d'encodage sur la console Windows (emoji, accents)."""
    if sys.platform == "win32" and sys.stdout.isatty():
        # La sortie est reconvertie en UTF-8 plus bas : la console doit lire en
        # UTF-8 aussi, sinon les accents et les cadres s'affichent en mojibake.
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


if __name__ == "__main__":
    raise SystemExit(main())

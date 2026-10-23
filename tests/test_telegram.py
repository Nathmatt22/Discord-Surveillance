"""Tests des alertes Telegram (aucun appel réseau)."""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from selfai.analyzer import Alert, IncomingMessage
from selfai.bot import AccountNotifier
from selfai.config import ConfigError, NotifyConfig, parse_config
from selfai.telegram import API_ROOT, TelegramNotifier, format_alert


class Verdict:
    important = True
    confidence = 0.87
    reason = "Alerte de sécurité"


def make_alert(content: str = "connexion suspecte", url: str = "") -> Alert:
    return Alert(
        message=IncomingMessage(
            id=1,
            channel_id=2,
            channel_name="général",
            author_name="Lucas",
            content=content,
            jump_url=url,
        ),
        verdict=Verdict(),  # type: ignore[arg-type]
    )


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "ok") -> None:
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, response: FakeResponse | None = None) -> None:
        self.response = response or FakeResponse()
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict, timeout: float) -> FakeResponse:
        self.calls.append((url, json))
        return self.response


class FormatTests(unittest.TestCase):
    def test_infos_essentielles(self) -> None:
        text = format_alert(make_alert())
        self.assertIn("Message important", text)
        self.assertIn("#général", text)
        self.assertIn("Lucas", text)
        self.assertIn("Alerte de sécurité", text)
        self.assertIn("confiance 87%", text)
        self.assertIn("connexion suspecte", text)

    def test_html_echappe(self) -> None:
        text = format_alert(make_alert(content="<b>gras</b> & co"))
        self.assertIn("&lt;b&gt;gras&lt;/b&gt; &amp; co", text)
        self.assertNotIn("<b>gras</b>", text)

    def test_lien_vers_discord(self) -> None:
        text = format_alert(make_alert(url="https://discord.com/channels/1/2/3"))
        self.assertIn('<a href="https://discord.com/channels/1/2/3">', text)

    def test_sans_lien(self) -> None:
        self.assertNotIn("<a href=", format_alert(make_alert()))

    def test_date_jour_et_heure(self) -> None:
        alert = Alert(
            message=IncomingMessage(
                id=1,
                channel_id=2,
                channel_name="general",
                author_name="Lucas",
                content="x",
                created_at=datetime(2026, 10, 23, 9, 30, tzinfo=timezone.utc),
            ),
            verdict=Verdict(),  # type: ignore[arg-type]
        )
        self.assertIn("23/10/2026", format_alert(alert))

    def test_contenu_tronque(self) -> None:
        text = format_alert(make_alert(content="a" * 5000))
        self.assertLess(len(text), 4096)
        self.assertIn("…", text)


class SendTests(unittest.TestCase):
    def test_envoi(self) -> None:
        session = FakeSession()
        TelegramNotifier("TOKEN", "-100123", session=session).send(make_alert())
        url, payload = session.calls[0]
        self.assertEqual(url, f"{API_ROOT}/botTOKEN/sendMessage")
        self.assertEqual(payload["chat_id"], "-100123")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertIn("Alerte de sécurité", payload["text"])

    def test_erreur_http(self) -> None:
        session = FakeSession(FakeResponse(400, "chat not found"))
        with self.assertRaises(RuntimeError) as caught:
            TelegramNotifier("TOKEN", "42", session=session).send(make_alert())
        self.assertIn("400", str(caught.exception))

    def test_panne_reseau_sans_jeton_dans_le_message(self) -> None:
        class Broken:
            def post(self, url: str, json: dict, timeout: float):
                raise OSError(f"connexion impossible vers {url}")

        with self.assertRaises(RuntimeError) as caught:
            TelegramNotifier("SECRET-TOKEN", "42", session=Broken()).send(make_alert())
        self.assertNotIn("SECRET-TOKEN", str(caught.exception))
        self.assertIn("OSError", str(caught.exception))


class ConfigTests(unittest.TestCase):
    def example(self) -> dict:
        path = Path(__file__).resolve().parent.parent / "config.example.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_desactive_par_defaut(self) -> None:
        config = parse_config(self.example())
        self.assertFalse(config.notify.telegram_enabled)
        self.assertEqual(config.notify.telegram_token_env, "TELEGRAM_BOT_TOKEN")
        self.assertEqual(config.notify.telegram_chat_id, "")

    def test_chat_id_numerique(self) -> None:
        data = self.example()
        data["notify"]["telegram"] = {"enabled": True, "chat_id": -1001234567890}
        config = parse_config(data)
        self.assertTrue(config.notify.telegram_enabled)
        self.assertEqual(config.notify.telegram_chat_id, "-1001234567890")

    def test_chat_id_obligatoire_si_actif(self) -> None:
        data = self.example()
        data["notify"]["telegram"] = {"enabled": True}
        with self.assertRaises(ConfigError):
            parse_config(data)


class WiringTests(unittest.TestCase):
    def make_notifier(self, **kwargs) -> AccountNotifier:
        defaults = {"telegram_enabled": True, "telegram_chat_id": "42"}
        defaults.update(kwargs)
        return AccountNotifier(None, NotifyConfig(**defaults))  # type: ignore[arg-type]

    def test_notifier_cree_avec_le_jeton(self) -> None:
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "abc"}):
            self.assertIsNotNone(self.make_notifier().telegram_notifier())

    def test_sans_jeton_aucun_envoi(self) -> None:
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            self.assertIsNone(self.make_notifier().telegram_notifier())

    def test_desactive(self) -> None:
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "abc"}):
            self.assertIsNone(
                self.make_notifier(telegram_enabled=False).telegram_notifier()
            )

    def test_recree_si_la_conversation_change(self) -> None:
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "abc"}):
            notifier = self.make_notifier()
            premier = notifier.telegram_notifier()
            self.assertIs(notifier.telegram_notifier(), premier)
            notifier._settings.telegram_chat_id = "99"
            second = notifier.telegram_notifier()
            self.assertIsNot(second, premier)
            self.assertIs(second, notifier.telegram_notifier())


class NotifyFlowTests(unittest.IsolatedAsyncioTestCase):
    """L'alerte doit vraiment partir quand l'analyse en produit une."""

    async def test_notify_envoie_sur_telegram(self) -> None:
        class Client:
            """Aucun MP ni salon configuré : rien d'autre à simuler."""

        from selfai.settings import LiveSettings

        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "abc"}):
            notifier = AccountNotifier(
                Client(),  # type: ignore[arg-type]
                NotifyConfig(
                    telegram_enabled=True,
                    telegram_chat_id="42",
                    sound=False,
                ),
                settings=LiveSettings(
                    sound=False, telegram_enabled=True, telegram_chat_id="42"
                ),
            )

            sender = notifier.telegram_notifier()
            self.assertIsNotNone(sender)
            envoye = []
            sender.send = envoye.append  # type: ignore[method-assign]

            await notifier.notify(make_alert())

        self.assertEqual(len(envoye), 1)
        self.assertIn("Alerte de sécurité", format_alert(envoye[0]))


if __name__ == "__main__":
    unittest.main()

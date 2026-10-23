"""Édition des jetons et des réglages depuis l'écran Paramètres."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from selfai.dashboard import Dashboard
from selfai.settings import LiveSettings

ENV_VARIABLES = {
    "discord_token": "DISCORD_USER_TOKEN",
    "ai_key": "VYCEAI_API_KEY",
    "telegram_token": "TELEGRAM_BOT_TOKEN",
    "webhook": "DISCORD_WEBHOOK_URL",
}


class FakeClassifier:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def set_api_key(self, value: str) -> None:
        self.keys.append(value)


class SettingsEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.config_path = root / "config.json"
        self.env_path = root / ".env"

        data = json.loads(
            (Path(__file__).resolve().parent.parent / "config.example.json").read_text(
                encoding="utf-8"
            )
        )
        data["notify"]["telegram"] = {"enabled": False, "chat_id": ""}
        self.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.env_path.write_text(
            "# mes secrets\nDISCORD_USER_TOKEN=ancien-jeton\nVYCEAI_API_KEY=\n",
            encoding="utf-8",
        )

        # L'environnement est partagé : on le restaure après chaque test.
        self._env = mock.patch.dict(
            os.environ,
            {"DISCORD_USER_TOKEN": "ancien-jeton", "VYCEAI_API_KEY": ""},
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)

        self.settings = LiveSettings()
        self.classifier = FakeClassifier()
        self.dashboard = Dashboard(
            settings=self.settings,
            config_path=self.config_path,
            env_path=self.env_path,
            secret_variables=ENV_VARIABLES,
            classifier=self.classifier,
        )
        self.dashboard._view = "settings"

    # -- aides -------------------------------------------------------------

    def index(self, key: str) -> int:
        return next(
            index for index, row in enumerate(self.dashboard._rows) if row.key == key
        )

    def type_text(self, text: str) -> None:
        for character in text:
            self.dashboard.key(character)

    def snap(self):
        return self.dashboard.snapshot()

    # -- lecture de l'état ---------------------------------------------------

    def test_jetons_presents_ou_absents(self) -> None:
        status = self.snap().secret_status
        self.assertTrue(status["discord_token"])
        self.assertFalse(status["ai_key"])
        self.assertFalse(status["telegram_token"])

    def test_valeurs_jamais_affichees(self) -> None:
        frame = self.dashboard.frame(100, 30)
        texte = "".join(line for line in frame.to_lines())
        self.assertNotIn("ancien-jeton", texte)

    # -- saisie --------------------------------------------------------------

    def test_saisie_masquee(self) -> None:
        self.dashboard.start_edit(self.index("ai_key"))
        self.type_text("secret")
        self.assertEqual(self.snap().edit_display, "•" * 6)

    def test_saisie_ecrite_dans_env_et_appliquee(self) -> None:
        self.dashboard.start_edit(self.index("ai_key"))
        self.type_text("sk-test")
        self.dashboard.key("enter")

        contenu = self.env_path.read_text(encoding="utf-8")
        self.assertIn("VYCEAI_API_KEY=", contenu)
        self.assertIn("sk-test", contenu)
        self.assertIn("# mes secrets", contenu)
        self.assertIn("DISCORD_USER_TOKEN", contenu)
        self.assertEqual(self.classifier.keys, ["sk-test"])
        self.assertTrue(self.snap().secret_status["ai_key"])
        self.assertEqual(self.snap().editing_row, -1)

    def test_jeton_discord_actif_au_redemarrage(self) -> None:
        self.dashboard.start_edit(self.index("discord_token"))
        self.type_text("nouveau")
        self.dashboard.key("enter")
        self.assertIn("prochain démarrage", self.snap().status)
        self.assertEqual(self.env_path.read_text(encoding="utf-8").count("nouveau"), 1)

    def test_entree_sans_texte_garde_le_jeton(self) -> None:
        self.dashboard.start_edit(self.index("discord_token"))
        self.dashboard.key("enter")
        self.assertTrue(self.snap().secret_status["discord_token"])
        self.assertIn(
            "DISCORD_USER_TOKEN=ancien-jeton",
            self.env_path.read_text(encoding="utf-8"),
        )
        self.assertEqual(self.snap().editing_row, -1)

    def test_echap_annule_sans_ecrire(self) -> None:
        self.dashboard.start_edit(self.index("ai_key"))
        self.type_text("brouillon")
        self.dashboard.key("escape")
        self.assertEqual(self.snap().editing_row, -1)
        self.assertFalse(self.snap().secret_status["ai_key"])
        self.assertNotIn("brouillon", self.env_path.read_text(encoding="utf-8"))

    def test_retour_arriere(self) -> None:
        self.dashboard.start_edit(self.index("telegram_chat_id"))
        self.type_text("123")
        self.dashboard.key("backspace")
        self.dashboard.key("enter")
        self.assertEqual(self.settings.telegram_chat_id, "12")
        self.assertEqual(self.snap().editing_row, -1)

    def test_chat_id_affiche_en_clair(self) -> None:
        self.settings.telegram_chat_id = "987654"
        self.dashboard.start_edit(self.index("telegram_chat_id"))
        self.assertEqual(self.snap().edit_display, "987654")

    def test_clic_sur_la_valeur_ouvre_la_saisie(self) -> None:
        cible = self.index("telegram_token")
        frame = self.dashboard.frame(100, 30)
        spot = next(h for h in frame.hotspots if h.action == "edit" and h.payload == cible)
        self.dashboard.click(spot.x + 1, spot.y)
        self.assertEqual(self.snap().editing_row, cible)

    def test_clic_ailleurs_annule_la_saisie(self) -> None:
        self.dashboard.start_edit(self.index("ai_key"))
        self.dashboard.activate("home")
        self.assertEqual(self.snap().editing_row, -1)

    # -- réglages ------------------------------------------------------------

    def test_telegram_active_et_enregistre(self) -> None:
        self.dashboard._setting_index = self.index("telegram_enabled")
        self.dashboard.key("enter")
        self.assertTrue(self.settings.telegram_enabled)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["notify"]["telegram"]["enabled"])

    def test_webhook_applique_a_chaud(self) -> None:
        self.dashboard.start_edit(self.index("webhook"))
        self.type_text("https://exemple.test/hook")
        self.dashboard.key("enter")
        self.assertEqual(self.settings.webhook_url, "https://exemple.test/hook")
        self.assertIn("webhook", self.snap().secret_status)

    def test_toutes_les_lignes_tiennent_a_l_ecran(self) -> None:
        for width, height in ((80, 24), (100, 30), (120, 36)):
            lignes = self.dashboard.frame(width, height).to_lines()
            self.assertEqual(len(lignes), height)
            texte = "\n".join(lignes)
            self.assertIn("Jeton Discord", texte)
            self.assertIn("Ignorer les bots", texte)


if __name__ == "__main__":
    unittest.main()

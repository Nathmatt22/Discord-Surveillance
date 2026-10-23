"""Tests de la lecture et de l'écriture des secrets dans .env."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from selfai.secrets import SecretField, SecretStore

FIELDS = [
    SecretField("discord_token", "DISCORD_USER_TOKEN", "Jeton Discord"),
    SecretField("ai_key", "AI_API_KEY", "Clé IA"),
]


class SecretStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / ".env"
        self.environ: dict[str, str] = {}
        self.store = SecretStore(self.path, FIELDS, environ=self.environ)

    def test_statut_vide_sans_fichier(self) -> None:
        self.assertEqual(
            self.store.status(), {"discord_token": False, "ai_key": False}
        )

    def test_ecriture_cree_le_fichier(self) -> None:
        self.assertTrue(self.store.set("discord_token", "abc"))
        self.assertTrue(self.path.exists())
        self.assertIn("DISCORD_USER_TOKEN", self.path.read_text(encoding="utf-8"))

    def test_relecture(self) -> None:
        self.store.set("discord_token", "abc")
        self.assertTrue(self.store.status()["discord_token"])
        self.assertEqual(self.store.values()["discord_token"], "abc")

    def test_commentaires_et_autres_lignes_conserves(self) -> None:
        self.path.write_text(
            "# mon commentaire\nDISCORD_USER_TOKEN=ancien\nAUTRE=chose\n",
            encoding="utf-8",
        )
        self.store.set("discord_token", "nouveau")
        texte = self.path.read_text(encoding="utf-8")
        self.assertIn("# mon commentaire", texte)
        self.assertIn("AUTRE=chose", texte)
        self.assertIn("nouveau", texte)
        self.assertNotIn("ancien", texte)

    def test_env_prioritaire_sur_le_fichier(self) -> None:
        self.path.write_text("AI_API_KEY=fichier\n", encoding="utf-8")
        self.environ["AI_API_KEY"] = "environnement"
        self.assertEqual(self.store.values()["ai_key"], "environnement")

    def test_valeur_vide_efface(self) -> None:
        self.store.set("ai_key", "sk-123")
        self.store.set("ai_key", "")
        self.assertFalse(self.store.status()["ai_key"])

    def test_cle_inconnue_refusee(self) -> None:
        self.assertFalse(self.store.set("inconnue", "x"))

    def test_valeur_jamais_dans_le_retour_de_statut(self) -> None:
        self.store.set("discord_token", "tres-secret")
        self.assertNotIn("tres-secret", str(self.store.status()))


if __name__ == "__main__":
    unittest.main()

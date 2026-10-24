"""Tests de l'interface plein écran (aucun terminal requis)."""

from __future__ import annotations

import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from selfai.console_input import Click
from selfai.dashboard import (
    AlertEntry,
    Dashboard,
    DashboardLogHandler,
    Snapshot,
    fit_text,
    render,
    set_ascii_only,
    supports_unicode,
    terminal_size,
    text_width,
    wrap_text,
)
from selfai.settings import LiveSettings

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def plain(line: str) -> str:
    return ANSI.sub("", line)


def snapshot(connected: bool = True, alerts: int = 2) -> Snapshot:
    now = time.time()
    return Snapshot(
        connected=connected,
        user="mon_compte",
        channels=["#💬│general", "#Rangee"],
        analyzed=12,
        important=alerts,
        alerts=[
            AlertEntry(now, "#💬│general", "Lucas", "Alerte de sécurité", 0.95),
            AlertEntry(now - 60, "#Rangee", "Nina", "Réunion avancée à 15h", 0.82),
        ][:alerts],
        last_event="#💬│general · Lucas → important (95%)",
        last_alert_at=now,
        log_path="selfai.log",
    )


class FitTextTests(unittest.TestCase):
    def test_texte_court_inchange(self) -> None:
        self.assertEqual(fit_text("salut", 10), "salut")

    def test_troncature_avec_ellipse(self) -> None:
        result = fit_text("abcdefghij", 6)
        self.assertEqual(text_width(result), 6)
        self.assertTrue(result.endswith("…"))

    def test_emoji_compte_double(self) -> None:
        self.assertEqual(text_width("💬"), 2)
        self.assertLessEqual(text_width(fit_text("💬│général", 6)), 6)


class RenderTests(unittest.TestCase):
    def test_chaque_ligne_efface_la_fin(self) -> None:
        # Sans \x1b[K, le texte d'une vue précédente reste visible dessous.
        for line in render(snapshot(), 100, 30, 1.0).to_lines():
            self.assertTrue(line.endswith("\x1b[K"), repr(line[-10:]))

    def test_enchainer_les_vues_ne_laisse_pas_de_residus(self) -> None:
        grid = [[" "] * 100 for _ in range(30)]
        for view in ("help", "settings", "messages", "home"):
            snap = snapshot()
            snap.view = view
            paint(grid, render(snap, 100, 30, 1.0).to_lines())

        fresh = [[" "] * 100 for _ in range(30)]
        snap = snapshot()
        snap.view = "home"
        paint(fresh, render(snap, 100, 30, 1.0).to_lines())

        self.assertEqual(
            ["".join(row).rstrip() for row in grid],
            ["".join(row).rstrip() for row in fresh],
        )

    def test_dimensions_respectees(self) -> None:
        for width, height in ((80, 24), (100, 30), (120, 36)):
            lines = [plain(line) for line in render(snapshot(), width, height, 1.3).to_lines()]
            self.assertEqual(len(lines), height)
            for line in lines:
                self.assertLessEqual(text_width(line), width)

    def test_panneau_alertes_present(self) -> None:
        text = "\n".join(plain(l) for l in render(snapshot(), 100, 30, 1.0).to_lines())
        self.assertIn("ALERTES", text)
        self.assertIn("Lucas", text)
        self.assertIn("#Rangee", text)
        self.assertIn("CONNECTÉ", text)

    def test_sans_alerte_affiche_un_message(self) -> None:
        text = "\n".join(
            plain(l) for l in render(snapshot(alerts=0), 100, 30, 1.0).to_lines()
        )
        self.assertIn("aucun message important", text)

    def test_mode_compact(self) -> None:
        lines = [plain(l) for l in render(snapshot(), 40, 12, 1.0).to_lines()]
        self.assertEqual(len(lines), 12)
        self.assertIn("JARVIS", lines[0])
        for line in lines:
            self.assertLessEqual(text_width(line), 40)

    def test_compteurs_visibles_a_toutes_les_tailles(self) -> None:
        for width, height in ((90, 28), (100, 30), (110, 32), (120, 36)):
            text = "\n".join(
                plain(line) for line in render(snapshot(), width, height, 1.0).to_lines()
            )
            self.assertIn("importantes", text, (width, height))
            self.assertIn("CONNECTÉ", text, (width, height))

    def test_connexion_en_cours(self) -> None:
        text = "\n".join(
            plain(l) for l in render(snapshot(connected=False), 90, 28, 0.0).to_lines()
        )
        self.assertIn("CONNEXION", text)

    def test_orb_utilise_plusieurs_luminosites(self) -> None:
        screen = render(snapshot(), 120, 36, 0.7)
        styles = {style for row in screen._cells for _, style in row if style.startswith("orb")}
        self.assertGreaterEqual(len(styles), 3)


class WrapTests(unittest.TestCase):
    def test_coupe_les_lignes_longues(self) -> None:
        lines = wrap_text("un deux trois quatre cinq six", 12)
        self.assertTrue(all(text_width(line) <= 12 for line in lines))
        self.assertEqual(" ".join(lines), "un deux trois quatre cinq six")

    def test_conserve_les_sauts_de_ligne(self) -> None:
        self.assertEqual(wrap_text("a\n\nb", 10), ["a", "", "b"])

    def test_mot_plus_long_que_la_largeur(self) -> None:
        lines = wrap_text("abcdefghij", 4)
        self.assertTrue(all(text_width(line) <= 4 for line in lines))


DEF_ALERT = AlertEntry(1_700_000_000, "#general", "Lucas", "raison", 0.9, "contenu complet", "https://discord.com/channels/1/2/3")


def screen_text(snap: Snapshot, width: int = 100, height: int = 30) -> str:
    return "\n".join(plain(line) for line in render(snap, width, height, 1.0).to_lines())


def row_index(dashboard: Dashboard, key: str) -> int:
    """Position d'un réglage dans la liste des paramètres."""
    return next(
        index for index, row in enumerate(dashboard._rows) if row.key == key
    )


def paint(grid: list[list[str]], lines: list[str]) -> None:
    """Applique une image à une grille, comme le ferait le terminal."""
    for y, line in enumerate(lines):
        column = 0
        index = 0
        while index < len(line):
            if line.startswith("\x1b[K", index):  # efface jusqu'à la fin du rang
                for x in range(column, len(grid[y])):
                    grid[y][x] = " "
                index += 3
                continue
            if line[index] == "\x1b":  # couleur : sans effet sur le texte
                index = line.index("m", index) + 1
                continue
            if column < len(grid[y]):
                grid[y][column] = line[index]
            column += 1
            index += 1


class ViewRenderTests(unittest.TestCase):
    def test_vue_messages(self) -> None:
        snap = snapshot()
        snap.view = "messages"
        text = screen_text(snap)
        self.assertIn("messages importants", text)
        self.assertIn("Lucas", text)
        self.assertIn("›", text)
        self.assertIn("Ouvrir", text)
        self.assertIn("▲", text)
        # L'aide ne doit pas déborder sur les autres vues.
        self.assertNotIn("molette", text)
        self.assertNotIn("accueil, messages, paramètres", text)

    def test_vue_detail_montre_le_contenu_et_le_lien(self) -> None:
        snap = snapshot()
        snap.view = "detail"
        snap.alerts = [DEF_ALERT]
        text = screen_text(snap)
        self.assertIn("contenu complet", text)
        self.assertIn("https://discord.com/channels/1/2/3", text)
        self.assertIn("90 % de confiance", text)

    def test_vue_parametres(self) -> None:
        snap = snapshot()
        snap.view = "settings"
        snap.settings = LiveSettings(min_confidence=0.65, sound=False, ignore_bots=True)
        text = screen_text(snap)
        self.assertIn("Seuil de confiance", text)
        self.assertIn("65 %", text)
        self.assertIn("désactivé", text)
        self.assertIn("Ignorer les bots", text)

    def test_vue_aide(self) -> None:
        snap = snapshot()
        snap.view = "help"
        text = screen_text(snap)
        self.assertIn("À la souris", text)
        self.assertIn("Windows Terminal", text)

    def test_toutes_les_vues_tiennent_dans_l_ecran(self) -> None:
        for view in ("home", "messages", "detail", "settings", "help"):
            snap = snapshot()
            snap.view = view
            lines = [plain(l) for l in render(snap, 90, 28, 1.0).to_lines()]
            self.assertEqual(len(lines), 28, view)
            for line in lines:
                self.assertLessEqual(text_width(line), 90, view)


class DashboardStateTests(unittest.TestCase):
    class Message:
        channel_name = "general"
        author_name = "Lucas"

    class Verdict:
        important = True
        confidence = 0.9
        reason = "connexion suspecte"

    def test_record_compte_et_empile(self) -> None:
        dashboard = Dashboard()
        dashboard.record(self.Message(), self.Verdict(), notified=True)

        snap = dashboard.snapshot()
        self.assertEqual(snap.analyzed, 1)
        self.assertEqual(snap.important, 1)
        self.assertEqual(len(snap.alerts), 1)
        self.assertEqual(snap.alerts[0].reason, "connexion suspecte")
        self.assertEqual(snap.alerts[0].channel, "#general")
        self.assertIn("important", snap.last_event)

    def test_record_sans_alerte(self) -> None:
        dashboard = Dashboard()
        dashboard.record(self.Message(), self.Verdict(), notified=False)

        snap = dashboard.snapshot()
        self.assertEqual(snap.analyzed, 1)
        self.assertEqual(snap.important, 0)
        self.assertEqual(snap.alerts, [])

    def test_alertes_conservees_pour_le_defilement(self) -> None:
        dashboard = Dashboard()
        for _ in range(250):
            dashboard.record(self.Message(), self.Verdict(), notified=True)
        self.assertEqual(len(dashboard.snapshot().alerts), 200)
        self.assertEqual(dashboard.stats(), (250, 250))

    def test_contenu_et_lien_conserves(self) -> None:
        class RichMessage:
            channel_name = "general"
            author_name = "Lucas"
            content = "le texte complet du message"
            jump_url = "https://discord.com/channels/1/2/3"

        dashboard = Dashboard()
        dashboard.record(RichMessage(), self.Verdict(), notified=True)
        entry = dashboard.snapshot().alerts[0]
        self.assertEqual(entry.content, "le texte complet du message")
        self.assertEqual(entry.jump_url, "https://discord.com/channels/1/2/3")


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_path = Path(self._tmp.name) / "config.json"
        data = json.loads(
            (Path(__file__).resolve().parent.parent / "config.example.json").read_text(
                encoding="utf-8"
            )
        )
        self.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        self.settings = LiveSettings()
        self.dashboard = Dashboard(
            settings=self.settings, config_path=self.config_path
        )
        for _ in range(5):
            self.dashboard.record(
                DashboardStateTests.Message(), DashboardStateTests.Verdict(), True
            )

    def test_ouverture_des_vues(self) -> None:
        self.dashboard.handle_command("2")
        self.assertEqual(self.dashboard.snapshot().view, "messages")
        self.dashboard.handle_command("3")
        self.assertEqual(self.dashboard.snapshot().view, "settings")
        self.dashboard.handle_command("?")
        self.assertEqual(self.dashboard.snapshot().view, "help")
        self.dashboard.handle_command("q")
        self.assertEqual(self.dashboard.snapshot().view, "home")

    def test_defilement_des_messages(self) -> None:
        self.dashboard.handle_command("2")
        self.dashboard.handle_command("n")
        self.dashboard.handle_command("n")
        self.assertEqual(self.dashboard.snapshot().selection, 2)
        self.dashboard.handle_command("p")
        self.assertEqual(self.dashboard.snapshot().selection, 1)

    def test_la_selection_ne_deborde_pas(self) -> None:
        self.dashboard.handle_command("2")
        for _ in range(20):
            self.dashboard.handle_command("p")
        self.assertEqual(self.dashboard.snapshot().selection, 0)
        for _ in range(20):
            self.dashboard.handle_command("n")
        self.assertEqual(self.dashboard.snapshot().selection, 4)

    def test_ouverture_du_detail(self) -> None:
        self.dashboard.handle_command("2")
        self.dashboard.handle_command("n")
        self.dashboard.handle_command("o")
        self.assertEqual(self.dashboard.snapshot().view, "detail")
        self.dashboard.handle_command("n")
        self.assertEqual(self.dashboard.snapshot().selection, 2)
        self.dashboard.handle_command("q")
        self.assertEqual(self.dashboard.snapshot().view, "messages")

    def select(self, key: str) -> None:
        self.dashboard._setting_index = row_index(self.dashboard, key)

    def test_seuil_modifie_et_sauvegarde(self) -> None:
        self.dashboard.handle_command("3")
        self.select("confidence")
        self.dashboard.handle_command("+")
        self.assertEqual(self.settings.min_confidence, 0.55)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["analysis"]["min_confidence"], 0.55)
        self.assertIn("enregistré", self.dashboard.snapshot().status)

    def test_son_et_bots_bascules(self) -> None:
        self.dashboard.handle_command("3")
        self.select("sound")
        self.dashboard.handle_command("+")
        self.assertFalse(self.settings.sound)
        self.select("ignore_bots")
        self.dashboard.handle_command("+")
        self.assertFalse(self.settings.ignore_bots)

        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertFalse(saved["notify"]["sound"])
        self.assertFalse(saved["analysis"]["ignore_bots"])

    def test_commande_inconnue(self) -> None:
        self.dashboard.handle_command("blabla")
        self.assertIn("inconnue", self.dashboard.snapshot().status)

    def test_quitter_demande_l_arret(self) -> None:
        with mock.patch("selfai.dashboard._thread.interrupt_main") as interrupt:
            self.dashboard.handle_command("x")
        self.assertTrue(self.dashboard.quit_requested)
        interrupt.assert_called_once()


class MouseTests(unittest.TestCase):
    """Les boutons doivent être de vraies zones cliquables."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_path = Path(self._tmp.name) / "config.json"
        data = json.loads(
            (Path(__file__).resolve().parent.parent / "config.example.json").read_text(
                encoding="utf-8"
            )
        )
        self.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        self.settings = LiveSettings()
        self.dashboard = Dashboard(
            settings=self.settings, config_path=self.config_path
        )
        self.dashboard._mouse_ui = True
        for _ in range(5):
            self.dashboard.record(
                DashboardStateTests.Message(), DashboardStateTests.Verdict(), True
            )

    def frame(self, width: int = 100, height: int = 30):
        return self.dashboard.frame(width, height)

    def hotspot(self, action: str, payload=None, frame=None):
        frame = frame if frame is not None else self.frame()
        for candidate in frame.hotspots:
            if candidate.action == action and (
                payload is None or candidate.payload == payload
            ):
                return candidate
        self.fail(f"aucune zone cliquable « {action} » ({payload})")

    def click(self, action: str, payload=None) -> bool:
        hotspot = self.hotspot(action, payload)
        return self.dashboard.click(hotspot.x + hotspot.width // 2, hotspot.y)

    def test_barre_de_navigation_dans_chaque_vue(self) -> None:
        for view in ("home", "messages", "detail", "settings", "help"):
            self.dashboard._view = view
            actions = {spot.action for spot in self.frame().hotspots}
            for action in ("home", "messages", "settings", "help", "quit"):
                self.assertIn(action, actions, view)

    def test_clic_sur_parametres_ouvre_la_vue(self) -> None:
        self.assertTrue(self.click("settings"))
        self.assertEqual(self.dashboard.snapshot().view, "settings")

    def test_clic_sur_une_ligne_puis_ouverture(self) -> None:
        self.click("messages")
        self.assertTrue(self.click("select", 1))
        snap = self.dashboard.snapshot()
        self.assertEqual(snap.view, "messages")
        self.assertEqual(snap.selection, 1)
        self.click("select", 1)  # déjà sélectionnée : ouvre le message
        self.assertEqual(self.dashboard.snapshot().view, "detail")

    def test_bouton_ouvrir(self) -> None:
        self.click("messages")
        self.click("open")
        self.assertEqual(self.dashboard.snapshot().view, "detail")

    def test_fleches_de_defilement(self) -> None:
        self.click("messages")
        self.click("scroll_down")
        self.assertEqual(self.dashboard.snapshot().selection, 1)
        self.click("scroll_up")
        self.assertEqual(self.dashboard.snapshot().selection, 0)

    def test_molette(self) -> None:
        self.click("messages")
        self.dashboard.scroll(-3)
        self.assertEqual(self.dashboard.snapshot().selection, 3)
        self.dashboard.scroll(10)
        self.assertEqual(self.dashboard.snapshot().selection, 0)

    def test_boutons_moins_et_plus(self) -> None:
        self.click("settings")
        seuil = row_index(self.dashboard, "confidence")
        self.click("adjust", (seuil, 1))
        self.assertEqual(self.settings.min_confidence, 0.55)
        self.click("adjust", (seuil, -1))
        self.assertEqual(self.settings.min_confidence, 0.50)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["analysis"]["min_confidence"], 0.50)

    def test_boutons_prioritaires_sur_la_ligne(self) -> None:
        # « + » se trouve dans la zone de la ligne : c'est le bouton qui gagne.
        self.click("settings")
        seuil = row_index(self.dashboard, "confidence")
        self.click("adjust", (seuil, 1))
        self.assertEqual(self.settings.min_confidence, 0.55)
        self.assertEqual(self.dashboard.snapshot().setting_index, seuil)

    def test_clic_dans_le_vide(self) -> None:
        self.assertFalse(self.dashboard.click(1, 1))
        self.assertEqual(self.dashboard.snapshot().view, "home")

    def test_fleches_et_entree(self) -> None:
        self.dashboard.key("2")
        self.dashboard.key("down")
        self.assertEqual(self.dashboard.snapshot().selection, 1)
        self.dashboard.key("enter")
        self.assertEqual(self.dashboard.snapshot().view, "detail")
        self.dashboard.key("escape")
        self.assertEqual(self.dashboard.snapshot().view, "messages")

    def test_defilement_du_message_borne(self) -> None:
        class LongMessage:
            channel_name = "general"
            author_name = "Lucas"
            content = "ligne de texte assez longue pour occuper plusieurs lignes\n" * 40
            jump_url = ""

        self.dashboard.record(LongMessage(), DashboardStateTests.Verdict(), True)
        self.click("messages")
        self.click("open")
        for _ in range(40):
            self.dashboard.scroll(-5)
        maximum = self.dashboard.snapshot().detail_scroll
        self.assertGreater(maximum, 0)
        # On ne peut pas aller plus bas que la dernière page.
        self.dashboard.scroll(-5)
        self.assertEqual(self.dashboard.snapshot().detail_scroll, maximum)
        for _ in range(80):
            self.dashboard.scroll(5)
        self.assertEqual(self.dashboard.snapshot().detail_scroll, 0)

    def test_boutons_precedent_et_suivant(self) -> None:
        self.click("messages")
        self.click("open")
        actions = {spot.action for spot in self.frame().hotspots}
        self.assertNotIn("prev", actions)
        self.click("next")
        self.assertEqual(self.dashboard.snapshot().selection, 1)
        self.assertIn("prev", {spot.action for spot in self.frame().hotspots})

    def test_retour_depuis_le_detail(self) -> None:
        self.click("messages")
        self.click("open")
        self.click("back")
        self.assertEqual(self.dashboard.snapshot().view, "messages")

    def test_barre_de_navigation_tient_dans_la_largeur(self) -> None:
        for width in (70, 90, 120):
            frame = self.frame(width, 26)
            quit_button = self.hotspot("quit", frame=frame)
            self.assertLessEqual(quit_button.x + quit_button.width, width)


class FallbackTests(unittest.TestCase):
    def test_mode_sans_souris_affiche_l_invite(self) -> None:
        snap = snapshot()
        snap.mouse_ui = False
        lines = [plain(line) for line in render(snap, 100, 30, 1.0).to_lines()]
        text = "\n".join(lines)
        self.assertTrue(lines[-1].startswith("  >"), lines[-1])
        self.assertIn("souris indisponible", text)
        self.assertIn("tape une commande", text)
        self.assertNotIn("souris active", text)

    def test_mode_souris_masque_l_invite(self) -> None:
        snap = snapshot()
        lines = [plain(line) for line in render(snap, 100, 30, 1.0).to_lines()]
        self.assertEqual(lines[-1].strip(), "")
        self.assertNotIn("tape une commande", "\n".join(lines))

    def test_aucun_texte_dans_la_derniere_ligne(self) -> None:
        for view in ("home", "messages", "detail", "settings", "help"):
            snap = snapshot()
            snap.view = view
            lines = [plain(line) for line in render(snap, 100, 30, 1.0).to_lines()]
            self.assertEqual(lines[-1].strip(), "", view)


class LogHandlerTests(unittest.TestCase):
    def test_avertissements_masques_et_erreurs_affichees(self) -> None:
        import logging

        dashboard = Dashboard()
        handler = DashboardLogHandler(dashboard)
        logger = logging.getLogger("selfai.test.dashboard")
        logger.handlers = [handler]
        logger.propagate = False
        self.addCleanup(logger.handlers.clear)

        logger.warning("Rappel : automatiser un compte est contraire aux CGU")
        self.assertEqual(dashboard.snapshot().last_event, "")

        logger.error("connexion perdue")
        self.assertIn("connexion perdue", dashboard.snapshot().last_event)


class InteractiveLoopTests(unittest.TestCase):
    """Boucle complète : backend d'entrées -> clic -> rendu."""

    class FakeBackend:
        """Joue un clic réel sur le bouton demandé, dès la première image."""

        def __init__(self, dashboard: Dashboard, action: str) -> None:
            self.dashboard = dashboard
            self.action = action
            self.closed = False

        def events(self, running):
            deadline = time.time() + 3.0
            while running() and time.time() < deadline and not self.dashboard._hotspots:
                time.sleep(0.01)
            for spot in list(self.dashboard._hotspots):
                if spot.action == self.action:
                    yield Click(spot.x + spot.width // 2, spot.y)
                    break
            while running():
                time.sleep(0.02)

        def close(self) -> None:
            self.closed = True

    def test_clic_traverse_la_boucle_de_rendu(self) -> None:
        import io

        dashboard = Dashboard(refresh=0.02)
        dashboard._stdout = io.StringIO()
        dashboard.record(
            DashboardStateTests.Message(), DashboardStateTests.Verdict(), True
        )
        backend = self.FakeBackend(dashboard, "messages")

        with mock.patch("selfai.dashboard.create_input", return_value=backend):
            dashboard.start()
            try:
                deadline = time.time() + 3.0
                while (
                    time.time() < deadline
                    and dashboard.snapshot().view != "messages"
                ):
                    time.sleep(0.01)
                # Laisse la boucle de rendu peindre la nouvelle vue.
                while (
                    time.time() < deadline
                    and "messages importants"
                    not in ANSI.sub("", dashboard._stdout.getvalue())
                ):
                    time.sleep(0.01)
            finally:
                dashboard.stop()

        self.assertEqual(dashboard.snapshot().view, "messages")
        output = dashboard._stdout.getvalue()
        self.assertIn("\x1b[?1049h", output)
        self.assertIn("\x1b[?1049l", output)
        self.assertIn("messages importants", ANSI.sub("", output))
        self.assertTrue(backend.closed)


class AsciiFallbackTests(unittest.TestCase):
    """Terminal non UTF-8 : l'interface doit rester lisible et alignée."""

    def tearDown(self) -> None:
        set_ascii_only(False)

    def test_meme_mise_en_page_en_ascii(self) -> None:
        snap = snapshot()
        reference = [plain(line) for line in render(snap, 100, 30, 0.0).to_lines()]
        set_ascii_only(True)
        converted = [plain(line) for line in render(snap, 100, 30, 0.0).to_lines()]
        self.assertEqual(len(reference), len(converted))
        for before, after in zip(reference, converted):
            self.assertTrue(after.isascii(), after)
            # Un caractère décoratif = un caractère ASCII : rien ne déborde.
            self.assertEqual(text_width(before), text_width(after))

    def test_aucune_ligne_plus_large_que_le_terminal(self) -> None:
        set_ascii_only(True)
        for view in ("home", "messages", "settings", "help", "detail"):
            snap = snapshot()
            snap.view = view
            for line in render(snap, 90, 28, 0.0).to_lines():
                self.assertLessEqual(text_width(plain(line)), 90)

    def test_accents_et_cadres_remplaces(self) -> None:
        set_ascii_only(True)
        snap = snapshot()
        snap.view = "settings"
        reglages = "\n".join(
            plain(line) for line in render(snap, 96, 30, 0.0).to_lines()
        )
        self.assertIn("SECURITE", reglages)
        self.assertIn("REGLAGES", reglages)
        self.assertIn("parametres", reglages)
        accueil = "\n".join(
            plain(line) for line in render(snapshot(), 96, 30, 0.0).to_lines()
        )
        self.assertIn("+---", accueil)  # cadre du panneau d'alertes
        for texte in (reglages, accueil):
            self.assertNotIn("─", texte)
            self.assertNotIn("│", texte)

    def test_detection_du_terminal(self) -> None:
        class Stream:
            def __init__(self, encoding: str | None, tty: bool = True) -> None:
                self.encoding = encoding
                self._tty = tty

            def isatty(self) -> bool:
                return self._tty

            def fileno(self) -> int:
                return 0

        with mock.patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=True):
            with mock.patch("os.ttyname", return_value="/dev/pts/0"):
                self.assertTrue(supports_unicode(Stream("utf-8")))
            # Console texte du Pi, même si TERM a été écrasé.
            with mock.patch("os.ttyname", return_value="/dev/tty1"):
                self.assertFalse(supports_unicode(Stream("utf-8")))
            self.assertFalse(supports_unicode(Stream("ANSI_X3.4-1968")))
            self.assertTrue(supports_unicode(Stream(None, tty=False)))
            with mock.patch.dict("os.environ", {"TERM": "linux"}):
                self.assertFalse(supports_unicode(Stream("utf-8")))
            for forced in ("1", "oui", "YES"):
                with mock.patch.dict("os.environ", {"SELFAI_ASCII": forced}):
                    self.assertFalse(supports_unicode(Stream("utf-8")))
            for forced in ("0", "non", "NO"):
                with mock.patch.dict("os.environ", {"SELFAI_ASCII": forced}):
                    self.assertTrue(supports_unicode(Stream("ANSI_X3.4-1968")))

    def test_taille_lue_sur_le_terminal(self) -> None:
        class Stream:
            def fileno(self) -> int:
                return 1

        taille = mock.Mock(columns=123, lines=45)
        with mock.patch("os.get_terminal_size", return_value=taille):
            self.assertEqual(terminal_size(Stream()), (123, 45))

        class SansFil:
            def fileno(self) -> int:
                raise OSError("pas de terminal")

        with mock.patch.dict("os.environ", {"COLUMNS": "120", "LINES": "40"}):
            self.assertEqual(terminal_size(SansFil()), (120, 40))


class SettingsTests(unittest.TestCase):
    def test_from_config(self) -> None:
        from selfai.config import parse_config

        data = json.loads(
            (Path(__file__).resolve().parent.parent / "config.example.json").read_text(
                encoding="utf-8"
            )
        )
        config = parse_config(data)
        settings = LiveSettings.from_config(config)
        self.assertEqual(settings.min_confidence, config.analysis.min_confidence)
        self.assertEqual(settings.sound, config.notify.sound)
        self.assertEqual(settings.ignore_bots, config.analysis.ignore_bots)

    def test_reglage_borne(self) -> None:
        settings = LiveSettings(min_confidence=0.05)
        settings.adjust_confidence(-1)
        self.assertEqual(settings.min_confidence, 0.05)
        settings = LiveSettings(min_confidence=1.0)
        settings.adjust_confidence(1)
        self.assertEqual(settings.min_confidence, 1.0)


if __name__ == "__main__":
    unittest.main()

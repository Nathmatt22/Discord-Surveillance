"""Tests du décodage des entrées souris/clavier (aucun terminal requis)."""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from selfai import console_input as ci
from selfai.console_input import Click, Key, Scroll, parse_ansi


class ClickTests(unittest.TestCase):
    def test_clic_gauche(self) -> None:
        event, consumed = parse_ansi(b"\x1b[<0;10;5M")
        self.assertEqual(event, Click(x=9, y=4))
        self.assertEqual(consumed, 10)

    def test_clic_relache_ignore(self) -> None:
        event, consumed = parse_ansi(b"\x1b[<0;10;5m")
        self.assertIsNone(event)
        self.assertEqual(consumed, 10)

    def test_souris_deplacee_ignoree(self) -> None:
        event, consumed = parse_ansi(b"\x1b[<35;10;5M")
        self.assertIsNone(event)
        self.assertEqual(consumed, 11)

    def test_molette(self) -> None:
        self.assertEqual(parse_ansi(b"\x1b[<64;3;2M")[0], Scroll(steps=1, x=2, y=1))
        self.assertEqual(parse_ansi(b"\x1b[<65;3;2M")[0], Scroll(steps=-1, x=2, y=1))


class KeyTests(unittest.TestCase):
    def test_fleches(self) -> None:
        self.assertEqual(parse_ansi(b"\x1b[A")[0], Key("up"))
        self.assertEqual(parse_ansi(b"\x1b[B")[0], Key("down"))
        self.assertEqual(parse_ansi(b"\x1b[C")[0], Key("right"))
        self.assertEqual(parse_ansi(b"\x1b[D")[0], Key("left"))

    def test_fleches_mode_application(self) -> None:
        self.assertEqual(parse_ansi(b"\x1bOA")[0], Key("up"))

    def test_touches_speciales(self) -> None:
        self.assertEqual(parse_ansi(b"\r")[0], Key("enter"))
        self.assertEqual(parse_ansi(b"\x7f")[0], Key("backspace"))
        self.assertEqual(parse_ansi(b"\t")[0], Key("tab"))

    def test_caractere_simple_en_minuscule(self) -> None:
        self.assertEqual(parse_ansi(b"N")[0], Key("n"))

    def test_caractere_utf8(self) -> None:
        self.assertEqual(parse_ansi("é".encode())[0], Key("é"))

    def test_deux_evenements_dans_le_tampon(self) -> None:
        event, consumed = parse_ansi(b"nm")
        self.assertEqual(event, Key("n"))
        self.assertEqual(consumed, 1)
        self.assertEqual(parse_ansi(b"nm"[consumed:])[0], Key("m"))


@unittest.skipUnless(sys.platform == "win32", "enregistrements de console Windows")
class WindowsRecordTests(unittest.TestCase):
    """Convertit de vrais INPUT_RECORD Windows (sans console attachée)."""

    def setUp(self) -> None:
        self.api = ci._windows_api()
        self.console = ci.WindowsConsole()

    def key_record(self, char: str = "\x00", vk: int = 0):
        record = self.api["INPUT_RECORD"]()
        record.EventType = ci.KEY_EVENT
        record.Event.KeyEvent.bKeyDown = 1
        record.Event.KeyEvent.wVirtualKeyCode = vk
        record.Event.KeyEvent.uChar = char
        return record

    def mouse_record(self, x: int, y: int, buttons: int = 1, flags: int = 0):
        record = self.api["INPUT_RECORD"]()
        record.EventType = ci.MOUSE_EVENT
        record.Event.MouseEvent.dwMousePosition.X = x
        record.Event.MouseEvent.dwMousePosition.Y = y
        record.Event.MouseEvent.dwButtonState = buttons
        record.Event.MouseEvent.dwEventFlags = flags
        return record

    def test_touche_imprimee(self) -> None:
        self.assertEqual(self.console.decode_record(self.key_record("N")), Key("n"))

    def test_touche_speciale(self) -> None:
        self.assertEqual(self.console.decode_record(self.key_record(vk=0x26)), Key("up"))
        self.assertEqual(self.console.decode_record(self.key_record(vk=0x0D)), Key("enter"))

    def test_relachement_ignore(self) -> None:
        record = self.key_record("a")
        record.Event.KeyEvent.bKeyDown = 0
        self.assertIsNone(self.console.decode_record(record))

    def test_clic_decale_de_la_fenetre(self) -> None:
        record = self.mouse_record(10, 5)
        self.assertEqual(self.console.decode_record(record, (0, 2)), Click(x=10, y=3))

    def test_molette(self) -> None:
        up = self.mouse_record(4, 4, buttons=120 << 16, flags=ci.MOUSE_WHEELED)
        down = self.mouse_record(4, 4, buttons=(0x10000 - 120) << 16, flags=ci.MOUSE_WHEELED)
        self.assertEqual(self.console.decode_record(up), Scroll(steps=1, x=4, y=4))
        self.assertEqual(self.console.decode_record(down), Scroll(steps=-1, x=4, y=4))

    def test_mouvement_ignore(self) -> None:
        record = self.mouse_record(4, 4, buttons=0, flags=ci.MOUSE_MOVED)
        self.assertIsNone(self.console.decode_record(record))

    def test_double_clic(self) -> None:
        record = self.mouse_record(3, 3, flags=ci.DOUBLE_CLICK)
        self.assertEqual(
            self.console.decode_record(record), Click(x=3, y=3, double=True)
        )


class FactoryTests(unittest.TestCase):
    def test_aucun_terminal_compatible(self) -> None:
        with mock.patch.object(ci.WindowsConsole, "available", return_value=False), \
             mock.patch.object(ci.PosixTerminal, "available", return_value=False):
            self.assertIsNone(ci.create_input())

    def test_ouverture_ratee_retombe_sur_le_repli(self) -> None:
        with mock.patch.object(ci.WindowsConsole, "available", return_value=True), \
             mock.patch.object(ci.WindowsConsole, "open", side_effect=OSError("non")), \
             mock.patch.object(ci.PosixTerminal, "available", return_value=False):
            self.assertIsNone(ci.create_input())


class IncompleteTests(unittest.TestCase):
    def test_sequence_incomplete_attend_la_suite(self) -> None:
        self.assertEqual(parse_ansi(b"\x1b[<0;10"), (None, 0))
        # Un ESC seul peut être le début d'une séquence : le backend POSIX
        # attend un tour de boucle avant d'en faire la touche Échap.
        self.assertEqual(parse_ansi(b"\x1b"), (None, 0))

    def test_utf8_incomplet(self) -> None:
        self.assertEqual(parse_ansi(b"\xc3"), (None, 0))


if __name__ == "__main__":
    unittest.main()

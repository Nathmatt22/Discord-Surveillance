"""Tests du son d'alerte : aucun haut-parleur nécessaire."""

from __future__ import annotations

import io
import json
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from selfai import sound
from selfai.bot import AccountNotifier
from selfai.config import NotifyConfig


class FakeWinSound:
    MB_ICONEXCLAMATION = 48
    SND_FILENAME = 1
    SND_ASYNC = 2

    def __init__(self, fail_play: bool = False, fail_beep: bool = False) -> None:
        self.calls: list[tuple] = []
        self._fail_play = fail_play
        self._fail_beep = fail_beep

    def PlaySound(self, *args: object) -> None:
        if self._fail_play:
            raise RuntimeError("fichier invalide")
        self.calls.append(("PlaySound", args))

    def MessageBeep(self, kind: int) -> None:
        if self._fail_beep:
            raise RuntimeError("muet")
        self.calls.append(("MessageBeep", kind))

    def Beep(self, frequency: int, duration: int) -> None:
        self.calls.append(("Beep", frequency, duration))


class WindowsSoundTests(unittest.TestCase):
    def play(self, fake: FakeWinSound, path: str | None = None) -> FakeWinSound:
        with mock.patch.dict(sys.modules, {"winsound": fake}):
            sound.play_alert(path, platform="nt")
        return fake

    def test_bip_systeme_par_defaut(self) -> None:
        fake = self.play(FakeWinSound())
        self.assertEqual(fake.calls, [("MessageBeep", 48)])

    def test_fichier_wav(self) -> None:
        fake = self.play(FakeWinSound(), "jarvis.wav")
        self.assertEqual(fake.calls, [("PlaySound", ("jarvis.wav", 3))])

    def test_fichier_invalide_replie_sur_le_bip(self) -> None:
        fake = self.play(FakeWinSound(fail_play=True), "absent.wav")
        self.assertEqual(fake.calls, [("MessageBeep", 48)])

    def test_bip_impossible_utilise_beep(self) -> None:
        fake = self.play(FakeWinSound(fail_beep=True))
        self.assertEqual(fake.calls, [("Beep", 880, 180)])

    def test_backend_absent_ne_leve_pas(self) -> None:
        with mock.patch.dict(sys.modules, {"winsound": None}):
            sound.play_alert(platform="nt")  # ne doit pas lever


class PosixSoundTests(unittest.TestCase):
    def test_cloche_du_terminal(self) -> None:
        stream = io.StringIO()
        with mock.patch.object(sound.sys, "stdout", stream):
            sound.play_alert(platform="posix")
        self.assertEqual(stream.getvalue(), "\a")

    def test_lecteur_audio_utilise(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
            with mock.patch.object(
                sound.shutil,
                "which",
                side_effect=lambda name: "/usr/bin/afplay" if name == "afplay" else None,
            ), mock.patch.object(sound.subprocess, "Popen") as popen:
                sound.play_alert(tmp.name, platform="posix")

            popen.assert_called_once_with(
                ["/usr/bin/afplay", tmp.name],
                stdout=sound.subprocess.DEVNULL,
                stderr=sound.subprocess.DEVNULL,
            )

    def test_sans_lecteur_replie_sur_la_cloche(self) -> None:
        stream = io.StringIO()
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp, mock.patch.object(
            sound.shutil, "which", return_value=None
        ), mock.patch.object(sound.sys, "stdout", stream):
            sound.play_alert(tmp.name, platform="posix")
        self.assertEqual(stream.getvalue(), "\a")


ROOT = Path(__file__).resolve().parent.parent


class BundledSoundTests(unittest.TestCase):
    def test_jarvis_wav_valide(self) -> None:
        path = ROOT / "jarvis.wav"
        self.assertTrue(path.exists(), "jarvis.wav manquant à la racine du projet")

        with wave.open(str(path)) as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), 44100)
            self.assertAlmostEqual(wav.getnframes() / 44100, 1.0, places=2)
            frames = wav.readframes(wav.getnframes())

        samples = struct.unpack(f"<{len(frames) // 2}h", frames)
        self.assertGreater(max(abs(s) for s in samples), 5000)  # son audible
        self.assertLess(abs(samples[0]), 200)  # départ à zéro, pas de clic
        self.assertLess(abs(samples[-1]), 200)  # fin en fondu, pas de clic
        self.assertLess(max(abs(s) for s in samples[-300:]), 2000)

    def test_exemple_configure_le_son(self) -> None:
        example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertTrue(example["notify"]["sound"])
        self.assertEqual(example["notify"]["sound_file"], "jarvis.wav")


class NotifierSoundTests(unittest.IsolatedAsyncioTestCase):
    async def test_bip_avec_delai_minimum(self) -> None:
        notifier = AccountNotifier(None, NotifyConfig(sound=True))  # type: ignore[arg-type]
        with mock.patch("selfai.bot.play_alert") as play:
            await notifier._maybe_play_sound()
            await notifier._maybe_play_sound()
        self.assertEqual(play.call_count, 1)

    async def test_bip_desactive(self) -> None:
        notifier = AccountNotifier(None, NotifyConfig(sound=False))  # type: ignore[arg-type]
        with mock.patch("selfai.bot.play_alert") as play:
            await notifier._maybe_play_sound()
        play.assert_not_called()

    async def test_fichier_transmis(self) -> None:
        notifier = AccountNotifier(
            None, NotifyConfig(sound=True, sound_file="jarvis.wav")  # type: ignore[arg-type]
        )
        with mock.patch("selfai.bot.play_alert") as play:
            await notifier._maybe_play_sound()
        play.assert_called_once_with("jarvis.wav")


if __name__ == "__main__":
    unittest.main()

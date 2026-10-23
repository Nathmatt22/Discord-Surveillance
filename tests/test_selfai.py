"""Tests qui n'ont besoin ni de Discord ni d'une vraie clé API."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from selfai.analyzer import IncomingMessage, MessageAnalyzer
from selfai.classifier import Classifier, ClassifierError, Verdict
from selfai.config import AIConfig, AnalysisConfig, ConfigError, parse_config
from selfai.state import StateStore


# --------------------------------------------------------------------------
# Faux serveur « compatible OpenAI »
# --------------------------------------------------------------------------


def chat_response(content: str) -> str:
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": content}}]}
    )


class FakeAI:
    """Serveur HTTP local qui rejoue une file de réponses."""

    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (API de http.server)
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": json.loads(raw.decode("utf-8") or "{}"),
                    }
                )
                status, payload = (
                    outer.responses.pop(0)
                    if outer.responses
                    else (500, '{"error": "aucune réponse prévue"}')
                )
                body = payload.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def config_data(**overrides: object) -> dict:
    data = {
        "discord": {
            "channels": [
                {"id": "111", "name": "annonces", "context": "Annonces"},
                {"id": 222, "name": "general"},
            ]
        },
        "ai": {"base_url": "https://api.example.com/v1", "model": "modele-test"},
        "analysis": {"min_confidence": 0.5, "context_messages": 2},
        "notify": {"dm_user_id": "999"},
        "runtime": {},
    }
    data.update(overrides)
    return data


class ConfigTests(unittest.TestCase):
    def test_parse_accepte_les_ids_en_texte(self) -> None:
        config = parse_config(config_data())
        self.assertEqual(config.channel_ids, {111, 222})
        self.assertEqual(config.channel(111).name, "annonces")
        self.assertEqual(config.notify.dm_user_id, 999)
        self.assertEqual(config.ai.base_url, "https://api.example.com/v1")

    def test_au_moins_un_salon(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config(config_data(discord={"channels": []}))

    def test_salon_duplique(self) -> None:
        data = config_data()
        data["discord"]["channels"].append({"id": 111})
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_id_invalide(self) -> None:
        data = config_data()
        data["discord"]["channels"][0]["id"] = "mon-salon"
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_son_active_par_defaut(self) -> None:
        config = parse_config(config_data())
        self.assertTrue(config.notify.sound)
        self.assertIsNone(config.notify.sound_file)

    def test_son_personnalise(self) -> None:
        config = parse_config(
            config_data(notify={"sound": False, "sound_file": "jarvis.wav"})
        )
        self.assertFalse(config.notify.sound)
        self.assertEqual(config.notify.sound_file, "jarvis.wav")

    def test_ignore_user_ids_accepte_texte_et_nombre(self) -> None:
        config = parse_config(config_data(analysis={"ignore_user_ids": ["123", 456]}))
        self.assertEqual(config.analysis.ignore_user_ids, frozenset({123, 456}))

    def test_ignore_user_ids_invalide(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config(config_data(analysis={"ignore_user_ids": ["pas-un-id"]}))

    def test_seuil_hors_bornes(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config(config_data(analysis={"min_confidence": 1.5}))

    def test_base_url_absente(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config(config_data(ai={"model": "x"}))

    def test_preset_vyceai(self) -> None:
        config = parse_config(config_data(ai={"provider": "vyceai"}))
        self.assertEqual(config.ai.provider, "vyceai")
        self.assertEqual(config.ai.base_url, "https://vyceai.com/v1")
        self.assertEqual(config.ai.model, "deepseek-v4-flash")
        self.assertEqual(config.ai.api_key_env, "VYCEAI_API_KEY")

    def test_preset_surcharge_par_les_champs(self) -> None:
        config = parse_config(
            config_data(
                ai={
                    "provider": "vyceai",
                    "model": "claude-haiku-4-5",
                    "api_key_env": "MA_CLE",
                }
            )
        )
        self.assertEqual(config.ai.base_url, "https://vyceai.com/v1")
        self.assertEqual(config.ai.model, "claude-haiku-4-5")
        self.assertEqual(config.ai.api_key_env, "MA_CLE")

    def test_provider_inconnu(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse_config(config_data(ai={"provider": "nawak"}))
        self.assertIn("vyceai", str(ctx.exception))

    def test_valeurs_par_defaut(self) -> None:
        config = parse_config(config_data())
        self.assertEqual(config.runtime.token_env, "DISCORD_USER_TOKEN")
        self.assertEqual(config.runtime.state_file, "state.json")
        self.assertTrue(config.analysis.ignore_bots)
        self.assertTrue(config.ai.json_mode)


# --------------------------------------------------------------------------
# Classifier
# --------------------------------------------------------------------------


class ClassifierTests(unittest.TestCase):
    def build(self, fake: FakeAI, *, retries: int = 0, json_mode: bool = True) -> Classifier:
        ai = AIConfig(
            base_url=fake.base_url,
            model="modele-test",
            retries=retries,
            retry_backoff_seconds=0.0,
            json_mode=json_mode,
        )
        return Classifier(ai, AnalysisConfig(context_messages=2), "cle-secrete")

    def test_verdict_important(self) -> None:
        fake = FakeAI(
            [(200, chat_response('{"important": true, "confidence": 0.92, "reason": "réunion avancée"}'))]
        )
        self.addCleanup(fake.close)

        verdict = self.build(fake).classify(
            text="La réunion est avancée à 15h", channel_name="annonces", author="Bob"
        )

        self.assertTrue(verdict.important)
        self.assertAlmostEqual(verdict.confidence, 0.92)
        self.assertEqual(verdict.reason, "réunion avancée")

        request = fake.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["authorization"], "Bearer cle-secrete")
        self.assertEqual(request["body"]["model"], "modele-test")
        self.assertEqual(request["body"]["response_format"], {"type": "json_object"})
        user_prompt = request["body"]["messages"][1]["content"]
        self.assertIn("La réunion est avancée à 15h", user_prompt)
        self.assertIn("#annonces", user_prompt)

    def test_json_entoure_de_markdown(self) -> None:
        fake = FakeAI(
            [(200, chat_response('```json\n{"important": false, "confidence": 0.2, "reason": "bavardage"}\n```'))]
        )
        self.addCleanup(fake.close)

        verdict = self.build(fake).classify(text="mdr")
        self.assertFalse(verdict.important)
        self.assertEqual(verdict.reason, "bavardage")

    def test_bool_textuel_et_confiance_manquante(self) -> None:
        fake = FakeAI([(200, chat_response('{"important": "oui"}'))])
        self.addCleanup(fake.close)

        verdict = self.build(fake).classify(text="test")
        self.assertTrue(verdict.important)
        self.assertAlmostEqual(verdict.confidence, 0.5)
        self.assertEqual(verdict.reason, "aucune raison fournie")

    def test_reponse_invalide_leve_une_erreur(self) -> None:
        fake = FakeAI([(200, chat_response("je ne sais pas"))])
        self.addCleanup(fake.close)

        with self.assertRaises(ClassifierError):
            self.build(fake).classify(text="test")

    def test_reessai_apres_erreur_serveur(self) -> None:
        fake = FakeAI(
            [
                (500, '{"error": "boom"}'),
                (200, chat_response('{"important": true, "confidence": 0.8, "reason": "ok"}')),
            ]
        )
        self.addCleanup(fake.close)

        verdict = self.build(fake, retries=1).classify(text="test")
        self.assertTrue(verdict.important)
        self.assertEqual(len(fake.requests), 2)

    def test_cle_refusee(self) -> None:
        fake = FakeAI([(401, '{"error": "unauthorized"}')])
        self.addCleanup(fake.close)

        with self.assertRaises(ClassifierError) as ctx:
            self.build(fake).classify(text="test")
        self.assertIn("Clé API refusée", str(ctx.exception))

    def test_repli_quand_json_mode_refuse(self) -> None:
        fake = FakeAI(
            [
                (400, '{"error": "response_format is not supported"}'),
                (200, chat_response('{"important": false, "confidence": 0.1, "reason": "ok"}')),
            ]
        )
        self.addCleanup(fake.close)

        verdict = self.build(fake).classify(text="test")
        self.assertFalse(verdict.important)
        self.assertNotIn("response_format", fake.requests[1]["body"])

    def test_reponse_vide_leve_une_erreur(self) -> None:
        fake = FakeAI([(200, json.dumps({"choices": [{"message": {"content": ""}}]}))])
        self.addCleanup(fake.close)

        with self.assertRaises(ClassifierError):
            self.build(fake).classify(text="test")


# --------------------------------------------------------------------------
# Analyseur
# --------------------------------------------------------------------------


class FakeClassifier:
    def __init__(self, verdict: Verdict | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.verdict = verdict or Verdict(True, 0.9, "raison")
        self.error = error

    def classify(self, **kwargs: object) -> Verdict:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.verdict


class FakeNotifier:
    def __init__(self) -> None:
        self.alerts: list[object] = []

    async def notify(self, alert: object) -> None:
        self.alerts.append(alert)


class AnalyzerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def make(
        self,
        verdict: Verdict | None = None,
        error: Exception | None = None,
        min_confidence: float = 0.5,
        ignore_user_ids: tuple[int, ...] = (),
        on_verdict: object | None = None,
    ) -> tuple[MessageAnalyzer, FakeClassifier, FakeNotifier, StateStore]:
        data = config_data()
        data["analysis"] = {
            "min_confidence": min_confidence,
            "context_messages": 2,
            "ignore_user_ids": list(ignore_user_ids),
        }
        config = parse_config(data)
        state = StateStore(Path(self._tmp.name) / "state.json")
        state.load()
        classifier = FakeClassifier(verdict, error)
        notifier = FakeNotifier()
        analyzer = MessageAnalyzer(
            config, classifier, notifier, state, on_verdict=on_verdict  # type: ignore[arg-type]
        )
        return analyzer, classifier, notifier, state

    @staticmethod
    def message(**overrides: object) -> IncomingMessage:
        values = {
            "id": 10,
            "channel_id": 111,
            "channel_name": "annonces",
            "author_name": "Bob",
            "content": "La réunion est avancée à 15h",
        }
        values.update(overrides)
        return IncomingMessage(**values)  # type: ignore[arg-type]

    async def test_message_important_notifie(self) -> None:
        analyzer, classifier, notifier, state = self.make()
        alert = await analyzer.analyze(self.message())

        self.assertIsNotNone(alert)
        self.assertEqual(len(notifier.alerts), 1)
        self.assertEqual(len(classifier.calls), 1)
        self.assertEqual(state.last_message_id(111), 10)

    async def test_message_sans_importance(self) -> None:
        analyzer, _, notifier, state = self.make(Verdict(False, 0.9, "bavardage"))
        alert = await analyzer.analyze(self.message())

        self.assertIsNone(alert)
        self.assertEqual(notifier.alerts, [])
        self.assertEqual(state.last_message_id(111), 10)

    async def test_confiance_trop_basse(self) -> None:
        analyzer, _, notifier, _ = self.make(Verdict(True, 0.3, "doute"), min_confidence=0.6)
        alert = await analyzer.analyze(self.message())

        self.assertIsNone(alert)
        self.assertEqual(notifier.alerts, [])

    async def test_salon_non_surveille(self) -> None:
        analyzer, classifier, notifier, _ = self.make()
        alert = await analyzer.analyze(self.message(channel_id=333))

        self.assertIsNone(alert)
        self.assertEqual(classifier.calls, [])
        self.assertEqual(notifier.alerts, [])

    async def test_compte_ignore(self) -> None:
        analyzer, classifier, notifier, _ = self.make(ignore_user_ids=(42,))
        alert = await analyzer.analyze(self.message(author_id=42))

        self.assertIsNone(alert)
        self.assertEqual(classifier.calls, [])
        self.assertEqual(notifier.alerts, [])

    async def test_compte_non_ignore(self) -> None:
        analyzer, classifier, _, _ = self.make(ignore_user_ids=(42,))
        alert = await analyzer.analyze(self.message(author_id=7))

        self.assertIsNotNone(alert)
        self.assertEqual(len(classifier.calls), 1)

    async def test_message_de_bot_ignore(self) -> None:
        analyzer, classifier, _, _ = self.make()
        alert = await analyzer.analyze(self.message(author_is_bot=True))

        self.assertIsNone(alert)
        self.assertEqual(classifier.calls, [])

    async def test_message_vide_ignore(self) -> None:
        analyzer, classifier, _, _ = self.make()
        alert = await analyzer.analyze(self.message(content="   "))

        self.assertIsNone(alert)
        self.assertEqual(classifier.calls, [])

    async def test_erreur_ia_avance_quand_meme_l_etat(self) -> None:
        analyzer, _, notifier, state = self.make(error=ClassifierError("API en panne"))
        alert = await analyzer.analyze(self.message())

        self.assertIsNone(alert)
        self.assertEqual(notifier.alerts, [])
        self.assertEqual(state.last_message_id(111), 10)

    async def test_contexte_recent_transmis(self) -> None:
        analyzer, classifier, _, _ = self.make(Verdict(False, 0.8, "ok"))

        await analyzer.analyze(self.message(id=10, content="premier"))
        await analyzer.analyze(self.message(id=11, content="second"))

        self.assertIn("premier", classifier.calls[1]["context"][0])
        self.assertEqual(len(classifier.calls[1]["context"]), 1)

    async def test_retour_verdict_transmis(self) -> None:
        seen: list[tuple[object, object, bool]] = []
        analyzer, _, _, _ = self.make(
            on_verdict=lambda message, verdict, notified: seen.append(
                (message, verdict, notified)
            )
        )

        await analyzer.analyze(self.message(id=10))
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][2])  # notifié

        analyzer2, _, _, _ = self.make(
            Verdict(False, 0.9, "bavardage"),
            on_verdict=lambda message, verdict, notified: seen.append(
                (message, verdict, notified)
            ),
        )
        await analyzer2.analyze(self.message(id=11))
        self.assertEqual(len(seen), 2)
        self.assertFalse(seen[1][2])

    async def test_seuil_du_reglage_vivant(self) -> None:
        analyzer, _, notifier, _ = self.make(Verdict(True, 0.4, "doute"))
        analyzer._settings.min_confidence = 0.3

        alert = await analyzer.analyze(self.message())

        self.assertIsNotNone(alert)
        self.assertEqual(len(notifier.alerts), 1)

    async def test_ignore_bots_du_reglage_vivant(self) -> None:
        analyzer, classifier, _, _ = self.make()
        analyzer._settings.ignore_bots = False

        alert = await analyzer.analyze(self.message(author_is_bot=True))

        self.assertIsNotNone(alert)
        self.assertEqual(len(classifier.calls), 1)

    async def test_message_deja_traite_ignore(self) -> None:
        analyzer, classifier, notifier, _ = self.make()

        premier = await analyzer.analyze(self.message(id=10))
        second = await analyzer.analyze(self.message(id=10))

        self.assertIsNotNone(premier)
        self.assertIsNone(second)
        self.assertEqual(len(classifier.calls), 1)
        self.assertEqual(len(notifier.alerts), 1)

    async def test_etat_garde_le_plus_recent(self) -> None:
        analyzer, _, _, state = self.make(Verdict(False, 0.8, "ok"))

        await analyzer.analyze(self.message(id=50))
        await analyzer.analyze(self.message(id=40))

        self.assertEqual(state.last_message_id(111), 50)


# --------------------------------------------------------------------------
# État
# --------------------------------------------------------------------------


class StateTests(unittest.TestCase):
    def test_ecriture_et_relecture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            first = StateStore(path, save_interval_seconds=0)
            first.load()
            first.set_last(111, 42)
            first.flush()

            second = StateStore(path)
            second.load()
            self.assertEqual(second.last_message_id(111), 42)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["channels"]["111"], 42)

    def test_fichier_corrompu_ignore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("pas du json", encoding="utf-8")
            store = StateStore(path)
            store.load()
            self.assertIsNone(store.last_message_id(111))


class AlertFormatTests(unittest.TestCase):
    def test_alerte_tronquee_et_lisible(self) -> None:
        from selfai.bot import format_alert
        from selfai.analyzer import Alert

        alert = Alert(
            message=IncomingMessage(
                id=1,
                channel_id=111,
                channel_name="annonces",
                author_name="Bob",
                content="x" * 2000,
                jump_url="https://discord.com/channels/1/111/1",
            ),
            verdict=Verdict(True, 0.9, "échéance"),
        )
        text = format_alert(alert)

        self.assertLessEqual(len(text), 1990)
        self.assertIn("**Raison :** échéance", text)
        self.assertIn("https://discord.com/channels/1/111/1", text)
        self.assertIn("> ", text)


if __name__ == "__main__":
    unittest.main()

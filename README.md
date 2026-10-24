# Filtre IA pour Discord

Surveille quelques salons de **ton compte Discord** et te prévient quand un message
mérite ton attention. Une IA lit chaque nouveau message et répond « important » ou
non, avec une raison — tu n'as plus à tout relire.

## ⚠️ À savoir avant de commencer

Discord interdit l'automatisation des comptes personnels. Utiliser ce script peut
faire suspendre ton compte.

- Utilise un **compte secondaire**, pas ton compte principal.
- Ne partage jamais ton jeton, et ne le mets pas sur GitHub.
- Ne l'utilise que sur des serveurs où tu as le droit de lire ces salons.

C'est ton choix et ta responsabilité.

## Ce que ça fait

- Lit **uniquement** les salons que tu choisis.
- Ignore tes propres messages, et ceux des bots par défaut.
- Affiche l'alerte dans l'interface, la sonne, et peut aussi la poster ailleurs
  (webhook Discord, message privé, autre salon, ou **Telegram**).
- N'écrit **jamais** dans les salons surveillés.
- Ne relit pas l'historique : l'analyse commence au lancement.

## Installer

Windows (PowerShell ou cmd) :

```bat
cd D:\code\selfAI-discord
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
copy config.example.json config.json
```

macOS / Linux :

```bash
cd ~/selfAI-discord
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
cp config.example.json config.json
```

Dans la suite, `.venv\Scripts\python` désigne Windows et `.venv/bin/python`
désigne macOS / Linux.

À la fin, tu dois avoir deux nouveaux fichiers : `.env` (tes secrets) et
`config.json` (tes réglages). Ouvre-les dans un éditeur de texte (Bloc-notes,
Notepad++, VS Code…) pour les étapes suivantes.

## Étape 1 — Ton jeton Discord

1. Ouvre <https://discord.com/app> dans ton navigateur, connecté au compte concerné.
2. `F12`, onglet **Application** → **Local Storage** → `https://discord.com`.
3. Cherche la clé `token`, copie sa valeur (sans les guillemets).
4. Colle-la dans `.env` :

```
DISCORD_USER_TOKEN=la_valeur_copiée
```

Ce jeton donne accès à tout ton compte. S'il fuite, change ton mot de passe : cela
l'annule immédiatement.

## Étape 2 — La clé de l'IA

Le projet est déjà réglé sur **VyceAI** (une clé qui donne accès à plusieurs modèles)
avec `deepseek-v4-flash`, rapide et économique — largement suffisant pour un filtre
oui/non.

1. Crée un compte sur <https://vyceai.com> et génère une clé API.
2. Colle-la dans `.env` :

```
VYCEAI_API_KEY=ta_clé
```

**Changer de fournisseur** : remplace `"provider"` dans `config.json`. Le reste
(adresse, modèle, variable de clé) s'adapte tout seul.

| `provider` | Variable à remplir dans `.env` |
|---|---|
| `vyceai` | `VYCEAI_API_KEY` |
| `zai` | `AI_API_KEY` |
| `openai` | `OPENAI_API_KEY` |
| `groq` | `GROQ_API_KEY` |
| `ollama` (modèle local) | `OLLAMA_API_KEY`, n'importe quelle valeur |

Les messages analysés passent par le fournisseur choisi. Si tes salons contiennent
des informations sensibles, utilise `ollama` en local.

## Étape 3 — Les salons à surveiller

Dans Discord : **Paramètres → Avancé → Mode développeur**. Puis clic droit sur un
salon → **Copier l'identifiant**.

Dans `config.json`, sous `"discord"`, remplace la liste `"channels"` par tes salons :

```json
"channels": [
  {
    "id": "123456789012345678",
    "name": "annonces",
    "context": "Annonces officielles : mises à jour, événements, planning."
  },
  {
    "id": "234567890123456789",
    "name": "général",
    "context": "Discussion : alerte-moi seulement si on me demande quelque chose."
  }
]
```

- `id` : obligatoire, c'est le nombre copié depuis Discord.
- `name` : juste l'étiquette affichée dans les alertes.
- `context` : à quoi sert le salon. L'IA s'en sert pour juger, donc sois précis.

## Étape 4 — Régler la pertinence

Tout est dans la partie `analysis` de `config.json`. Trois réglages suffisent :

| Réglage | À quoi ça sert |
|---|---|
| `instructions` | **Le plus important.** Écris ce qui est important pour toi et ce qui ne l'est pas, en une ou deux phrases. |
| `min_confidence` | À partir de quel niveau de confiance alerter (`0.5` = 50 %). Monte-la si tu reçois trop de faux positifs. |
| `ignore_bots` | `true` ignore les bots. Passe à `false` si les annonces utiles viennent d'un bot. |

Exemple d'`instructions` efficace :

> Je gère un serveur de jeu. Sont importants : les annonces, les changements de date,
> les questions qui attendent une réponse de moi, les signalements de bug. Ne sont pas
> importants : les blagues, les memes, les remerciements, le bavardage.

Pour ne jamais être alerté par certains comptes, ajoute leurs identifiants dans
`ignore_user_ids` (clic droit sur le pseudo → **Copier l'identifiant**). Leurs
messages ne coûtent alors aucun appel à l'IA :

```json
"ignore_user_ids": ["1138072372545589359"]
```

## Étape 5 — Où arrivent les alertes

Par défaut : dans l'interface, avec un son. Pas besoin de configurer le reste.

**Webhook (recommandé pour être prévenu sur ton téléphone)** : dans le salon où tu
veux recevoir les alertes, **Modifier le salon → Intégrations → Webhooks → Nouveau
webhook → Copier l'URL du webhook**, puis dans `.env` :

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Ça n'ajoute aucun bot et ne dépend pas de la disponibilité de ton compte.

### Recevoir les alertes sur Telegram

Pratique sur téléphone, et ça ne demande aucun bot côté Discord.

1. Dans Telegram, écris à **@BotFather** → `/newbot` → choisis un nom. Il te donne un
   jeton qui ressemble à `123456789:AAE...`. Mets-le dans `.env` :

   ```
   TELEGRAM_BOT_TOKEN=123456789:AAE...
   ```

2. Envoie n'importe quel message à **ton** bot (il ne peut pas écrire le premier).
3. Ouvre `https://api.telegram.org/bot<TON_JETON>/getUpdates` dans ton navigateur et
   cherche `"chat":{"id":123456789}` : ce nombre est ton `chat_id`.
4. Ajoute ce bloc dans `notify`, dans `config.json` :

```json
"telegram": {
  "enabled": true,
  "chat_id": "123456789"
}
```

Ça marche aussi dans un groupe (ajoute le bot au groupe, l'identifiant est négatif)
ou un canal (ajoute le bot comme administrateur). `--check` affiche `Telegram : actif`
quand tout est en place.

**Autres cibles**, dans `notify` : `dm_user_id` (message privé à un autre compte —
Discord refuse les MP à soi-même) et `log_channel_id` (poster dans un salon, en ton nom).

**Le son** : un bip par alerte, au maximum un toutes les 2 secondes. Un `jarvis.wav`
d'une seconde est déjà fourni et configuré. Pour en mettre un autre, dépose ton `.wav`
à la racine et écris son nom dans `sound_file`. Pour tout couper : `"sound": false`.

## Lancer

```bat
.venv\Scripts\python main.py --check
.venv\Scripts\python main.py --test "La réunion est avancée à 15h, réponds avant demain"
.venv\Scripts\python main.py
```

- `--check` vérifie que `config.json`, le jeton et la clé sont en place.
- `--test "texte"` fait juger une phrase par l'IA, sans se connecter à Discord.
  Idéal pour vérifier ta clé et ajuster `instructions`.
- Sans argument : surveillance en continu, dans l'interface.

Fais `--check` puis `--test` la première fois : tu sauras tout de suite si un réglage
cloche, sans attendre un vrai message.

## L'interface

Tout se fait à la souris : une barre de boutons en bas, les messages importants à
droite, et un écran Paramètres pour ajuster le seuil, le son et les bots sans quitter
le programme (les changements sont enregistrés tout de suite).

```text
  J A R V I S  ·  veille discord                            11:02:14
 ───────────────────────────────────────────────────────────────────
                              ╭─ ALERTES ──────────────────╮
                     ◉        │ 11:01 #general             │
                   ( orb )    │ Lucas · connexion suspecte │
                              ╰────────────────────────────╯
                    ● CONNECTÉ · mon_compte
                    messages 42  ·   importantes 3
 ───────────────────────────────────────────────────────────────────
   Accueil   Messages   Paramètres   Aide           Quitter (x)
```

- Un clic sélectionne, un second clic ouvre le message complet (texte, auteur, heure,
  raison, lien Discord).
- La molette et `▲` `▼` font défiler ; `‹` `›` modifient un réglage.
- Au clavier : `1` `2` `3` pour les écrans, `?` pour l'aide, `x` pour quitter.

**Tes jetons dans l'interface** : l'écran Paramètres a une section `SÉCURITÉ`
(jeton Discord, clé IA, jeton Telegram, webhook). Clique sur la valeur, tape la
nouvelle, `Entrée` : elle est écrite dans `.env` à la place de l'ancienne. La saisie
est masquée et la valeur n'apparaît jamais à l'écran, seulement `défini` ou `absent`.
Le jeton Discord ne peut pas changer à chaud : il prend effet au prochain lancement
(le message te le rappelle). Les autres s'appliquent immédiatement.

**Où l'interface fonctionne** : Windows Terminal, cmd, PowerShell. Dans Git Bash,
les clics ne passent pas : elle reste utilisable mais avec des commandes tapées puis
`Entrée`. Sur macOS / Linux, ça marche directement — y compris sur un Raspberry Pi.

**Terminal sans UTF-8** : sur la console texte d'un Raspberry Pi, en SSH depuis un
client réglé en Latin-1, ou avec une locale `C`, les cadres Unicode sortent en
`â•”€` et les lignes, trop larges, se replient en laissant des restes de l'écran
précédent. L'interface le détecte et se limite alors à de l'ASCII (`+---+`, `|`,
accentuation retirée), sans rien changer à la disposition. Pour forcer un choix :
`SELFAI_ASCII=1` (toujours en ASCII) ou `SELFAI_ASCII=0` (toujours en Unicode).

Laisse l'interface ouverte : c'est elle qui analyse et qui t'alerte. `Ctrl+C` arrête.

## Si ça ne marche pas

| Symptôme | À vérifier |
|---|---|
| `LoginFailure` | Le jeton est périmé : recopie-le depuis le navigateur. |
| `Clé API refusée` | La clé dans `.env` ne correspond pas au fournisseur, ou `.env` n'est pas à la racine. |
| Aucune alerte alors que ça discute | Lance `--test`, baisse `min_confidence`, vérifie que l'identifiant est bien un salon où ton compte peut lire. |
| Les salons sont « introuvables » | Identifiant erroné, ou ton compte n'a pas accès à ce salon. |
| Rien avant le lancement | Normal : le script ne relit pas l'historique. |
| Cadres en `â•”€`, restes d'écran | Le terminal n'est pas en UTF-8 : passe-le en UTF-8, ou force `SELFAI_ASCII=1` (voir plus haut). |
| Pas de son | Son de Windows coupé, `sound` à `false`, ou `.wav` invalide. |
| Le compte passe en ligne | Comportement normal d'une connexion. Un compte secondaire règle ça. |
| Rien n'arrive sur Telegram | Écris d'abord un message au bot (il ne peut pas écrire en premier), et vérifie le `chat_id`. |

Le détail de ce qui se passe est écrit dans `selfai.log`, à côté du script.

## Tes fichiers

| Fichier | Ce que c'est |
|---|---|
| `config.json` | Tes réglages (salons, IA, alertes). |
| `.env` | Tes secrets : jetons, clé API, webhook. Modifiable dans Paramètres. **Ne le partage jamais.** |
| `selfai.log` | Le journal, utile si quelque chose cloche. |
| `state.json` | Mémoire interne (dernier message traité). Supprimable sans risque. |

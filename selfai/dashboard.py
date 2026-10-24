"""Interface terminal interactive inspirée de JARVIS.

- Vue **Accueil** : orb bleu animé, alertes en haut à droite, état de la connexion.
- Vue **Messages** : liste défilante des messages importants, avec détail complet.
- Vue **Paramètres** : seuil de confiance, son et filtre bots, appliqués à chaud
  puis sauvegardés dans ``config.json``.
- Vue **Aide**.

Chaque rendu enregistre ses zones cliquables (barre de navigation, lignes de la
liste, boutons ``−``/``+``…) : le clic est testé sur ces rectangles, donc les
boutons sont de vrais boutons, pas des commandes à taper. Clavier et molette
restent disponibles.

Les entrées viennent de :mod:`selfai.console_input`. Si le terminal ne transmet
pas la souris (Git Bash / mintty), l'interface retombe sur des commandes tapées.

Aucune dépendance externe : ANSI + deux threads (rendu et lecture des entrées).
"""

from __future__ import annotations

import _thread
import logging
import math
import os
import shutil
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .console_input import Click, InputBackend, Key, Scroll, create_input
from .secrets import SecretField, SecretStore
from .settings import LiveSettings, save_settings

# --------------------------------------------------------------------------
# ANSI
# --------------------------------------------------------------------------

RESET = "\x1b[0m"
ALT_SCREEN_ON = "\x1b[?1049h"
ALT_SCREEN_OFF = "\x1b[?1049l"
CURSOR_HIDE = "\x1b[?25l"
CURSOR_SHOW = "\x1b[?25h"
CLEAR = "\x1b[2J"
ERASE_LINE_END = "\x1b[K"


def _fg(red: int, green: int, blue: int) -> str:
    return f"\x1b[38;2;{red};{green};{blue}m"


STYLES: dict[str, str] = {
    "": "",
    "frame": _fg(28, 74, 128),
    "dim": _fg(58, 88, 130),
    "text": _fg(150, 198, 232),
    "title": _fg(96, 222, 255),
    "good": _fg(84, 232, 190),
    "warn": _fg(255, 196, 90),
    "hot": _fg(190, 245, 255),
    "danger": _fg(255, 120, 120),
    "button": "\x1b[48;2;16;38;66m\x1b[38;2;130;180;220m",
    "button_on": "\x1b[1m\x1b[48;2;34;102;172m\x1b[38;2;228;246;255m",
    "button_warn": "\x1b[48;2;66;22;28m\x1b[38;2;255;160;160m",
    "pad": "\x1b[48;2;13;30;52m\x1b[38;2;96;150;200m",
}
for _index, _color in enumerate(
    [(18, 50, 110), (28, 88, 170), (44, 140, 215), (80, 190, 245), (150, 232, 255)]
):
    STYLES[f"orb{_index}"] = _fg(*_color)


# --------------------------------------------------------------------------
# Jeu de caractères de sortie
# --------------------------------------------------------------------------
#
# Un terminal qui n'est pas en UTF-8 (console texte d'un Raspberry Pi, client
# SSH réglé en Latin-1, locale C…) affiche les octets UTF-8 un par un : « ─ »
# devient « â•”€ », chaque ligne déborde de la largeur de l'écran, se replie sur
# la suivante et laisse les restes de l'ancienne vue un peu partout. Dans ce cas
# on remplace chaque caractère décoratif par un équivalent ASCII **de même
# largeur**, donc la mise en page ne bouge pas.

ASCII_ENV = "SELFAI_ASCII"

ASCII_FALLBACK = {
    "─": "-",
    "═": "=",
    "│": "|",
    "║": "|",
    "╭": "+",
    "╮": "+",
    "╰": "+",
    "╯": "+",
    "▏": "|",
    "·": ".",
    "…": ".",
    "•": "*",
    "●": "o",
    "›": ">",
    "‹": "<",
    "«": "<",
    "»": ">",
    "→": ">",
    "▲": "^",
    "▼": "v",
    "⚠": "!",
    "—": "-",
    "−": "-",
}

_ASCII_ONLY = False


def set_ascii_only(enabled: bool) -> None:
    """Force l'interface en ASCII pur (terminaux non UTF-8)."""
    global _ASCII_ONLY
    _ASCII_ONLY = bool(enabled)


def ascii_only() -> bool:
    return _ASCII_ONLY


def _ascii_char(char: str) -> str:
    """Équivalent ASCII d'un caractère, toujours large d'une colonne."""
    if char.isascii():
        return char
    replacement = ASCII_FALLBACK.get(char)
    if replacement is not None:
        return replacement
    # « é » -> « e », « ô » -> « o » : les accents sautent. Un seul caractère
    # est renvoyé, sinon les colonnes voisines se décaleraient.
    for letter in unicodedata.normalize("NFKD", char):
        if letter.isascii() and letter.isprintable():
            return letter
    return "?"


def supports_unicode(stream: object) -> bool:
    """Le terminal peut-il afficher les cadres et les accents (UTF-8) ?

    ``SELFAI_ASCII=1`` force le repli ASCII, ``SELFAI_ASCII=0`` le désactive.
    """
    override = os.environ.get(ASCII_ENV, "").strip().lower()
    if override in ("1", "oui", "yes", "true", "on"):
        return False
    if override in ("0", "non", "no", "false", "off"):
        return True
    try:
        if not stream.isatty():  # fichier redirigé : l'UTF-8 est sans risque
            return True
    except (AttributeError, ValueError):
        return True
    if sys.platform == "win32":
        # La page de code 65001 a été posée au démarrage.
        return True
    encoding = (getattr(stream, "encoding", "") or "").lower()
    if "utf" not in encoding:
        return False
    # Console texte Linux (Raspberry Pi branché en HDMI) : selon la police et la
    # locale chargées, les octets UTF-8 y sortent en « â•”€ ». Elle porte
    # ``TERM=linux`` et s'appelle /dev/tty1 (un pseudo-terminal est /dev/pts/…).
    if os.environ.get("TERM", "").strip() == "linux":
        return False
    try:
        name = os.ttyname(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return True
    return not (name.startswith("/dev/tty") and name[len("/dev/tty"):].isdigit())


# --------------------------------------------------------------------------
# Largeur d'affichage (les emoji comptent double)
# --------------------------------------------------------------------------


def char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    return 1


def text_width(text: str) -> int:
    return sum(char_width(char) for char in text)


def fit_text(text: str, limit: int) -> str:
    """Tronque un texte à ``limit`` colonnes, avec des points de suspension."""
    if limit <= 0:
        return ""
    if text_width(text) <= limit:
        return text
    out: list[str] = []
    used = 0
    for char in text:
        width = char_width(char)
        if used + width > limit - 1:
            break
        out.append(char)
        used += width
    return "".join(out) + "…"


def wrap_text(text: str, limit: int) -> list[str]:
    """Découpe un texte en lignes de ``limit`` colonnes maximum."""
    if limit <= 0:
        return []
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        if not paragraph.strip():
            lines.append("")
            continue
        current = ""
        for word in paragraph.split(" "):
            candidate = word if not current else f"{current} {word}"
            if text_width(candidate) <= limit:
                current = candidate
                continue
            if current:
                lines.append(current)
            while text_width(word) > limit:
                chunk, used = "", 0
                for char in word:
                    width = char_width(char)
                    if used + width > limit:
                        break
                    chunk += char
                    used += width
                lines.append(chunk)
                word = word[len(chunk):]
            current = word
        if current:
            lines.append(current)
    return lines


# --------------------------------------------------------------------------
# Zones cliquables
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Hotspot:
    """Un rectangle de l'écran qui réagit au clic."""

    x: int
    y: int
    width: int
    height: int
    action: str
    payload: object = None

    def contains(self, x: int, y: int) -> bool:
        return (
            self.x <= x < self.x + self.width
            and self.y <= y < self.y + self.height
        )


def hit_test(hotspots: list[Hotspot], x: int, y: int) -> Hotspot | None:
    """Trouve la zone cliquée ; les dernières dessinées passent devant."""
    for hotspot in reversed(hotspots):
        if hotspot.contains(x, y):
            return hotspot
    return None


# --------------------------------------------------------------------------
# Écran
# --------------------------------------------------------------------------


class Screen:
    """Grille de cellules (caractère + style) rendue en ANSI."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.hotspots: list[Hotspot] = []
        self._cells: list[list[tuple[str, str]]] = [
            [(" ", "") for _ in range(width)] for _ in range(height)
        ]

    def put(self, x: int, y: int, text: str, style: str = "") -> None:
        if y < 0 or y >= self.height:
            return
        cursor = x
        for char in text:
            width = char_width(char)
            if width == 0:
                continue
            if 0 <= cursor < self.width:
                self._cells[y][cursor] = (char, style)
                if width == 2 and cursor + 1 < self.width:
                    self._cells[y][cursor + 1] = ("", style)
            cursor += width

    def put_right(self, right: int, y: int, text: str, style: str = "") -> None:
        self.put(right - text_width(text), y, text, style)

    def put_centered(self, center_x: int, y: int, text: str, style: str = "") -> None:
        self.put(center_x - text_width(text) // 2, y, text, style)

    def hline(self, x: int, y: int, length: int, char: str = "─", style: str = "frame") -> None:
        self.put(x, y, char * max(0, length), style)

    def box(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        title: str = "",
        style: str = "frame",
        title_style: str = "title",
    ) -> None:
        if width < 2 or height < 2:
            return
        self.put(x, y, "╭" + "─" * (width - 2) + "╮", style)
        for row in range(y + 1, y + height - 1):
            self.put(x, row, "│", style)
            self.put(x + width - 1, row, "│", style)
        self.put(x, y + height - 1, "╰" + "─" * (width - 2) + "╯", style)
        if title:
            self.put(x + 2, y, f" {fit_text(title, width - 6)} ", title_style)

    # -- zones cliquables --------------------------------------------------

    def region(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        action: str,
        payload: object = None,
    ) -> None:
        if width > 0 and height > 0:
            self.hotspots.append(Hotspot(x, y, width, height, action, payload))

    def button(
        self,
        x: int,
        y: int,
        label: str,
        style: str = "button",
        action: str = "",
        payload: object = None,
    ) -> int:
        """Dessine un bouton et renvoie sa largeur (0 s'il ne rentre pas)."""
        width = text_width(label)
        if x < 0 or y < 0 or y >= self.height or x + width > self.width:
            return 0
        self.put(x, y, label, style)
        if action:
            self.region(x, y, width, 1, action, payload)
        return width

    def button_right(
        self,
        right: int,
        y: int,
        label: str,
        style: str = "button",
        action: str = "",
        payload: object = None,
    ) -> int:
        return self.button(right - text_width(label), y, label, style, action, payload)

    def to_lines(self) -> list[str]:
        lines: list[str] = []
        for row in self._cells:
            # Chaque ligne efface la fin de son rang : sans cela, le texte d'une
            # ancienne vue reste visible dessous (« effet fantôme »).
            last = -1
            for index, (char, style) in enumerate(row):
                if char != " " or style:
                    last = index
            chunks: list[str] = []
            current = None
            for char, style in row[: last + 1]:
                if char == "":
                    continue  # moitié droite d'un caractère double
                if style != current:
                    chunks.append(STYLES.get(style, "") or RESET)
                    current = style
                if not _ASCII_ONLY:
                    chunks.append(char)
                    continue
                chunks.append(_ascii_char(char))
                if char_width(char) == 2:
                    # Un emoji devient « ? » : la colonne de droite reste prise.
                    chunks.append(" ")
            chunks.append(RESET + ERASE_LINE_END)
            lines.append("".join(chunks))
        return lines

    def to_ansi(self) -> str:
        return "".join(
            f"\x1b[{index + 1};1H{line}" for index, line in enumerate(self.to_lines())
        )


# --------------------------------------------------------------------------
# Données affichées
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AlertEntry:
    at: float
    channel: str
    author: str
    reason: str
    confidence: float
    content: str = ""
    jump_url: str = ""


@dataclass(frozen=True)
class SettingRow:
    """Une ligne de l'écran Paramètres.

    ``secret`` : valeur rangée dans ``.env`` (jamais affichée). ``text`` : texte
    libre (identifiant de conversation). ``toggle`` : oui / non. ``number`` : seuil
    ajusté par pas.
    """

    key: str
    label: str
    kind: str
    variable: str = ""


def build_setting_rows(variables: dict[str, str] | None = None) -> list[SettingRow]:
    names = variables or {}
    return [
        SettingRow(
            "discord_token",
            "Jeton Discord",
            "secret",
            names.get("discord_token", "DISCORD_USER_TOKEN"),
        ),
        SettingRow("ai_key", "Clé IA", "secret", names.get("ai_key", "AI_API_KEY")),
        SettingRow(
            "telegram_token",
            "Jeton bot Telegram",
            "secret",
            names.get("telegram_token", "TELEGRAM_BOT_TOKEN"),
        ),
        SettingRow(
            "webhook",
            "Webhook Discord",
            "secret",
            names.get("webhook", "DISCORD_WEBHOOK_URL"),
        ),
        SettingRow("telegram_enabled", "Alertes Telegram", "toggle"),
        SettingRow("telegram_chat_id", "Conversation Telegram", "text"),
        SettingRow("confidence", "Seuil de confiance", "number"),
        SettingRow("sound", "Son des alertes", "toggle"),
        SettingRow("ignore_bots", "Ignorer les bots", "toggle"),
    ]


@dataclass
class Snapshot:
    view: str = "home"
    connected: bool = False
    user: str = ""
    channels: list[str] = field(default_factory=list)
    analyzed: int = 0
    important: int = 0
    alerts: list[AlertEntry] = field(default_factory=list)
    last_event: str = ""
    started_at: float = 0.0
    last_alert_at: float = 0.0
    log_path: str = ""
    selection: int = 0
    setting_index: int = 0
    settings: LiveSettings = field(default_factory=LiveSettings)
    status: str = ""
    mouse_ui: bool = True
    detail_scroll: int = 0
    rows: list[SettingRow] = field(default_factory=list)
    secret_status: dict[str, bool] = field(default_factory=dict)
    editing_row: int = -1
    edit_display: str = ""


# --------------------------------------------------------------------------
# Éléments communs
# --------------------------------------------------------------------------


def _header(screen: Screen, width: int, subtitle: str, right: str = "") -> None:
    screen.put(2, 0, "J A R V I S", "title")
    if width >= 84:
        screen.put(15, 0, f"·  {fit_text(subtitle, width - 40)}", "dim")
    if right:
        screen.put_right(width - 2, 0, right, "dim")
    screen.hline(1, 1, width - 2, "─", "frame")


def _footer(screen: Screen, width: int, height: int, snap: Snapshot) -> None:
    """Barre de boutons + une seule ligne de statut (l'aide reste dans Aide)."""
    screen.hline(1, height - 4, width - 2, "─", "frame")
    _nav_bar(screen, width, height, snap)
    left = ""
    if not snap.mouse_ui:
        left = fit_text(TYPED_HINT, max(12, width - 26))
        screen.put(2, height - 2, left, "dim")
    if snap.status:
        # Le statut ne doit jamais recouvrir l'aide des commandes tapées.
        room = width - 4 - (len(left) + 2 if left else 0)
        screen.put_right(
            width - 2, height - 2, fit_text(snap.status, max(10, room)), "good"
        )
    if not snap.mouse_ui:
        screen.put(2, height - 1, ">", "title")
        screen.put_right(
            width - 3,
            height - 1,
            fit_text("souris indisponible ici · lance depuis Windows Terminal", width - 8),
            "warn",
        )


NAV_ITEMS = (
    (" Accueil ", "home"),
    (" Messages ", "messages"),
    (" Paramètres ", "settings"),
    (" Aide ", "help"),
)


def _nav_bar(screen: Screen, width: int, height: int, snap: Snapshot) -> None:
    """Barre de boutons toujours visible, en bas de l'écran."""
    y = height - 3
    active = "messages" if snap.view == "detail" else snap.view
    x = 2
    for label, action in NAV_ITEMS:
        style = "button_on" if active == action else "button"
        drawn = screen.button(x, y, label, style, action)
        if drawn == 0:
            break
        x += drawn + 1
    screen.button_right(width - 3, y, " Quitter (x) ", "button_warn", "quit")


TYPED_HINT = "tape une commande puis Entrée : 1 2 3 · n/p · o · q · x"


# --------------------------------------------------------------------------
# Orb et vue Accueil
# --------------------------------------------------------------------------


ORB_RAMP = " .:-=+*#%@"


def _draw_orb(
    screen: Screen, cx: int, cy: int, radius: int, t: float, pulse: float
) -> None:
    radius = max(radius, 2)
    for dy in range(-radius - 1, radius + 2):
        for dx in range(-radius - 1, radius + 2):
            distance = math.hypot(dx, dy * 2.0) / radius
            if distance > 1.18:
                continue
            if distance > 1.0:
                screen.put(cx + dx, cy + dy, "·", "dim")
                continue
            angle = math.atan2(dy * 2.0, dx)
            wave = 0.5 + 0.5 * math.sin(t * 2.4 - distance * 7.5)
            sweep = 0.5 + 0.5 * math.cos(angle - t * 1.6)
            core = (1.0 - distance) ** 1.4
            value = 0.62 * core + 0.22 * wave + 0.16 * sweep + 0.22 * pulse
            value = min(1.0, max(0.0, value))
            char = ORB_RAMP[min(len(ORB_RAMP) - 1, int(value * len(ORB_RAMP)))]
            screen.put(cx + dx, cy + dy, char, f"orb{min(4, int(value * 5))}")


def _render_home(snap: Snapshot, width: int, height: int, t: float) -> Screen:
    screen = Screen(width, height)
    panel_width = min(44, max(28, width // 3))
    panel_x = width - panel_width - 2
    panel_y = 3
    visible_alerts = min(3, len(snap.alerts))
    panel_height = max(6, 3 + 3 * max(1, visible_alerts))
    panel_height = min(panel_height, height - 10)

    _header(
        screen,
        width,
        "veille discord",
        time.strftime("%H:%M:%S"),
    )

    body_top, body_bottom = 3, height - 8
    center_x = width // 2
    center_y = (body_top + body_bottom) // 2
    radius = min(13, max(3, panel_x - center_x - 4))
    pulse = 0.0
    if snap.last_alert_at:
        pulse = max(0.0, 1.0 - (time.time() - snap.last_alert_at) / 2.0)
    _draw_orb(screen, center_x, center_y, radius, t, pulse)

    state_lines = [
        (
            f"● {('CONNECTÉ' if snap.connected else 'CONNEXION…')}"
            + (f" · {snap.user}" if snap.user else ""),
            "good" if snap.connected else "warn",
        ),
        (" ".join(snap.channels), "dim"),
        (f"messages {snap.analyzed}   ·   importantes {snap.important}", "text"),
    ]
    if snap.last_event:
        state_lines.append((snap.last_event, "dim"))

    start_y = center_y + radius // 2 + 3
    panel_bottom = panel_y + panel_height
    if start_y <= panel_bottom:
        start_y = panel_bottom + 1
    # La dernière ligne utilisable est height-6 : le pied de page occupe
    # height-4 à height-1. On ne sacrifie une ligne d'état qu'en dernier recours.
    while len(state_lines) > 1 and start_y + len(state_lines) - 1 > height - 6:
        state_lines.pop()
    start_y = max(body_top, min(start_y, height - 6 - len(state_lines) + 1))
    for offset, (line, style) in enumerate(state_lines):
        screen.put_centered(
            center_x, start_y + offset, fit_text(line, width - 4), style
        )

    screen.box(panel_x, panel_y, panel_width, panel_height, "ALERTES")
    inner = panel_width - 4
    if not snap.alerts:
        screen.put(panel_x + 2, panel_y + 2, "aucun message important", "dim")
    for index, entry in enumerate(snap.alerts[:visible_alerts]):
        row = panel_y + 1 + index * 3
        head = f"{time.strftime('%H:%M', time.localtime(entry.at))} {entry.channel}"
        screen.put(panel_x + 2, row, fit_text(head, inner), "title")
        screen.put(
            panel_x + 2,
            row + 1,
            fit_text(f"{entry.author} · {entry.reason}", inner),
            "text",
        )
        # Toute la fiche est cliquable : elle ouvre le message en détail.
        screen.region(panel_x + 1, row, panel_width - 2, 2, "alert", index)

    _footer(screen, width, height, snap)
    return screen


# --------------------------------------------------------------------------
# Vue Messages
# --------------------------------------------------------------------------


def _render_messages(snap: Snapshot, width: int, height: int) -> Screen:
    screen = Screen(width, height)
    total = len(snap.alerts)
    _header(
        screen,
        width,
        "messages importants",
        f"{total} message(s)" if total else "aucun message",
    )

    per_page = max(1, (height - 9) // 2)
    selection = max(0, min(snap.selection, total - 1)) if total else 0
    start = max(0, min(selection - per_page + 1, total - per_page))
    page = snap.alerts[start:start + per_page]

    screen.button(2, 2, " Ouvrir (o) ", "button", "open" if total else "")
    screen.button_right(width - 5, 2, " ▲ ", "pad", "scroll_up" if total else "")
    screen.button_right(width - 2, 2, " ▼ ", "pad", "scroll_down" if total else "")
    if total:
        screen.put_right(
            width - 13,
            2,
            fit_text(f"{selection + 1}/{total}", 9),
            "dim",
        )

    row = 3
    for offset, entry in enumerate(page):
        index = start + offset
        selected = index == selection
        head = (
            f"{index + 1:>2}. {time.strftime('%H:%M', time.localtime(entry.at))}  "
            f"{entry.channel} · {entry.author}"
        )
        confidence = f"{entry.confidence * 100:.0f} %"
        marker = "›" if selected else " "
        screen.put(2, row, marker, "hot" if selected else "dim")
        screen.put(
            4,
            row,
            fit_text(head, width - 16),
            "hot" if selected else "text",
        )
        screen.put_right(width - 3, row, confidence, "title" if selected else "dim")
        screen.put(
            6,
            row + 1,
            fit_text(entry.reason, width - 8),
            "text" if selected else "dim",
        )
        # Clic sur une ligne : elle devient la sélection ; un second clic l'ouvre.
        screen.region(1, row, width - 2, 2, "select", index)
        row += 2

    if not total:
        screen.put_centered(
            width // 2, height // 2 - 2, "aucun message important pour l'instant", "dim"
        )

    _footer(screen, width, height, snap)
    return screen


def detail_layout(
    entry: AlertEntry, width: int, height: int
) -> tuple[list[str], int, int, int]:
    """Lignes du message, haut du cadre, hauteur du cadre, lignes visibles."""
    box_top = 7
    content = entry.content.strip() or "(message sans texte)"
    lines = wrap_text(content, max(10, width - 10))
    box_height = min(max(5, height - 12), max(4, len(lines) + 2))
    return lines, box_top, box_height, box_height - 2


def detail_max_scroll(entry: AlertEntry, width: int, height: int) -> int:
    """Nombre de lignes dont on peut décaler le message vers le haut."""
    lines, _, _, available = detail_layout(entry, width, height)
    return max(0, len(lines) - available)


def _render_detail(snap: Snapshot, width: int, height: int) -> Screen:
    screen = Screen(width, height)
    total = len(snap.alerts)
    if not total:
        _header(screen, width, "message", "")
        screen.put_centered(width // 2, height // 2, "aucun message à afficher", "dim")
        screen.button(2, 2, " ‹ retour ", "button", "back")
        _footer(screen, width, height, snap)
        return screen

    selection = max(0, min(snap.selection, total - 1))
    entry = snap.alerts[selection]
    _header(screen, width, "détail du message", f"{selection + 1}/{total}")

    cursor = 2
    cursor += screen.button(cursor, 2, " ‹ retour ", "button", "back") + 2
    if selection > 0:
        cursor += screen.button(cursor, 2, " ‹ précédent ", "button", "prev") + 2
    if selection < total - 1:
        screen.button(cursor, 2, " suivant › ", "button", "next")

    when = time.strftime("%d/%m/%Y %H:%M", time.localtime(entry.at))
    screen.put(3, 3, fit_text(entry.channel, width // 2), "title")
    screen.put_right(width - 3, 3, f"{entry.confidence * 100:.0f} % de confiance", "dim")
    screen.put(3, 4, f"{entry.author} · {when}", "text")
    screen.put(3, 5, fit_text(f"Raison : {entry.reason}", width - 6), "dim")

    lines, box_top, box_height, available = detail_layout(entry, width, height)
    screen.box(2, box_top, width - 4, box_height, "MESSAGE")

    maximum = max(0, len(lines) - available)
    scroll = max(0, min(snap.detail_scroll, maximum))
    for offset, line in enumerate(lines[scroll:scroll + available]):
        screen.put(4, box_top + 1 + offset, line, "text")

    if maximum:
        end = min(len(lines), scroll + available)
        screen.put_right(
            width - 6, box_top, f" {scroll + 1}-{end}/{len(lines)} ", "dim"
        )
        screen.button_right(width - 4, box_top + 1, "▲", "pad", "detail_up")
        screen.button_right(width - 4, box_top + box_height - 2, "▼", "pad", "detail_down")

    if entry.jump_url:
        screen.put(
            3,
            box_top + box_height,
            fit_text(f"Lien : {entry.jump_url}", width - 6),
            "dim",
        )

    _footer(screen, width, height, snap)
    return screen


# --------------------------------------------------------------------------
# Vue Paramètres
# --------------------------------------------------------------------------


def _setting_value(snap: Snapshot, row: SettingRow) -> tuple[str, str]:
    """Texte et style de la valeur affichée à droite."""
    settings = snap.settings
    if row.kind == "secret":
        return ("défini", "good") if snap.secret_status.get(row.key) else ("absent", "warn")
    if row.key == "telegram_enabled":
        return ("activé", "title") if settings.telegram_enabled else ("désactivé", "dim")
    if row.key == "telegram_chat_id":
        if settings.telegram_chat_id:
            return (settings.telegram_chat_id, "title")
        return ("—", "dim")
    if row.key == "confidence":
        return (f"{settings.min_confidence * 100:.0f} %", "title")
    if row.key == "sound":
        return ("activé", "title") if settings.sound else ("désactivé", "dim")
    return ("oui", "title") if settings.ignore_bots else ("non", "dim")


def _draw_setting_row(
    screen: Screen, row: SettingRow, index: int, y: int, snap: Snapshot, width: int
) -> None:
    selected = index == snap.setting_index
    screen.put(2, y, "›" if selected else " ", "hot" if selected else "dim")
    screen.put(4, y, fit_text(row.label, 24), "hot" if selected else "text")
    if row.variable:
        screen.put(30, y, fit_text(row.variable, 20), "dim")

    # La ligne entière sélectionne ; les boutons, ajoutés ensuite, sont prioritaires.
    screen.region(1, y, width - 2, 1, "setting", index)

    if snap.editing_row == index:
        screen.put(width - 24, y, fit_text(f" {snap.edit_display}▏", 20), "pad")
        screen.region(width - 24, y, 22, 1, "setting", index)
        return

    value, style = _setting_value(snap, row)
    screen.put_right(width - 6, y, fit_text(value, 14), style)
    if row.kind == "number":
        screen.button_right(width - 14, y, " ‹ ", "pad", "adjust", (index, -1))
        screen.button_right(width - 2, y, " › ", "pad", "adjust", (index, 1))
        return

    action = "edit" if row.kind in ("secret", "text") else "flip"
    screen.region(width - 22, y, 22, 1, action, index)


def _render_settings(snap: Snapshot, width: int, height: int) -> Screen:
    screen = Screen(width, height)
    _header(screen, width, "paramètres", "")

    rows = snap.rows or build_setting_rows()
    y = 3
    group = ""
    for index, row in enumerate(rows):
        if row.kind == "secret" and group != "secret":
            group = "secret"
            screen.put(2, y, "SÉCURITÉ · enregistré dans .env", "dim")
            if width >= 72:
                screen.put_right(width - 4, y, "clic : saisir · masqué", "dim")
            y += 1
        elif row.kind != "secret" and group != "settings":
            if group:
                y += 1
            group = "settings"
            screen.put(2, y, "RÉGLAGES · enregistré dans config.json", "dim")
            y += 1
        if y > height - 6:
            break  # terminal trop petit : on garde le pied de page
        _draw_setting_row(screen, row, index, y, snap, width)
        y += 1

    _footer(screen, width, height, snap)
    return screen


# --------------------------------------------------------------------------
# Vue Aide
# --------------------------------------------------------------------------


HELP_LINES = [
    ("À la souris", "title"),
    ("  clic        sur un bouton de la barre du bas, une ligne, une flèche", "text"),
    ("  molette     fait défiler la liste et le message ouvert", "text"),
    ("  2e clic     sur une ligne déjà sélectionnée : ouvre le message", "text"),
    ("", ""),
    ("Au clavier", "title"),
    ("  1 / 2 / 3   accueil, messages, paramètres      ? aide      x quitter", "text"),
    ("  flèches     même chose que les boutons ▲ ▼ et ‹ ›", "text"),
    ("  Entrée      ouvrir le message, ou modifier le réglage sélectionné", "text"),
    ("  Échap / q   revenir en arrière (Échap annule une saisie)", "text"),
    ("", ""),
    ("Dans Paramètres", "title"),
    ("  clic sur une valeur, ou Entrée : modifier (jetons, conversation)", "text"),
    ("  ‹ ›         changer le seuil de confiance", "text"),
    ("  la saisie est masquée, puis écrite dans .env (ou config.json)", "text"),
    ("", ""),
    ("Si la souris ne répond pas", "title"),
    ("  Lance depuis Windows Terminal (ou cmd, PowerShell) : Git Bash / mintty", "text"),
    ("  ne transmet ni les clics ni la molette à un Python natif.", "text"),
]


def _render_help(snap: Snapshot, width: int, height: int) -> Screen:
    screen = Screen(width, height)
    _header(screen, width, "aide", "")
    for index, (line, style) in enumerate(HELP_LINES):
        if 3 + index >= height - 5:
            break
        screen.put(4, 3 + index, fit_text(line, width - 8), style)
    _footer(screen, width, height, snap)
    return screen


# --------------------------------------------------------------------------
# Repli pour petit terminal
# --------------------------------------------------------------------------


def _render_compact(snap: Snapshot, width: int, height: int) -> Screen:
    screen = Screen(width, height)
    state_text = "connecté" if snap.connected else "connexion…"
    titles = {
        "messages": "messages importants",
        "detail": "détail",
        "settings": "paramètres",
        "help": "aide",
    }
    subtitle = titles.get(snap.view, "")
    screen.put(
        0,
        0,
        fit_text(f"JARVIS · {state_text} · {snap.user} {subtitle}".strip(), width),
        "title",
    )
    screen.put(
        0,
        1,
        fit_text(
            f"messages {snap.analyzed} · alertes {snap.important} · "
            + " ".join(snap.channels),
            width,
        ),
        "dim",
    )
    if snap.view in ("messages", "detail") and snap.alerts:
        entry = snap.alerts[min(snap.selection, len(snap.alerts) - 1)]
        screen.put(
            0,
            2,
            fit_text(f"⚠ {entry.channel} · {entry.author} : {entry.reason}", width),
            "hot",
        )
    elif snap.alerts:
        latest = snap.alerts[0]
        screen.put(
            0,
            2,
            fit_text(f"⚠ {latest.channel} · {latest.author} : {latest.reason}", width),
            "hot",
        )
    screen.put(0, height - 1, fit_text("1 accueil · 2 messages · 3 paramètres · ? aide", width), "dim")
    return screen


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------


def render(snap: Snapshot, width: int, height: int, t: float) -> Screen:
    """Compose une image complète de l'interface."""
    if width < 70 or height < 16:
        return _render_compact(snap, width, height)
    if snap.view == "messages":
        return _render_messages(snap, width, height)
    if snap.view == "detail":
        return _render_detail(snap, width, height)
    if snap.view == "settings":
        return _render_settings(snap, width, height)
    if snap.view == "help":
        return _render_help(snap, width, height)
    return _render_home(snap, width, height, t)


# --------------------------------------------------------------------------
# Contrôleur
# --------------------------------------------------------------------------


def terminal_size(stream: object) -> tuple[int, int]:
    """Taille réelle du terminal, en colonnes et lignes.

    ``shutil.get_terminal_size`` obéit d'abord à ``COLUMNS``/``LINES`` : une
    valeur héritée d'un autre terminal casse toute la mise en page (des lignes
    plus larges que l'écran se replient et laissent des restes). On interroge
    donc d'abord le terminal lui-même.
    """
    try:
        size = os.get_terminal_size(stream.fileno())
        return size.columns, size.lines
    except (AttributeError, OSError, ValueError):
        size = shutil.get_terminal_size((100, 30))
        return size.columns, size.lines


class Dashboard:
    """État partagé avec le bot, rendu et lecture des commandes."""

    QUIET_SECONDS = 10.0

    def __init__(
        self,
        refresh: float = 0.08,
        log_path: str = "",
        settings: LiveSettings | None = None,
        config_path: str | Path | None = None,
        env_path: str | Path | None = None,
        secret_variables: dict[str, str] | None = None,
        classifier: object | None = None,
    ) -> None:
        self._rows = build_setting_rows(secret_variables)
        self._secrets = (
            SecretStore(
                env_path,
                [
                    SecretField(row.key, row.variable, row.label)
                    for row in self._rows
                    if row.kind == "secret"
                ],
            )
            if env_path is not None
            else None
        )
        self._classifier = classifier
        self._edit_row = -1
        self._edit_buffer = ""
        self._refresh = refresh
        self.log_path = log_path
        self._lock = threading.Lock()
        self._settings = settings or LiveSettings()
        self._config_path = Path(config_path) if config_path else None
        self._snapshot = Snapshot(started_at=time.time(), log_path=log_path)
        self._view = "home"
        self._selection = 0
        self._setting_index = 0
        self._status = ""
        self._dirty = True
        self._last_input = 0.0
        self._running = False
        self._thread: threading.Thread | None = None
        self._reader: threading.Thread | None = None
        self._stdout = sys.stdout
        self.quit_requested = False
        # Zones cliquables du dernier rendu : les clics y sont comparés.
        self._hotspots: list[Hotspot] = []
        self._size = (100, 30)
        self._detail_scroll = 0
        self._mouse_ui = False
        self._backend: InputBackend | None = None

    # -- état --------------------------------------------------------------

    def connected(self, user: str, channels: list[str]) -> None:
        with self._lock:
            self._snapshot.connected = True
            self._snapshot.user = user
            self._snapshot.channels = list(channels)
            self._dirty = True

    def event(self, text: str) -> None:
        with self._lock:
            self._snapshot.last_event = text
            self._dirty = True

    def record(self, message: object, verdict: object, notified: bool) -> None:
        """Appelé par l'analyseur pour chaque verdict."""
        now = time.time()
        channel = getattr(message, "channel_name", "") or "?"
        author = getattr(message, "author_name", "") or "?"
        with self._lock:
            self._snapshot.analyzed += 1
            important = bool(getattr(verdict, "important", False))
            confidence = float(getattr(verdict, "confidence", 0.0))
            reason = str(getattr(verdict, "reason", ""))
            if notified:
                self._snapshot.important += 1
                self._snapshot.alerts.insert(
                    0,
                    AlertEntry(
                        at=now,
                        channel=f"#{channel}",
                        author=author,
                        reason=reason,
                        confidence=confidence,
                        content=str(getattr(message, "content", "")),
                        jump_url=str(getattr(message, "jump_url", "")),
                    ),
                )
                del self._snapshot.alerts[200:]
                self._snapshot.last_alert_at = now
            self._snapshot.last_event = fit_text(
                f"#{channel} · {author} → {'important' if important else 'normal'}"
                f" ({confidence:.0%})",
                70,
            )
            self._dirty = True

    def snapshot(self) -> Snapshot:
        with self._lock:
            current = self._snapshot
            return Snapshot(
                view=self._view,
                connected=current.connected,
                user=current.user,
                channels=list(current.channels),
                analyzed=current.analyzed,
                important=current.important,
                alerts=list(current.alerts),
                last_event=current.last_event,
                started_at=current.started_at,
                last_alert_at=current.last_alert_at,
                log_path=current.log_path,
                selection=self._selection,
                setting_index=self._setting_index,
                settings=self._settings.copy(),
                status=self._status,
                mouse_ui=self._mouse_ui,
                detail_scroll=self._detail_scroll,
                rows=list(self._rows),
                secret_status=self._secret_status(),
                editing_row=self._edit_row,
                edit_display=self._edit_display(),
            )

    def stats(self) -> tuple[int, int]:
        with self._lock:
            return self._snapshot.analyzed, self._snapshot.important

    # -- commandes ---------------------------------------------------------

    def handle_command(self, line: str) -> None:
        """Traite une commande tapée par l'utilisateur."""
        command = " ".join(line.strip().lower().split())
        quit_now = False
        with self._lock:
            self._status = ""
            view = self._view

            if command in ("x", "quitter", "exit"):
                self.quit_requested = True
                self._status = "arrêt en cours…"
                quit_now = True
            elif command in ("", "entree", "console"):
                self._status = "tape une commande puis Entrée"
            elif command in ("1", "a", "accueil", "home"):
                self._view = "home"
            elif command in ("2", "m", "messages"):
                self._view = "messages"
                self._selection = 0
                self._detail_scroll = 0
            elif command in ("3", "s", "parametres", "settings"):
                self._view = "settings"
                self._setting_index = 0
            elif command in ("?", "aide", "help"):
                self._view = "help"
            elif view == "messages":
                self._command_messages(command)
            elif view == "detail":
                self._command_detail(command)
            elif view == "settings":
                self._command_settings(command)
            elif command in ("q", "b", "retour"):
                self._view = "home"
            else:
                self._status = f"commande inconnue : {command}"

            self._last_input = time.monotonic()
            self._dirty = True

        if quit_now:
            _thread.interrupt_main()

    # -- souris et clavier --------------------------------------------------

    def click(self, x: int, y: int, double: bool = False) -> bool:
        """Traite un clic ; renvoie ``True`` si un bouton a réagi."""
        hotspot = hit_test(self._hotspots, x, y)
        if hotspot is None:
            return False
        self.activate(hotspot.action, hotspot.payload, double=double)
        return True

    def scroll(self, steps: int) -> None:
        """Molette : positif vers le haut."""
        if steps == 0:
            return
        with self._lock:
            total = len(self._snapshot.alerts)
            if self._view == "messages" and total:
                self._selection = max(0, min(total - 1, self._selection - steps))
            elif self._view == "detail" and total:
                self._scroll_detail(-steps * 3)
            else:
                return
            self._last_input = time.monotonic()
            self._dirty = True

    KEY_COMMANDS = {"up": "p", "down": "n", "left": "-", "right": "+", "tab": "n"}

    def key(self, name: str) -> None:
        """Touche pressée (sans Entrée) : flèches, Échap ou caractère."""
        if self._edit_row >= 0:
            self._edit_key(name)
            return
        if name in self.KEY_COMMANDS:
            self.handle_command(self.KEY_COMMANDS[name])
        elif name == "enter":
            with self._lock:
                view = self._view
            if view in ("messages", "detail"):
                self.handle_command("o")
            elif view == "settings":
                with self._lock:
                    self._activate_row(self._setting_index)
                    self._dirty = True
        elif name == "escape":
            self.handle_command("q")
        elif len(name) == 1:
            self.handle_command(name)

    def activate(
        self, action: str, payload: object = None, double: bool = False
    ) -> None:
        """Exécute l'action d'une zone cliquable."""
        quit_now = False
        with self._lock:
            self._status = ""
            if self._edit_row >= 0 and action != "quit":
                self._cancel_edit("modification annulée")
            total = len(self._snapshot.alerts)

            if action == "quit":
                self.quit_requested = True
                self._status = "arrêt en cours…"
                quit_now = True
            elif action in ("home", "messages", "settings", "help"):
                self._view = action
                self._selection = 0
                self._detail_scroll = 0
            elif action == "back":
                self._view = "messages" if self._view == "detail" else "home"
            elif action == "open":
                if total:
                    self._view = "detail"
                    self._detail_scroll = 0
                    self._selection = max(0, min(total - 1, self._selection))
            elif action == "alert":
                if total:
                    self._selection = max(0, min(total - 1, int(payload or 0)))
                    self._view = "detail"
                    self._detail_scroll = 0
            elif action == "select":
                if total:
                    index = max(0, min(total - 1, int(payload or 0)))
                    if index == self._selection or double:
                        # Deuxième clic sur la ligne déjà choisie : on l'ouvre.
                        self._view = "detail"
                        self._detail_scroll = 0
                    self._selection = index
            elif action in ("prev", "next", "scroll_up", "scroll_down"):
                if total:
                    step = -1 if action in ("prev", "scroll_up") else 1
                    self._selection = max(0, min(total - 1, self._selection + step))
                    self._detail_scroll = 0
            elif action in ("detail_up", "detail_down"):
                self._scroll_detail(-1 if action == "detail_up" else 1)
            elif action == "setting":
                self._setting_index = int(payload or 0) % len(self._rows)
                if self._view == "settings" and not self._status:
                    self._status = "clic sur une valeur pour la modifier"
            elif action == "edit":
                self.start_edit(int(payload or 0))
            elif action == "flip":
                self._setting_index = int(payload or 0) % len(self._rows)
                self._change_setting(1)
            elif action == "adjust" and isinstance(payload, tuple):
                index, direction = payload[:2]
                self._setting_index = int(index) % len(self._rows)
                self._change_setting(int(direction))

            self._last_input = time.monotonic()
            self._dirty = True

        if quit_now:
            _thread.interrupt_main()

    def _scroll_detail(self, step: int) -> None:
        total = len(self._snapshot.alerts)
        if not total:
            return
        entry = self._snapshot.alerts[max(0, min(self._selection, total - 1))]
        maximum = detail_max_scroll(entry, *self._size)
        self._detail_scroll = max(0, min(maximum, self._detail_scroll + step))

    def _command_messages(self, command: str) -> None:
        total = len(self._snapshot.alerts)
        if command in ("q", "b", "retour"):
            self._view = "home"
        elif command in ("n", "+", "j", "suivant", "bas"):
            if total:
                self._selection = min(total - 1, self._selection + 1)
        elif command in ("p", "-", "k", "precedent", "haut"):
            if total:
                self._selection = max(0, self._selection - 1)
        elif command in ("o", "ouvrir", "entree"):
            if total:
                self._view = "detail"
                self._detail_scroll = 0
            else:
                self._status = "aucun message à ouvrir"
        else:
            self._status = f"commande inconnue : {command}"

    def _command_detail(self, command: str) -> None:
        total = len(self._snapshot.alerts)
        if command in ("q", "b", "retour"):
            self._view = "messages"
        elif command in ("n", "+", "j", "suivant"):
            if total:
                self._selection = min(total - 1, self._selection + 1)
                self._detail_scroll = 0
        elif command in ("p", "-", "k", "precedent"):
            if total:
                self._selection = max(0, self._selection - 1)
                self._detail_scroll = 0
        else:
            self._status = f"commande inconnue : {command}"

    def _command_settings(self, command: str) -> None:
        total = len(self._rows)
        if command in ("q", "b", "retour"):
            self._cancel_edit()
            self._view = "home"
        elif command in ("n", "j", "suivant", "bas"):
            self._setting_index = (self._setting_index + 1) % total
        elif command in ("p", "k", "precedent", "haut"):
            self._setting_index = (self._setting_index - 1) % total
        elif command in ("+", "plus", "droite"):
            self._change_setting(1)
        elif command in ("-", "moins", "gauche"):
            self._change_setting(-1)
        elif command in ("o", "entree", "ouvrir", "modifier"):
            self._activate_row(self._setting_index)
        else:
            self._status = f"commande inconnue : {command}"

    # -- édition d'un réglage ---------------------------------------------

    def _secret_status(self) -> dict[str, bool]:
        if self._secrets is not None:
            return self._secrets.status()
        return {
            row.key: bool(os.environ.get(row.variable, "").strip())
            for row in self._rows
            if row.kind == "secret"
        }

    def _edit_display(self) -> str:
        """Ce que l'écran a le droit d'afficher pendant la saisie."""
        if self._edit_row < 0:
            return ""
        text = self._edit_buffer
        if self._rows[self._edit_row].kind == "secret":
            text = "•" * len(text)
        return text[-18:]

    def start_edit(self, index: int) -> bool:
        """Passe une ligne en saisie (secret ou texte libre)."""
        row = self._rows[index % len(self._rows)]
        if row.kind not in ("secret", "text"):
            return False
        self._setting_index = index % len(self._rows)
        self._edit_row = self._setting_index
        self._edit_buffer = (
            self._settings.telegram_chat_id if row.key == "telegram_chat_id" else ""
        )
        self._status = "Entrée : enregistrer · Échap : annuler"
        return True

    def _cancel_edit(self, message: str = "") -> None:
        self._edit_row = -1
        self._edit_buffer = ""
        if message:
            self._status = message

    def _edit_key(self, name: str) -> None:
        with self._lock:
            if name == "escape":
                self._cancel_edit("modification annulée")
            elif name == "enter":
                self._commit_edit()
            elif name == "backspace":
                self._edit_buffer = self._edit_buffer[:-1]
            elif len(name) == 1 and name.isprintable() and len(self._edit_buffer) < 400:
                self._edit_buffer += name
            if self._edit_row >= 0:
                self._status = "Entrée : enregistrer · Échap : annuler"
            self._last_input = time.monotonic()
            self._dirty = True

    def _commit_edit(self) -> None:
        row = self._rows[self._edit_row]
        value = self._edit_buffer.strip()
        self._edit_row = -1
        self._edit_buffer = ""
        if row.kind == "secret":
            # Une valeur vide n'efface rien : un double Entrée ne doit pas
            # supprimer un jeton par accident (le fichier .env reste éditable).
            if not value:
                self._cancel_edit("inchangé · rien n'a été saisi")
                return
            self._save_secret(row, value)
        else:
            self._settings.telegram_chat_id = value
            self._status = (
                "conversation enregistrée"
                if self._persist()
                else "conversation (non enregistrée)"
            )

    def _save_secret(self, row: SettingRow, value: str) -> None:
        if self._secrets is None:
            self._status = "fichier .env introuvable"
            return
        if not self._secrets.set(row.key, value):
            self._status = "enregistrement impossible (voir selfai.log)"
            return
        if row.key == "webhook":
            self._settings.webhook_url = value
        elif row.key == "ai_key":
            setter = getattr(self._classifier, "set_api_key", None)
            if callable(setter):
                setter(value)
        self._status = (
            "enregistré · actif au prochain démarrage"
            if row.key == "discord_token"
            else "enregistré · actif"
        )

    def _activate_row(self, index: int) -> None:
        """Entrée, ou clic sur la valeur : modifier ou basculer."""
        self._setting_index = index % len(self._rows)
        row = self._rows[self._setting_index]
        if row.kind in ("secret", "text"):
            self.start_edit(self._setting_index)
        elif row.kind == "toggle":
            self._change_setting(1)
        else:
            self._status = "seuil : utilise les boutons ‹ ›"

    def _change_setting(self, direction: int, toggle: bool = False) -> None:
        row = self._rows[self._setting_index]
        if row.kind in ("secret", "text"):
            self.start_edit(self._setting_index)
            return
        if row.kind == "number":
            self._settings.adjust_confidence(direction)
            label = f"seuil {self._settings.min_confidence:.0%}"
        elif row.key == "sound":
            self._settings.sound = not self._settings.sound
            label = f"son {'activé' if self._settings.sound else 'désactivé'}"
        elif row.key == "ignore_bots":
            self._settings.ignore_bots = not self._settings.ignore_bots
            label = f"bots {'ignorés' if self._settings.ignore_bots else 'analysés'}"
        else:
            self._settings.telegram_enabled = not self._settings.telegram_enabled
            label = (
                f"Telegram {'activé' if self._settings.telegram_enabled else 'désactivé'}"
            )

        self._status = f"{label} — enregistré" if self._persist() else f"{label} (non enregistré)"

    def _persist(self) -> bool:
        if self._config_path is None:
            return False
        try:
            save_settings(self._config_path, self._settings)
            return True
        except Exception:
            logging.getLogger(__name__).exception("Sauvegarde des réglages impossible")
            return False

    # -- boucle de rendu ---------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        # Souris + touches par touche quand le terminal sait les transmettre.
        self._backend = create_input()
        self._mouse_ui = self._backend is not None
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="selfai-dashboard", daemon=True
        )
        self._thread.start()
        target = self._read_events if self._mouse_ui else self._read_commands
        self._reader = threading.Thread(
            target=target, name="selfai-input", daemon=True
        )
        self._reader.start()

    def stop(self) -> None:
        self._running = False
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.join(timeout=1.0)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        # Terminal restauré après la dernière image : sinon les séquences de la
        # souris peuvent atterrir au milieu d'un rendu.
        if self._backend is not None:
            try:
                self._backend.close()
            except Exception:
                pass
            self._backend = None

    def frame(self, width: int, height: int, t: float = 0.0) -> Screen:
        """Compose l'écran courant et mémorise ses zones cliquables."""
        self._size = (width, height)
        screen = render(self.snapshot(), width, height, t)
        self._hotspots = list(screen.hotspots)
        return screen

    def _should_draw(self) -> bool:
        if self._dirty:
            return True
        if self._view != "home":
            return False
        if self._mouse_ui:
            return True  # orb animé en continu
        return time.monotonic() - self._last_input > self.QUIET_SECONDS

    def _loop(self) -> None:
        write = self._stdout.write
        try:
            write(ALT_SCREEN_ON + CURSOR_HIDE + CLEAR)
            self._stdout.flush()
            started = time.monotonic()
            while self._running:
                if self._should_draw():
                    columns, lines = terminal_size(self._stdout)
                    width = max(20, columns)
                    height = max(12, lines - 1)
                    screen = self.frame(width, height, time.monotonic() - started)
                    write("\x1b[H" + screen.to_ansi())
                    if self._mouse_ui:
                        write(CURSOR_HIDE)
                    else:
                        # Curseur sur la ligne de commande, juste après « > ».
                        write(f"\x1b[{height};3H" + CURSOR_SHOW)
                    self._stdout.flush()
                    self._dirty = False
                time.sleep(self._refresh)
        finally:
            write(RESET + CURSOR_SHOW + ALT_SCREEN_OFF)
            self._stdout.flush()

    def _read_events(self) -> None:
        backend = self._backend
        if backend is None:
            return
        try:
            for event in backend.events(lambda: self._running):
                if isinstance(event, Click):
                    self.click(event.x, event.y, double=event.double)
                elif isinstance(event, Scroll):
                    self.scroll(event.steps)
                elif isinstance(event, Key):
                    self.key(event.name)
        except Exception:
            logging.getLogger(__name__).exception("Lecture des entrées interrompue")

    def _read_commands(self) -> None:
        stream = sys.stdin
        if stream is None:
            return
        empty_reads = 0
        while self._running:
            try:
                line = stream.readline()
            except Exception:
                return
            if line == "":
                empty_reads += 1
                if empty_reads > 200:
                    return
                time.sleep(0.05)
                continue
            empty_reads = 0
            self.handle_command(line)


class DashboardLogHandler(logging.Handler):
    """Remonte les erreurs dans la ligne d'état, pas les avertissements.

    Les avertissements (PyNaCl manquant, rappel des CGU, etc.) partent dans le
    journal : ils n'ont rien à faire au milieu de l'interface.
    """

    def __init__(self, dashboard: Dashboard) -> None:
        super().__init__(logging.ERROR)
        self._dashboard = dashboard

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._dashboard.event(f"{record.levelname.lower()} · {record.getMessage()}")
        except Exception:  # un handler ne doit jamais casser le programme
            pass

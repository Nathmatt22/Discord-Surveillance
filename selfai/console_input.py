"""Souris et clavier touche par touche, sans dépendance externe.

Deux implémentations :

- ``WindowsConsole`` lit les enregistrements de la console Windows
  (``ReadConsoleInputW``) : c'est la seule façon fiable d'obtenir les clics et la
  molette avec un Python natif. Elle fonctionne dans Windows Terminal, cmd,
  PowerShell et VS Code — pas dans Git Bash (mintty), qui n'est pas une console.
- ``PosixTerminal`` passe le pseudo-terminal en mode brut et décode les
  séquences ANSI (flèches, souris SGR).

``create_input`` choisit la bonne implémentation et renvoie ``None`` quand aucune
n'est disponible : l'interface retombe alors sur les commandes tapées.
"""

from __future__ import annotations

import os
import select
import sys
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol, Union


# --------------------------------------------------------------------------
# Événements
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Key:
    """Une touche : ``up``, ``down``, ``enter``, ``escape`` ou un caractère."""

    name: str


@dataclass(frozen=True)
class Click:
    """Un clic gauche, en colonnes/lignes de l'écran (0 = coin haut gauche)."""

    x: int
    y: int
    double: bool = False


@dataclass(frozen=True)
class Scroll:
    """La molette : ``steps`` positif vers le haut, négatif vers le bas."""

    steps: int
    x: int = 0
    y: int = 0


Event = Union[Key, Click, Scroll]


class InputBackend(Protocol):
    def events(self, running: Callable[[], bool]) -> Iterator[Event]: ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------
# Windows : lecture directe de la console
# --------------------------------------------------------------------------

KEY_EVENT = 0x0001
MOUSE_EVENT = 0x0002

MOUSE_MOVED = 0x0001
DOUBLE_CLICK = 0x0002
MOUSE_WHEELED = 0x0004

FROM_LEFT_1ST_BUTTON_PRESSED = 0x0001

ENABLE_MOUSE_INPUT = 0x0010
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

_VK_NAMES = {
    0x0D: "enter",
    0x1B: "escape",
    0x09: "tab",
    0x08: "backspace",
    0x25: "left",
    0x26: "up",
    0x27: "right",
    0x28: "down",
}

_HIGH_BIT = 0x80000000


def _windows_api():  # pragma: no cover - dépend de l'OS
    import ctypes
    from ctypes import wintypes

    class COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class SMALL_RECT(ctypes.Structure):
        _fields_ = [
            ("Left", wintypes.SHORT),
            ("Top", wintypes.SHORT),
            ("Right", wintypes.SHORT),
            ("Bottom", wintypes.SHORT),
        ]

    class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
        _fields_ = [
            ("dwSize", COORD),
            ("dwCursorPosition", COORD),
            ("wAttributes", wintypes.WORD),
            ("srWindow", SMALL_RECT),
            ("dwMaximumWindowSize", COORD),
        ]

    class KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("bKeyDown", wintypes.BOOL),
            ("wRepeatCount", wintypes.WORD),
            ("wVirtualKeyCode", wintypes.WORD),
            ("wVirtualScanCode", wintypes.WORD),
            ("uChar", wintypes.WCHAR),
            ("dwControlKeyState", wintypes.DWORD),
        ]

    class MOUSE_EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("dwMousePosition", COORD),
            ("dwButtonState", wintypes.DWORD),
            ("dwControlKeyState", wintypes.DWORD),
            ("dwEventFlags", wintypes.DWORD),
        ]

    class _EventUnion(ctypes.Union):
        _fields_ = [
            ("KeyEvent", KEY_EVENT_RECORD),
            ("MouseEvent", MOUSE_EVENT_RECORD),
            ("_padding", ctypes.c_byte * 16),
        ]

    class INPUT_RECORD(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", _EventUnion)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.ReadConsoleInputW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(INPUT_RECORD),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetConsoleScreenBufferInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO),
    ]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    return {
        "ctypes": ctypes,
        "wintypes": wintypes,
        "kernel32": kernel32,
        "INPUT_RECORD": INPUT_RECORD,
        "KEY_EVENT_RECORD": KEY_EVENT_RECORD,
        "MOUSE_EVENT_RECORD": MOUSE_EVENT_RECORD,
        "CONSOLE_SCREEN_BUFFER_INFO": CONSOLE_SCREEN_BUFFER_INFO,
    }


class WindowsConsole:
    """Clics, molette et touches lues directement dans la console Windows."""

    def __init__(self) -> None:
        self._api = _windows_api()
        self._ctypes = self._api["ctypes"]
        kernel32 = self._api["kernel32"]
        self._stdin = kernel32.GetStdHandle(-10)
        self._stdout = kernel32.GetStdHandle(-11)
        self._saved_input: int | None = None
        self._saved_output: int | None = None

    @classmethod
    def available(cls) -> bool:
        if sys.platform != "win32":
            return False
        try:
            api = _windows_api()
        except Exception:  # pragma: no cover - plateforme sans console
            return False
        return cls._mode_of(api, -10) is not None

    @staticmethod
    def _mode_of(api, handle_id: int):  # pragma: no cover - dépend de l'OS
        mode = api["wintypes"].DWORD()
        handle = api["kernel32"].GetStdHandle(handle_id)
        if not api["kernel32"].GetConsoleMode(handle, api["ctypes"].byref(mode)):
            return None
        return mode.value

    def open(self) -> None:  # pragma: no cover - dépend de l'OS
        kernel32 = self._api["kernel32"]
        input_mode = self._mode_of(self._api, -10)
        if input_mode is None:
            raise OSError("console d'entrée indisponible")
        self._saved_input = input_mode  # mode d'origine, restauré à la sortie
        # Sans ENABLE_MOUSE_INPUT aucun clic n'arrive ; sans EXTENDED_FLAGS, on
        # ne peut pas désactiver la sélection à la souris (qui bloque le reste).
        wanted = (input_mode | ENABLE_MOUSE_INPUT | ENABLE_EXTENDED_FLAGS) & ~(
            ENABLE_QUICK_EDIT_MODE
        )
        if not kernel32.SetConsoleMode(self._stdin, wanted):
            self._saved_input = None
            raise OSError("impossible d'activer la souris")

        output_mode = self._mode_of(self._api, -11)
        if output_mode is not None and not output_mode & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            if kernel32.SetConsoleMode(
                self._stdout, output_mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            ):
                self._saved_output = output_mode

    def close(self) -> None:  # pragma: no cover - dépend de l'OS
        kernel32 = self._api["kernel32"]
        if self._saved_input is not None:
            kernel32.SetConsoleMode(self._stdin, self._saved_input)
            self._saved_input = None
        if self._saved_output is not None:
            kernel32.SetConsoleMode(self._stdout, self._saved_output)
            self._saved_output = None

    # -- lecture -----------------------------------------------------------

    def _window_origin(self) -> tuple[int, int]:  # pragma: no cover - dépend de l'OS
        info = self._api["CONSOLE_SCREEN_BUFFER_INFO"]()
        if self._api["kernel32"].GetConsoleScreenBufferInfo(
            self._stdout, self._ctypes.byref(info)
        ):
            return info.srWindow.Left, info.srWindow.Top
        return 0, 0

    def events(self, running: Callable[[], bool]) -> Iterator[Event]:  # pragma: no cover
        kernel32 = self._api["kernel32"]
        ctypes = self._ctypes
        record = self._api["INPUT_RECORD"]()
        count = self._api["wintypes"].DWORD()
        while running():
            # Attente courte : permet de sortir proprement à l'arrêt.
            if kernel32.WaitForSingleObject(self._stdin, 120) != 0:
                continue
            read = kernel32.ReadConsoleInputW(
                self._stdin, ctypes.byref(record), 1, ctypes.byref(count)
            )
            if not read or count.value == 0:
                return
            event = self.decode_record(record, self._window_origin())
            if event is not None:
                yield event

    def decode_record(self, record, origin: tuple[int, int] = (0, 0)) -> Event | None:
        """Convertit un enregistrement de console en événement de l'interface."""
        if record.EventType == KEY_EVENT:
            return self._key_event(record.Event.KeyEvent)
        if record.EventType != MOUSE_EVENT:
            return None

        mouse = record.Event.MouseEvent
        x = mouse.dwMousePosition.X - origin[0]
        y = mouse.dwMousePosition.Y - origin[1]
        if mouse.dwEventFlags & MOUSE_WHEELED:
            delta = mouse.dwButtonState >> 16
            if delta & 0x8000:
                delta -= 0x10000
            return Scroll(steps=1 if delta > 0 else -1, x=x, y=y)
        if mouse.dwEventFlags & MOUSE_MOVED:
            return None
        if mouse.dwEventFlags & DOUBLE_CLICK:
            return Click(x=x, y=y, double=True)
        if mouse.dwButtonState & FROM_LEFT_1ST_BUTTON_PRESSED:
            return Click(x=x, y=y)
        return None

    def _key_event(self, key) -> Key | None:
        if not key.bKeyDown:
            return None
        name = _VK_NAMES.get(key.wVirtualKeyCode)
        if name is not None:
            return Key(name)
        char = key.uChar
        if char and char.isprintable():
            return Key(char.lower())
        return None


# --------------------------------------------------------------------------
# POSIX : mode brut + séquences ANSI
# --------------------------------------------------------------------------


class PosixTerminal:
    """Terminal POSIX en mode brut, souris au format SGR (``ESC [ < b ; x ; y M``)."""

    def __init__(self) -> None:
        self._fd = sys.stdin.fileno()
        self._saved: list | None = None
        self._buffer = b""
        self._pending_escape = 0

    @classmethod
    def available(cls) -> bool:
        if sys.platform == "win32":
            return False
        try:
            return sys.stdin.isatty()
        except (AttributeError, ValueError):
            return False

    def open(self) -> None:  # pragma: no cover - dépend de l'OS
        import termios
        import tty

        self._saved = termios.tcgetattr(self._fd)
        # Mode « cbreak » et non « raw » : les touches arrivent une par une mais le
        # terminal continue d'interpréter Ctrl+C, sinon impossible d'arrêter le
        # programme au clavier.
        tty.setcbreak(self._fd)
        self._write("\x1b[?1000h\x1b[?1006h\x1b[?1007l")

    @staticmethod
    def _write(sequence: str) -> None:
        try:
            sys.stdout.write(sequence)
            sys.stdout.flush()
        except Exception:
            try:
                os.write(1, sequence.encode())
            except OSError:
                pass

    def close(self) -> None:  # pragma: no cover - dépend de l'OS
        self._write("\x1b[?1000l\x1b[?1006l")
        if self._saved is None:
            return
        import termios

        try:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        except Exception:
            pass
        self._saved = None

    def events(self, running: Callable[[], bool]) -> Iterator[Event]:  # pragma: no cover
        while running():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.12)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                chunk = os.read(self._fd, 64)
            except OSError:
                return
            if not chunk:
                return
            self._buffer += chunk
            if self._buffer == b"\x1b":
                # Peut être le début d'une séquence : on laisse une chance de
                # plus, puis on considère que c'était la touche Échap.
                self._pending_escape += 1
                if self._pending_escape >= 2:
                    self._buffer = b""
                    self._pending_escape = 0
                    yield Key("escape")
                continue
            self._pending_escape = 0
            for event in self._decode():
                yield event

    def _decode(self) -> list[Event]:
        events: list[Event] = []
        while self._buffer:
            event, consumed = parse_ansi(self._buffer)
            if consumed == 0:
                break
            self._buffer = self._buffer[consumed:]
            if event is not None:
                events.append(event)
        return events


def parse_ansi(data: bytes) -> tuple[Event | None, int]:
    """Décode un événement en tête de ``data`` ; ``(None, 0)`` si incomplet."""
    if not data:
        return None, 0
    if data[:1] == b"\x1b":
        if len(data) == 1:
            return None, 0
        if data[1:2] == b"[":
            end = 2
            while end < len(data) and not (0x40 <= data[end] <= 0x7E):
                end += 1
            if end >= len(data):
                return None, 0  # séquence incomplète : on attend la suite
            final = chr(data[end])
            body = data[2:end].decode("ascii", "replace")
            consumed = end + 1
            if final == "A":
                return Key("up"), consumed
            if final == "B":
                return Key("down"), consumed
            if final == "C":
                return Key("right"), consumed
            if final == "D":
                return Key("left"), consumed
            if body.startswith("<") and final in ("M", "m"):
                parts = body[1:].split(";")
                if len(parts) == 3:
                    try:
                        button, x, y = (int(part) for part in parts)
                    except ValueError:
                        return None, consumed
                    if button & 64:
                        return Scroll(steps=1 if (button & 1) == 0 else -1, x=x - 1, y=y - 1), consumed
                    if button & 32 or final == "m":
                        return None, consumed  # mouvement / relâchement
                    if (button & 3) == 0:
                        return Click(x=x - 1, y=y - 1), consumed
                return None, consumed
            return None, consumed
        if data[1:2] == b"O":
            if len(data) < 3:
                return None, 0
            final = data[2:3]
            consumed = 3
            return {
                b"A": Key("up"),
                b"B": Key("down"),
                b"C": Key("right"),
                b"D": Key("left"),
            }.get(final), consumed
        return Key("escape"), 1
    byte = data[0]
    if byte in (0x0D, 0x0A):
        return Key("enter"), 1
    if byte == 0x7F:
        return Key("backspace"), 1
    if byte == 0x09:
        return Key("tab"), 1
    if byte == 0x03:  # Ctrl+C : on laisse le signal faire son travail
        return None, 1
    if byte >= 0x80:
        # Caractère UTF-8 multi-octets : on attend d'avoir la séquence entière.
        for size in (2, 3, 4):
            if len(data) < size:
                return None, 0
            try:
                return Key(data[:size].decode("utf-8").lower()), size
            except UnicodeDecodeError:
                continue
        return None, 1
    return Key(chr(byte).lower()), 1


# --------------------------------------------------------------------------
# Fabrique
# --------------------------------------------------------------------------


def create_input() -> InputBackend | None:
    """Renvoie le backend adapté au terminal courant, ou ``None``."""
    if WindowsConsole.available():
        backend: InputBackend = WindowsConsole()
    elif PosixTerminal.available():
        backend = PosixTerminal()
    else:
        return None
    try:
        backend.open()  # type: ignore[attr-defined]
    except Exception:
        return None
    return backend

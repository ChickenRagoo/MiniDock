"""Автообновление через GitHub Releases.

Раз в несколько часов смотрим последний релиз репозитория, скачиваем установщик (MiniDock-Setup-x.y.z.exe),
сверяем контрольную сумму и ставим его в тихом режиме, когда компьютером не пользуются и не идёт игра.
Установщик сам закрывает MiniDock и запускает новую версию.
"""
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from ctypes import wintypes

from PySide6.QtCore import QObject, QTimer, Signal

from .version import REPO, __version__

API = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE = f"https://github.com/{REPO}/releases/latest"
CHECK_EVERY = 6 * 3600   # секунд между проверками
IDLE_TO_INSTALL = 180    # столько секунд без мыши и клавиатуры — можно обновляться
ASSET = re.compile(r"^MiniDock-Setup-[\d.]+\.exe$", re.I)
TRUSTED_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds():
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": f"MiniDock/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


class Updater(QObject):
    """state: idle | checking | latest | available | downloading | ready | installing | error."""
    changed = Signal()
    _done = Signal(str, object)  # из фонового потока: (новое состояние, данные)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.state = "idle"
        self.latest = None       # версия последнего релиза
        self.installer = None    # скачанный установщик
        self.error = ""
        self.busy = False
        self.frozen = bool(getattr(sys, "frozen", False))
        self._done.connect(self._on_done)
        self.timer = QTimer(self, interval=CHECK_EVERY * 1000, timeout=self.check)
        self.timer.start()
        self.idle_timer = QTimer(self, interval=60 * 1000, timeout=self.maybe_install)
        self.idle_timer.start()
        QTimer.singleShot(90 * 1000, self.check)  # первая проверка — когда всё уже запустилось

    @property
    def configured(self):
        return "/" in REPO and not REPO.startswith("OWNER/")

    def status_text(self):
        s = self.state
        if not self.configured:
            return "Репозиторий обновлений не задан."
        if s == "checking":
            return "Проверяем обновления…"
        if s == "latest":
            return f"Установлена последняя версия ({__version__})."
        if s == "available":
            return f"Есть версия {self.latest}. Запущено из исходников — обновите их (git pull)."
        if s == "downloading":
            return f"Скачиваем версию {self.latest}…"
        if s == "ready":
            return f"Версия {self.latest} скачана и установится, когда компьютер будет простаивать."
        if s == "installing":
            return f"Устанавливаем версию {self.latest}…"
        if s == "error":
            return f"Не удалось проверить обновления: {self.error}"
        return f"Версия {__version__}."

    # -- проверка и скачивание (в фоне)
    def check(self):
        if self.busy or not self.configured or self.state in ("ready", "installing"):
            return
        self.busy = True
        self.state = "checking"
        self.changed.emit()
        threading.Thread(target=self._check, daemon=True, name="MiniDockUpdate").start()

    def _check(self):
        try:
            with _get(API) as r:
                rel = json.load(r)
            latest = str(rel.get("tag_name") or "").lstrip("vV")
            if version_tuple(latest) <= version_tuple(__version__):
                self._done.emit("latest", latest)
                return
            if not self.frozen:
                self._done.emit("available", latest)
                return
            asset = next((a for a in rel.get("assets", []) if ASSET.match(a.get("name", ""))), None)
            if asset is None:
                self._done.emit("error", "в релизе нет установщика")
                return
            self._done.emit("downloading", latest)
            self._done.emit("ready", (latest, self._download(asset)))
        except Exception as e:
            self._done.emit("error", str(e)[:120])

    def _download(self, asset):
        url = asset["browser_download_url"]
        if urllib.parse.urlparse(url).hostname not in TRUSTED_HOSTS:
            raise RuntimeError("неожиданный адрес загрузки")
        folder = os.path.join(tempfile.gettempdir(), "MiniDockUpdate")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, asset["name"])
        h = hashlib.sha256()
        with _get(url, timeout=60) as r, open(path + ".part", "wb") as f:
            if urllib.parse.urlparse(r.geturl()).hostname not in TRUSTED_HOSTS:
                raise RuntimeError("неожиданный адрес загрузки")
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
        if asset.get("size") and os.path.getsize(path + ".part") != asset["size"]:
            raise RuntimeError("файл скачался не полностью")
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:") and digest[7:].lower() != h.hexdigest():
            raise RuntimeError("контрольная сумма не совпала")
        os.replace(path + ".part", path)
        return path

    def _on_done(self, state, data):
        if state == "ready":
            self.latest, self.installer = data
            self.busy = False
        elif state == "downloading":
            self.latest = data
        else:
            self.busy = False
            if state == "error":
                self.error = data
            else:
                self.latest = data
        self.state = state
        self.changed.emit()
        if state == "ready":
            self.maybe_install()

    # -- установка
    def maybe_install(self):
        """Готовое обновление ставим сами, когда за компьютером никого нет и не запущена игра."""
        if self.state != "ready" or not self.app.cfg.get("auto_update", True):
            return
        if self.app.fullscreen or idle_seconds() < IDLE_TO_INSTALL:
            return
        self.install()

    def install(self):
        if not self.installer or not os.path.isfile(self.installer):
            return
        self.state = "installing"
        self.changed.emit()
        # установщик закроет MiniDock (вернёт панель задач), обновит файлы и запустит новую версию
        subprocess.Popen([self.installer, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE"],
                         creationflags=subprocess.DETACHED_PROCESS)

"""Виджет MiniDock «VPN по программам»: сам подключает VPN, когда запущена программа, которой он нужен
(Discord, вкладка YouTube в браузере…), и отключает, когда она закрыта или запущена программа, которой
VPN мешает (банк, Госуслуги, маркетплейсы).

Установка: положите этот файл в %APPDATA%\\MiniDock\\widgets, перезапустите MiniDock и включите виджет:
Настройки → «Верхняя панель» → «Виджеты». Свои VPN и программы добавляются кнопкой «Настроить…» в окне
виджета (настройки хранятся рядом, в vpn_auto.json).

Какие VPN умеет:
    Windows VPN  — подключения из «Параметры → Сеть → VPN» (имя подключения как там)
    WireGuard    — файл туннеля .conf (WireGuard для Windows)
    AmneziaWG    — файл туннеля .conf (клиент AmneziaWG; такой файл можно выгрузить и из Amnezia VPN)
    OpenVPN      — файл .ovpn или имя конфигурации OpenVPN GUI
    Свои команды — любой другой клиент: команды подключения, отключения и проверки (код 0 — подключён)
WireGuard и AmneziaWG Windows подключает только с правами администратора — включите
Настройки → «Основные» → «Работать с правами администратора».
"""
import ctypes
import json
import os
import shutil
import subprocess
import threading
import time

import psutil
import win32gui
from PySide6.QtCore import QEasingCurve, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QFrame, QGraphicsOpacityEffect,
                               QGridLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QPushButton, QScrollArea,
                               QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from minidock.barwidgets import WIDGETS_DIR, BarWidget
from minidock.settings import SmoothScrollArea, TrafficLight
from minidock.ui import animate, fade_in, rounded_path
from minidock.winapi import enable_blur, exe_description, extract_icon, round_window

CFG_PATH = WIDGETS_DIR / "vpn_auto.json"
NO_WINDOW = 0x08000000
KINDS = [("windows", "Windows VPN"), ("wireguard", "WireGuard"), ("amneziawg", "AmneziaWG"),
         ("openvpn", "OpenVPN"), ("command", "Свои команды")]
KIND_NAMES = dict(KINDS)
# что обычно не открывается без VPN или, наоборот, с ним — подсказка в настройках
DEFAULT_NO_VPN = ["окно: Госуслуги", "окно: Сбербанк", "окно: Т-Банк", "окно: Wildberries", "окно: Ozon"]


def load_cfg():
    try:
        cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("auto", True)
    cfg.setdefault("vpns", [])
    cfg.setdefault("no_vpn", list(DEFAULT_NO_VPN))
    cfg.setdefault("delay", 15)
    return cfg


def save_cfg(cfg):
    try:
        CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run(args, timeout=25):
    """(код, вывод) команды без окна консоли."""
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NO_WINDOW,
                           shell=isinstance(args, str))
        out = (r.stdout + r.stderr).decode("oem", errors="replace")
        return r.returncode, out.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def find_exe(*candidates):
    for c in candidates:
        path = os.path.expandvars(c)
        if os.path.isfile(path):
            return path
        found = shutil.which(c)
        if found:
            return found
    return None


# -- как подключать каждый вид VPN ---------------------------------------------------------------
# каждая функция возвращает текст ошибки или "" при успехе

def _tunnel(vpn):
    """Имя туннеля WireGuard/AmneziaWG — имя файла .conf без расширения."""
    return os.path.splitext(os.path.basename(vpn.get("target", "")))[0]


def _wg_exe(kind):
    if kind == "amneziawg":
        return find_exe(r"%ProgramFiles%\AmneziaWG\amneziawg.exe", "amneziawg.exe")
    return find_exe(r"%ProgramFiles%\WireGuard\wireguard.exe", "wireguard.exe")


def _wg_service(vpn):
    prefix = "AmneziaWGTunnel$" if vpn["kind"] == "amneziawg" else "WireGuardTunnel$"
    return prefix + _tunnel(vpn)


def _ovpn_name(vpn):
    return os.path.splitext(os.path.basename(vpn.get("target", "")))[0]


def is_connected(vpn):
    kind, target = vpn["kind"], vpn.get("target", "")
    if kind == "windows":
        code, out = run(["rasdial"], timeout=10)
        return code == 0 and target.lower() in out.lower()
    if kind in ("wireguard", "amneziawg"):
        try:
            return psutil.win_service_get(_wg_service(vpn)).status() == "running"
        except Exception:  # службы нет — туннель не запущен
            return False
    if kind == "openvpn":
        name = _ovpn_name(vpn).lower()
        for p in psutil.process_iter(["name", "cmdline"]):
            if (p.info["name"] or "").lower() == "openvpn.exe":
                cmd = " ".join(p.info["cmdline"] or []).lower()
                if not cmd or name in cmd:  # командную строку службы иногда не прочитать — считаем, что наш
                    return True
        return False
    if kind == "command":
        cmd = vpn.get("check", "").strip()
        return bool(cmd) and run(cmd, timeout=10)[0] == 0
    return False


def connect(vpn):
    kind, target = vpn["kind"], vpn.get("target", "")
    if kind == "windows":
        code, out = run(["rasdial", target])
        return "" if code == 0 else out.splitlines()[0] if out else f"код {code}"  # первая строка — суть ошибки
    if kind in ("wireguard", "amneziawg"):
        exe = _wg_exe(kind)
        if not exe:
            return f"не установлен {KIND_NAMES[kind]}"
        if not is_admin():
            return "нужен режим администратора MiniDock (Настройки → Основные)"
        if not os.path.isfile(target):
            return "не найден файл туннеля .conf"
        code, out = run([exe, "/installtunnelservice", target])
        return "" if code == 0 else out or f"код {code}"
    if kind == "openvpn":
        exe = find_exe(r"%ProgramFiles%\OpenVPN\bin\openvpn-gui.exe", "openvpn-gui.exe")
        if not exe:
            return "не установлен OpenVPN GUI"
        gui_running = any((p.info["name"] or "").lower() == "openvpn-gui.exe" for p in psutil.process_iter(["name"]))
        args = [exe, "--command", "connect", _ovpn_name(vpn)] if gui_running else [exe, "--connect", os.path.basename(target)]
        if os.path.isfile(target) and not gui_running:
            args = [exe, "--config_dir", os.path.dirname(target), "--connect", os.path.basename(target)]
        subprocess.Popen(args, creationflags=NO_WINDOW)  # GUI остаётся работать — не ждём его
        return ""
    if kind == "command":
        cmd = vpn.get("on", "").strip()
        if not cmd:
            return "не задана команда подключения"
        code, out = run(cmd)
        return "" if code == 0 else out or f"код {code}"
    return "неизвестный вид VPN"


def disconnect(vpn):
    kind, target = vpn["kind"], vpn.get("target", "")
    if kind == "windows":
        code, out = run(["rasdial", target, "/disconnect"])
        return "" if code == 0 else out or f"код {code}"
    if kind in ("wireguard", "amneziawg"):
        exe = _wg_exe(kind)
        if not exe:
            return f"не установлен {KIND_NAMES[kind]}"
        if not is_admin():
            return "нужен режим администратора MiniDock (Настройки → Основные)"
        code, out = run([exe, "/uninstalltunnelservice", _tunnel(vpn)])
        return "" if code == 0 else out or f"код {code}"
    if kind == "openvpn":
        exe = find_exe(r"%ProgramFiles%\OpenVPN\bin\openvpn-gui.exe", "openvpn-gui.exe")
        if not exe:
            return "не установлен OpenVPN GUI"
        code, out = run([exe, "--command", "disconnect", _ovpn_name(vpn)])
        return "" if code == 0 else out or f"код {code}"
    if kind == "command":
        cmd = vpn.get("off", "").strip()
        if not cmd:
            return "не задана команда отключения"
        code, out = run(cmd)
        return "" if code == 0 else out or f"код {code}"
    return "неизвестный вид VPN"


# -- какие программы сейчас открыты ---------------------------------------------------------------

def running_now():
    """(имена запущенных .exe в нижнем регистре, заголовки видимых окон в нижнем регистре)."""
    names = {(p.info["name"] or "").lower() for p in psutil.process_iter(["name"])}
    titles = []

    def add(h, _):
        if win32gui.IsWindowVisible(h):
            t = win32gui.GetWindowText(h)
            if t:
                titles.append(t.lower())
    try:
        win32gui.EnumWindows(add, None)
    except Exception:
        pass
    return names, titles


def matches(rule, names, titles):
    """Правило — имя программы (discord или discord.exe) или «окно: слово» (есть в заголовке окна,
    например открытая вкладка браузера)."""
    rule = rule.strip().lower()
    if not rule:
        return False
    if rule.startswith("окно:"):
        word = rule[5:].strip()
        return bool(word) and any(word in t for t in titles)
    if not rule.endswith(".exe"):
        rule += ".exe"
    return rule in names


def first_match(rules, names, titles):
    return next((r for r in rules if matches(r, names, titles)), None)


# -- виджет ---------------------------------------------------------------------------------------

class VpnAuto(BarWidget):
    id = "vpn_auto"
    name = "VPN по программам"
    icon = "\ue705"  # замок
    interval = 4.0

    def __init__(self):
        self.lock = threading.Lock()
        self.ours = None          # имя VPN, который подключили мы (его и отключаем, когда программы закрыли)
        self.suspended = None     # VPN, который отключили ради программы «без VPN», — вернуть потом
        self.idle_since = None    # с какого момента подключённый нами VPN никому не нужен
        self.hold = None          # после ручного переключения не трогаем, пока набор программ тот же
        self.last_error = ""
        self.last_key = None
        self.busy = False

    # фоновый поток MiniDock — здесь можно ждать команды
    def update(self, detailed):
        cfg = load_cfg()
        names, titles = running_now()
        states = {v["name"]: is_connected(v) for v in cfg["vpns"]}
        blocker = first_match(cfg["no_vpn"], names, titles)
        want, reason = None, None
        for v in cfg["vpns"]:  # первый по списку VPN, чья программа открыта
            hit = first_match(v.get("apps", []), names, titles)
            if hit:
                want, reason = v, hit
                break
        key = (blocker, want and want["name"])
        if self.hold is not None and key != self.hold:
            self.hold = None  # набор программ поменялся — снова решаем сами
        self.last_key = key
        if cfg["auto"] and self.hold is None and not self.busy:
            # подключение идёт секунды — в своём потоке, чтобы не задерживать остальные виджеты панели
            self.busy = True
            threading.Thread(target=self.apply_bg, args=(cfg, states, blocker, want), daemon=True).start()
        on = next((n for n, s in states.items() if s), None)
        return dict(cfg=cfg, states=states, on=on, blocker=blocker, want=want and want["name"], reason=reason,
                    error=self.last_error, hold=self.hold is not None, ours=self.ours)

    def apply_bg(self, *args):
        try:
            with self.lock:
                self.apply(*args)
        finally:
            self.busy = False

    def apply(self, cfg, states, blocker, want):
        by_name = {v["name"]: v for v in cfg["vpns"]}
        if blocker:  # программе VPN мешает — отключить любой из наших, запомнить, чтобы вернуть
            for n, on in states.items():
                if on:
                    self.do(disconnect, by_name[n])
                    if self.suspended is None:
                        self.suspended = n
            self.ours, self.idle_since = None, None
            return
        if self.suspended and not want:  # программа «без VPN» закрыта — вернуть VPN, который был включён
            n, self.suspended = self.suspended, None
            if n in by_name and not states.get(n):
                self.do(connect, by_name[n])
                return
        self.suspended = None
        if want:
            self.idle_since = None
            for n, on in states.items():  # одновременно — только один VPN
                if on and n != want["name"] and n == self.ours:
                    self.do(disconnect, by_name[n])
            if not states.get(want["name"]):
                if not self.do(connect, want):
                    self.ours = want["name"]
            return
        if self.ours and states.get(self.ours):  # программы закрыли — отключить через паузу (вдруг перезапуск)
            if self.idle_since is None:
                self.idle_since = time.monotonic()
            elif time.monotonic() - self.idle_since >= cfg["delay"]:
                self.do(disconnect, by_name[self.ours])
                self.ours, self.idle_since = None, None
        elif self.ours and not states.get(self.ours):
            self.ours, self.idle_since = None, None  # отключили не мы — больше не наш

    def do(self, fn, vpn):
        err = fn(vpn)
        self.last_error = f"{vpn['name']}: {err}" if err else ""
        return err

    def manual(self, name, turn_on):
        """Кнопка в окне виджета: вручную. Автоматика ждёт, пока не поменяется набор открытых программ."""
        def work():
            cfg = load_cfg()
            vpn = next((v for v in cfg["vpns"] if v["name"] == name), None)
            if vpn is None:
                return
            with self.lock:
                self.hold = self.last_key
                if turn_on:
                    for v in cfg["vpns"]:
                        if v is not vpn and is_connected(v):
                            self.do(disconnect, v)
                    self.do(connect, vpn)
                    self.ours = None  # включили руками — сами не выключаем
                else:
                    self.do(disconnect, vpn)
                    self.ours = None
                self.suspended = None
        threading.Thread(target=work, daemon=True).start()

    def toggle_auto(self):
        cfg = load_cfg()
        cfg["auto"] = not cfg["auto"]
        save_cfg(cfg)
        self.hold = None

    def configure(self):
        dlg = VpnDialog(load_cfg())
        if dlg.exec() == QDialog.Accepted:
            save_cfg(dlg.result_cfg())
            self.hold = None

    def text(self, d):
        if d["on"]:
            return d["on"]
        return "VPN выкл" if d["cfg"]["vpns"] else "VPN"

    def panel(self, d):
        cfg = d["cfg"]
        rows = [("title", "VPN по программам")]
        if not cfg["vpns"]:
            rows += [("text", "Добавьте свои VPN и программы, которым они нужны, — дальше виджет сам будет "
                              "подключать и отключать VPN."),
                     ("button", "Настроить…", self.configure)]
            return rows
        for v in cfg["vpns"]:
            on = d["states"].get(v["name"])
            rows.append(("value", f"{v['name']} ({KIND_NAMES.get(v['kind'], v['kind'])})",
                         "подключён" if on else "выключен"))
        rows.append(("sep",))
        if not cfg["auto"]:
            rows.append(("text", "Автоматика выключена — VPN переключаются только вручную."))
        elif d["blocker"]:
            rows.append(("text", f"Открыто «{d['blocker']}» — ему VPN мешает, поэтому VPN отключён."))
        elif d["want"]:
            rows.append(("text", f"Открыто «{d['reason']}» — для него нужен {d['want']}."))
        else:
            rows.append(("text", "Программ из списков не открыто."))
        if d["hold"]:
            rows.append(("text", "Вы переключили VPN вручную — автоматика подождёт, пока не откроете или "
                                 "не закроете программу из списков."))
        if d["error"]:
            rows.append(("text", f"Не получилось: {d['error']}"))
        rows.append(("sep",))
        for v in cfg["vpns"]:
            on = d["states"].get(v["name"])
            rows.append(("button", f"{'Отключить' if on else 'Подключить'} {v['name']}",
                         lambda n=v["name"], s=not on: self.manual(n, s)))
        rows += [("button", "Выключить автоматику" if cfg["auto"] else "Включить автоматику", self.toggle_auto),
                 ("button", "Настроить…", self.configure)]
        return rows


# -- окно настроек --------------------------------------------------------------------------------

ACCENT = QColor("#0a84ff")
TEXT = QColor(242, 242, 245)
DIM = QColor(255, 255, 255, 135)
KIND_STYLE = {  # подпись на значке и цвет вида VPN
    "windows": ("Win", "#0a84ff", "Встроенный в Windows"),
    "wireguard": ("WG", "#c4302b", "Файл туннеля .conf"),
    "amneziawg": ("AWG", "#8e5cf7", "Файл .conf из Amnezia"),
    "openvpn": ("OVPN", "#f08c00", "Файл .ovpn"),
    "command": (">_", "#5d6270", "Любой клиент"),
}
CLIENT_URL = {
    "wireguard": "https://www.wireguard.com/install/",
    "amneziawg": "https://github.com/amnezia-vpn/amneziawg-windows-client/releases",
    "openvpn": "https://openvpn.net/community-downloads/",
}
# сайты для быстрых подсказок: слово из заголовка окна -> (название, домен для значка, цвет запасного значка)
SITES = {
    "youtube": ("YouTube", "youtube.com", "#ff0033"),
    "discord": ("Discord", "discord.com", "#5865f2"),
    "instagram": ("Instagram", "instagram.com", "#e1306c"),
    "chatgpt": ("ChatGPT", "chatgpt.com", "#10a37f"),
    "claude": ("Claude", "claude.ai", "#d97757"),
    "x.com": ("X", "x.com", "#000000"),
    "facebook": ("Facebook", "facebook.com", "#1877f2"),
    "twitch": ("Twitch", "twitch.tv", "#9146ff"),
    "госуслуги": ("Госуслуги", "gosuslugi.ru", "#0d4cd3"),
    "сбербанк": ("Сбербанк", "sberbank.ru", "#21a038"),
    "т-банк": ("Т-Банк", "tbank.ru", "#ffdd2d"),
    "wildberries": ("Wildberries", "wildberries.ru", "#a73afd"),
    "ozon": ("Ozon", "ozon.ru", "#005bff"),
    "яндекс": ("Яндекс", "ya.ru", "#fc3f1d"),
    "кинопоиск": ("Кинопоиск", "kinopoisk.ru", "#ff6600"),
}
SUGGEST_VPN = ["discord.exe", "окно: YouTube", "окно: Instagram", "окно: ChatGPT", "окно: Claude", "окно: X.com",
               "окно: Twitch"]
SUGGEST_NO_VPN = ["окно: Госуслуги", "окно: Сбербанк", "окно: Т-Банк", "окно: Wildberries", "окно: Ozon",
                  "окно: Яндекс", "окно: Кинопоиск"]
ICON_DIR = WIDGETS_DIR / "vpn_icons"


def font(pt, bold=False):
    f = QFont("Segoe UI", pt)
    f.setBold(bold)
    return f


def dark_title(w):
    try:  # тёмная полоса заголовка, как у остального MiniDock
        ctypes.windll.dwmapi.DwmSetWindowAttribute(int(w.winId()), 20, ctypes.byref(ctypes.c_int(1)), 4)
    except Exception:
        pass


def site_of(rule):
    r = rule.strip().lower()
    if not r.startswith("окно:"):
        return None
    word = r[5:].strip()
    return SITES.get(word) or next((v for k, v in SITES.items() if k in word or word in k), None)


def letter_icon(text, color, size):
    """Запасной значок: буква на цветной плашке."""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    c = QColor(color)
    g = QLinearGradient(0, 0, 0, size)
    g.setColorAt(0, c.lighter(125))
    g.setColorAt(1, c)
    p.setBrush(g)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.28, size * 0.28)
    p.setPen(QColor(20, 20, 20) if c.lightness() > 170 else QColor(255, 255, 255))
    text = text[:4]
    p.setFont(font(max(5, int(size * (0.32 if len(text) <= 3 else 0.24))), True))
    p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, text)
    p.end()
    return pix


class Icons(QObject):
    """Значки правил: у программ — из её .exe, у сайтов — значок сайта (скачивается один раз и хранится
    рядом с виджетом), пока его нет — буква на цветной плашке."""
    ready = Signal()

    def __init__(self, paths):
        super().__init__()
        self.paths = {k.lower(): v for k, v in paths.items() if v and os.path.isfile(v)}
        self.cache = {}
        self.loading = set()
        for p in psutil.process_iter(["name", "exe"]):  # пути запущенных программ — для их значков
            try:
                if p.info["exe"]:
                    self.paths.setdefault(p.info["name"].lower(), p.info["exe"])
            except Exception:
                pass

    def learn(self, rule, path):
        if path:
            self.paths[exe_rule(rule).lower()] = path

    def path(self, rule):
        return self.paths.get(exe_rule(rule).lower())

    def label(self, rule):
        site = site_of(rule)
        if rule.strip().lower().startswith("окно:"):
            return site[0] if site else rule.split(":", 1)[1].strip()
        path = self.path(rule)
        desc = exe_description(path) if path else None
        name = exe_rule(rule)[:-4]
        return desc if desc and len(desc) <= 24 else name[:1].upper() + name[1:]

    def icon(self, rule, size):
        key = (rule.lower(), size)
        if key in self.cache:
            return self.cache[key]
        pix = None
        site = site_of(rule)
        if rule.strip().lower().startswith("окно:"):
            if site:
                pix = self.site_icon(site[1], size)
            if pix is None:
                pix = letter_icon(self.label(rule), site[2] if site else "#3a3f4b", size)
                if site:  # пока качается — буква; кэш не заполняем, чтобы потом подставить значок
                    return pix
        else:
            path = self.path(rule)
            raw = extract_icon(path, 0, 64) if path else None
            if raw is not None:
                pix = raw.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            else:
                pix = letter_icon(self.label(rule), "#3a3f4b", size)
        self.cache[key] = pix
        return pix

    def site_icon(self, domain, size):
        path = ICON_DIR / (domain + ".png")
        if path.is_file():
            img = QImage(str(path))
            if not img.isNull():
                return QPixmap.fromImage(img).scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            return None
        if domain not in self.loading:
            self.loading.add(domain)
            threading.Thread(target=self.fetch, args=(domain, path), daemon=True).start()
        return None

    def fetch(self, domain, path):
        import urllib.request
        try:  # сервис значков DuckDuckGo: отдаёт значок сайта, не зная, кто спрашивает
            req = urllib.request.Request(f"https://icons.duckduckgo.com/ip3/{domain}.ico",
                                         headers={"User-Agent": "MiniDock"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = r.read(512 * 1024)
            img = QImage.fromData(data)
            if img.isNull():
                return
            ICON_DIR.mkdir(parents=True, exist_ok=True)
            img.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation).save(str(path), "PNG")
            self.cache.clear()
            self.ready.emit()  # сигнал придёт в основной поток — чипсы перерисуются со значком
        except Exception:
            pass


def exe_rule(rule):
    r = rule.strip()
    return r if r.lower().endswith(".exe") else r + ".exe"


def windows_vpns():
    """Имена VPN-подключений, созданных в параметрах Windows (их телефонные книги)."""
    names = []
    for base in (os.environ.get("APPDATA", ""), os.environ.get("PROGRAMDATA", "")):
        pbk = os.path.join(base, "Microsoft", "Network", "Connections", "Pbk", "rasphone.pbk")
        try:
            with open(pbk, encoding="utf-8-sig", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("[") and line.endswith("]") and line[1:-1] not in names:
                        names.append(line[1:-1])
        except OSError:
            pass
    return names


def open_programs():
    """[(имя .exe, путь)] запущенных программ с окнами — для окна выбора."""
    import win32process
    pids = set()

    def add(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h):
            try:
                pids.add(win32process.GetWindowThreadProcessId(h)[1])
            except Exception:
                pass
    try:
        win32gui.EnumWindows(add, None)
    except Exception:
        pass
    out = {}
    for pid in pids:
        try:
            p = psutil.Process(pid)
            out.setdefault(p.name().lower(), (p.name(), p.exe()))
        except psutil.Error:
            pass
    skip = {"explorer.exe", "minidock.exe", "python.exe", "pythonw.exe", "textinputhost.exe",
            "applicationframehost.exe", "systemsettings.exe", "shellexperiencehost.exe", "searchhost.exe",
            "startmenuexperiencehost.exe", "lockapp.exe", "nvidia overlay.exe"}
    return sorted((v for k, v in out.items() if k not in skip), key=lambda v: v[0].lower())


class Anim(QWidget):
    """Виджет с одной анимируемой величиной self.t (0..1) — плавные наведения и выделения."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.t_hover = 0.0
        self.hovered = False
        self._tw = None
        self.setAttribute(Qt.WA_Hover)

    def fade_hover(self, on):
        self.hovered = on
        start = self.t_hover
        end = 1.0 if on else 0.0

        def step(v):
            self.t_hover = start + (end - start) * v
            self.update()
        self._tw = animate(self, 140, step)

    def enterEvent(self, e):
        self.fade_hover(True)

    def leaveEvent(self, e):
        self.fade_hover(False)


class FlowLayout(QLayout):
    """Элементы в строку с переносом — для «чипсов» программ."""

    def __init__(self, parent=None, spacing=8):
        super().__init__(parent)
        self.items = []
        self.gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self.items.append(item)

    def count(self):
        return len(self.items)

    def itemAt(self, i):
        return self.items[i] if 0 <= i < len(self.items) else None

    def takeAt(self, i):
        return self.items.pop(i) if 0 <= i < len(self.items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self.arrange(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self.arrange(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self.items:
            s = s.expandedTo(it.minimumSize())
        return s

    def arrange(self, rect, test):
        x, y, line = rect.x(), rect.y(), 0
        for it in self.items:
            hint = it.sizeHint()
            if x + hint.width() > rect.right() + 1 and line > 0:
                x, y, line = rect.x(), y + line + self.gap, 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self.gap
            line = max(line, hint.height())
        return y + line - rect.y()


class Chip(Anim):
    """Программа или сайт в списке: значок, название, крестик. Появляется «пружинкой», исчезает сжимаясь."""
    removed = Signal(str)
    clicked = Signal(str)
    H = 34

    def __init__(self, rule, icons, ghost=False):
        super().__init__()
        self.rule, self.icons, self.ghost = rule, icons, ghost
        self.text = ("+ " if ghost else "") + icons.label(rule)
        self.appear = 0.0
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(rule)
        w = QFontMetricsF(font(9)).horizontalAdvance(self.text)
        self.setFixedSize(int(w + (40 if ghost else 62)), self.H)
        self._in = animate(self, 380 if not ghost else 220, self.set_appear, curve=QEasingCurve.OutBack)

    def set_appear(self, v):
        self.appear = v
        self.update()

    def close_x(self):
        return QRectF(self.width() - 26, (self.H - 18) / 2, 18, 18)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self.ghost:
            self.clicked.emit(self.rule)
        elif self.close_x().adjusted(-6, -6, 6, 6).contains(e.position()):
            self.vanish()

    def vanish(self):
        self.setEnabled(False)
        self._in = animate(self, 170, lambda v: self.set_appear(1 - v), lambda: self.removed.emit(self.rule),
                           curve=QEasingCurve.InCubic)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        a = max(0.0, self.appear)
        p.setOpacity(min(1.0, a))
        c = QPointF(self.width() / 2, self.height() / 2)
        p.translate(c)
        p.scale(0.6 + 0.4 * a, 0.6 + 0.4 * a)
        p.translate(-c)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        h = self.t_hover
        if self.ghost:
            pen = QPen(QColor(255, 255, 255, int(60 + 60 * h)), 1.2, Qt.DashLine)
            p.setPen(pen)
            p.setBrush(QColor(255, 255, 255, int(10 * h)))
        else:
            p.setPen(QPen(QColor(255, 255, 255, int(18 + 20 * h)), 1))
            p.setBrush(QColor(255, 255, 255, int(16 + 14 * h)))
        p.drawRoundedRect(r, self.H / 2, self.H / 2)
        size = 20
        p.setOpacity(min(1.0, a) * (0.55 if self.ghost else 1.0))
        p.drawPixmap(QRectF(8, (self.H - size) / 2, size, size).toRect(), self.icons.icon(self.rule, 40))
        p.setOpacity(min(1.0, a))
        p.setPen(DIM if self.ghost else TEXT)
        p.setFont(font(9))
        p.drawText(QRectF(34, 0, self.width() - 34, self.H), Qt.AlignVCenter, self.text)
        if not self.ghost:  # крестик: ярче при наведении
            x = self.close_x()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, int(20 + 40 * h)))
            p.drawEllipse(x)
            p.setPen(QPen(QColor(255, 255, 255, int(120 + 135 * h)), 1.4))
            d = 4
            cx, cy = x.center().x(), x.center().y()
            p.drawLine(QPointF(cx - d, cy - d), QPointF(cx + d, cy + d))
            p.drawLine(QPointF(cx - d, cy + d), QPointF(cx + d, cy - d))


class AddChip(Anim):
    """Кнопка «Добавить» в конце списка программ."""
    clicked = Signal()

    def __init__(self, text="Добавить"):
        super().__init__()
        self.text = text
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(int(QFontMetricsF(font(9, True)).horizontalAdvance(text) + 46), Chip.H)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(ACCENT)
        c.setAlpha(int(40 + 50 * self.t_hover))
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), Chip.H / 2, Chip.H / 2)
        p.setPen(QPen(QColor(150, 200, 255), 1.8))
        cx, cy = 18, self.height() / 2
        p.drawLine(QPointF(cx - 5, cy), QPointF(cx + 5, cy))
        p.drawLine(QPointF(cx, cy - 5), QPointF(cx, cy + 5))
        p.setPen(QColor(170, 210, 255))
        p.setFont(font(9, True))
        p.drawText(QRectF(32, 0, self.width() - 32, self.height()), Qt.AlignVCenter, self.text)


class ChipBox(QWidget):
    """Список правил «чипсами» + кнопка добавления + подсказки популярного."""
    changed = Signal()

    def __init__(self, icons, suggest, empty_text):
        super().__init__()
        self.icons, self.suggest, self.empty_text = icons, suggest, empty_text
        self.rules = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.box = QWidget()
        self.flow = FlowLayout(self.box)
        lay.addWidget(self.box)
        self.hint_row = QWidget()
        hl = QVBoxLayout(self.hint_row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(6)
        cap = QLabel("Популярное — нажмите, чтобы добавить:")
        cap.setObjectName("dim")
        hl.addWidget(cap)
        self.hint_box = QWidget()
        self.hint_flow = FlowLayout(self.hint_box, 6)
        hl.addWidget(self.hint_box)
        lay.addWidget(self.hint_row)
        icons.ready.connect(self.update_all)

    def update_all(self):
        for w in self.findChildren(Chip):
            w.update()

    def set_items(self, rules):
        self.rules = list(rules)
        self.rebuild()

    def items(self):
        return list(self.rules)

    def clear_flow(self, flow):
        while flow.count():
            w = flow.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()

    def rebuild(self, fresh=None):
        self.clear_flow(self.flow)
        for r in self.rules:
            chip = Chip(r, self.icons)
            if fresh is None or r != fresh:
                chip._in.stop()
                chip.appear = 1.0
            chip.removed.connect(self.remove)
            self.flow.addWidget(chip)
        add = AddChip("Добавить программу или сайт" if not self.rules else "Добавить")
        add.clicked.connect(self.pick)
        self.flow.addWidget(add)
        self.clear_flow(self.hint_flow)
        have = {r.lower() for r in self.rules}
        hints = [s for s in self.suggest if s.lower() not in have]
        for s in hints:
            g = Chip(s, self.icons, ghost=True)
            g.clicked.connect(self.add)
            self.hint_flow.addWidget(g)
        self.hint_row.setVisible(bool(hints))
        self.box.updateGeometry()
        self.updateGeometry()

    def add(self, rule, path=None):
        self.icons.learn(rule, path)
        if rule.lower() in (r.lower() for r in self.rules):
            return
        self.rules.append(rule)
        self.rebuild(fresh=rule)
        self.changed.emit()

    def remove(self, rule):
        self.rules = [r for r in self.rules if r != rule]
        self.rebuild()
        self.changed.emit()

    def pick(self):
        picker = AppPicker(self.icons, self.window())
        picker.picked.connect(self.add)
        picker.popup_at(self.mapToGlobal(QPoint(0, 0)))


class AppTile(Anim):
    """Плитка программы в окне выбора: большой значок и название."""
    clicked = Signal()

    def __init__(self, title, pix, sub=""):
        super().__init__()
        self.title, self.pix, self.sub = title, pix, sub
        self.appear = 0.0
        self.setFixedSize(104, 92)
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setOpacity(self.appear)
        p.translate(0, 8 * (1 - self.appear))
        h = self.t_hover
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, int(28 * h)))
        p.drawRoundedRect(QRectF(2, 2, self.width() - 4, self.height() - 4), 12, 12)
        s = 40 + 4 * h  # значок чуть увеличивается при наведении, как в доке
        p.drawPixmap(QRectF((self.width() - s) / 2, 12 - 2 * h, s, s).toRect(), self.pix)
        p.setPen(TEXT)
        p.setFont(font(8))
        fm = QFontMetricsF(font(8))
        p.drawText(QRectF(4, 60, self.width() - 8, 26), Qt.AlignHCenter | Qt.AlignTop,
                   fm.elidedText(self.title, Qt.ElideRight, self.width() - 10))


class AppPicker(QDialog):
    """Окно выбора: открытые сейчас программы плитками, поиск, сайт по слову из заголовка, файл .exe."""
    picked = Signal(str, object)

    def __init__(self, icons, parent):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setObjectName("picker")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.icons = icons
        self.setFixedWidth(500)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        h = QLabel("Что добавить?")
        h.setObjectName("h")
        lay.addWidget(h)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск среди открытых программ…")
        self.search.textChanged.connect(self.filter)
        lay.addWidget(self.search)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)
        scroll = QScrollArea()
        scroll.setWidget(self.grid_host)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        rows = max(1, (len(open_programs()) + 3) // 4)
        scroll.setFixedHeight(min(300, rows * 96 + 4))
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lay.addWidget(scroll)
        self.tiles = []
        for name, path in open_programs():
            icons.learn(name, path)
            tile = AppTile(icons.label(name), icons.icon(name, 64))
            tile.clicked.connect(lambda n=name, pa=path: self.choose(n, pa))
            self.tiles.append((name.lower() + " " + tile.title.lower(), tile))
        self.filter("")
        for i, (_, tile) in enumerate(self.tiles):  # плитки появляются по очереди
            QTimer.singleShot(18 * i, tile, lambda t=tile: setattr(t, "_a", animate(t, 260, lambda v, t=t: (
                setattr(t, "appear", v), t.update()))))  # окно закрыли раньше — таймер умрёт вместе с плиткой
        sep = QLabel("Сайт в браузере или любое окно")
        sep.setObjectName("sub")
        lay.addWidget(sep)
        row = QHBoxLayout()
        self.word = QLineEdit()
        self.word.setPlaceholderText("слово из заголовка окна, например YouTube")
        self.word.returnPressed.connect(self.add_word)
        add = QPushButton("Добавить")
        add.setObjectName("ok")
        add.clicked.connect(self.add_word)
        row.addWidget(self.word, 1)
        row.addWidget(add)
        lay.addLayout(row)
        note = QLabel("Сработает, когда слово есть в заголовке окна — у браузера это открытая вкладка.")
        note.setObjectName("dim")
        note.setWordWrap(True)
        lay.addWidget(note)
        browse = QPushButton("Выбрать программу на диске…")
        browse.clicked.connect(self.browse)
        lay.addWidget(browse)

    def filter(self, text):
        text = text.strip().lower()
        while self.grid.count():
            self.grid.takeAt(0)
        shown = [t for key, t in self.tiles if text in key]
        for _, t in self.tiles:
            t.setVisible(t in shown)
        for i, t in enumerate(shown):
            self.grid.addWidget(t, i // 4, i % 4)
        self.grid.setRowStretch(len(shown) // 4 + 1, 1)

    def choose(self, rule, path=None):
        self.picked.emit(rule, path)
        self.close()

    def add_word(self):
        w = self.word.text().strip()
        if w:
            self.choose("окно: " + w)

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Программа", os.environ.get("ProgramFiles", ""),
                                              "Программы (*.exe)")
        if path:
            self.choose(os.path.basename(path), os.path.normpath(path))

    def popup_at(self, pos):
        self.setStyleSheet(QSS)
        self.adjustSize()
        scr = QApplication.screenAt(pos) or QApplication.primaryScreen()
        g = scr.availableGeometry()
        x = min(max(g.left() + 8, pos.x()), g.right() - self.width() - 8)
        y = min(max(g.top() + 8, pos.y() - 40), g.bottom() - self.height() - 8)
        self.move(x, y)
        self.show()
        try:
            enable_blur(int(self.winId()))
            round_window(self, 16)
        except Exception:
            pass
        fade_in(self, slide=10)
        self.search.setFocus()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.setBrush(QColor(38, 38, 45, 228))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 16, 16)


class Badge(QWidget):
    """Значок вида VPN: цветная плашка с короткой подписью."""

    def __init__(self, kind, size=36):
        super().__init__()
        self.kind = kind
        self.setFixedSize(size, size)

    def set_kind(self, kind):
        self.kind = kind
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.drawPixmap(0, 0, letter_icon(KIND_STYLE[self.kind][0], KIND_STYLE[self.kind][1], self.width()))


class VpnCard(Anim):
    """Карточка VPN в списке слева: номер (приоритет), значок вида, название, сколько программ, статус."""
    chosen = Signal(int)
    moved = Signal(int, int)

    def __init__(self, idx, vpn, special=False):
        super().__init__()
        self.idx, self.vpn, self.special = idx, vpn, special
        self.sel = 0.0
        self.selected = False
        self.online = False
        self.setFixedHeight(62)
        self.setCursor(Qt.PointingHandCursor)

    def set_selected(self, on):
        if on == self.selected:
            return
        self.selected = on
        start = self.sel

        def step(v):
            self.sel = start + ((1.0 if on else 0.0) - start) * v
            self.update()
        self._sel = animate(self, 200, step)

    def arrows(self):
        x = self.width() - 26
        return QRectF(x, 8, 20, 20), QRectF(x, self.height() - 28, 20, 20)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        up, down = self.arrows()
        if self.special:
            self.chosen.emit(self.idx)
        elif self.hovered and up.contains(e.position()):
            self.moved.emit(self.idx, -1)
        elif self.hovered and down.contains(e.position()):
            self.moved.emit(self.idx, 1)
        else:
            self.chosen.emit(self.idx)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        bg = QColor(ACCENT)
        bg.setAlpha(int(70 * self.sel))
        p.setPen(QPen(QColor(10, 132, 255, int(200 * self.sel)), 1.2) if self.sel > 0.01 else Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, int(12 * self.t_hover)) if self.sel < 0.01 else bg)
        p.drawRoundedRect(r, 12, 12)
        kind = self.vpn.get("kind", "windows")
        n = len(self.vpn.get("apps", []))
        if self.special:  # «Без VPN»: красный значок, без номера и стрелок
            p.drawPixmap(12, 13, letter_icon("Без", "#ff453a", 36))
            p.setPen(TEXT)
            p.setFont(font(10, True))
            p.drawText(QRectF(58, 10, self.width() - 70, 22), Qt.AlignVCenter, "Без VPN")
            p.setPen(DIM)
            p.setFont(font(8))
            p.drawText(QRectF(58, 32, self.width() - 70, 20), Qt.AlignVCenter,
                       f"Банки, Госуслуги · {n} {plural(n, 'правило', 'правила', 'правил')}" if n else "Банки, Госуслуги")
            return
        p.drawPixmap(12, 13, letter_icon(KIND_STYLE[kind][0], KIND_STYLE[kind][1], 36))
        if self.online:  # зелёная точка — подключён прямо сейчас
            p.setPen(QPen(QColor(34, 34, 40), 2))
            p.setBrush(QColor(48, 209, 88))
            p.drawEllipse(QRectF(40, 39, 11, 11))
        p.setPen(TEXT)
        p.setFont(font(10, True))
        fm = QFontMetricsF(font(10, True))
        p.drawText(QRectF(58, 10, self.width() - 90, 22), Qt.AlignVCenter,
                   fm.elidedText(self.vpn.get("name") or "Без названия", Qt.ElideRight, self.width() - 92))
        sub = f"{KIND_NAMES[kind]} · " + (f"{n} {plural(n, 'правило', 'правила', 'правил')}" if n else "нет программ")
        p.setPen(DIM)
        p.setFont(font(8))
        p.drawText(QRectF(58, 32, self.width() - 90, 20), Qt.AlignVCenter, sub)
        p.setFont(font(7, True))
        p.setPen(QColor(255, 255, 255, 90))
        p.drawText(QRectF(self.width() - 30, 0, 26, self.height()), Qt.AlignCenter,
                   "" if self.t_hover > 0.5 else str(self.idx + 1))
        if self.t_hover > 0.01:  # стрелки «выше/ниже» при наведении — порядок = приоритет
            up, down = self.arrows()
            p.setOpacity(self.t_hover)
            for rect, sign in ((up, -1), (down, 1)):
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 26))
                p.drawEllipse(rect)
                p.setPen(QPen(TEXT, 1.5))
                c = rect.center()
                p.drawLine(QPointF(c.x() - 4, c.y() - 2 * sign), QPointF(c.x(), c.y() + 2 * sign))
                p.drawLine(QPointF(c.x(), c.y() + 2 * sign), QPointF(c.x() + 4, c.y() - 2 * sign))


def plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


class NewCard(Anim):
    """«+ Новый VPN» внизу списка — пунктиром."""
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setFixedHeight(48)
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, int(55 + 70 * self.t_hover)), 1.2, Qt.DashLine))
        p.setBrush(QColor(255, 255, 255, int(10 * self.t_hover)))
        p.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), 12, 12)
        p.setPen(QColor(255, 255, 255, int(150 + 90 * self.t_hover)))
        p.setFont(font(9, True))
        p.drawText(self.rect(), Qt.AlignCenter, "+  Новый VPN")


class KindTile(Anim):
    """Плитка выбора вида VPN."""
    clicked = Signal(str)

    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        self.sel = 0.0
        self.selected = False
        self.setFixedSize(112, 80)
        self.setCursor(Qt.PointingHandCursor)

    def set_selected(self, on):
        if on == self.selected:
            return
        self.selected = on
        start = self.sel

        def step(v):
            self.sel = start + ((1.0 if on else 0.0) - start) * v
            self.update()
        self._sel = animate(self, 220, step, curve=QEasingCurve.OutBack if on else QEasingCurve.OutCubic)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.kind)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        label, color, sub = KIND_STYLE[self.kind]
        c = QColor(color)
        r = QRectF(1.5, 1.5, self.width() - 3, self.height() - 3)
        fill = QColor(c)
        fill.setAlpha(int(10 * self.t_hover + 55 * min(1.0, self.sel)))
        p.setBrush(fill if self.sel > 0.01 or self.t_hover > 0.01 else QColor(255, 255, 255, 10))
        ring = QColor(c.lighter(130))
        ring.setAlpha(int(255 * min(1.0, self.sel)))
        p.setPen(QPen(ring, 2) if self.sel > 0.01 else QPen(QColor(255, 255, 255, 22), 1))
        p.drawRoundedRect(r, 14, 14)
        s = 30 + 4 * self.sel  # значок выбранного чуть подрастает
        p.drawPixmap(QRectF((self.width() - s) / 2, 10 - 2 * self.sel, s, s).toRect(), letter_icon(label, color, 72))
        p.setPen(TEXT)
        p.setFont(font(8, True))
        p.drawText(QRectF(0, 44, self.width(), 18), Qt.AlignCenter, KIND_NAMES[self.kind])
        p.setPen(DIM)
        p.setFont(font(7))
        p.drawText(QRectF(4, 60, self.width() - 8, 16), Qt.AlignCenter, sub)


class DropZone(Anim):
    """Файл туннеля: перетащите сюда или нажмите, чтобы выбрать. Подсвечивается, когда над ним файл."""
    changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.path = ""
        self.kind = "wireguard"
        self.drag = 0.0
        self.setAcceptDrops(True)
        self.setFixedHeight(64)
        self.setCursor(Qt.PointingHandCursor)

    def ext(self):
        return ".ovpn" if self.kind == "openvpn" else ".conf"

    def set_path(self, path):
        self.path = path
        self.update()

    def anim_drag(self, on):
        start = self.drag

        def step(v):
            self.drag = start + ((1.0 if on else 0.0) - start) * v
            self.update()
        self._d = animate(self, 160, step)

    def dragEnterEvent(self, e):
        urls = e.mimeData().urls()
        if urls and urls[0].toLocalFile().lower().endswith(self.ext()):
            e.acceptProposedAction()
            self.anim_drag(True)

    def dragLeaveEvent(self, e):
        self.anim_drag(False)

    def dropEvent(self, e):
        self.anim_drag(False)
        path = os.path.normpath(e.mimeData().urls()[0].toLocalFile())
        self.set_path(path)
        self.changed.emit(path)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        flt = "Конфигурация OpenVPN (*.ovpn)" if self.kind == "openvpn" else "Туннель (*.conf)"
        path, _ = QFileDialog.getOpenFileName(self, "Файл VPN", os.path.dirname(self.path), flt)
        if path:
            self.set_path(os.path.normpath(path))
            self.changed.emit(self.path)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        k = max(self.drag, self.t_hover * 0.5)
        if self.path:
            p.setPen(QPen(QColor(255, 255, 255, int(25 + 40 * k)), 1))
            p.setBrush(QColor(255, 255, 255, int(14 + 14 * k)))
        else:
            p.setPen(QPen(QColor(10, 132, 255, int(110 + 145 * k)), 1.5, Qt.DashLine))
            p.setBrush(QColor(10, 132, 255, int(18 + 50 * self.drag)))
        p.drawRoundedRect(r, 12, 12)
        if self.path:
            ok = os.path.isfile(self.path)
            label, color, _ = KIND_STYLE[self.kind]
            p.drawPixmap(14, 14, letter_icon(label, color, 36))
            p.setPen(TEXT)
            p.setFont(font(10, True))
            p.drawText(QRectF(62, 10, self.width() - 80, 22), Qt.AlignVCenter, os.path.basename(self.path))
            p.setPen(DIM if ok else QColor(255, 120, 110))
            p.setFont(font(8))
            fm = QFontMetricsF(font(8))
            sub = (fm.elidedText(os.path.dirname(self.path), Qt.ElideMiddle, self.width() - 200) + "  ·  нажмите, чтобы заменить"
                   if ok else "файл не найден — выберите заново")
            p.drawText(QRectF(62, 32, self.width() - 80, 20), Qt.AlignVCenter, sub)
        else:
            p.setPen(QColor(150, 200, 255))
            p.setFont(font(10, True))
            p.drawText(QRectF(0, 10, self.width(), 24), Qt.AlignCenter,
                       f"Перетащите сюда файл {self.ext()}")
            p.setPen(DIM)
            p.setFont(font(8))
            p.drawText(QRectF(0, 34, self.width(), 20), Qt.AlignCenter, "или нажмите, чтобы выбрать")


class Switch(Anim):
    """Переключатель как в macOS: ползунок едет, фон перетекает в синий."""
    toggled = Signal(bool)

    def __init__(self, on):
        super().__init__()
        self.on = on
        self.pos_t = 1.0 if on else 0.0
        self.setFixedSize(44, 26)
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self.on = not self.on
        start = self.pos_t

        def step(v):
            self.pos_t = start + ((1.0 if self.on else 0.0) - start) * v
            self.update()
        self._p = animate(self, 220, step, curve=QEasingCurve.OutBack)
        self.toggled.emit(self.on)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t = max(0.0, min(1.0, self.pos_t))
        off, on = QColor(255, 255, 255, 40), QColor(48, 209, 88)
        bg = QColor(int(off.red() + (on.red() - off.red()) * t), int(off.green() + (on.green() - off.green()) * t),
                    int(off.blue() + (on.blue() - off.blue()) * t), int(off.alpha() + (255 - off.alpha()) * t))
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(0, 0, 44, 26), 13, 13)
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QRectF(3 + 18 * self.pos_t, 3, 20, 20))


class EmptyState(QWidget):
    """Пока нет ни одного VPN: пульсирующий замок и большая кнопка."""
    add = Signal()

    def __init__(self):
        super().__init__()
        self.phase = 0.0
        lay = QVBoxLayout(self)
        lay.addStretch(1)
        lay.addSpacing(150)
        self.h = h = QLabel("Добавьте свой первый VPN")
        h.setObjectName("h")
        h.setAlignment(Qt.AlignCenter)
        lay.addWidget(h)
        t = QLabel("Укажите, для каких программ и сайтов он нужен, —\nдальше виджет сам будет его включать и выключать.")
        t.setObjectName("dim")
        t.setAlignment(Qt.AlignCenter)
        lay.addWidget(t)
        lay.addSpacing(10)
        b = QPushButton("+  Добавить VPN")
        b.setObjectName("ok")
        b.setFixedWidth(200)
        b.clicked.connect(self.add)
        lay.addWidget(b, 0, Qt.AlignCenter)
        lay.addStretch(1)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    def tick(self):
        if self.isVisible():
            self.phase = (self.phase + 0.016 / 2.2) % 1.0
            self.update(QRect(self.width() // 2 - 110, 0, 220, self.h.geometry().top()))

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx = self.width() / 2
        cy = self.h.geometry().top() - 80
        for k in range(3):  # расходящиеся волны
            ph = (self.phase + k / 3) % 1.0
            rad = 34 + 60 * ph
            c = QColor(ACCENT)
            c.setAlpha(int(110 * (1 - ph)))
            p.setPen(QPen(c, 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), rad, rad)
        g = QLinearGradient(cx, cy - 34, cx, cy + 34)
        g.setColorAt(0, QColor("#3d9bff"))
        g.setColorAt(1, QColor("#0060df"))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(cx, cy), 34, 34)
        p.setPen(QColor(255, 255, 255))
        p.setFont(QFont("Segoe MDL2 Assets", 22))
        p.drawText(QRectF(cx - 34, cy - 34, 68, 68), Qt.AlignCenter, "")  # замок


QSS = """
QDialog { background: transparent; }
QDialog#picker { background: transparent; }
QWidget { color: #f2f2f5; font: 9pt 'Segoe UI'; }
QLabel#h { font: bold 13pt 'Segoe UI'; }
QLabel#sub { font: bold 9pt 'Segoe UI'; color: rgba(255, 255, 255, 200); margin-top: 4px; }
QLabel#dim { color: rgba(255, 255, 255, 135); font: 8pt 'Segoe UI'; }
QLabel#warn { color: #ffb340; font: 8pt 'Segoe UI'; }
QLineEdit, QComboBox, QSpinBox { background: rgba(255, 255, 255, 16); border: 1px solid rgba(255, 255, 255, 18);
    border-radius: 8px; padding: 6px 10px; font: 9pt 'Segoe UI'; selection-background-color: #0a84ff; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #0a84ff; }
QLineEdit#title { background: transparent; border: none; border-bottom: 1px solid rgba(255, 255, 255, 25);
    border-radius: 0; padding: 2px 0 6px 0; font: bold 15pt 'Segoe UI'; }
QLineEdit#title:focus { border-bottom: 1px solid #0a84ff; }
QComboBox::drop-down, QSpinBox::up-button, QSpinBox::down-button { border: none; width: 18px; }
QComboBox QAbstractItemView { background: #2a2a31; border: none; selection-background-color: #0a84ff; outline: 0; }
QPushButton { background: rgba(255, 255, 255, 20); border: none; border-radius: 8px; padding: 7px 16px; }
QPushButton:hover { background: rgba(255, 255, 255, 36); }
QPushButton#ok { background: #0a84ff; font-weight: bold; }
QPushButton#ok:hover { background: #3d9bff; }
QPushButton#link { background: transparent; color: #64b0ff; padding: 0; text-align: left; }
QPushButton#link:hover { color: #9ccbff; }
QPushButton#trash { background: transparent; color: rgba(255, 255, 255, 120); font: 12pt 'Segoe MDL2 Assets';
    padding: 6px; }
QPushButton#trash:hover { color: #ff6b5f; background: rgba(255, 80, 70, 30); }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(255, 255, 255, 50); border-radius: 3px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: rgba(255, 255, 255, 90); }
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page { height: 0; background: none; }
QFrame#panel { background: rgba(255, 255, 255, 7); border: 1px solid rgba(255, 255, 255, 14); border-radius: 14px; }
QFrame#left { background: rgba(0, 0, 0, 40); border-radius: 14px; }
"""


def section(text, hint=None):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 6, 0, 0)
    lay.setSpacing(2)
    t = QLabel(text)
    t.setObjectName("sub")
    lay.addWidget(t)
    if hint:
        h = QLabel(hint)
        h.setObjectName("dim")
        h.setWordWrap(True)
        lay.addWidget(h)
    return w


class VpnDialog(QDialog):
    status = Signal(dict)

    def __init__(self, cfg):
        # как окно настроек MiniDock: без рамки Windows, скруглённое, полупрозрачное с размытием под ним
        super().__init__(None, Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("VPN по программам")
        self.setStyleSheet(QSS)
        self.setFixedSize(980, 700)
        self.cfg = json.loads(json.dumps(cfg))
        self.icons = Icons(self.cfg.get("icons", {}))
        self.cur = -1
        self.loading = False
        self.drag = None
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # слева — боковая колонка: «светофор», карточки VPN, исключения
        side = QWidget()
        side.setFixedWidth(self.SIDE)
        ll = QVBoxLayout(side)
        ll.setContentsMargins(12, 14, 12, 14)
        ll.setSpacing(6)
        lights = QHBoxLayout()
        lights.setSpacing(8)
        lights.addWidget(TrafficLight("#ff5f57", self.reject))
        lights.addWidget(TrafficLight("#febc2e", self.showMinimized))
        lights.addWidget(TrafficLight("#3a3a40", lambda: None))
        lights.addStretch()
        ll.addLayout(lights)
        ll.addSpacing(14)
        brand = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(letter_icon("VPN", "#0a84ff", 34))
        brand.addWidget(logo)
        bt = QLabel("VPN по программам")
        bt.setStyleSheet("font: 11pt 'Segoe UI Semibold';")
        brand.addWidget(bt, 1)
        ll.addLayout(brand)
        ll.addSpacing(12)
        cap = QLabel("ВАШИ VPN")
        cap.setObjectName("dim")
        ll.addWidget(cap)
        self.cards_box = QVBoxLayout()
        self.cards_box.setSpacing(4)
        ll.addLayout(self.cards_box)
        self.new_card = NewCard()
        self.new_card.clicked.connect(self.add_vpn)
        ll.addWidget(self.new_card)
        ll.addSpacing(14)
        cap2 = QLabel("ИСКЛЮЧЕНИЯ")
        cap2.setObjectName("dim")
        ll.addWidget(cap2)
        self.no_card = VpnCard(-2, {"apps": cfg["no_vpn"]}, special=True)
        self.no_card.chosen.connect(self.select)
        ll.addWidget(self.no_card)
        ll.addStretch(1)
        prio = QLabel("Чем выше в списке, тем главнее: если открыты программы для разных VPN, включится верхний.")
        prio.setObjectName("dim")
        prio.setWordWrap(True)
        ll.addWidget(prio)
        outer.addWidget(side)

        main = QWidget()
        root = QVBoxLayout(main)
        root.setContentsMargins(26, 16, 24, 18)
        root.setSpacing(14)
        top = QHBoxLayout()
        d = QLabel("Открыли программу из списка — VPN включился. Закрыли — выключился.")
        d.setObjectName("dim")
        top.addWidget(d, 1)
        top.addWidget(QLabel("Автоматически"))
        self.auto = Switch(bool(cfg["auto"]))
        top.addWidget(self.auto)
        root.addLayout(top)
        body = QHBoxLayout()
        body.setSpacing(0)
        outer.addWidget(main, 1)

        # справа — редактор выбранного VPN (или приглашение добавить первый)
        self.stack = QStackedWidget()
        self.empty = EmptyState()
        self.empty.add.connect(self.add_vpn)
        self.stack.addWidget(self.empty)
        self.editor = self.build_editor()
        scroll = SmoothScrollArea()
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(self.editor)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.stack.addWidget(scroll)
        nv = QWidget()
        nl = QVBoxLayout(nv)
        nl.setContentsMargins(4, 0, 8, 0)
        nl.setSpacing(10)
        title = QLabel("Без VPN")
        title.setStyleSheet("font: bold 15pt 'Segoe UI';")
        nl.addWidget(title)
        hint = QLabel("Пока открыто что-то из этого списка, VPN выключен: банки, Госуслуги и маркетплейсы часто "
                      "не пускают с ним. Закроете — VPN, который был включён, вернётся сам.")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        nl.addWidget(hint)
        nl.addSpacing(6)
        self.no_vpn = ChipBox(self.icons, SUGGEST_NO_VPN, "")
        self.no_vpn.set_items(cfg["no_vpn"])
        self.no_vpn.changed.connect(self.no_changed)
        nl.addWidget(self.no_vpn)
        nl.addStretch(1)
        self.stack.addWidget(nv)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        # подвал
        foot = QHBoxLayout()
        foot.addWidget(QLabel("Выключать VPN через"))
        self.delay = QSpinBox()
        self.delay.setRange(0, 600)
        self.delay.setSuffix(" с")
        self.delay.setValue(int(cfg["delay"]))
        foot.addWidget(self.delay)
        dl = QLabel("после закрытия программы")
        dl.setToolTip("Пауза — на случай, если программа просто перезапускается")
        dl.setObjectName("dim")
        foot.addWidget(dl, 1)
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Сохранить")
        ok.setObjectName("ok")
        ok.clicked.connect(self.accept)
        foot.addWidget(cancel)
        foot.addWidget(ok)
        root.addLayout(foot)

        self.cards = []
        self.online = {}
        self.status.connect(self.on_status)
        self.rebuild_cards()
        self.select(0 if self.cfg["vpns"] else -1)
        threading.Thread(target=self.check_status, daemon=True).start()

    SIDE = 262

    def showEvent(self, e):
        super().showEvent(e)
        try:  # размытие того, что под окном (как у всплывающих окон панели), по форме скруглённых углов
            enable_blur(int(self.winId()))
            round_window(self, 12)
        except Exception:
            pass
        fade_in(self, slide=-8)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = rounded_path(QRectF(self.rect()), 12)
        p.fillPath(path, QColor(30, 30, 36, 222))
        side = QPainterPath()
        side.addRect(QRectF(0, 0, self.SIDE, self.height()))
        p.fillPath(side.intersected(path), QColor(255, 255, 255, 10))
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.drawPath(path)
        p.drawLine(self.SIDE, 0, self.SIDE, self.height())

    # окно без рамки: перетаскивание за верхнюю полосу
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() < 48:
            self.drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self.drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self.drag)

    def mouseReleaseEvent(self, e):
        self.drag = None

    # -- правая часть
    def build_editor(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 0, 8, 0)
        lay.setSpacing(10)
        head = QHBoxLayout()
        self.name = QLineEdit()
        self.name.setObjectName("title")
        self.name.setPlaceholderText("Название VPN")
        self.name.textChanged.connect(self.store)
        head.addWidget(self.name, 1)
        trash = QPushButton("")  # корзина
        trash.setObjectName("trash")
        trash.setToolTip("Удалить этот VPN")
        trash.clicked.connect(self.remove_vpn)
        head.addWidget(trash)
        lay.addLayout(head)

        lay.addWidget(section("Какой у вас VPN"))
        kinds = QHBoxLayout()
        kinds.setSpacing(8)
        self.kind_tiles = {}
        for k, _ in KINDS:
            t = KindTile(k)
            t.clicked.connect(self.set_kind)
            self.kind_tiles[k] = t
            kinds.addWidget(t)
        kinds.addStretch(1)
        lay.addLayout(kinds)

        self.target_title = section("")
        lay.addWidget(self.target_title)
        self.drop = DropZone()
        self.drop.changed.connect(self.file_chosen)
        lay.addWidget(self.drop)
        self.win_row = QWidget()
        wr = QHBoxLayout(self.win_row)
        wr.setContentsMargins(0, 0, 0, 0)
        self.win_name = QComboBox()
        self.win_name.setEditable(True)
        self.win_name.lineEdit().setPlaceholderText("имя подключения")
        self.win_name.currentTextChanged.connect(self.store)
        wr.addWidget(self.win_name, 1)
        ms = QPushButton("Параметры VPN Windows…")
        ms.clicked.connect(lambda: os.startfile("ms-settings:network-vpn"))
        wr.addWidget(ms)
        lay.addWidget(self.win_row)
        self.cmd_box = QWidget()
        cl = QGridLayout(self.cmd_box)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setVerticalSpacing(6)
        self.on_cmd, self.off_cmd, self.check_cmd = QLineEdit(), QLineEdit(), QLineEdit()
        self.on_cmd.setPlaceholderText("команда, которая подключает VPN")
        self.off_cmd.setPlaceholderText("команда, которая отключает")
        self.check_cmd.setPlaceholderText("необязательно: код выхода 0 — подключён")
        for i, (lab, e) in enumerate((("Подключить", self.on_cmd), ("Отключить", self.off_cmd),
                                      ("Проверить", self.check_cmd))):
            cl.addWidget(QLabel(lab), i, 0)
            cl.addWidget(e, i, 1)
            e.textChanged.connect(self.store)
        lay.addWidget(self.cmd_box)
        self.warn = QLabel()
        self.warn.setObjectName("warn")
        self.warn.setWordWrap(True)
        lay.addWidget(self.warn)
        self.get_client = QPushButton()
        self.get_client.setObjectName("link")
        self.get_client.setCursor(Qt.PointingHandCursor)
        self.get_client.clicked.connect(lambda: os.startfile(CLIENT_URL.get(self.kind(), "")))
        lay.addWidget(self.get_client)

        lay.addWidget(section("Включать для", "Программы и сайты, которым нужен этот VPN."))
        self.apps = ChipBox(self.icons, SUGGEST_VPN, "")
        self.apps.changed.connect(self.store)
        lay.addWidget(self.apps)
        lay.addStretch(1)
        self.fx = QGraphicsOpacityEffect(w)
        self.fx.setOpacity(1.0)
        w.setGraphicsEffect(self.fx)
        return w

    def kind(self):
        return next((k for k, t in self.kind_tiles.items() if t.selected), "windows")

    def set_kind(self, kind, store=True):
        for k, t in self.kind_tiles.items():
            t.set_selected(k == kind)
        file_kind = kind in ("wireguard", "amneziawg", "openvpn")
        self.drop.kind = kind
        self.drop.setVisible(file_kind)
        self.win_row.setVisible(kind == "windows")
        self.cmd_box.setVisible(kind == "command")
        titles = {"windows": ("Подключение Windows", "Выберите VPN, который уже добавлен в «Параметры → Сеть → VPN»."),
                  "wireguard": ("Файл туннеля", "Файл .conf, который выдал ваш VPN-сервис."),
                  "amneziawg": ("Файл туннеля", "В Amnezia VPN: «Поделиться» → формат AmneziaWG — получится файл .conf."),
                  "openvpn": ("Файл конфигурации", "Файл .ovpn от вашего VPN-сервиса."),
                  "command": ("Команды", "Для любого другого клиента — если он умеет подключаться из командной строки.")}
        t, h = titles[kind]
        labels = self.target_title.findChildren(QLabel)
        labels[0].setText(t)
        if len(labels) > 1:
            labels[1].setText(h)
        else:
            lab = QLabel(h)
            lab.setObjectName("dim")
            lab.setWordWrap(True)
            self.target_title.layout().addWidget(lab)
        warn, client = [], ""
        if file_kind:
            exe = (_wg_exe(kind) if kind != "openvpn"
                   else find_exe(r"%ProgramFiles%\OpenVPN\bin\openvpn-gui.exe", "openvpn-gui.exe"))
            if not exe:
                warn.append(f"{KIND_NAMES[kind]} не установлен на этом компьютере.")
                client = f"Скачать {KIND_NAMES[kind]} →"
            if kind != "openvpn" and not is_admin():
                warn.append("Чтобы виджет мог его включать, включите в MiniDock: Настройки → Основные → "
                            "«Работать с правами администратора».")
        if kind == "windows" and not windows_vpns():
            warn.append("В Windows пока нет ни одного VPN-подключения — добавьте его в параметрах.")
        self.warn.setText(chr(10).join(warn))
        self.warn.setVisible(bool(warn))
        self.get_client.setText(client)
        self.get_client.setVisible(bool(client))
        if store:
            self.store()

    def file_chosen(self, path):
        if not self.name.text().strip() or self.name.text().startswith("VPN "):
            self.name.setText(os.path.splitext(os.path.basename(path))[0])
        self.store()

    # -- данные <-> форма
    def store(self, *_):
        if self.loading or not 0 <= self.cur < len(self.cfg["vpns"]):
            return
        kind = self.kind()
        v = self.cfg["vpns"][self.cur]
        target = (self.win_name.currentText().strip() if kind == "windows"
                  else self.drop.path if kind in ("wireguard", "amneziawg", "openvpn") else "")
        v.update(name=self.name.text().strip(), kind=kind, target=target, on=self.on_cmd.text().strip(),
                 off=self.off_cmd.text().strip(), check=self.check_cmd.text().strip(), apps=self.apps.items())
        if self.cur < len(self.cards):
            self.cards[self.cur].vpn = v
            self.cards[self.cur].update()

    def no_changed(self):
        self.no_card.vpn = {"apps": self.no_vpn.items()}
        self.no_card.update()

    def select(self, row):
        self.cur = row
        has = 0 <= row < len(self.cfg["vpns"])
        self.stack.setCurrentIndex(1 if has else 2 if row == -2 else 0)
        for i, c in enumerate(self.cards):
            c.set_selected(i == row)
        self.no_card.set_selected(row == -2)
        if not has:
            return
        v = self.cfg["vpns"][row]
        self.loading = True
        self.name.setText(v.get("name", ""))
        kind = v.get("kind", "windows")
        self.win_name.clear()
        self.win_name.addItems(windows_vpns())
        self.win_name.setCurrentText(v.get("target", "") if kind == "windows" else "")
        self.drop.set_path(v.get("target", "") if kind in ("wireguard", "amneziawg", "openvpn") else "")
        self.on_cmd.setText(v.get("on", ""))
        self.off_cmd.setText(v.get("off", ""))
        self.check_cmd.setText(v.get("check", ""))
        self.apps.set_items(v.get("apps", []))
        for t in self.kind_tiles.values():  # без анимации: сразу нужная плитка
            t.selected = t.kind == kind
            t.sel = 1.0 if t.selected else 0.0
            t.update()
        self.set_kind(kind, store=False)
        self.loading = False
        self._fx = animate(self, 220, lambda x: self.fx.setOpacity(0.25 + 0.75 * x))  # плавная смена VPN

    def rebuild_cards(self):
        for c in self.cards:
            c.hide()
            c.deleteLater()
        self.cards = []
        for i, v in enumerate(self.cfg["vpns"]):
            c = VpnCard(i, v)
            c.online = self.online.get(v.get("name"), False)
            c.chosen.connect(self.select)
            c.moved.connect(self.move_vpn)
            self.cards_box.addWidget(c)
            self.cards.append(c)
            if i == self.cur:
                c.selected, c.sel = True, 1.0

    def add_vpn(self):
        n = len(self.cfg["vpns"]) + 1
        kind = "amneziawg" if _wg_exe("amneziawg") else "wireguard" if _wg_exe("wireguard") else "windows"
        self.cfg["vpns"].append(dict(name=f"VPN {n}", kind=kind, target="", apps=[]))
        self.rebuild_cards()
        card = self.cards[-1]
        card._in = animate(card, 260, lambda v: (card.setFixedHeight(int(62 * v)), card.update()),
                           curve=QEasingCurve.OutCubic)
        self.select(len(self.cfg["vpns"]) - 1)
        self.name.setFocus()
        self.name.selectAll()

    def remove_vpn(self):
        row = self.cur
        if not 0 <= row < len(self.cards):
            return
        card = self.cards[row]

        def done():
            del self.cfg["vpns"][row]
            self.cur = -1
            self.rebuild_cards()
            self.select(min(row, len(self.cfg["vpns"]) - 1))
        card._out = animate(card, 200, lambda v: card.setFixedHeight(max(0, int(62 * (1 - v)))), done,
                            curve=QEasingCurve.InCubic)

    def move_vpn(self, row, step):
        new = row + step
        if not 0 <= new < len(self.cfg["vpns"]):
            return
        vpns = self.cfg["vpns"]
        vpns[row], vpns[new] = vpns[new], vpns[row]
        self.cur = new
        self.rebuild_cards()
        self.select(new)

    # -- кто подключён прямо сейчас — зелёная точка на карточке
    def check_status(self):
        try:
            self.status.emit({v.get("name"): is_connected(v) for v in self.cfg["vpns"] if v.get("target") or v.get("check")})
        except Exception:
            pass

    def on_status(self, states):
        self.online = states
        for c in self.cards:
            c.online = states.get(c.vpn.get("name"), False)
            c.update()

    def result_cfg(self):
        self.store()
        names = set()
        for i, v in enumerate(self.cfg["vpns"]):  # имена — ключи состояния: не пустые и разные
            v["name"] = v["name"] or f"VPN {i + 1}"
            base, n = v["name"], 2
            while v["name"] in names:
                v["name"] = f"{base} {n}"
                n += 1
            names.add(v["name"])
        rules = {r for v in self.cfg["vpns"] for r in v.get("apps", [])} | set(self.no_vpn.items())
        icons = {exe_rule(r).lower(): self.icons.path(r) for r in rules
                 if not r.lower().startswith("окно:") and self.icons.path(r)}
        self.cfg.update(no_vpn=self.no_vpn.items(), delay=self.delay.value(), auto=self.auto.on, icons=icons)
        return self.cfg

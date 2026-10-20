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
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from minidock.barwidgets import WIDGETS_DIR, BarWidget

CFG_PATH = WIDGETS_DIR / "vpn_auto.json"
NO_WINDOW = 0x08000000
KINDS = [("windows", "Windows VPN"), ("wireguard", "WireGuard"), ("amneziawg", "AmneziaWG"),
         ("openvpn", "OpenVPN"), ("command", "Свои команды")]
KIND_NAMES = dict(KINDS)
# что обычно не открывается без VPN или, наоборот, с ним — подсказка в настройках
DEFAULT_NO_VPN = ["SberBank.exe", "tinkoff.exe", "gosuslugi.exe", "окно: Госуслуги", "окно: Сбербанк Онлайн",
                  "окно: Wildberries", "окно: Ozon"]


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

QSS = """
QDialog, QWidget#page { background: #222228; }
QLabel, QCheckBox { color: #f2f2f5; font: 9pt 'Segoe UI'; }
QLabel#h { font: bold 11pt 'Segoe UI'; }
QLabel#dim { color: rgba(255, 255, 255, 140); font: 8pt 'Segoe UI'; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QListWidget { background: rgba(255, 255, 255, 18); color: #f2f2f5;
    border: none; border-radius: 6px; padding: 5px 8px; font: 9pt 'Segoe UI'; }
QListWidget::item { padding: 5px; border-radius: 5px; }
QListWidget::item:selected { background: #0a84ff; }
QComboBox QAbstractItemView { background: #2a2a31; color: #f2f2f5; selection-background-color: #0a84ff; }
QPushButton { background: rgba(255, 255, 255, 22); color: #f2f2f5; border: none; border-radius: 7px;
    padding: 6px 14px; font: 9pt 'Segoe UI'; }
QPushButton:hover { background: rgba(255, 255, 255, 40); }
QPushButton#ok { background: #0a84ff; }
QPushButton#ok:hover { background: #3d9bff; }
"""

TARGET_HINT = {
    "windows": ("Имя подключения", "как в «Параметры → Сеть и Интернет → VPN»"),
    "wireguard": ("Файл туннеля", "файл .conf, который выдал ваш VPN"),
    "amneziawg": ("Файл туннеля", "файл .conf для AmneziaWG (в Amnezia VPN: «Поделиться» → «AmneziaWG»)"),
    "openvpn": ("Файл .ovpn", "или имя конфигурации из OpenVPN GUI"),
    "command": ("Название", "для себя — команды ниже"),
}


def user_programs():
    """Запущенные программы с окнами — чтобы добавить в список, не вспоминая имя .exe."""
    pids = set()

    def add(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h):
            try:
                import win32process
                pids.add(win32process.GetWindowThreadProcessId(h)[1])
            except Exception:
                pass
    try:
        win32gui.EnumWindows(add, None)
    except Exception:
        pass
    names = set()
    for pid in pids:
        try:
            names.add(psutil.Process(pid).name())
        except psutil.Error:
            pass
    skip = {"explorer.exe", "minidock.exe", "python.exe", "pythonw.exe", "textinputhost.exe",
            "applicationframehost.exe", "systemsettings.exe"}
    return sorted((n for n in names if n.lower() not in skip), key=str.lower)


class AppList(QWidget):
    """Список программ: по строке на программу + «добавить открытую»."""

    def __init__(self, items, hint):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.edit = QPlainTextEdit("\n".join(items))
        self.edit.setPlaceholderText(hint)
        self.edit.setFixedHeight(96)
        lay.addWidget(self.edit)
        row = QHBoxLayout()
        self.pick = QComboBox()
        self.pick.addItems(user_programs())
        add = QPushButton("Добавить открытую")
        add.clicked.connect(self.add_current)
        row.addWidget(self.pick, 1)
        row.addWidget(add)
        lay.addLayout(row)

    def add_current(self):
        name = self.pick.currentText()
        if name and name.lower() not in [i.lower() for i in self.items()]:
            self.edit.appendPlainText(name)

    def items(self):
        return [line.strip() for line in self.edit.toPlainText().splitlines() if line.strip()]

    def set_items(self, items):
        self.edit.setPlainText("\n".join(items))


class VpnDialog(QDialog):
    def __init__(self, cfg):
        super().__init__(None, Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint)
        self.setWindowTitle("VPN по программам")
        self.setStyleSheet(QSS)
        self.resize(720, 560)
        self.cfg = json.loads(json.dumps(cfg))
        self.cur = -1
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        h = QLabel("Какой VPN для каких программ")
        h.setObjectName("h")
        lay.addWidget(h)
        hint = QLabel("Открыли программу из списка — VPN подключится; закрыли — отключится. Если программ "
                      "для разных VPN открыто несколько, побеждает тот, что выше. Вместо программы можно "
                      "написать «окно: YouTube» — сработает, когда слово есть в заголовке окна (вкладка браузера).")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        body = QHBoxLayout()
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.setFixedWidth(170)
        self.list.currentRowChanged.connect(self.select)
        left.addWidget(self.list, 1)
        btns = QHBoxLayout()
        for text, fn in (("+", self.add_vpn), ("−", self.remove_vpn), ("↑", lambda: self.move(-1)),
                         ("↓", lambda: self.move(1))):
            b = QPushButton(text)
            b.setFixedWidth(36)
            b.clicked.connect(fn)
            btns.addWidget(b)
        left.addLayout(btns)
        body.addLayout(left)

        self.page = QWidget()
        self.page.setObjectName("page")
        form = QFormLayout(self.page)
        form.setContentsMargins(6, 0, 0, 0)
        form.setVerticalSpacing(8)
        self.name = QLineEdit()
        self.kind = QComboBox()
        for k, label in KINDS:
            self.kind.addItem(label, k)
        self.kind.currentIndexChanged.connect(self.kind_changed)
        self.target = QLineEdit()
        self.browse = QPushButton("Обзор…")
        self.browse.clicked.connect(self.pick_file)
        trow = QHBoxLayout()
        trow.addWidget(self.target, 1)
        trow.addWidget(self.browse)
        self.target_label = QLabel()
        self.target_hint = QLabel()
        self.target_hint.setObjectName("dim")
        self.on_cmd, self.off_cmd, self.check_cmd = QLineEdit(), QLineEdit(), QLineEdit()
        self.on_cmd.setPlaceholderText("например: \"C:\\Program Files\\Hiddify\\hiddify.exe\" connect")
        self.check_cmd.setPlaceholderText("код выхода 0 — подключён; можно оставить пустым")
        self.apps = AppList([], "discord.exe\nокно: YouTube")
        form.addRow("Название", self.name)
        form.addRow("Вид", self.kind)
        form.addRow(self.target_label, trow)
        form.addRow("", self.target_hint)
        self.cmd_rows = [("Подключить", self.on_cmd), ("Отключить", self.off_cmd), ("Проверить", self.check_cmd)]
        self.cmd_labels = []
        for label, w in self.cmd_rows:
            lab = QLabel(label)
            self.cmd_labels.append(lab)
            form.addRow(lab, w)
        form.addRow("Программы", self.apps)
        body.addWidget(self.page, 1)
        lay.addLayout(body, 1)

        lab = QLabel("Программы, которым VPN мешает — пока они открыты, VPN отключён (банки, Госуслуги, маркетплейсы):")
        lab.setWordWrap(True)
        lay.addWidget(lab)
        self.no_vpn = AppList(cfg["no_vpn"], "SberBank.exe\nокно: Госуслуги")
        lay.addWidget(self.no_vpn)
        row = QHBoxLayout()
        row.addWidget(QLabel("Отключать VPN через"))
        self.delay = QSpinBox()
        self.delay.setRange(0, 600)
        self.delay.setSuffix(" с")
        self.delay.setValue(int(cfg["delay"]))
        row.addWidget(self.delay)
        row.addWidget(QLabel("после закрытия программы"))
        row.addStretch(1)
        self.auto = QCheckBox("Переключать автоматически")
        self.auto.setChecked(bool(cfg["auto"]))
        row.addWidget(self.auto)
        lay.addLayout(row)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Сохранить")
        ok.setObjectName("ok")
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

        for v in self.cfg["vpns"]:
            self.list.addItem(v["name"])
        if self.cfg["vpns"]:
            self.list.setCurrentRow(0)
        else:
            self.page.setEnabled(False)
            self.kind_changed()

    # перенос полей формы в данные и обратно
    def store(self):
        if 0 <= self.cur < len(self.cfg["vpns"]):
            v = self.cfg["vpns"][self.cur]
            v.update(name=self.name.text().strip() or f"VPN {self.cur + 1}", kind=self.kind.currentData(),
                     target=self.target.text().strip(), on=self.on_cmd.text().strip(),
                     off=self.off_cmd.text().strip(), check=self.check_cmd.text().strip(), apps=self.apps.items())
            self.list.item(self.cur).setText(v["name"])

    def select(self, row):
        self.store()
        self.cur = row
        has = 0 <= row < len(self.cfg["vpns"])
        self.page.setEnabled(has)
        if not has:
            return
        v = self.cfg["vpns"][row]
        self.name.setText(v.get("name", ""))
        self.kind.setCurrentIndex(max(0, self.kind.findData(v.get("kind", "windows"))))
        self.target.setText(v.get("target", ""))
        self.on_cmd.setText(v.get("on", ""))
        self.off_cmd.setText(v.get("off", ""))
        self.check_cmd.setText(v.get("check", ""))
        self.apps.set_items(v.get("apps", []))
        self.kind_changed()

    def kind_changed(self):
        kind = self.kind.currentData()
        label, hint = TARGET_HINT[kind]
        self.target_label.setText(label)
        self.target_hint.setText(hint)
        self.browse.setVisible(kind in ("wireguard", "amneziawg", "openvpn"))
        for lab, (_, w) in zip(self.cmd_labels, self.cmd_rows):
            lab.setVisible(kind == "command")
            w.setVisible(kind == "command")

    def pick_file(self):
        kind = self.kind.currentData()
        flt = "Конфигурация OpenVPN (*.ovpn)" if kind == "openvpn" else "Туннель (*.conf)"
        path, _ = QFileDialog.getOpenFileName(self, "Файл VPN", os.path.dirname(self.target.text()), flt)
        if path:
            self.target.setText(os.path.normpath(path))
            if not self.name.text().strip() or self.name.text().startswith("VPN "):
                self.name.setText(os.path.splitext(os.path.basename(path))[0])

    def add_vpn(self):
        self.store()
        self.cfg["vpns"].append(dict(name=f"VPN {len(self.cfg['vpns']) + 1}", kind="windows", target="", apps=[]))
        self.list.addItem(self.cfg["vpns"][-1]["name"])
        self.list.setCurrentRow(len(self.cfg["vpns"]) - 1)

    def remove_vpn(self):
        row = self.list.currentRow()
        if row < 0:
            return
        self.cur = -1
        del self.cfg["vpns"][row]
        self.list.takeItem(row)
        self.select(self.list.currentRow())

    def move(self, step):
        row = self.list.currentRow()
        new = row + step
        if row < 0 or not 0 <= new < len(self.cfg["vpns"]):
            return
        self.store()
        vpns = self.cfg["vpns"]
        vpns[row], vpns[new] = vpns[new], vpns[row]
        self.list.item(row).setText(vpns[row]["name"])
        self.list.item(new).setText(vpns[new]["name"])
        self.cur = new
        self.list.setCurrentRow(new)

    def result_cfg(self):
        self.store()
        names = set()
        for v in self.cfg["vpns"]:  # имена — ключи состояния, должны различаться
            base, n = v["name"], 2
            while v["name"] in names:
                v["name"] = f"{base} {n}"
                n += 1
            names.add(v["name"])
        self.cfg.update(no_vpn=self.no_vpn.items(), delay=self.delay.value(), auto=self.auto.isChecked())
        return self.cfg

"""Виджеты верхней панели: короткая надпись на панели, по клику — окно с подробностями
(как Headroom, iStat Menus и подобные на macOS).

Встроенные виджеты — ниже в этом файле. Свои кладутся файлами *.py в папку
%APPDATA%\\MiniDock\\widgets (пример и описание создаются там при первом запуске) и включаются
в настройках: «Верхняя панель» → «Виджеты».

Виджет — класс-наследник BarWidget:
    id, name    — уникальный ключ и название для настроек
    icon        — значок перед надписью (символ шрифта Segoe MDL2 Assets) или ""
    interval    — раз во сколько секунд вызывать update()
    update(detailed) — в фоновом потоке: собрать данные и вернуть что угодно (обычно dict).
                    detailed=True, пока открыто окно виджета — можно собрать и тяжёлые подробности
    text(data)  — надпись на панели
    panel(data) — содержимое окна: список строк
        ("title", текст)                       — заголовок
        ("text", текст)                        — серый поясняющий текст
        ("value", подпись, значение)           — строка «подпись … значение»
        ("bar", подпись, доля 0..1, значение)  — полоса заполнения (лимиты, загрузка)
        ("button", подпись, действие)          — кнопка: функция или адрес/путь (открывается в Windows)
        ("sep",)                               — разделитель
"""
import datetime
import importlib.util
import json
import os
import sys
import threading
import time
import traceback

import psutil
from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter
from PySide6.QtWidgets import QWidget

from .config import CFG_DIR
from .flyouts import Flyout
from .ui import animate, rounded_path

WIDGETS_DIR = CFG_DIR / "widgets"


class BarWidget:
    id = ""
    name = ""
    icon = ""
    interval = 5.0

    def update(self, detailed):
        return None

    def text(self, data):
        return ""

    def panel(self, data):
        return []


# -- встроенные -----------------------------------------------------------------------------------

def _size(n):
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit in ("Б", "КБ") else f"{n:.1f} {unit}"
        n /= 1024


def _uptime():
    s = int(time.time() - psutil.boot_time())
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    return (f"{d} д " if d else "") + f"{h} ч {s // 60} мин"


class SystemWidget(BarWidget):
    id = "system"
    name = "Система — процессор и память"
    icon = ""  # чип
    interval = 2.0

    def __init__(self):
        psutil.cpu_percent()  # первый замер всегда 0 — делаем его заранее
        self.last_detailed = 0.0

    def update(self, detailed):
        mem = psutil.virtual_memory()
        data = dict(cpu=psutil.cpu_percent(), mem=mem.percent, mem_used=mem.used, mem_total=mem.total)
        if detailed:
            disks = []
            for part in psutil.disk_partitions():
                if "fixed" in part.opts or part.fstype:
                    try:
                        u = psutil.disk_usage(part.mountpoint)
                        disks.append((part.mountpoint.rstrip("\\"), u.percent / 100, u.used, u.total))
                    except OSError:
                        pass
            if time.monotonic() - self.last_detailed > 5:
                # загрузку процесса psutil считает между двумя замерами — первый делаем сразу
                for p in psutil.process_iter(["cpu_percent"]):
                    pass
                time.sleep(0.5)
            self.last_detailed = time.monotonic()
            procs = []
            for p in psutil.process_iter(["name", "cpu_percent", "memory_info"]):
                if p.info["name"] in ("System Idle Process", None):
                    continue
                procs.append((p.info["cpu_percent"] or 0.0, p.info["name"],
                              p.info["memory_info"].rss if p.info["memory_info"] else 0))
            procs.sort(reverse=True)
            data.update(disks=disks, procs=procs[:5], uptime=_uptime(), cores=psutil.cpu_count())
        return data

    def text(self, d):
        return f"ЦП {d['cpu']:.0f}%   ОЗУ {d['mem']:.0f}%"

    def panel(self, d):
        rows = [("title", "Система"),
                ("bar", "Процессор", d["cpu"] / 100, f"{d['cpu']:.0f}%"),
                ("bar", "Память", d["mem"] / 100, f"{_size(d['mem_used'])} из {_size(d['mem_total'])}")]
        for name, frac, used, total in d.get("disks", []):
            rows.append(("bar", f"Диск {name}", frac, f"{_size(used)} из {_size(total)}"))
        if d.get("procs"):
            rows += [("sep",), ("text", "Больше всего нагружают процессор")]
            cores = d.get("cores") or 1
            rows += [("value", name, f"{cpu / cores:.0f}%  ·  {_size(mem)}") for cpu, name, mem in d["procs"]]
        if d.get("uptime"):
            rows += [("sep",), ("value", "Без перезагрузки", d["uptime"])]
        rows.append(("button", "Диспетчер задач…", "taskmgr.exe"))
        return rows


class NetworkWidget(BarWidget):
    id = "network"
    name = "Сеть — скорость загрузки и отдачи"
    icon = ""  # стрелка вниз
    interval = 1.0

    def __init__(self):
        self.last = None

    def update(self, detailed):
        now, io = time.monotonic(), psutil.net_io_counters()
        down = up = 0.0
        if self.last is not None:
            dt = max(now - self.last[0], 0.1)
            down, up = (io.bytes_recv - self.last[1].bytes_recv) / dt, (io.bytes_sent - self.last[1].bytes_sent) / dt
        self.last = (now, io)
        data = dict(down=down, up=up, recv=io.bytes_recv, sent=io.bytes_sent)
        if detailed:
            addrs = []
            stats = psutil.net_if_stats()
            for name, lst in psutil.net_if_addrs().items():
                if not (stats.get(name) and stats[name].isup):
                    continue
                for a in lst:
                    if a.family == 2 and not a.address.startswith(("127.", "169.254.")):
                        addrs.append((name, a.address))
            data["addrs"] = addrs
        return data

    def text(self, d):
        return f"{_size(d['down'])}/с"

    def panel(self, d):
        rows = [("title", "Сеть"),
                ("value", "Загрузка", f"{_size(d['down'])}/с"),
                ("value", "Отдача", f"{_size(d['up'])}/с"),
                ("sep",),
                ("value", "Получено с включения", _size(d["recv"])),
                ("value", "Отправлено", _size(d["sent"]))]
        if d.get("addrs"):
            rows += [("sep",), ("text", "Адреса")] + [("value", n, a) for n, a in d["addrs"]]
        rows.append(("button", "Параметры сети…", "ms-settings:network"))
        return rows


# Лимиты Claude берутся из status line Claude Code: он передаёт команде строки состояния поле rate_limits
# (окна five_hour и seven_day: used_percentage и resets_at) — https://code.claude.com/docs/en/statusline.
# Наш скрипт строки состояния сохраняет их в файл, виджет этот файл читает. Ни токенов, ни запросов в сеть.
CLAUDE_USAGE = CFG_DIR / "claude_usage.json"
CLAUDE_HOOK = CFG_DIR / "claude_statusline.ps1"
CLAUDE_HOOK_MARK = "MiniDock/claude_statusline.ps1"
CLAUDE_HOOK_PS1 = r"""# MiniDock: сохраняет лимиты Claude (rate_limits из status line Claude Code) для виджета верхней панели
$ErrorActionPreference = 'SilentlyContinue'
[Console]::InputEncoding = [Text.Encoding]::UTF8
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$d = [Console]::In.ReadToEnd() | ConvertFrom-Json
$r = $d.rate_limits
if ($r -and ($r.five_hour -or $r.seven_day)) {
    $out = Join-Path $env:APPDATA 'MiniDock\claude_usage.json'
    $tmp = "$out.$PID.tmp"
    $obj = [ordered]@{ updated = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); rate_limits = $r }
    [IO.File]::WriteAllText($tmp, ($obj | ConvertTo-Json -Depth 6 -Compress), (New-Object Text.UTF8Encoding $false))
    Move-Item -Force -LiteralPath $tmp -Destination $out
}
$parts = @()
if ($d.model.display_name) { $parts += $d.model.display_name }
if ($r.five_hour) { $parts += ('5ч {0}%' -f [math]::Round($r.five_hour.used_percentage)) }
if ($r.seven_day) { $parts += ('нед {0}%' -f [math]::Round($r.seven_day.used_percentage)) }
$parts -join ' · '
"""
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def _claude_settings():
    """(путь к settings.json Claude Code, его содержимое | None, если файл не читается как JSON)."""
    path = os.path.join(_claude_dir(), "settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            return path, json.load(f)
    except FileNotFoundError:
        return path, {}
    except (OSError, ValueError):
        return path, None


def _claude_hook_state():
    """"ours" — наш скрипт подключён, "other" — у Claude Code своя строка состояния, "none", "broken"."""
    _, s = _claude_settings()
    if s is None:
        return "broken"
    cmd = (s.get("statusLine") or {}).get("command", "") if isinstance(s.get("statusLine"), dict) else ""
    if not cmd:
        return "none"
    return "ours" if CLAUDE_HOOK_MARK in cmd.replace("\\", "/") else "other"


def _write_json(path, data):
    tmp = f"{path}.minidock.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def claude_connect():
    """Подключает скрипт MiniDock строкой состояния Claude Code (если своей там нет)."""
    path, s = _claude_settings()
    if s is None or _claude_hook_state() == "other":
        return
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    CLAUDE_HOOK.write_text(CLAUDE_HOOK_PS1, encoding="utf-8-sig")  # BOM — иначе PowerShell 5 не поймёт кириллицу
    os.makedirs(os.path.dirname(path), exist_ok=True)
    s["statusLine"] = {"type": "command", "command": "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
                                                     f"-File \"{str(CLAUDE_HOOK).replace(os.sep, '/')}\""}
    _write_json(path, s)


def claude_disconnect():
    path, s = _claude_settings()
    if s and _claude_hook_state() == "ours":
        del s["statusLine"]
        _write_json(path, s)


def _until(ts):
    left = int(ts - time.time())
    if left <= 0:
        return "сейчас"
    d, left = divmod(left, 86400)
    h, left = divmod(left, 3600)
    m = left // 60
    if d:
        return f"{d} д {h} ч"
    return f"{h} ч {m} мин" if h else f"{max(m, 1)} мин"


def _at(ts):
    t = datetime.datetime.fromtimestamp(ts)
    today = datetime.date.today()
    if t.date() == today:
        return t.strftime("%H:%M")
    if t.date() == today + datetime.timedelta(days=1):
        return "завтра " + t.strftime("%H:%M")
    return f"{WEEKDAYS[t.weekday()]} {t.day:02d}.{t.month:02d} {t:%H:%M}"


class ClaudeWidget(BarWidget):
    id = "claude"
    name = "Claude — лимиты на 5 часов и неделю"
    icon = ""  # спидометр
    interval = 10.0

    WINDOWS = (("five_hour", "5ч", "Сессия (5 часов)"), ("seven_day", "нед", "Неделя"))

    def update(self, detailed):
        hook = _claude_hook_state()
        data = dict(hook=hook, claude=os.path.isdir(_claude_dir()), windows={}, updated=None)
        try:
            with open(CLAUDE_USAGE, encoding="utf-8-sig") as f:
                saved = json.load(f)
        except FileNotFoundError:
            return data
        except (OSError, ValueError):
            data["bad_file"] = True
            return data
        now = time.time()
        limits = saved.get("rate_limits") or {}
        for key, _, _ in self.WINDOWS:
            w = limits.get(key)
            if not isinstance(w, dict) or w.get("used_percentage") is None:
                continue
            resets = float(w.get("resets_at") or 0)
            used = float(w["used_percentage"])
            if resets and resets <= now:
                used, resets = 0.0, 0.0  # окно уже сбросилось, а Claude Code с тех пор не запускали
            data["windows"][key] = (max(0.0, used), resets)
        data["updated"] = saved.get("updated")
        return data

    def text(self, d):
        parts = [f"{short} {d['windows'][k][0]:.0f}%" for k, short, _ in self.WINDOWS if k in d["windows"]]
        return " · ".join(parts) if parts else "Claude"

    def panel(self, d):
        rows = [("title", "Лимиты Claude")]
        for key, _, label in self.WINDOWS:
            if key not in d["windows"]:
                continue
            used, resets = d["windows"][key]
            rows.append(("bar", label, used / 100, f"{used:.0f}%"))
            rows.append(("value", f"Сброс через {_until(resets)}", _at(resets)) if resets
                        else ("value", "Сброс", "уже был"))
        if d["windows"]:
            age = time.time() - float(d["updated"] or 0)
            ago = "только что" if age < 90 else f"{_until(time.time() + age)} назад"
            rows += [("sep",), ("text", f"Данные из Claude Code, обновлены {ago}")]
        elif d.get("bad_file"):
            rows.append(("text", "Файл с лимитами повреждён — обновится при следующем ответе Claude"))
        elif not d["claude"]:
            rows.append(("text", "Claude Code на этом компьютере не найден"))
        hook = d["hook"]
        if hook == "ours" and not d["windows"]:
            rows += [("text", "Подключено. Цифры появятся после первого ответа"),
                     ("text", "Claude Code (нужна подписка Pro или Max)")]
        elif hook == "none" and d["claude"]:
            rows += [("text", "Лимиты передаёт строка состояния Claude Code."),
                     ("text", "MiniDock подключит к ней свой короткий скрипт."),
                     ("button", "Подключить к Claude Code", claude_connect)]
        elif hook == "other":
            rows += [("text", "У Claude Code уже настроена своя строка состояния —"),
                     ("text", "MiniDock не стал её заменять (statusLine в settings.json)")]
        elif hook == "broken":
            rows.append(("text", "Не удалось прочитать settings.json Claude Code"))
        rows.append(("button", "Лимиты на claude.ai…", "https://claude.ai/settings/usage"))
        if hook == "ours":
            rows.append(("button", "Отключить от Claude Code", claude_disconnect))
        return rows


BUILTIN = [SystemWidget, NetworkWidget, ClaudeWidget]


# -- свои виджеты из папки ------------------------------------------------------------------------

EXAMPLE = '''"""Пример своего виджета MiniDock. Скопируйте файл, поменяйте id и name — и он появится в настройках:
«Верхняя панель» → «Виджеты». Описание всех возможностей — в начале файла minidock/barwidgets.py
(https://github.com/ChickenRagoo/MiniDock/blob/main/minidock/barwidgets.py).

Файлы, имя которых начинается с «_», не загружаются — этот пример включается так: переименуйте его
в example_widget.py и перезапустите MiniDock."""
import datetime

from minidock.barwidgets import BarWidget


class DaysLeft(BarWidget):
    id = "example_days_left"
    name = "Пример: дней до Нового года"
    icon = ""        # символ шрифта Segoe MDL2 Assets, можно оставить пустым
    interval = 60     # раз в минуту

    def update(self, detailed):
        # здесь можно ходить в интернет, читать файлы, запускать программы — это фоновый поток
        today = datetime.date.today()
        new_year = datetime.date(today.year + 1, 1, 1)
        return {"left": (new_year - today).days, "year_part": today.timetuple().tm_yday / 365}

    def text(self, data):
        return f"{data['left']} дн."

    def panel(self, data):
        return [
            ("title", "До Нового года"),
            ("value", "Осталось дней", str(data["left"])),
            ("bar", "Год прошёл", data["year_part"], f"{data['year_part'] * 100:.0f}%"),
            ("button", "Открыть календарь", "outlookcal:"),
        ]
'''


def ensure_folder():
    try:
        WIDGETS_DIR.mkdir(parents=True, exist_ok=True)
        example = WIDGETS_DIR / "_example_widget.py"
        if not example.exists():
            example.write_text(EXAMPLE, encoding="utf-8")
    except OSError:
        pass


def load_plugins():
    """[(класс виджета, None) | (None, текст ошибки)] из папки виджетов."""
    out = []
    if not WIDGETS_DIR.is_dir():
        return out
    pkg = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if pkg not in sys.path:
        sys.path.insert(0, pkg)
    for path in sorted(WIDGETS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"minidock_widget_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            found = [v for v in vars(mod).values()
                     if isinstance(v, type) and issubclass(v, BarWidget) and v is not BarWidget and v.id]
            out += [(cls, None) for cls in found]
            if not found:
                out.append((None, f"{path.name}: нет класса-наследника BarWidget с заполненным id"))
        except Exception as e:
            out.append((None, f"{path.name}: {e}"))
    return out


# -- движок: опрос в фоне, данные — в интерфейс ---------------------------------------------------

class WidgetHost(QObject):
    """Все виджеты: какие есть, какие включены, их последние данные. Один фоновый поток опрашивает включённые."""
    changed = Signal()      # пришли новые данные — панели перерисовываются
    listed = Signal()       # изменился список включённых виджетов
    _data = Signal(str, object)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.classes = {}
        self.errors = []
        self.instances = {}
        self.data = {}
        self.detailed = set()  # у каких виджетов открыто окно — собираем подробности
        self.due = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self._data.connect(self._on_data)
        self.reload()
        threading.Thread(target=self._run, daemon=True, name="MiniDockWidgets").start()

    def reload(self):
        ensure_folder()
        classes = {c.id: c for c in BUILTIN}
        self.errors = []
        for cls, err in load_plugins():
            if cls is not None:
                classes.setdefault(cls.id, cls)
            else:
                self.errors.append(err)
        with self.lock:
            self.classes = classes
            self.instances = {k: v for k, v in self.instances.items() if k in classes}
        self.wake.set()
        self.listed.emit()

    def available(self):
        return [(c.id, c.name or c.id) for c in self.classes.values()]

    def enabled(self):
        return [i for i in self.cfg.get("bar_widgets", []) if i in self.classes]

    def set_enabled(self, wid, on):
        lst = [i for i in self.cfg.get("bar_widgets", []) if i != wid]
        if on:
            lst.append(wid)
        self.cfg["bar_widgets"] = lst
        self.due.pop(wid, None)
        self.wake.set()
        self.changed.emit()

    def instance(self, wid):
        with self.lock:
            inst = self.instances.get(wid)
            if inst is None and wid in self.classes:
                try:
                    inst = self.instances[wid] = self.classes[wid]()
                except Exception:
                    traceback.print_exc()
                    return None
            return inst

    def set_detailed(self, wid, on):
        (self.detailed.add if on else self.detailed.discard)(wid)
        if on:
            self.due.pop(wid, None)  # окно открыли — подробности сразу
            self.wake.set()

    def _run(self):
        while True:
            now = time.monotonic()
            nearest = 5.0
            for wid in self.enabled():
                inst = self.instance(wid)
                if inst is None:
                    continue
                if self.due.get(wid, 0) <= now:
                    try:
                        data = inst.update(wid in self.detailed)
                    except Exception as e:
                        data = {"__error__": str(e)[:200]}
                    self._data.emit(wid, data)
                    self.due[wid] = time.monotonic() + max(float(inst.interval), 0.5)
                nearest = min(nearest, self.due[wid] - time.monotonic())
            self.wake.wait(max(nearest, 0.05))
            self.wake.clear()

    def _on_data(self, wid, data):
        self.data[wid] = data
        self.changed.emit()

    # -- то, что рисует панель
    def bar_text(self, wid):
        inst, data = self.instances.get(wid), self.data.get(wid)
        if inst is None or data is None:
            return "…"
        if isinstance(data, dict) and "__error__" in data:
            return "⚠"
        try:
            return str(inst.text(data))
        except Exception:
            return "⚠"

    def panel_rows(self, wid):
        inst, data = self.instances.get(wid), self.data.get(wid)
        name = self.classes[wid].name if wid in self.classes else wid
        if inst is None or data is None:
            return [("title", name), ("text", "Загружаем…")]
        if isinstance(data, dict) and "__error__" in data:
            return [("title", name), ("text", "Ошибка: " + data["__error__"])]
        try:
            return list(inst.panel(data))
        except Exception as e:
            return [("title", name), ("text", f"Ошибка: {e}")]


# -- окно виджета ---------------------------------------------------------------------------------

ROW_H = {"title": 26, "text": 20, "value": 24, "bar": 40, "sep": 13, "button": 34}


class PanelView(QWidget):
    """Строки окна виджета, нарисованные вручную: новые данные меняются на месте, полосы плавно доезжают."""
    def __init__(self, flyout):
        super().__init__()
        self.flyout = flyout
        self.rows = []
        self.shown = {}   # номер строки-полосы -> показанная доля (анимируется)
        self.hover = None
        self.buttons = []
        self.f_title = QFont("Segoe UI", 10, QFont.Bold)
        self.f_text = QFont("Segoe UI", 8)
        self.f_reg = QFont("Segoe UI", 9)
        self.setMouseTracking(True)
        self.setCursor(Qt.ArrowCursor)

    def height_for(self, rows):
        return int(sum(ROW_H.get(r[0], 0) for r in rows if r))

    def set_rows(self, rows):
        rows = [r for r in rows if r and r[0] in ROW_H]
        old = {i: v for i, v in self.shown.items()}
        self.rows = rows
        targets = {i: max(0.0, min(1.0, float(r[2] or 0))) for i, r in enumerate(rows) if r[0] == "bar"}
        start = {i: old.get(i, 0.0) for i in targets}

        def step(k):
            self.shown = {i: start[i] + (t - start[i]) * k for i, t in targets.items()}
            self.update()
        try:
            if getattr(self, "_anim", None) is not None:
                self._anim.stop()  # прошлая анимация ещё идёт — новая продолжит с текущего места
        except RuntimeError:
            pass
        self._anim = None
        if any(abs(start[i] - t) > 0.001 for i, t in targets.items()):
            self._anim = animate(self, 380, step, lambda: setattr(self, "_anim", None))
        else:
            self.shown = dict(targets)
        self.setFixedHeight(self.height_for(rows))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        y = 0.0
        self.buttons = []
        for i, r in enumerate(self.rows):
            kind, h = r[0], ROW_H[r[0]]
            if kind == "title":
                p.setFont(self.f_title)
                p.setPen(QColor(245, 245, 248))
                p.drawText(QRectF(0, y, w, h), Qt.AlignLeft | Qt.AlignVCenter, self.elide(self.f_title, r[1], w))
            elif kind == "text":
                p.setFont(self.f_text)
                p.setPen(QColor(255, 255, 255, 140))
                p.drawText(QRectF(0, y, w, h), Qt.AlignLeft | Qt.AlignVCenter, self.elide(self.f_text, r[1], w))
            elif kind == "value":
                p.setFont(self.f_reg)
                value = str(r[2])
                vw = min(QFontMetricsF(self.f_reg).horizontalAdvance(value) + 2, w * 0.6)
                p.setPen(QColor(255, 255, 255, 170))
                p.drawText(QRectF(0, y, w - vw - 8, h), Qt.AlignLeft | Qt.AlignVCenter,
                           self.elide(self.f_reg, str(r[1]), w - vw - 8))
                p.setPen(QColor(245, 245, 248))
                p.drawText(QRectF(w - vw, y, vw, h), Qt.AlignRight | Qt.AlignVCenter, self.elide(self.f_reg, value, vw))
            elif kind == "bar":
                p.setFont(self.f_reg)
                p.setPen(QColor(255, 255, 255, 190))
                p.drawText(QRectF(0, y + 2, w * 0.5, 18), Qt.AlignLeft | Qt.AlignVCenter, str(r[1]))
                p.setPen(QColor(245, 245, 248))
                p.drawText(QRectF(w * 0.35, y + 2, w * 0.65, 18), Qt.AlignRight | Qt.AlignVCenter,
                           self.elide(self.f_reg, str(r[3]) if len(r) > 3 else "", w * 0.65))
                track = QRectF(0, y + 24, w, 8)
                p.fillPath(rounded_path(track, 4), QColor(255, 255, 255, 30))
                frac = self.shown.get(i, 0.0)
                if frac > 0.005:
                    target = float(r[2] or 0)
                    color = QColor(255, 95, 87) if target >= 0.9 else QColor(255, 189, 46) if target >= 0.75 \
                        else QColor(10, 132, 255)
                    p.fillPath(rounded_path(QRectF(0, y + 24, max(8.0, w * frac), 8), 4), color)
            elif kind == "sep":
                p.setPen(QColor(255, 255, 255, 30))
                p.drawLine(0, int(y + h / 2), w, int(y + h / 2))
            elif kind == "button":
                rect = QRectF(0, y + 4, w, h - 6)
                self.buttons.append((rect, r[2]))
                p.fillPath(rounded_path(rect, 7), QColor(255, 255, 255, 40 if self.hover == len(self.buttons) - 1 else 16))
                p.setFont(self.f_reg)
                p.setPen(QColor(245, 245, 248))
                p.drawText(rect.adjusted(10, 0, -10, 0), Qt.AlignLeft | Qt.AlignVCenter, str(r[1]))
            y += h

    @staticmethod
    def elide(font, text, width):
        return QFontMetricsF(font).elidedText(str(text), Qt.ElideRight, width)

    def button_at(self, pos):
        for i, (rect, _) in enumerate(self.buttons):
            if rect.contains(pos):
                return i
        return None

    def mouseMoveEvent(self, e):
        b = self.button_at(e.position())
        if b != self.hover:
            self.hover = b
            self.setCursor(Qt.PointingHandCursor if b is not None else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def mouseReleaseEvent(self, e):
        b = self.button_at(e.position())
        if b is None or e.button() != Qt.LeftButton:
            return
        action = self.buttons[b][1]
        self.flyout.close()
        try:
            if callable(action):
                action()
            elif action:
                os.startfile(str(action))
        except Exception:
            traceback.print_exc()


class WidgetFlyout(Flyout):
    """Окно виджета под его надписью на панели. Пока оно открыто, виджет собирает подробности,
    а строки обновляются на месте."""
    WIDTH = 320

    def __init__(self, bar, host, wid):
        super().__init__(bar)
        self.host, self.wid = host, wid
        self.view = None
        host.changed.connect(self.refresh)

    def build(self):
        self.view = PanelView(self)
        self.lay.addWidget(self.view)
        self.view.set_rows(self.host.panel_rows(self.wid))

    def refresh(self):
        if not self.isVisible() or self.view is None:
            return
        before = self.view.height()
        self.view.set_rows(self.host.panel_rows(self.wid))
        if self.view.height() != before:
            self.refit()

    def showEvent(self, e):
        self.host.set_detailed(self.wid, True)
        super().showEvent(e)

    def hideEvent(self, e):
        self.host.set_detailed(self.wid, False)
        super().hideEvent(e)

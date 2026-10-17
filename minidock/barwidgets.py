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
    paint_tile(painter, rect, data) — необязательно: как виджет выглядит в доке (квадрат размером с иконку);
                    без него в доке — плитка со значком и надписью text(data).
Виджеты добавляются и на панель (настройки), и в док (правый клик по доку → «Добавить виджет»).
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
from PySide6.QtCore import QByteArray, QObject, QPointF, QRectF, Qt, QTime, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox,
                               QTimeEdit, QVBoxLayout, QWidget)

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

    paint_icon = None  # необязательно: функция (painter, QRectF) — свой значок вместо символа icon
    paint_tile = None  # необязательно: (painter, QRectF, данные) — как виджет выглядит в доке (размер иконки)


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

    def paint_tile(self, p, rect, d):
        """В доке: процессор — внешнее кольцо, память — внутреннее, в центре процент процессора."""
        tile_base(p, rect)
        s = rect.width()
        w = max(2.0, s * 0.07)
        outer = rect.adjusted(s * 0.14, s * 0.14, -s * 0.14, -s * 0.14)
        inner = outer.adjusted(w * 1.6, w * 1.6, -w * 1.6, -w * 1.6)
        tile_ring(p, outer, d["cpu"] / 100, _level_color(d["cpu"] / 100), w)
        tile_ring(p, inner, d["mem"] / 100, _level_color(d["mem"] / 100, QColor(48, 209, 88)), w)
        f = QFont("Segoe UI Semibold")
        f.setPixelSize(max(6, int(s * 0.17)))
        p.setFont(f)
        p.setPen(QColor(245, 245, 248))
        p.drawText(inner, Qt.AlignCenter, f"{d['cpu']:.0f}%")

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
# Калибровка по claude.ai/settings/usage: размер лимитов в токенах и начало недели (см. ClaudeWidget.calibrate)
CLAUDE_CALIB = CFG_DIR / "claude_calib.json"


def load_calib():
    try:
        with open(CLAUDE_CALIB, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_calib(data):
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(str(CLAUDE_CALIB), data)
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


def _tokens(n):
    if n >= 1e9:
        return f"{n / 1e9:.1f} млрд".replace(".", ",")
    if n >= 1e6:
        return f"{n / 1e6:.1f} млн".replace(".", ",")
    if n >= 1e3:
        return f"{n / 1e3:.0f} тыс"
    return str(int(n))


def _parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


CLAUDE_ORANGE = QColor(217, 119, 87)
# Логотип Claude (контур из Simple Icons, CC0), закрашенный фирменным оранжевым
CLAUDE_SVG = (b'<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path fill="#D97757" d="'
              b"m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z"
              b'"/></svg>')
_logo_cache = {}


def paint_claude_logo(p, rect):
    """Логотип Claude в rect: рисуется из SVG один раз на размер и дальше берётся из кэша."""
    dpr = p.device().devicePixelRatioF() if p.device() else 1.0
    side = max(1, round(min(rect.width(), rect.height()) * dpr))
    pix = _logo_cache.get(side)
    if pix is None:
        from PySide6.QtSvg import QSvgRenderer
        img = QImage(side, side, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        q = QPainter(img)
        q.setRenderHint(QPainter.Antialiasing)
        QSvgRenderer(QByteArray(CLAUDE_SVG)).render(q, QRectF(0, 0, side, side))
        q.end()
        pix = _logo_cache[side] = QPixmap.fromImage(img)
        pix.setDevicePixelRatio(dpr)
    s = side / dpr
    p.drawPixmap(QPointF(rect.center().x() - s / 2, rect.center().y() - s / 2), pix)


class ClaudeLog:
    """Расход Claude Code по его локальным журналам (~/.claude/projects/**/*.jsonl): у каждого ответа модели
    там записано, сколько токенов ушло. Первый раз читаем журналы за 8 дней целиком, дальше — только
    дописанное. Отказы «лимит исчерпан» (quotaLimits) дают точное время сброса и калибруют оценку."""
    KEEP = 31 * 86400  # месяц — чтобы было с чем сравнивать неделю
    BLOCK = 5 * 3600

    def __init__(self):
        self.offsets = {}   # путь -> сколько байт уже прочитано
        self.msgs = {}      # id ответа -> (время, ввод, вывод, запись в кэш, чтение из кэша)
        self.limits = []    # [(время отказа, тип окна, время сброса)]
        self.lock = threading.Lock()  # журнал читает фоновый поток виджетов, калибровка — поток интерфейса

    def scan(self):
        with self.lock:
            self._scan()

    def _scan(self):
        root = os.path.join(_claude_dir(), "projects")
        horizon = time.time() - self.KEEP
        for dirpath, _, files in os.walk(root):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if st.st_mtime < horizon:
                    continue
                start = self.offsets.get(path, 0)
                if st.st_size < start:  # файл переписали — читаем заново
                    start = 0
                if st.st_size == start:
                    continue
                self.offsets[path] = self._read(path, start)
                time.sleep(0)  # отдать GIL интерфейсу между файлами
        self.msgs = {k: v for k, v in self.msgs.items() if v[0] >= horizon}
        self.limits = [x for x in self.limits if x[0] >= horizon]

    def _read(self, path, start):
        with open(path, "rb") as f:
            f.seek(start)
            pos = start
            for line in f:
                if not line.endswith(b"\n"):
                    break  # строку ещё дописывают — дочитаем в следующий раз
                pos += len(line)
                if b'"usage"' not in line and b"quotaLimits" not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                ts = _parse_ts(d.get("timestamp"))
                if ts is None:
                    continue
                q = d.get("quotaLimits")
                if isinstance(q, dict) and q.get("status") == "rejected" and q.get("resetsAt"):
                    self.limits.append((ts, q.get("rateLimitType") or "", float(q["resetsAt"])))
                msg = d.get("message")
                u = msg.get("usage") if isinstance(msg, dict) else None
                if not isinstance(u, dict) or d.get("isApiErrorMessage"):
                    continue
                key = msg.get("id") or d.get("requestId") or d.get("uuid")
                self.msgs[key] = (ts, u.get("input_tokens") or 0, u.get("output_tokens") or 0,
                                  u.get("cache_creation_input_tokens") or 0, u.get("cache_read_input_tokens") or 0)
            return pos

    @staticmethod
    def weight(m):
        """«Тяжёлые» токены: ввод, вывод и запись в кэш. Чтение из кэша почти не тратит лимит — его не считаем."""
        return m[1] + m[2] + m[3]

    def blocks(self):
        """Окна по 5 часов, как их считает подписка: окно начинается с первого запроса после конца
        предыдущего. [(начало, конец, [ответы])]"""
        out = []
        for m in sorted(self.msgs.values()):
            if not out or m[0] >= out[-1][1]:
                start = m[0]
                out.append((start, start + self.BLOCK, []))
            out[-1][2].append(m)
        return out

    def tokens_between(self, start, end):
        with self.lock:
            return sum(self.weight(m) for m in self.msgs.values() if start <= m[0] <= end)

    @staticmethod
    def week_start(anchor, now):
        """Начало текущей недели лимитов: от известного сброса шагами по 7 дней."""
        week = 7 * 86400
        return anchor + ((now - anchor) // week) * week

    def summary(self, calib=None):
        with self.lock:
            return self._summary(calib or {})

    def _summary(self, calib):
        now = time.time()
        blocks = self.blocks()
        cur = blocks[-1] if blocks and blocks[-1][1] > now else None
        reset5 = float(calib.get("five_reset") or 0)
        if reset5 > now:  # время сброса сессии известно с claude.ai — окно ровно по нему
            cur = (reset5 - self.BLOCK, reset5, [m for m in self.msgs.values() if reset5 - self.BLOCK <= m[0] <= now])
        # точное время сброса — из последнего отказа «лимит исчерпан», если он ещё в силе
        hit5 = next((r for t, k, r in reversed(self.limits) if k == "five_hour" and r > now), None)
        hit7 = next((r for t, k, r in reversed(self.limits) if k == "seven_day" and r > now), None)
        # оценка лимита на 5 часов: сколько было потрачено в окне, которое упёрлось в лимит
        cap = None
        for t, kind, resets in reversed(self.limits):
            if kind != "five_hour":
                continue
            used = sum(self.weight(m) for m in self.msgs.values() if resets - self.BLOCK <= m[0] <= t)
            if used:
                cap = used
                break
        done = [b for b in blocks if b is not cur]
        record = max((sum(self.weight(m) for m in b[2]) for b in done), default=0)
        anchor = float(calib.get("week_anchor") or 0)
        week_from = self.week_start(anchor, now) if anchor else now - 7 * 86400
        week = [m for m in self.msgs.values() if m[0] >= week_from]
        # самая «тяжёлая» неделя: суммы по 7 дней подряд, закончившиеся до сегодняшнего дня
        day = 86400
        today = now - now % day
        days = {}
        for m in self.msgs.values():
            d = m[0] - m[0] % day
            days[d] = days.get(d, 0) + self.weight(m)
        week_record = 0
        for end in range(int(today), int(today - 24 * day), -day):
            week_record = max(week_record, sum(days.get(end - k * day, 0) for k in range(1, 8)))
        cur_msgs = cur[2] if cur else []
        return dict(
            five=sum(self.weight(m) for m in cur_msgs),
            five_out=sum(m[2] for m in cur_msgs),
            five_count=len(cur_msgs),
            five_reset=hit5 or (cur[1] if cur else None),
            five_hit=hit5 is not None,
            cap=cap, record=record, week_record=week_record,
            week=sum(self.weight(m) for m in week),
            week_reads=sum(m[4] for m in week),
            week_count=len(week),
            week_reset=hit7 or (week_from + 7 * 86400 if anchor else None), week_hit=hit7 is not None,
        )


class ClaudeWidget(BarWidget):
    id = "claude"
    name = "Claude — расход за 5 часов и неделю"
    icon = ""  # значок — логотип Claude, рисуется в paint_icon
    interval = 15.0

    WINDOWS = (("five_hour", "5ч", "Сессия (5 часов)"), ("seven_day", "нед", "Неделя"))

    def __init__(self):
        self.log = ClaudeLog()

    def exact(self):
        """Точные проценты из строки состояния терминального claude, если они свежие (не старше 15 мин)."""
        try:
            with open(CLAUDE_USAGE, encoding="utf-8-sig") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return {}
        if time.time() - float(saved.get("updated") or 0) > 900:
            return {}
        out = {}
        for key, _, _ in self.WINDOWS:
            w = (saved.get("rate_limits") or {}).get(key)
            if isinstance(w, dict) and w.get("used_percentage") is not None:
                resets = float(w.get("resets_at") or 0)
                if not resets or resets > time.time():
                    out[key] = (float(w["used_percentage"]), resets)
        return out

    def update(self, detailed):
        if not os.path.isdir(_claude_dir()):
            return dict(claude=False)
        self.log.scan()
        calib = load_calib()
        return dict(claude=True, est=self.log.summary(calib), exact=self.exact(), hook=_claude_hook_state(),
                    calib=calib)

    @staticmethod
    def paint_icon(p, rect):
        paint_claude_logo(p, rect)

    def calibrate(self):
        """Окно калибровки: проценты и время сброса с claude.ai/settings/usage -> размер лимитов в токенах."""
        dlg = CalibrateDialog(load_calib())
        if dlg.exec() != QDialog.Accepted:
            return
        now = time.time()
        five_pct, five_reset, week_pct, week_reset = dlg.values()
        calib = load_calib()
        if five_pct > 0:
            used = self.log.tokens_between(five_reset - ClaudeLog.BLOCK, now)
            if used:
                calib["five_cap"] = used / (five_pct / 100)
        calib["five_reset"] = five_reset
        week_from = week_reset - 7 * 86400
        if week_pct > 0:
            used = self.log.tokens_between(week_from, now)
            if used:
                calib["week_cap"] = used / (week_pct / 100)
        calib.update(week_anchor=week_from, at=now, five_pct=five_pct, week_pct=week_pct)
        save_calib(calib)

    def gauges(self, d):
        """[(доля 0..1 | None, точно ли)] для окна 5 часов и недели. Точные — из строки состояния claude,
        иначе оценка: от расхода до упора в лимит или от самого большого окна / недели за месяц."""
        ex, e, cal = d["exact"], d["est"], d.get("calib") or {}
        if "five_hour" in ex:
            five = (ex["five_hour"][0] / 100, True)
        elif e["five_hit"]:
            five = (1.0, True)
        elif cal.get("five_cap"):
            five = (e["five"] / cal["five_cap"], True)
        elif e["cap"] or e["record"]:
            # база — больший из «расхода до упора в лимит» и «самого большого окна за месяц»
            five = (e["five"] / max(e["cap"] or 0, e["record"], e["five"]), False)
        else:
            five = (None, False)
        if "seven_day" in ex:
            week = (ex["seven_day"][0] / 100, True)
        elif e["week_hit"]:
            week = (1.0, True)
        elif cal.get("week_cap"):
            week = (e["week"] / cal["week_cap"], True)
        elif e["week_record"]:
            week = (e["week"] / max(e["week_record"], e["week"]), False)
        else:
            week = (None, False)
        return five, week

    @staticmethod
    def pct(g):
        return "—" if g[0] is None else f"{min(g[0], 1.0) * 100:.0f}%"

    def paint_tile(self, p, rect, d):
        """В доке: сессия — внешнее кольцо, неделя — внутреннее, логотип в центре."""
        tile_base(p, rect)
        s = rect.width()
        w = max(2.0, s * 0.07)
        outer = rect.adjusted(s * 0.13, s * 0.13, -s * 0.13, -s * 0.13)
        inner = outer.adjusted(w * 1.6, w * 1.6, -w * 1.6, -w * 1.6)
        five, week = self.gauges(d) if d.get("claude") else ((None, False), (None, False))
        tile_ring(p, outer, five[0], _level_color(five[0], CLAUDE_ORANGE), w)
        tile_ring(p, inner, week[0], _level_color(week[0], QColor(240, 170, 140)), w)
        c = inner.center()
        paint_claude_logo(p, QRectF(c.x() - s * 0.13, c.y() - s * 0.13, s * 0.26, s * 0.26))

    def text(self, d):
        if not d.get("claude"):
            return "—"
        five, week = self.gauges(d)
        return f"{self.pct(five)} · {self.pct(week)}"

    def panel(self, d):
        if not d.get("claude"):
            return [("title", "Claude"), ("text", "Claude Code на этом компьютере не найден"),
                    ("button", "Лимиты на claude.ai", "https://claude.ai/settings/usage")]
        ex, e = d["exact"], d["est"]
        five, week = self.gauges(d)
        resets = ex["five_hour"][1] if "five_hour" in ex else e["five_reset"]
        week_reset = ex["seven_day"][1] if "seven_day" in ex else e["week_reset"]
        rows = [("title", "Claude"),
                ("rings", [("Сессия · 5 часов", five[0], self.pct(five),
                            f"сброс в {_at(resets)}" if resets else "окно не начато"),
                           ("Неделя", week[0], self.pct(week),
                            f"сброс {_at(week_reset)}" if week_reset else f"{_tokens(e['week'])} токенов")]),
                ("stats", [(_tokens(e["five"]), "за сессию"),
                           (_until(resets) if resets else "—", "до сброса"),
                           (str(e["five_count"]), "ответов")])]
        cal = d.get("calib") or {}
        if ex:
            note = "Точные данные из Claude Code"
        elif cal.get("five_cap") or cal.get("week_cap"):
            when = datetime.datetime.fromtimestamp(float(cal.get("at") or time.time()))
            note = f"По калибровке с claude.ai от {when.day} {MONTHS_GEN[when.month - 1]}"
        else:
            note = "Оценка по вашему расходу за месяц — откалибруйте"
        rows += [("text", note), ("button", "Откалибровать по claude.ai…", self.calibrate),
                 ("button", "Открыть лимиты на claude.ai", "https://claude.ai/settings/usage")]
        return rows


MONTHS_GEN = ("янв.", "февр.", "марта", "апр.", "мая", "июня", "июля", "авг.", "сент.", "окт.", "нояб.", "дек.")
DAYS_FULL = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


class CalibrateDialog(QDialog):
    """Калибровка виджета Claude: два процента и два времени сброса со страницы claude.ai/settings/usage."""
    QSS = """
    QDialog { background: #222228; }
    QLabel { color: #f2f2f5; font: 9pt 'Segoe UI'; }
    QLabel#h { font: bold 11pt 'Segoe UI'; }
    QLabel#dim { color: rgba(255, 255, 255, 140); font: 8pt 'Segoe UI'; }
    QSpinBox, QComboBox, QTimeEdit { background: rgba(255, 255, 255, 18); color: #f2f2f5; border: none;
        border-radius: 6px; padding: 5px 8px; font: 10pt 'Segoe UI'; min-width: 70px; }
    QComboBox QAbstractItemView { background: #2a2a31; color: #f2f2f5; selection-background-color: #0a84ff; }
    QPushButton { background: rgba(255, 255, 255, 22); color: #f2f2f5; border: none; border-radius: 7px;
        padding: 7px 16px; font: 9pt 'Segoe UI'; }
    QPushButton:hover { background: rgba(255, 255, 255, 40); }
    QPushButton#ok { background: #d97757; }
    QPushButton#ok:hover { background: #e2896b; }
    """

    def __init__(self, calib):
        super().__init__(None, Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint)
        self.setWindowTitle("Калибровка лимитов Claude")
        self.setStyleSheet(self.QSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        h = QLabel("Откройте claude.ai → Настройки → Использование")
        h.setObjectName("h")
        lay.addWidget(h)
        hint = QLabel("и перенесите оттуда, сколько процентов использовано и когда сброс.")
        hint.setObjectName("dim")
        lay.addWidget(hint)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        now = datetime.datetime.now()
        self.five = QSpinBox()
        self.five.setRange(0, 100)
        self.five.setSuffix(" %")
        self.five_time = QTimeEdit()
        self.five_time.setDisplayFormat("HH:mm")
        old = float(calib.get("five_reset") or 0)
        t = datetime.datetime.fromtimestamp(old) if old > time.time() else now + datetime.timedelta(hours=5)
        self.five_time.setTime(QTime(t.hour, t.minute))
        grid.addWidget(QLabel("Текущая сессия"), 0, 0)
        grid.addWidget(self.five, 0, 1)
        grid.addWidget(QLabel("сброс в"), 0, 2)
        grid.addWidget(self.five_time, 0, 3)
        self.week = QSpinBox()
        self.week.setRange(0, 100)
        self.week.setSuffix(" %")
        self.week_day = QComboBox()
        self.week_day.addItems(DAYS_FULL)
        self.week_time = QTimeEdit()
        self.week_time.setDisplayFormat("HH:mm")
        anchor = float(calib.get("week_anchor") or 0)
        if anchor:
            a = datetime.datetime.fromtimestamp(anchor)
            self.week_day.setCurrentIndex(a.weekday())
            self.week_time.setTime(QTime(a.hour, a.minute))
        else:
            self.week_day.setCurrentIndex(now.weekday())
            self.week_time.setTime(QTime(12, 0))
        grid.addWidget(QLabel("Эта неделя"), 1, 0)
        grid.addWidget(self.week, 1, 1)
        grid.addWidget(QLabel("сброс"), 1, 2)
        week_box = QHBoxLayout()
        week_box.addWidget(self.week_day)
        week_box.addWidget(self.week_time)
        grid.addLayout(week_box, 1, 3)
        lay.addLayout(grid)
        note = QLabel("Чем больше процент, тем точнее калибровка. Лимит общий с чатами на claude.ai —\n"
                      "их расход MiniDock не видит.")
        note.setObjectName("dim")
        lay.addWidget(note)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Откалибровать")
        ok.setObjectName("ok")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        lay.addLayout(buttons)

    def values(self):
        """(% сессии, время сброса сессии, % недели, время ближайшего сброса недели)."""
        now = datetime.datetime.now()
        t = self.five_time.time()
        five = now.replace(hour=t.hour(), minute=t.minute(), second=0, microsecond=0)
        if five <= now:
            five += datetime.timedelta(days=1)
        t = self.week_time.time()
        week = now.replace(hour=t.hour(), minute=t.minute(), second=0, microsecond=0)
        week += datetime.timedelta(days=(self.week_day.currentIndex() - now.weekday()) % 7)
        if week <= now:
            week += datetime.timedelta(days=7)
        return self.five.value(), five.timestamp(), self.week.value(), week.timestamp()


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

    def active(self):
        """Что опрашивать: виджеты верхней панели и виджеты в доке."""
        ids = self.enabled()
        for e in self.cfg.get("pinned") or []:
            if isinstance(e, dict) and e.get("widget") in self.classes and e["widget"] not in ids:
                ids.append(e["widget"])
        return ids

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
            for wid in self.active():
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

    def paint_tile(self, wid, p, rect):
        """Виджет в доке: свой рисунок виджета или плитка «значок + надпись»."""
        inst, data = self.instance(wid), self.data.get(wid)
        cls = self.classes.get(wid)
        ok = data is not None and not (isinstance(data, dict) and "__error__" in data)
        if inst is not None and ok and getattr(inst, "paint_tile", None):
            try:
                p.save()
                inst.paint_tile(p, rect, data)
                return
            except Exception:
                traceback.print_exc()
            finally:
                p.restore()
        p.save()
        paint_default_tile(p, rect, cls, self.bar_text(wid))
        p.restore()

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

def tile_base(p, rect):
    """Подложка виджета в доке — скруглённый квадрат, как у иконок."""
    p.setPen(Qt.NoPen)
    p.fillPath(rounded_path(rect, rect.width() * 0.22), QColor(38, 38, 46, 235))
    p.setPen(QPen(QColor(255, 255, 255, 30), 1))
    p.setBrush(Qt.NoBrush)
    p.drawPath(rounded_path(rect, rect.width() * 0.22))


def tile_ring(p, rect, frac, color, width):
    pen = QPen(QColor(255, 255, 255, 30), width)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(rect)
    if frac is not None and frac > 0.004:
        pen.setColor(color)
        p.setPen(pen)
        p.drawArc(rect, 90 * 16, -int(360 * 16 * min(frac, 1.0)))


def paint_default_tile(p, rect, cls, text):
    tile_base(p, rect)
    s = rect.width()
    icon = QRectF(rect.center().x() - s * 0.17, rect.top() + s * 0.16, s * 0.34, s * 0.34)
    if cls is not None and cls.paint_icon:
        cls.paint_icon(p, icon)
    elif cls is not None and cls.icon:
        f = QFont("Segoe MDL2 Assets")
        f.setPixelSize(max(6, int(s * 0.28)))
        p.setFont(f)
        p.setPen(QColor(235, 235, 240))
        p.drawText(icon, Qt.AlignCenter, cls.icon)
    f = QFont("Segoe UI Semibold")
    f.setPixelSize(max(6, int(s * 0.17)))
    p.setFont(f)
    p.setPen(QColor(245, 245, 248))
    box = QRectF(rect.left() + s * 0.06, rect.top() + s * 0.56, s * 0.88, s * 0.3)
    p.drawText(box, Qt.AlignCenter, QFontMetricsF(f).elidedText(text, Qt.ElideRight, box.width()))


ROW_H = {"title": 26, "text": 20, "value": 24, "bar": 40, "sep": 13, "button": 34, "rings": 156, "stats": 62}


def _level_color(frac, base=QColor(10, 132, 255)):
    """Цвет заполнения: обычный, жёлтый от 75%, красный от 90%."""
    if frac is not None and frac >= 0.9:
        return QColor(255, 95, 87)
    if frac is not None and frac >= 0.75:
        return QColor(255, 189, 46)
    return base


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
        for i, r in enumerate(rows):
            if r[0] == "rings":
                targets.update({(i, k): max(0.0, min(1.0, float(g[1] or 0))) for k, g in enumerate(r[1])})
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
            elif kind == "rings":
                self.paint_rings(p, i, r[1], y, w)
            elif kind == "stats":
                self.paint_stats(p, r[1], y, w)
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

    def paint_rings(self, p, i, rings, y, w):
        """Круговые шкалы рядом: процент в центре, подпись и мелкий текст под кольцом."""
        n = max(len(rings), 1)
        cell = w / n
        d = 96.0
        for k, (label, frac, center, caption) in enumerate(rings):
            cx = cell * k + cell / 2
            box = QRectF(cx - d / 2, y + 8, d, d)
            pen = QPen(QColor(255, 255, 255, 28), 9)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(box)
            shown = self.shown.get((i, k), 0.0)
            if frac is not None and shown > 0.004:
                pen.setColor(_level_color(frac, CLAUDE_ORANGE))
                p.setPen(pen)
                p.drawArc(box, 90 * 16, -int(360 * 16 * shown))
            p.setFont(QFont("Segoe UI Semibold", 17))
            p.setPen(QColor(250, 250, 252))
            p.drawText(box, Qt.AlignCenter, center)
            p.setFont(self.f_reg)
            p.setPen(QColor(255, 255, 255, 215))
            p.drawText(QRectF(cell * k, y + 112, cell, 18), Qt.AlignCenter, self.elide(self.f_reg, label, cell - 8))
            p.setFont(self.f_text)
            p.setPen(QColor(255, 255, 255, 130))
            p.drawText(QRectF(cell * k, y + 130, cell, 16), Qt.AlignCenter, self.elide(self.f_text, caption, cell - 8))

    def paint_stats(self, p, stats, y, w):
        """Ряд плиток «число + подпись»."""
        n = max(len(stats), 1)
        gap = 8.0
        tw = (w - gap * (n - 1)) / n
        for k, (value, label) in enumerate(stats):
            r = QRectF(k * (tw + gap), y + 6, tw, 50)
            p.fillPath(rounded_path(r, 9), QColor(255, 255, 255, 14))
            p.setFont(QFont("Segoe UI Semibold", 11))
            p.setPen(QColor(245, 245, 248))
            p.drawText(QRectF(r.left(), r.top() + 6, r.width(), 22), Qt.AlignCenter,
                       self.elide(p.font(), value, r.width() - 8))
            p.setFont(self.f_text)
            p.setPen(QColor(255, 255, 255, 130))
            p.drawText(QRectF(r.left(), r.top() + 28, r.width(), 16), Qt.AlignCenter,
                       self.elide(self.f_text, label, r.width() - 8))

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

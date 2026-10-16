"""Всплывающие окна верхней панели: трей, Wi‑Fi, звук, календарь, раскладка, аккумулятор."""
import datetime
import os
import re
import subprocess
import threading

import psutil
import win32con
import win32gui
import win32process
from PySide6.QtCore import QDate, QLocale, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap, QTextCharFormat
from PySide6.QtWidgets import (QApplication, QCalendarWidget, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSlider,
                               QTableView, QToolButton, QVBoxLayout, QWidget)

from .ui import fade_in, make_glass, prepare_glass, rounded_path
from .winapi import (enable_blur, force_foreground, kernel32, keyboard_layouts, lang_name, press_keys, round_window,
                     tray_click, tray_icons, try_foreground, user32)

try:  # громкость (pycaw); импорт сразу при старте — поздний импорт comtypes бывает нестабилен
    from pycaw.pycaw import AudioUtilities
except Exception:
    AudioUtilities = None

DAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]

FLYOUT_QSS = """
QWidget { color: #f2f2f5; font: 9pt 'Segoe UI'; background: transparent; }
QLabel#title { font: bold 10pt 'Segoe UI'; }
QLabel#big { font: 30pt 'Segoe UI Light'; }
QLabel#dim { color: rgba(255, 255, 255, 140); }
QPushButton { background: rgba(255, 255, 255, 16); border: none; border-radius: 7px; padding: 7px 10px;
              text-align: left; }
QPushButton:hover { background: rgba(255, 255, 255, 38); }
QPushButton:pressed { background: rgba(255, 255, 255, 60); }
QPushButton#current { background: rgba(10, 132, 255, 200); }
QPushButton#round { font: 12pt 'Segoe MDL2 Assets'; border-radius: 14px; padding: 0; min-width: 28px;
                    max-width: 28px; min-height: 28px; max-height: 28px; text-align: center; }
QPushButton#round:checked { background: rgba(10, 132, 255, 220); }
QSlider::groove:horizontal { height: 22px; background: rgba(255, 255, 255, 30); border-radius: 11px; }
QSlider::sub-page:horizontal { background: rgba(245, 245, 248, 235); border-radius: 11px; }
QSlider::handle:horizontal { background: white; width: 22px; border-radius: 11px; }
QToolButton { background: transparent; border: none; border-radius: 7px; padding: 4px; }
QToolButton:hover { background: rgba(255, 255, 255, 40); }
QCalendarWidget QAbstractItemView { background: transparent; color: #f2f2f5; outline: 0;
    selection-background-color: rgba(10, 132, 255, 225); selection-color: white; }
QCalendarWidget QAbstractItemView:disabled { color: rgba(255, 255, 255, 60); }
QCalendarWidget QToolButton { color: #f2f2f5; font: bold 10pt 'Segoe UI'; padding: 3px 8px; }
QCalendarWidget QToolButton::menu-indicator { image: none; }
QCalendarWidget QSpinBox { background: transparent; color: #f2f2f5; border: none; }
QCalendarWidget QWidget#qt_calendar_navigationbar { background: transparent; }
"""


class Flyout(QWidget):
    """Всплывающее окно под верхней панелью: размытие, скругление, плавное выезжание сверху."""
    WIDTH = 300

    def __init__(self, bar):
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.bar = bar
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(FLYOUT_QSS)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(14, 12, 14, 14)
        self.lay.setSpacing(8)
        self.glass = make_glass(self)

    def clear(self):
        def wipe(layout):
            while layout.count():
                item = layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
                elif item.layout():
                    wipe(item.layout())
        wipe(self.lay)

    def build(self):
        pass

    def label(self, text, name=None):
        lab = QLabel(text)
        if name:
            lab.setObjectName(name)
        lab.setWordWrap(True)
        return lab

    def button(self, text, cb, name=None, close=True):
        b = QPushButton(text)
        if name:
            b.setObjectName(name)
        b.setCursor(Qt.PointingHandCursor)

        def clicked():
            if close:
                self.close()
            cb()
        b.clicked.connect(clicked)
        return b

    def open_under(self, anchor_global):
        self.clear()
        self.build()
        self.lay.activate()
        w = max(self.WIDTH, self.sizeHint().width())
        h = self.sizeHint().height()
        self.resize(w, h)
        scr = QApplication.primaryScreen().geometry()
        x = int(min(max(anchor_global.center().x() - w / 2, scr.left() + 6), scr.right() - w - 6))
        y = int(anchor_global.bottom() + 6)
        self.move(x, y)
        self.show()
        QTimer.singleShot(20, lambda: self.isVisible() and try_foreground(int(self.winId())))

    def refit(self):
        self.lay.activate()
        self.resize(max(self.WIDTH, self.sizeHint().width()), self.lay.totalSizeHint().height())

    def showEvent(self, e):
        prepare_glass(self.glass)
        fade_in(self, slide=10)

    def resizeEvent(self, e):
        if self.isVisible():  # Wi‑Fi дорисовывает список — фон снимаем заново под новый размер
            self.glass.refresh()

    def hideEvent(self, e):
        self.bar.flyout_closed(self)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        path = rounded_path(self.rect(), 12)
        self.glass.fill(p, path)
        p.fillPath(path, QColor(34, 34, 40, 150))
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.drawPath(path)


class TrayButton(QToolButton):
    def __init__(self, flyout, data):
        super().__init__()
        self.flyout, self.data = flyout, data
        self.setIconSize(QSize(22, 22))
        self.setFixedSize(40, 40)
        self.setToolTip(data[4])
        self.setCursor(Qt.PointingHandCursor)
        try:
            img = QImage.fromHICON(int(data[3]))
            if not img.isNull():
                self.setIcon(QIcon(QPixmap.fromImage(img)))
        except Exception:
            pass

    def mouseReleaseEvent(self, e):
        hwnd, uid, msg = self.data[:3]
        right = e.button() == Qt.RightButton
        self.flyout.close()
        QTimer.singleShot(160, lambda: tray_click(hwnd, uid, msg, right))


class TrayFlyout(Flyout):
    WIDTH = 252

    def build(self):
        self.lay.addWidget(self.label("Значки в трее", "title"))
        grid = QGridLayout()
        grid.setSpacing(4)
        icons = [d for d in tray_icons() if d[3]]
        for i, d in enumerate(icons):
            grid.addWidget(TrayButton(self, d), i // 5, i % 5)
        self.lay.addLayout(grid)
        if not icons:
            self.lay.addWidget(self.label("Значков нет", "dim"))
        self.lay.addWidget(self.label("Левый клик — открыть, правый — меню программы", "dim"))


def oem_decode(raw):
    return raw.decode(f"cp{kernel32.GetOEMCP()}", errors="replace")


def wifi_info():
    """Текущее подключение и список сетей через netsh (выполняется в фоне)."""
    flags = subprocess.CREATE_NO_WINDOW
    try:
        iface = oem_decode(subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True,
                                          creationflags=flags, timeout=8).stdout)
        nets = oem_decode(subprocess.run(["netsh", "wlan", "show", "networks", "mode=bssid"], capture_output=True,
                                         creationflags=flags, timeout=8).stdout)
    except Exception:
        return None
    if not re.search(r"^\s*(Name|Имя)\s*:", iface, re.M):
        return None
    m = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", iface, re.M)
    sig = re.search(r"(\d+)%", iface)
    current = (m.group(1), int(sig.group(1)) if sig else 0) if m else None
    networks, name = [], None
    for line in nets.splitlines():
        mm = re.match(r"^SSID \d+\s*:\s*(.*)$", line)
        if mm:
            name = mm.group(1).strip()
            networks.append([name, 0, True])
            continue
        if name is not None and networks:
            ms = re.search(r"(\d+)%", line)
            if ms and networks[-1][1] == 0:
                networks[-1][1] = int(ms.group(1))
            if re.search(r"(Authentication|Проверка подлинности)\s*:\s*(Open|Открыт)", line):
                networks[-1][2] = False
    networks = [n for n in networks if n[0]]
    networks.sort(key=lambda n: -n[1])
    return current, networks


def bars(signal):
    n = 1 + min(int(signal / 25), 3)
    return "▂▄▆█"[:n] + "·" * (4 - n)


class WifiFlyout(Flyout):
    WIDTH = 290

    def __init__(self, bar):
        super().__init__(bar)
        self.result = None
        self.timer = QTimer(self, interval=100, timeout=self.check)

    def build(self):
        self.lay.addWidget(self.label("Wi‑Fi", "title"))
        self.status = self.label("Поиск сетей…", "dim")
        self.lay.addWidget(self.status)
        self.list_box = QVBoxLayout()
        self.list_box.setSpacing(3)
        self.lay.addLayout(self.list_box)
        self.lay.addWidget(self.button("Параметры сети…", lambda: os.startfile("ms-settings:network-wifi")))
        self.result = "pending"
        threading.Thread(target=lambda: setattr(self, "result", wifi_info()), daemon=True).start()
        self.timer.start()

    def check(self):
        if self.result == "pending":
            return
        self.timer.stop()
        res = self.result
        if res is None:
            self.status.setText("Wi‑Fi адаптер не найден. Подключение по кабелю или сеть отключена.")
        else:
            current, networks = res
            self.status.setText(f"Подключено: {current[0]}" if current else "Не подключено")
            for name, signal, secure in networks[:9]:
                is_cur = bool(current and current[0] == name)
                text = f"{bars(signal)}   {name}{'   🔒' if secure and not is_cur else ''}"
                self.list_box.addWidget(self.button(text, lambda n=name: self.connect(n),
                                                    "current" if is_cur else None))
        self.refit()

    def connect(self, name):
        def run():
            r = subprocess.run(["netsh", "wlan", "connect", f"name={name}"], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode != 0:  # нет сохранённого профиля — нужен пароль, открываем системный список
                os.startfile("ms-availablenetworks:")
        threading.Thread(target=run, daemon=True).start()


_volume = None


def volume_endpoint(cached=False):
    global _volume
    if cached and _volume is not None:
        return _volume
    try:
        dev = AudioUtilities.GetSpeakers()
        _volume = (dev.EndpointVolume, getattr(dev, "FriendlyName", "") or "Динамики")
    except Exception:
        _volume = None
    return _volume


def volume_glyph(level, muted):
    if muted or level <= 0.001:
        return ""
    return "" if level < 0.34 else "" if level < 0.67 else ""


class VolumeFlyout(Flyout):
    WIDTH = 300

    def build(self):
        ep = volume_endpoint()
        self.lay.addWidget(self.label("Звук", "title"))
        if ep is None:
            self.lay.addWidget(self.label("Не удалось подключиться к звуковому устройству", "dim"))
            self.lay.addWidget(self.button("Микшер громкости", lambda: subprocess.Popen(["sndvol.exe"])))
            return
        vol, name = ep
        row = QHBoxLayout()
        self.mute = QPushButton()
        self.mute.setObjectName("round")
        self.mute.setCheckable(True)
        self.mute.setChecked(bool(vol.GetMute()))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(round(vol.GetMasterVolumeLevelScalar() * 100))
        self.value = QLabel()
        self.value.setFixedWidth(34)
        row.addWidget(self.mute)
        row.addWidget(self.slider, 1)
        row.addWidget(self.value)
        self.lay.addLayout(row)
        self.lay.addWidget(self.label(name, "dim"))
        self.lay.addWidget(self.button("Микшер громкости", lambda: subprocess.Popen(["sndvol.exe"])))
        self.lay.addWidget(self.button("Параметры звука…", lambda: os.startfile("ms-settings:sound")))

        def apply():
            v = self.slider.value()
            try:
                vol.SetMasterVolumeLevelScalar(v / 100, None)
                vol.SetMute(int(self.mute.isChecked()), None)
            except Exception:
                pass
            self.value.setText(f"{v}%")
            self.mute.setText(volume_glyph(v / 100, self.mute.isChecked()))
            self.bar.refresh()
        self.slider.valueChanged.connect(apply)
        self.mute.toggled.connect(apply)
        self.value.setText(f"{self.slider.value()}%")
        self.mute.setText(volume_glyph(self.slider.value() / 100, self.mute.isChecked()))


class ClockFlyout(Flyout):
    WIDTH = 300

    def build(self):
        now = datetime.datetime.now()
        self.time_label = self.label(now.strftime("%H:%M:%S"), "big")
        self.lay.addWidget(self.time_label)
        self.lay.addWidget(self.label(f"{DAYS_FULL[now.weekday()]}, {now.day} {MONTHS_GEN[now.month - 1]} "
                                      f"{now.year}", "dim"))
        cal = QCalendarWidget()
        cal.setLocale(QLocale(QLocale.Russian))
        cal.setFirstDayOfWeek(Qt.Monday)
        cal.setGridVisible(False)
        cal.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        cal.setHorizontalHeaderFormat(QCalendarWidget.ShortDayNames)
        for day in (Qt.Monday, Qt.Tuesday, Qt.Wednesday, Qt.Thursday, Qt.Friday, Qt.Saturday, Qt.Sunday):
            fmt = QTextCharFormat()
            weekend = day in (Qt.Saturday, Qt.Sunday)
            fmt.setForeground(QColor(255, 125, 115) if weekend else QColor(242, 242, 245))
            fmt.setBackground(Qt.transparent)
            cal.setWeekdayTextFormat(day, fmt)
        head = QTextCharFormat()
        head.setForeground(QColor(255, 255, 255, 130))
        head.setBackground(Qt.transparent)
        cal.setHeaderTextFormat(head)
        view = cal.findChild(QTableView)
        if view is not None:
            view.viewport().setAutoFillBackground(False)
            view.setStyleSheet("QTableView { background: transparent; alternate-background-color: transparent; }")
        for name, text in (("qt_calendar_prevmonth", "‹"), ("qt_calendar_nextmonth", "›")):
            btn = cal.findChild(QToolButton, name)
            if btn is not None:
                btn.setIcon(QIcon())
                btn.setText(text)
                btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        cal.setSelectedDate(QDate.currentDate())
        cal.setMinimumHeight(230)
        self.lay.addWidget(cal)
        row = QHBoxLayout()
        row.addWidget(self.button("Уведомления", lambda: press_keys(win32con.VK_LWIN, ord("A"))))
        row.addWidget(self.button("Дата и время…", lambda: os.startfile("ms-settings:dateandtime")))
        self.lay.addLayout(row)
        if getattr(self, "clock_timer", None) is None:
            self.clock_timer = QTimer(self, interval=200, timeout=self.update_time)
        self.clock_timer.start()

    def update_time(self):
        if self.isVisible():
            self.time_label.setText(datetime.datetime.now().strftime("%H:%M:%S"))
        else:
            self.clock_timer.stop()


class LangFlyout(Flyout):
    WIDTH = 240

    def build(self):
        self.lay.addWidget(self.label("Раскладка клавиатуры", "title"))
        target = self.bar.target or win32gui.GetForegroundWindow()
        try:
            tid = win32process.GetWindowThreadProcessId(target)[0] if target else 0
        except Exception:
            tid = 0
        current = user32.GetKeyboardLayout(tid) or 0
        for hkl in keyboard_layouts():
            name = lang_name(hkl & 0xFFFF, 0x02) or hex(hkl)
            self.lay.addWidget(self.button(name, lambda h=hkl, t=target: self.switch(t, h),
                                           "current" if hkl == current else None))
        self.lay.addWidget(self.button("Языковые параметры…", lambda: os.startfile("ms-settings:regionlanguage")))

    def switch(self, target, hkl):
        if target:
            force_foreground(target)
            user32.PostMessageW(target, 0x0050, 0, hkl)  # WM_INPUTLANGCHANGEREQUEST
        QTimer.singleShot(150, self.bar.refresh)


class BatteryFlyout(Flyout):
    WIDTH = 240

    def build(self):
        bat = psutil.sensors_battery()
        self.lay.addWidget(self.label("Аккумулятор", "title"))
        if bat is None:
            self.lay.addWidget(self.label("Нет данных", "dim"))
            return
        self.lay.addWidget(self.label(f"{bat.percent:.0f}%", "big"))
        if bat.power_plugged:
            state = "Подключено к сети"
        elif bat.secsleft not in (psutil.POWER_TIME_UNKNOWN, psutil.POWER_TIME_UNLIMITED):
            state = f"Осталось примерно {bat.secsleft // 3600} ч {bat.secsleft % 3600 // 60} мин"
        else:
            state = "Работа от батареи"
        self.lay.addWidget(self.label(state, "dim"))
        self.lay.addWidget(self.button("Параметры питания…", lambda: os.startfile("ms-settings:batterysaver")))

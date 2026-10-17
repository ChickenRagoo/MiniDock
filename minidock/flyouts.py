"""Всплывающие окна верхней панели: трей, Wi‑Fi, звук, календарь, раскладка, аккумулятор."""
import datetime
import os
import re
import subprocess
import threading

import numpy as np
import psutil
import win32con
import win32gui
import win32process
from PySide6.QtCore import QDate, QLocale, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap,
                           QTextCharFormat)
from PySide6.QtWidgets import (QApplication, QCalendarWidget, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QSlider, QTableView, QToolButton, QVBoxLayout, QWidget)

from . import audio
from .livecapture import blur_bgra
from .media import MediaWatcher
from .ui import MDL2, animate, fade_in, make_glass, prepare_glass, rounded_path
from .winapi import (enable_blur, file_icon, force_foreground, kernel32, keyboard_layouts, lang_name, press_keys,
                     round_window, shell_icon_image, tray_click, tray_icons, try_foreground, user32)

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
        self.glass = make_glass(self, part="bar")
        self.glass.region = self.glass_region

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

    def glass_region(self):
        """Фон снимаем с запасом — до низа экрана: окно может вырасти (раскрылся список, появился плеер),
        а переснимать фон, пока окно на экране, нельзя — в снимок попадёт само окно."""
        bottom = self.screen().geometry().bottom() - self.y()
        return QRectF(0, 0, self.width(), max(self.height(), bottom))

    def resizeEvent(self, e):
        # обычный снимок уже покрывает окно с запасом; живой фон снимает тот же участок — ничего не делаем
        if self.isVisible() and self.glass.live_on:
            self.glass.start_live()

    def hideEvent(self, e):
        self.bar.flyout_closed(self)

    panel_h = None  # высота нарисованной панели во время анимации (None — всё окно)

    def paintEvent(self, e):
        p = QPainter(self)
        # стираем прошлый кадр сами: иначе при анимации полупрозрачные кадры наслаиваются
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(e.rect(), Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        h = self.height() if self.panel_h is None else min(self.panel_h, self.height())
        path = rounded_path(QRectF(0, 0, self.width(), h), 12)
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


MIXER_QSS = """
QLabel#section { color: rgba(255, 255, 255, 150); font: 600 9pt 'Segoe UI'; padding-top: 2px; }
QLabel#dev { color: #f2f2f5; font: 9pt 'Segoe UI'; }
QPushButton#ico { font: 12pt 'Segoe MDL2 Assets'; background: transparent; border-radius: 8px; padding: 0;
                  min-width: 32px; max-width: 32px; min-height: 30px; max-height: 30px; text-align: center; }
QPushButton#ico:hover { background: rgba(255, 255, 255, 30); }
QPushButton#ico:checked { color: rgba(255, 255, 255, 110); }
QPushButton#mix { font: 12pt 'Segoe MDL2 Assets'; background: transparent; border-radius: 8px; padding: 0;
                  min-width: 32px; max-width: 32px; min-height: 30px; max-height: 30px; text-align: center; }
QPushButton#mix:hover { background: rgba(255, 255, 255, 30); }
QPushButton#mix:checked { background: rgba(92, 150, 166, 200); color: white; }
QPushButton#seg { font: 9pt 'Segoe MDL2 Assets'; background: rgba(255, 255, 255, 14); border: 1px solid rgba(255, 255, 255, 40);
                  padding: 0; min-width: 26px; max-width: 26px; min-height: 24px; max-height: 24px; text-align: center; }
QPushButton#seg:hover { background: rgba(255, 255, 255, 40); }
QPushButton#seg:checked { background: rgba(92, 150, 166, 235); border-color: rgba(120, 180, 196, 255); color: white; }
QSlider#vol::groove:horizontal { height: 5px; background: rgba(255, 255, 255, 36); border-radius: 2px; }
QSlider#vol::sub-page:horizontal { background: rgba(120, 172, 186, 255); border-radius: 2px; }
QSlider#vol::handle:horizontal { background: #f4f4f6; border: 3px solid rgba(120, 172, 186, 255); width: 12px;
                                 height: 12px; margin: -7px 0; border-radius: 9px; }
QSlider#appvol::groove:horizontal { height: 4px; background: rgba(255, 255, 255, 30); border-radius: 2px; }
QSlider#appvol::sub-page:horizontal { background: rgba(245, 245, 248, 220); border-radius: 2px; }
QSlider#appvol::handle:horizontal { background: white; width: 12px; margin: -4px 0; border-radius: 6px; }
QPushButton#play { font: 13pt 'Segoe MDL2 Assets'; background: transparent; color: white; border-radius: 16px; padding: 0;
                   min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px; text-align: center; }
QPushButton#play:hover { background: rgba(255, 255, 255, 40); }
"""

GLYPH_SPEAKER, GLYPH_MUTE, GLYPH_MIC, GLYPH_MIC_OFF = "", "", "", ""
GLYPH_MIXER, GLYPH_SETTINGS = "", ""
GLYPH_MUSIC, GLYPH_PHONE = "", ""
GLYPH_PREV, GLYPH_PLAY, GLYPH_PAUSE, GLYPH_NEXT = "", "", "", ""

_exe_cache = {}
_icon_cache = {}


def app_icon(app_id):
    """Значок программы по её exe или ID приложения (как у медиа-сессий: «Spotify.exe», «chrome.exe»)."""
    if app_id in _icon_cache:
        return _icon_cache[app_id]
    pix = None
    path = app_id if os.path.isabs(app_id) else None
    if path is None and app_id.lower().endswith(".exe"):
        path = _exe_cache.get(app_id.lower())
        if path is None:
            for p in psutil.process_iter(["name", "exe"]):
                if (p.info["name"] or "").lower() == app_id.lower() and p.info["exe"]:
                    path = _exe_cache[app_id.lower()] = p.info["exe"]
                    break
    try:
        if path:
            pix = file_icon(path)
        else:  # приложение Microsoft Store — значок как в меню «Пуск»
            from win32com.shell import shell, shellcon
            _, pidl, _ = shell.SHGetDesktopFolder().ParseDisplayName(0, None, "shell:AppsFolder\\" + app_id)
            _, info = shell.SHGetFileInfo(pidl, 0, shellcon.SHGFI_PIDL | shellcon.SHGFI_SYSICONINDEX)
            pix = QPixmap.fromImage(shell_icon_image(info[1]))
    except Exception:
        pix = None
    _icon_cache[app_id] = pix
    return pix


def sharp_icon(pix, side, dpr=1.0):
    """Уменьшить большой значок (обычно 256 px) без «лесенки»: сжимаем вдвое за шаг, пока не дойдём до
    нужного размера в физических пикселях, — один прыжок 256 → 20 сглаживание не вытягивает."""
    target = max(1, round(side * dpr))
    img = pix.toImage().convertToFormat(QImage.Format_ARGB32_Premultiplied)
    while img.width() >= target * 2 and img.height() >= target * 2:
        img = img.scaled(img.width() // 2, img.height() // 2, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    img = img.scaled(target, target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    out = QPixmap.fromImage(img)
    out.setDevicePixelRatio(dpr)
    return out


def trim_cover(img):
    """Обложка без прозрачных полей. Spotify отдаёт картинку 300×300: сама обложка сверху посередине,
    под ней — его логотип на прозрачном фоне. Оставляем только непрозрачный прямоугольник обложки,
    чтобы карточки всех плееров выглядели одинаково."""
    if img.isNull() or not img.hasAlphaChannel():
        return img
    a = img.convertToFormat(QImage.Format_ARGB32)
    w, h = a.width(), a.height()
    alpha = np.frombuffer(a.constBits(), np.uint8, a.bytesPerLine() * h).reshape(h, a.bytesPerLine() // 4, 4)[:, :w, 3]
    solid = alpha > 200
    rows = np.where(solid.mean(1) > 0.5)[0]
    if not len(rows):
        return img
    # первый сплошной блок строк сверху — это обложка, всё, что ниже пустой полосы, — логотип
    gaps = np.where(np.diff(rows) > 1)[0]
    top, bottom = rows[0], rows[gaps[0]] if len(gaps) else rows[-1]
    cols = np.where(solid[top:bottom + 1].mean(0) > 0.9)[0]
    if not len(cols) or (bottom - top + 1) * len(cols) < w * h * 0.25:
        return img
    return img.copy(int(cols[0]), int(top), int(cols[-1] - cols[0] + 1), int(bottom - top + 1))


def blurred(img, size, radius=18):
    """Сильно размытая и затемнённая копия картинки (фон карточки плеера)."""
    small = img.scaled(max(size.width() // 6, 8), max(size.height() // 6, 8), Qt.KeepAspectRatioByExpanding,
                       Qt.SmoothTransformation).convertToFormat(QImage.Format_RGB32)
    w, h = small.width(), small.height()
    a = np.frombuffer(small.constBits(), np.uint8, small.bytesPerLine() * h).reshape(h, small.bytesPerLine() // 4,
                                                                                     4)[:, :w, :3]
    out = blur_bgra(a.astype(np.float32), radius / 6, saturation=1.25)
    return QImage(out.data, w, h, w * 4, QImage.Format_RGB32).copy()


class SegPair(QWidget):
    """Две склеенные кнопки «♫ для музыки и игр | ☎ для звонков» с плавным перетеканием цвета."""
    clicked = Signal(bool)  # True — нажата «для звонков»
    ON = QColor(92, 150, 166)
    W, H = 54, 24

    def __init__(self, media, comm):
        super().__init__()
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.state = [bool(media), bool(comm)]
        self.k = [float(media), float(comm)]  # анимированная заливка каждой половины
        self.hover = None
        self.font = QFont(MDL2, 9)

    def set_state(self, media, comm):
        new = [bool(media), bool(comm)]
        if new == self.state:
            return
        start, self.state = list(self.k), new

        def step(v):
            self.k = [s + (float(t) - s) * v for s, t in zip(start, new)]
            self.update()
        self._anim = animate(self, 220, step)

    def half(self, x):
        return 1 if x >= self.W / 2 else 0

    def mouseMoveEvent(self, e):
        h = self.half(e.position().x())
        if h != self.hover:
            self.hover = h
            self.update()

    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(bool(self.half(e.position().x())))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        outer = rounded_path(QRectF(self.rect()), 7)
        p.fillPath(outer, QColor(255, 255, 255, 14))
        for i, glyph in enumerate((GLYPH_MUSIC, GLYPH_PHONE)):
            r = QRectF(i * self.W / 2, 0, self.W / 2, self.H)
            p.save()
            p.setClipRect(r)
            c = QColor(self.ON)
            c.setAlpha(int(235 * self.k[i]))
            p.fillPath(outer, c)
            if self.hover == i:
                p.fillPath(outer, QColor(255, 255, 255, 30))
            p.restore()
            p.setFont(self.font)
            p.setPen(QColor(255, 255, 255, int(170 + 85 * self.k[i])))
            p.drawText(r, Qt.AlignCenter, glyph)
        p.setPen(QPen(QColor(255, 255, 255, 50), 1))
        p.drawPath(outer)
        p.drawLine(QPointF(self.W / 2, 3), QPointF(self.W / 2, self.H - 3))


class Collapsible(QWidget):
    """Блок, который плавно раскрывается и сворачивается по высоте, как шторка: содержимое всегда полного
    размера и просто обрезается (не сплющивается). Размер окна во время анимации не меняется — на каждом
    шаге окно только перерисовывает панель нужной высоты (on_step)."""

    def __init__(self, on_step):
        super().__init__()
        self.on_step = on_step
        self.inner = QWidget(self)
        self.box = QVBoxLayout(self.inner)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(6)
        self.open = False
        self.setFixedHeight(0)

    def current(self):
        return self.height()

    def full_height(self):
        self.box.activate()
        return self.inner.sizeHint().height()

    def resizeEvent(self, e):
        self.inner.setGeometry(0, 0, self.width(), self.full_height())

    def toggle(self, open_, done=None):
        self.open = open_
        start, end = self.current(), (self.full_height() if open_ else 0)
        self.inner.setGeometry(0, 0, self.width(), self.full_height())

        def step(v):
            self.setFixedHeight(int(start + (end - start) * v))
            self.on_step()

        def finished():
            self.setFixedHeight(end)
            self.on_step()
            if done:
                done()
        self._anim = animate(self, 220, step, finished)


class MediaCard(QWidget):
    """Карточка плеера: размытая обложка фоном, обложка со значком программы, название, исполнитель, кнопки.
    Новые данные (другой трек) плавно проступают поверх старых — карточка не пересоздаётся."""
    H = 122

    def __init__(self, info, watcher, cfg):
        super().__init__()
        self.watcher = watcher
        self.setFixedHeight(0)  # появляется, плавно раскрываясь
        self.f_title = QFont("Segoe UI", 13, QFont.DemiBold)
        self.f_artist = QFont("Segoe UI", 9)
        self.layers = []   # [(инфо, обложка, размытый фон)] — старый слой гаснет, новый проступает
        self.fade = 1.0
        self.appear = 0.0
        self.cfg = cfg
        self.icon = app_icon(info["app"])
        if self.icon is not None and not self.icon.isNull():
            self.icon = sharp_icon(self.icon, 26, self.devicePixelRatioF())
        self.buttons = []
        for glyph, action in ((GLYPH_PREV, "prev"), (GLYPH_PLAY, "toggle"), (GLYPH_NEXT, "next")):
            b = QPushButton(glyph, self)
            b.setObjectName("play")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, a=action: self.watcher.control(self.info["id"], a))
            self.buttons.append(b)
        self.set_info(info, animated=False)

    def set_info(self, info, animated=True):
        self.info = info
        self.buttons[1].setText(GLYPH_PAUSE if info["playing"] else GLYPH_PLAY)
        key = (info["title"], info["artist"], bool(info["cover"]))
        if self.layers and self.layers[-1][0] == key:
            return
        cover = trim_cover(QImage.fromData(info["cover"])) if info["cover"] else QImage()
        self.layers = self.layers[-1:] + [(key, info, cover, None)]
        if not animated or len(self.layers) == 1:
            self.layers = self.layers[-1:]
            self.update()
            return
        self.fade = 0.0

        def step(v):
            self.fade = v
            self.update()
        self._fade_anim = animate(self, 350, step, lambda: setattr(self, "layers", self.layers[-1:]))

    def show_animated(self, on_step, done=None):
        def step(v):
            self.appear = v
            self.setFixedHeight(int(self.H * v))
            on_step()
        self._appear_anim = animate(self, 260, step, done)

    def hide_animated(self, on_step, done):
        start = self.height()

        def step(v):
            self.appear = 1 - v
            self.setFixedHeight(int(start * (1 - v)))
            on_step()
        self._appear_anim = animate(self, 220, step, done)

    def resizeEvent(self, e):
        self.layers = [(k, i, c, None) for k, i, c, _ in self.layers]  # фон под новый размер
        x0 = self.H - 8 + (self.width() - self.H) / 2
        for i, b in enumerate(self.buttons):
            b.move(int(x0 + (i - 1) * 54 - 16), self.H - 46)

    def paint_layer(self, p, i, opacity):
        key, info, cover, bg = self.layers[i]
        p.setOpacity(opacity * self.appear)
        card = rounded_path(QRectF(0, 0, self.width(), self.H), 12)
        p.save()
        p.setClipPath(card)
        if self.cfg.get("media_bg", "blur") == "solid":  # фон карточки — сплошной цвет из настроек
            p.fillRect(self.rect(), QColor(self.cfg.get("media_color", "#26262e")))
        elif not cover.isNull():
            if bg is None:
                bg = blurred(cover, QSize(self.width(), self.H))
                self.layers[i] = (key, info, cover, bg)
            p.drawImage(QRectF(0, 0, self.width(), self.H), bg, QRectF(bg.rect()))
            p.fillRect(self.rect(), QColor(10, 10, 16, 120))
        else:
            p.fillRect(self.rect(), QColor(255, 255, 255, 22))
        p.restore()
        art = QRectF(10, 10, self.H - 20, self.H - 20)
        ap = rounded_path(art, 9)
        if not cover.isNull():
            p.save()
            p.setClipPath(ap)
            side = min(cover.width(), cover.height())
            p.drawImage(art, cover, QRectF((cover.width() - side) / 2, (cover.height() - side) / 2, side, side))
            p.restore()
        else:
            p.fillPath(ap, QColor(255, 255, 255, 30))
            p.setFont(QFont(MDL2, 26))
            p.setPen(QColor(255, 255, 255, 160))
            p.drawText(art, Qt.AlignCenter, GLYPH_MUSIC)
        text = QRectF(art.right() + 10, 14, self.width() - art.right() - 20, 30)
        p.setFont(self.f_title)
        p.setPen(QColor(255, 255, 255))
        p.drawText(text, Qt.AlignHCenter | Qt.AlignVCenter,
                   QFontMetricsF(self.f_title).elidedText(info["title"] or "Без названия", Qt.ElideRight, text.width()))
        p.setFont(self.f_artist)
        p.setPen(QColor(255, 255, 255, 200))
        sub = QRectF(text.left(), text.bottom(), text.width(), 20)
        p.drawText(sub, Qt.AlignHCenter | Qt.AlignVCenter,
                   QFontMetricsF(self.f_artist).elidedText(info["artist"], Qt.ElideRight, sub.width()))

    def paintEvent(self, _):
        if not self.layers or self.height() < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        if len(self.layers) > 1:
            self.paint_layer(p, 0, 1.0)
            self.paint_layer(p, len(self.layers) - 1, self.fade)
        else:
            self.paint_layer(p, 0, 1.0)
        p.setOpacity(self.appear)
        art = QRectF(10, 10, self.H - 20, self.H - 20)
        if self.icon is not None and not self.icon.isNull():
            p.drawPixmap(QPointF(art.right() - 30, art.bottom() - 30), self.icon)
        p.setPen(QColor(255, 255, 255, 40))
        p.setBrush(Qt.NoBrush)
        p.drawPath(rounded_path(QRectF(0, 0, self.width(), self.H), 12))


class VolumeFlyout(Flyout):
    """Звук в стиле Seelen UI: общая громкость и микрофон, громкость программ, выбор устройств вывода и ввода
    (отдельно для музыки и для связи), карточки всего, что сейчас играет. Окно собирается один раз при
    открытии, дальше всё меняется на месте и с анимацией."""
    WIDTH = 400
    watcher = None

    def __init__(self, bar):
        super().__init__(bar)
        self.setStyleSheet(FLYOUT_QSS + MIXER_QSS)
        if VolumeFlyout.watcher is None:
            VolumeFlyout.watcher = MediaWatcher()
        self.watcher.sessions.connect(self.on_media)
        self.media_timer = QTimer(self, interval=1500, timeout=self.watcher.refresh)
        self.cards = {}

    # -- построение (один раз при открытии)
    def section(self, text):
        lab = QLabel(text)
        lab.setObjectName("section")
        return lab

    def line(self):
        f = QFrame()
        f.setFixedHeight(1)
        f.setStyleSheet("background: rgba(255, 255, 255, 30);")
        return f

    def icon_button(self, glyph, cb, checkable=False, name="ico"):
        b = QPushButton(glyph)
        b.setObjectName(name)
        b.setCursor(Qt.PointingHandCursor)
        b.setCheckable(checkable)
        b.clicked.connect(cb)
        return b

    def volume_row(self, flow, glyph, glyph_off, extra):
        row = QHBoxLayout()
        row.setSpacing(8)
        mute = self.icon_button(glyph, lambda: None, checkable=True)
        slider = QSlider(Qt.Horizontal)
        slider.setObjectName("vol")
        slider.setRange(0, 100)
        ctl = dict(flow=flow, vol=audio.default_volume(flow), slider=slider, mute=mute, glyphs=(glyph, glyph_off),
                   quiet=False)

        def apply():
            if ctl["quiet"]:
                return
            try:
                ctl["vol"].SetMasterVolumeLevelScalar(slider.value() / 100, None)
                ctl["vol"].SetMute(int(mute.isChecked()), None)
            except Exception:
                pass
            mute.setText(glyph_off if mute.isChecked() else glyph)
            self.bar.refresh()
        slider.valueChanged.connect(apply)
        mute.toggled.connect(apply)
        self.load_volume(ctl, animated=False)
        row.addWidget(mute)
        row.addWidget(slider, 1)
        row.addWidget(extra)
        self.volumes.append(ctl)
        return row

    def load_volume(self, ctl, animated=True):
        """Ползунок — к громкости основного устройства (после смены устройства — плавно)."""
        try:
            level = round(ctl["vol"].GetMasterVolumeLevelScalar() * 100)
            muted = bool(ctl["vol"].GetMute())
        except Exception:
            return
        ctl["quiet"] = True
        ctl["mute"].setChecked(muted)
        ctl["mute"].setText(ctl["glyphs"][1] if muted else ctl["glyphs"][0])
        slider, start = ctl["slider"], ctl["slider"].value()
        if not animated or start == level:
            slider.setValue(level)
            ctl["quiet"] = False
            return

        def step(v):
            slider.setValue(round(start + (level - start) * v))

        def done():
            ctl["quiet"] = False
        ctl["anim"] = animate(self, 300, step, done)

    def device_rows(self, flow):
        box = QVBoxLayout()
        box.setSpacing(4)
        for d in audio.devices(flow):
            row = QHBoxLayout()
            row.setSpacing(10)
            seg = SegPair(d["media"], d["comm"])
            seg.setToolTip("♫ — для музыки и игр, ☎ — для звонков")
            seg.clicked.connect(lambda comm, did=d["id"]: self.pick_device(did, comm))
            name = QLabel()
            name.setObjectName("dev")
            name.setText(QFontMetricsF(QFont("Segoe UI", 9)).elidedText(d["name"], Qt.ElideRight, self.WIDTH - 110))
            name.setToolTip(d["name"])
            row.addWidget(seg)
            row.addWidget(name, 1)
            box.addLayout(row)
            self.segs.append((flow, d["id"], seg))
        return box

    def pick_device(self, device_id, comm):
        if not audio.set_default(device_id, comm):
            return
        # отмечаем выбор сразу (плавно), не пересобирая окно; затем сверяемся с системой
        flow = next((f for f, d, _ in self.segs if d == device_id), None)
        for f, did, seg in self.segs:
            if f != flow:
                continue
            if comm:
                seg.set_state(seg.state[0], did == device_id)
            else:
                seg.set_state(did == device_id, seg.state[1])
        QTimer.singleShot(250, self.sync_devices)

    def sync_devices(self):
        states = {d["id"]: d for flow in (audio.OUTPUT, audio.INPUT) for d in audio.devices(flow)}
        for _, did, seg in self.segs:
            d = states.get(did)
            if d is not None:
                seg.set_state(d["media"], d["comm"])
        for ctl in self.volumes:  # основное устройство сменилось — ползунок к его громкости
            ctl["vol"] = audio.default_volume(ctl["flow"])
            self.load_volume(ctl)

    def fill_apps(self):
        box = self.apps.box
        while box.count():
            item = box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    w = item.layout().takeAt(0).widget()
                    if w:
                        w.deleteLater()
        sessions = audio.app_sessions()
        self.apps_filled = True
        self.app_sliders = []
        if not sessions:
            box.addWidget(self.label("Сейчас ни одна программа не воспроизводит звук", "dim"))
        for a in sessions:
            row = QHBoxLayout()
            row.setSpacing(8)
            icon = QLabel()
            pix = app_icon(a["exe"])
            if pix is not None and not pix.isNull():
                icon.setPixmap(sharp_icon(pix, 20, self.devicePixelRatioF()))
            icon.setFixedWidth(22)
            name = QLabel(os.path.splitext(a["name"])[0])
            name.setFixedWidth(110)
            s = QSlider(Qt.Horizontal)
            s.setObjectName("appvol")
            s.setRange(0, 100)
            try:
                s.setValue(round(a["volumes"][0].GetMasterVolume() * 100))
            except Exception:
                pass

            def set_vol(v, vols=a["volumes"]):
                for sv in vols:
                    try:
                        sv.SetMasterVolume(v / 100, None)
                    except Exception:
                        pass
            s.valueChanged.connect(set_vol)
            self.app_sliders.append((s, a["volumes"]))
            row.addWidget(icon)
            row.addWidget(name)
            row.addWidget(s, 1)
            box.addLayout(row)

    def toggle_apps(self, on):
        if on:
            if not self.apps_filled:
                self.fill_apps()
            else:
                self.refresh_app_levels()  # список уже готов — только свежие уровни громкости
            self.grow_by(self.apps.full_height() - self.apps.current())
        self.apps.toggle(on, self.settle)

    # -- плавное изменение высоты: окно меняет размер один раз, внутри анимируется панель
    def content_height(self):
        self.lay.activate()
        return self.lay.totalSizeHint().height()

    def grow_by(self, delta):
        """Перед раскрытием: окно сразу итоговой высоты (прозрачный низ), панель пока прежней высоты."""
        self.panel_h = self.content_height()
        if delta > 0:
            self.resize(self.width(), self.panel_h + delta)

    def anim_step(self):
        self.panel_h = self.content_height()
        self.update()

    def settle(self):
        """Анимация закончилась: окно ровно по содержимому (после сворачивания — уменьшается один раз)."""
        h = self.content_height()
        self.panel_h = None
        if h != self.height():
            self.resize(self.width(), h)
        self.update()

    def prefill_apps(self):
        """Список программ и их значки готовим сразу после открытия окна — раскрытие потом мгновенное."""
        if self.isVisible() and not self.apps_filled and hasattr(self, "apps"):
            self.fill_apps()

    def refresh_app_levels(self):
        for slider, vols in self.app_sliders:
            try:
                slider.blockSignals(True)
                slider.setValue(round(vols[0].GetMasterVolume() * 100))
            except Exception:
                pass
            finally:
                slider.blockSignals(False)

    def build(self):
        self.lay.setSpacing(8)
        self.volumes, self.segs, self.cards = [], [], {}
        self.apps_filled, self.app_sliders = False, []
        if not audio.available():
            self.lay.addWidget(self.label("Не удалось подключиться к звуковым устройствам", "dim"))
            self.lay.addWidget(self.button("Параметры звука…", lambda: os.startfile("ms-settings:sound")))
            return
        self.lay.addWidget(self.section("Общая громкость"))
        mixer = self.icon_button(GLYPH_MIXER, lambda: None, checkable=True, name="mix")
        mixer.setToolTip("Громкость программ")
        mixer.toggled.connect(self.toggle_apps)
        self.mixer_button = mixer
        self.lay.addLayout(self.volume_row(audio.OUTPUT, GLYPH_SPEAKER, GLYPH_MUTE, mixer))
        self.apps = Collapsible(self.anim_step)
        self.lay.addWidget(self.apps)
        settings = self.icon_button(GLYPH_SETTINGS, lambda: (self.close(), os.startfile("ms-settings:sound")))
        settings.setToolTip("Параметры звука")
        self.lay.addLayout(self.volume_row(audio.INPUT, GLYPH_MIC, GLYPH_MIC_OFF, settings))
        self.lay.addWidget(self.line())
        self.lay.addWidget(self.section("Устройство вывода"))
        self.lay.addLayout(self.device_rows(audio.OUTPUT))
        self.lay.addWidget(self.line())
        self.lay.addWidget(self.section("Устройство ввода"))
        self.lay.addLayout(self.device_rows(audio.INPUT))
        self.media_head = QWidget()
        hl = QVBoxLayout(self.media_head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        hl.addWidget(self.line())
        hl.addWidget(self.section("Медиаплееры"))
        self.media_head.setVisible(False)
        self.lay.addWidget(self.media_head)
        self.media_box = QVBoxLayout()
        self.media_box.setSpacing(8)
        self.lay.addLayout(self.media_box)
        for s in getattr(self, "sessions", []):  # последние известные плееры — сразу, без анимации
            self.add_card(s, animated=False)
        self.lay.addStretch(1)  # пока окно выше содержимого (анимация), всё прижато кверху

    # -- плееры: обновляются на месте
    @staticmethod
    def card_key(s):
        return (s["app"], s["id"])

    def add_card(self, s, animated=True):
        card = MediaCard(s, self.watcher, self.bar.cfg)
        before = self.content_height() if animated else 0
        self.cards[self.card_key(s)] = card
        self.media_box.addWidget(card)
        self.media_head.setVisible(True)
        if animated:
            # окно сразу итоговой высоты (вместе с заголовком «Медиаплееры», если он только что появился)
            need = self.content_height() + card.H
            self.panel_h = before
            if need > self.height():
                self.resize(self.width(), need)
            card.show_animated(self.anim_step, self.settle)
        else:
            card.appear = 1.0
            card.setFixedHeight(card.H)

    def on_media(self, sessions):
        self.sessions = sessions
        if not self.isVisible() or not hasattr(self, "media_box"):
            return
        alive = set()
        for s in sessions:
            key = self.card_key(s)
            alive.add(key)
            card = self.cards.get(key)
            if card is None:
                self.add_card(s)
            else:
                card.set_info(s)
        for key in [k for k in self.cards if k not in alive]:
            card = self.cards.pop(key)

            def gone(c=card):
                self.media_box.removeWidget(c)
                c.deleteLater()
                self.media_head.setVisible(bool(self.cards))
                self.settle()
            self.panel_h = self.content_height()
            card.hide_animated(self.anim_step, gone)

    def showEvent(self, e):
        super().showEvent(e)
        self.watcher.refresh()
        self.media_timer.start()
        QTimer.singleShot(250, self.prefill_apps)

    def hideEvent(self, e):
        self.media_timer.stop()
        super().hideEvent(e)


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

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
from PySide6.QtCore import QDate, QLocale, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap,
                           QTextCharFormat)
from PySide6.QtWidgets import (QApplication, QCalendarWidget, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QSlider, QTableView, QToolButton, QVBoxLayout, QWidget)

from . import audio
from .livecapture import blur_bgra
from .media import MediaWatcher
from .ui import MDL2, fade_in, make_glass, prepare_glass, rounded_path
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


def blurred(img, size, radius=18):
    """Сильно размытая и затемнённая копия картинки (фон карточки плеера)."""
    small = img.scaled(max(size.width() // 6, 8), max(size.height() // 6, 8), Qt.KeepAspectRatioByExpanding,
                       Qt.SmoothTransformation).convertToFormat(QImage.Format_RGB32)
    w, h = small.width(), small.height()
    a = np.frombuffer(small.constBits(), np.uint8, small.bytesPerLine() * h).reshape(h, small.bytesPerLine() // 4,
                                                                                     4)[:, :w, :3]
    out = blur_bgra(a.astype(np.float32), radius / 6, saturation=1.25)
    return QImage(out.data, w, h, w * 4, QImage.Format_RGB32).copy()


class MediaCard(QWidget):
    """Карточка плеера: размытая обложка фоном, обложка со значком программы, название, исполнитель, кнопки."""
    H = 122

    def __init__(self, info, watcher):
        super().__init__()
        self.info, self.watcher = info, watcher
        self.setFixedHeight(self.H)
        self.cover = QImage.fromData(info["cover"]) if info["cover"] else QImage()
        self.bg = None
        self.icon = app_icon(info["app"])
        self.f_title = QFont("Segoe UI", 13, QFont.DemiBold)
        self.f_artist = QFont("Segoe UI", 9)
        self.buttons = []
        for glyph, action in ((GLYPH_PREV, "prev"), (GLYPH_PAUSE if info["playing"] else GLYPH_PLAY, "toggle"),
                              (GLYPH_NEXT, "next")):
            b = QPushButton(glyph, self)
            b.setObjectName("play")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, a=action: watcher.control(info["id"], a))
            self.buttons.append(b)

    def resizeEvent(self, e):
        self.bg = None
        x0 = self.H - 8 + (self.width() - self.H) / 2
        for i, b in enumerate(self.buttons):
            b.move(int(x0 + (i - 1) * 54 - 16), self.H - 46)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        card = rounded_path(self.rect(), 12)
        p.setClipPath(card)
        if not self.cover.isNull():
            if self.bg is None:
                self.bg = blurred(self.cover, self.size())
            p.drawImage(QRectF(self.rect()), self.bg, QRectF(self.bg.rect()))
            p.fillRect(self.rect(), QColor(10, 10, 16, 120))
        else:
            p.fillRect(self.rect(), QColor(255, 255, 255, 22))
        p.setClipping(False)
        p.setPen(QColor(255, 255, 255, 40))
        p.drawPath(card)
        art = QRectF(10, 10, self.H - 20, self.H - 20)
        ap = rounded_path(art, 9)
        if not self.cover.isNull():
            p.save()
            p.setClipPath(ap)
            c = self.cover
            side = min(c.width(), c.height())
            src = QRectF((c.width() - side) / 2, (c.height() - side) / 2, side, side)
            p.drawImage(art, c, src)
            p.restore()
        else:
            p.fillPath(ap, QColor(255, 255, 255, 30))
            p.setFont(QFont(MDL2, 26))
            p.setPen(QColor(255, 255, 255, 160))
            p.drawText(art, Qt.AlignCenter, GLYPH_MUSIC)
        if self.icon is not None and not self.icon.isNull():
            badge = QRectF(art.right() - 30, art.bottom() - 30, 26, 26)
            p.drawPixmap(badge, self.icon, QRectF(self.icon.rect()))
        text = QRectF(art.right() + 10, 14, self.width() - art.right() - 20, 30)
        p.setFont(self.f_title)
        p.setPen(QColor(255, 255, 255))
        fm = QFontMetricsF(self.f_title)
        p.drawText(text, Qt.AlignHCenter | Qt.AlignVCenter,
                   fm.elidedText(self.info["title"] or "Без названия", Qt.ElideRight, text.width()))
        p.setFont(self.f_artist)
        p.setPen(QColor(255, 255, 255, 200))
        fa = QFontMetricsF(self.f_artist)
        sub = QRectF(text.left(), text.bottom(), text.width(), 20)
        p.drawText(sub, Qt.AlignHCenter | Qt.AlignVCenter, fa.elidedText(self.info["artist"], Qt.ElideRight, sub.width()))


class VolumeFlyout(Flyout):
    """Звук в стиле Seelen UI: общая громкость и микрофон, громкость программ, выбор устройств вывода и ввода
    (отдельно для музыки и для связи), карточки всего, что сейчас играет."""
    WIDTH = 400
    watcher = None

    def __init__(self, bar):
        super().__init__(bar)
        self.setStyleSheet(FLYOUT_QSS + MIXER_QSS)
        if VolumeFlyout.watcher is None:
            VolumeFlyout.watcher = MediaWatcher()
        self.watcher.sessions.connect(self.on_media)
        self.media_box = None
        self.media_state = None
        self.apps_open = False
        self.media_timer = QTimer(self, interval=1500, timeout=self.watcher.refresh)

    # -- построение
    def section(self, text):
        lab = QLabel(text)
        lab.setObjectName("section")
        return lab

    def line(self):
        f = QFrame()
        f.setFixedHeight(1)
        f.setStyleSheet("background: rgba(255, 255, 255, 30);")
        return f

    def icon_button(self, glyph, cb, checkable=False):
        b = QPushButton(glyph)
        b.setObjectName("ico")
        b.setCursor(Qt.PointingHandCursor)
        b.setCheckable(checkable)
        b.clicked.connect(cb)
        return b

    def volume_row(self, vol, glyph, glyph_off, extra):
        row = QHBoxLayout()
        row.setSpacing(8)
        mute = self.icon_button(glyph, lambda: None, checkable=True)
        slider = QSlider(Qt.Horizontal)
        slider.setObjectName("vol")
        slider.setRange(0, 100)
        try:
            slider.setValue(round(vol.GetMasterVolumeLevelScalar() * 100))
            mute.setChecked(bool(vol.GetMute()))
        except Exception:
            pass
        mute.setText(glyph_off if mute.isChecked() else glyph)

        def apply():
            try:
                vol.SetMasterVolumeLevelScalar(slider.value() / 100, None)
                vol.SetMute(int(mute.isChecked()), None)
            except Exception:
                pass
            mute.setText(glyph_off if mute.isChecked() else glyph)
            self.bar.refresh()
        slider.valueChanged.connect(apply)
        mute.toggled.connect(apply)
        row.addWidget(mute)
        row.addWidget(slider, 1)
        row.addWidget(extra)
        return row

    def device_rows(self, flow):
        box = QVBoxLayout()
        box.setSpacing(4)
        for d in audio.devices(flow):
            row = QHBoxLayout()
            row.setSpacing(0)
            for glyph, comm, side in ((GLYPH_MUSIC, False, "segL"), (GLYPH_PHONE, True, "segR")):
                b = QPushButton(glyph)
                b.setObjectName("seg")
                b.setCheckable(True)
                b.setChecked(d["comm"] if comm else d["media"])
                b.setToolTip("Для звонков" if comm else "Для музыки и игр")
                b.setCursor(Qt.PointingHandCursor)
                radius = "border-top-left-radius: 7px; border-bottom-left-radius: 7px;" if side == "segL" else \
                    "border-top-right-radius: 7px; border-bottom-right-radius: 7px; border-left: none;"
                b.setStyleSheet(radius)
                b.clicked.connect(lambda _=False, did=d["id"], c=comm: self.pick_device(did, c))
                row.addWidget(b)
            row.addSpacing(10)
            name = QLabel()
            name.setObjectName("dev")
            name.setText(QFontMetricsF(QFont("Segoe UI", 9)).elidedText(d["name"], Qt.ElideRight, self.WIDTH - 110))
            name.setToolTip(d["name"])
            row.addWidget(name, 1)
            box.addLayout(row)
        return box

    def pick_device(self, device_id, comm):
        audio.set_default(device_id, comm)
        QTimer.singleShot(150, self.rebuild)

    def apps_box(self):
        box = QVBoxLayout()
        box.setSpacing(6)
        for a in audio.app_sessions():
            row = QHBoxLayout()
            row.setSpacing(8)
            icon = QLabel()
            pix = app_icon(a["exe"])
            if pix is not None and not pix.isNull():
                icon.setPixmap(pix.scaled(20, 20, Qt.KeepAspectRatio, Qt.SmoothTransformation))
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
            row.addWidget(icon)
            row.addWidget(name)
            row.addWidget(s, 1)
            box.addLayout(row)
        if not box.count():
            box.addWidget(self.label("Сейчас ни одна программа не воспроизводит звук", "dim"))
        return box

    def toggle_apps(self):
        self.apps_open = not self.apps_open
        self.rebuild()

    def build(self):
        self.lay.setSpacing(8)
        if not audio.available():
            self.lay.addWidget(self.label("Не удалось подключиться к звуковым устройствам", "dim"))
            self.lay.addWidget(self.button("Параметры звука…", lambda: os.startfile("ms-settings:sound")))
            return
        self.lay.addWidget(self.section("Общая громкость"))
        out = audio.default_volume(audio.OUTPUT)
        mic = audio.default_volume(audio.INPUT)
        mixer = self.icon_button(GLYPH_MIXER, self.toggle_apps, checkable=True)
        mixer.setObjectName("mix")  # включённая — подсвечена (у кнопок звука «нажато» значит «выключено»)
        mixer.setChecked(self.apps_open)
        mixer.setToolTip("Громкость программ")
        if out is not None:
            self.lay.addLayout(self.volume_row(out, GLYPH_SPEAKER, GLYPH_MUTE, mixer))
        if self.apps_open:
            self.lay.addLayout(self.apps_box())
        if mic is not None:
            settings = self.icon_button(GLYPH_SETTINGS, lambda: (self.close(), os.startfile("ms-settings:sound")))
            settings.setToolTip("Параметры звука")
            self.lay.addLayout(self.volume_row(mic, GLYPH_MIC, GLYPH_MIC_OFF, settings))
        self.lay.addWidget(self.line())
        self.lay.addWidget(self.section("Устройство вывода"))
        self.lay.addLayout(self.device_rows(audio.OUTPUT))
        self.lay.addWidget(self.line())
        self.lay.addWidget(self.section("Устройство ввода"))
        self.lay.addLayout(self.device_rows(audio.INPUT))
        self.media_box = QVBoxLayout()
        self.media_box.setSpacing(8)
        self.lay.addLayout(self.media_box)
        self.fill_media()

    def rebuild(self):
        self.clear()
        self.build()
        self.refit()

    # -- плееры
    def on_media(self, sessions):
        state = [(s["app"], s["title"], s["artist"], s["playing"], bool(s["cover"])) for s in sessions]
        if state == self.media_state:
            return
        self.media_state = state
        self.sessions = sessions
        if self.isVisible():
            self.fill_media()
            self.refit()

    def fill_media(self):
        if self.media_box is None:
            return
        while self.media_box.count():
            item = self.media_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        sessions = getattr(self, "sessions", [])
        if not sessions:
            return
        self.media_box.addWidget(self.line())
        self.media_box.addWidget(self.section("Медиаплееры"))
        for s in sessions:
            self.media_box.addWidget(MediaCard(s, self.watcher))

    def showEvent(self, e):
        super().showEvent(e)
        self.watcher.refresh()
        self.media_timer.start()

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

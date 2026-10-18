"""Верхняя панель в стиле macOS: меню программы, часы, значки системы."""
import ctypes
import datetime
import os
import subprocess
import threading
import time

import psutil
import win32con
import win32gui
import win32process
from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from . import bluetooth
from .flyouts import (BatteryFlyout, BluetoothFlyout, ClockFlyout, LangFlyout, TrayFlyout, VolumeFlyout, WifiFlyout,
                      volume_endpoint,
                      volume_glyph)
from .ui import MDL2, TOP_FLAGS, MacMenu, ease_out, paint_background, per_frame, popup_menu, precise_timer, smooth
from .winapi import (app_windows, appbar_release, appbar_reserve, enable_blur, exe_description, force_foreground, keyboard_lang, press_keys,
                     syscommand, user32, window_exe)

DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS = ["янв.", "февр.", "мар.", "апр.", "мая", "июн.", "июл.", "авг.", "сент.", "окт.", "нояб.", "дек."]


class MenuBar(QWidget):
    def __init__(self, app, screen=None):
        super().__init__(None, TOP_FLAGS)
        self.app = app
        self.home = screen        # None — основной монитор, иначе дополнительный (там своё резервирование места)
        self.reserved = False
        self.cfg = app.cfg
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setWindowTitle("MiniDock Menu")
        self.target = 0           # окно, к которому относятся меню
        self.app_name = "Проводник"
        self.prev_name = None
        self.name_t = 1.0
        self.app_exe = ""
        self.hits = []
        self.active = None
        self.hover_key = None
        self.glow = {}            # key -> яркость подсветки 0..1 (анимируется)
        self.slide = 0.0          # 0 = панель спрятана вверх, 1 = на месте
        self.flyouts = {}
        self.open_flyout = None
        self.f_reg = QFont("Segoe UI", 10, QFont.DemiBold)
        self.f_bold = QFont("Segoe UI", 10, QFont.Bold)
        self.f_icon = QFont(MDL2)
        self.f_icon.setPointSizeF(10.5)
        self.f_mono = QFont("Segoe UI", 10, QFont.DemiBold)
        self.f_mono.setStyleHint(QFont.Monospace)
        try:
            self.f_mono.setFeature(QFont.Tag("tnum"), 1)  # цифры одинаковой ширины — часы не «прыгают»
        except Exception:
            pass
        self._state = None
        self.widget_w = {}  # ширина надписи виджета: (ширина, когда задана)
        app.widgets.changed.connect(self.update)
        self._battery = (0.0, None)  # (когда читали, значение) — заряд меняется медленно
        # часы обновляются раз в секунду ровно на границе секунды (а не 4 раза в секунду)
        self.timer = QTimer(self, singleShot=True, timeout=self.on_second)
        self.anim = precise_timer(self, 14, self.tick)
        self.on_second()

    def on_second(self):
        self.refresh()
        self.timer.start(1000 - datetime.datetime.now().microsecond // 1000 + 5)  # до начала следующей секунды

    @property
    def H(self):
        return int(self.cfg["bar_height"])

    def place(self):
        scr = self.home or QApplication.primaryScreen()
        g = scr.geometry()
        self.setGeometry(g.x(), g.y(), g.width(), self.H)
        if self.home is not None:  # на основном мониторе место резервирует рабочая область, здесь — сама панель
            dpr = scr.devicePixelRatio()
            x, y = round(g.x() * dpr), round(g.y() * dpr)
            appbar_reserve(int(self.winId()), (x, y, x + round(g.width() * dpr), y + round(self.H * dpr)))
            self.reserved = True
        self.apply_blur()
        self.update()

    def release(self):
        if self.reserved:
            appbar_release(int(self.winId()))
            self.reserved = False

    def apply_blur(self):
        if self.isVisible():
            enable_blur(int(self.winId()), bool(self.cfg["bar_blur"]))

    def showEvent(self, e):
        self.apply_blur()
        self.slide = 0.0
        self.anim.start()

    def clock_text(self):
        now = datetime.datetime.now()
        t = now.strftime("%H:%M:%S" if self.cfg["clock_seconds"] else "%H:%M")
        if self.cfg["clock_date"]:
            return f"{DAYS[now.weekday()]} {now.day} {MONTHS[now.month - 1]}  {t}"
        return t

    def battery(self):
        if not self.cfg["show_battery"]:
            return None
        when, value = self._battery
        if time.monotonic() - when > 30:
            value = psutil.sensors_battery()
            self._battery = (time.monotonic(), value)
        return value

    def refresh(self):
        bat = self.battery()
        vol = volume_endpoint(cached=True)
        try:
            vg = volume_glyph(vol[0].GetMasterVolumeLevelScalar(), vol[0].GetMute()) if vol else ""
        except Exception:
            vg = ""
        state = (self.clock_text(), keyboard_lang(), bat.percent if bat else None, vg)
        if state != self._state:
            self._state = state
            self.update()

    def on_foreground(self, hwnd):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return
        if pid == os.getpid():
            return
        cls = win32gui.GetClassName(hwnd) if hwnd else ""
        if cls in ("Progman", "WorkerW", "Shell_TrayWnd", ""):
            target, name, exe = 0, "Проводник", ""
        else:
            hwnd = user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
            exe = window_exe(hwnd)
            target = hwnd
            name = (exe_description(exe) if exe else None) or win32gui.GetWindowText(hwnd)[:40] or "Окно"
        self.target, self.app_exe = target, exe
        if name != self.app_name:  # название программы плавно сменяется
            self.prev_name, self.app_name, self.name_t = self.app_name, name, 0.0
            self.anim.start()
        self.update()

    def tick(self):
        busy = False
        if self.slide < 1:
            self.slide = min(1.0, self.slide + per_frame(0.07, self.anim.dt))
            busy = True
        if self.name_t < 1:
            self.name_t = min(1.0, self.name_t + per_frame(0.08, self.anim.dt))
            busy = True
        for key in set(self.glow) | {self.hover_key, self.active}:
            if key is None:
                continue
            target = 1.0 if key in (self.hover_key, self.active) else 0.0
            v = self.glow.get(key, 0.0)
            if abs(v - target) > 0.01:
                self.glow[key] = v + (target - v) * smooth(0.3, self.anim.dt)
                busy = True
            else:
                self.glow[key] = target
        self.glow = {k: v for k, v in self.glow.items() if v > 0}
        self.update()
        if not busy:
            self.anim.stop()

    # -- painting
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.translate(0, -self.H * (1 - ease_out(self.slide)))
        p.fillRect(self.rect(), QColor(24, 24, 28, int(self.cfg["bar_opacity"])))
        strip = QPainterPath()
        strip.addRect(QRectF(self.rect()))
        paint_background(p, strip, "menubar")
        p.setPen(QColor(255, 255, 255, 25))
        p.drawLine(0, self.H - 1, self.width(), self.H - 1)
        self.hits = []
        fm, fmb = QFontMetricsF(self.f_reg), QFontMetricsF(self.f_bold)
        h = float(self.H)

        def add(x, w, cb, key):
            r = QRectF(x, 3, w, h - 6)
            g = self.glow.get(key, 0.0)
            if g > 0:
                path = QPainterPath()
                path.addRoundedRect(r, 5, 5)
                p.fillPath(path, QColor(255, 255, 255, int((60 if key == self.active else 34) * g)))
            self.hits.append((r, cb, key))
            return r

        # логотип Windows
        r = add(8, 32, self.logo_menu, "logo")
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(245, 245, 248))
        cx, cy, s = r.center().x(), r.center().y(), 5.0
        for dx, dy in ((-1, -1), (0, -1), (-1, 0), (0, 0)):
            p.drawRect(QRectF(cx + dx * (s + 1) + 0.5, cy + dy * (s + 1) + 0.5, s, s))

        x = 44.0
        p.setFont(self.f_bold)
        w = fmb.horizontalAdvance(self.app_name) + 20
        r = add(x, w, self.app_menu, "app")
        k = ease_out(self.name_t)
        if self.prev_name and k < 1:
            p.setPen(QColor(250, 250, 252, int(255 * (1 - k))))
            p.drawText(r.translated(0, -8 * k), Qt.AlignCenter, self.prev_name)
        p.setPen(QColor(250, 250, 252, int(255 * k)))
        p.drawText(r.translated(0, 8 * (1 - k)), Qt.AlignCenter, self.app_name)
        x += w

        p.setFont(self.f_reg)
        p.setPen(QColor(245, 245, 248))
        menus = [(t, (lambda r, i=i: self.native_menu(r, i))) for i, t in enumerate(self.native_titles())]
        menus.append(("Окно", self.window_menu))
        for i, (title, cb) in enumerate(menus):
            w = fm.horizontalAdvance(title) + 18
            r = add(x, w, cb, f"m{i}")
            p.drawText(r, Qt.AlignCenter, title)
            x += w

        # правая часть: справа налево
        x = self.width() - 10.0
        clock, lang, bat, vol_glyph = self._state
        items = [("clock", clock, ClockFlyout, "clock")]
        if bat is not None:
            items.append(("text", f"{bat:.0f}%", BatteryFlyout, "bat"))
        items += [("text", lang, LangFlyout, "lang"),
                  ("icon", vol_glyph, VolumeFlyout, "vol"),
                  ("icon", "", WifiFlyout, "net"),
                  ("icon", "", lambda r: self.app.spotlight.toggle(), "search"),
                  ("icon", "", TrayFlyout, "tray")]
        if self.show_bt():  # Bluetooth — сразу левее Wi‑Fi
            items.insert(next(k for k, it in enumerate(items) if it[3] == "net") + 1,
                         ("icon", "", BluetoothFlyout, "bt"))
        host = self.app.widgets
        for wid in reversed(host.enabled()):  # виджеты — левее системных значков, в порядке включения
            items.append(("widget", host.bar_text(wid), (lambda r, wid=wid: self.toggle_widget(wid, r)), "w:" + wid))
        for kind, text, action, key in items:
            if kind == "widget":
                cls = host.classes.get(key[2:])
                painter = getattr(cls, "paint_icon", None)
                glyph = "" if painter or cls is None else cls.icon
                gw = 20 if painter else QFontMetricsF(self.f_icon).horizontalAdvance(glyph) + 6 if glyph else 0
                # ширина растёт сразу, а сужается не раньше чем через несколько секунд — надпись не «дёргается»
                tw = QFontMetricsF(self.f_mono).horizontalAdvance(text)
                keep = self.widget_w.get(key)
                if keep is None or tw > keep[0] or time.monotonic() - keep[1] > 4:
                    keep = self.widget_w[key] = (tw, time.monotonic())
                w = gw + keep[0] + 18
                x -= w
                r = add(x, w, action, key)
                p.setPen(QColor(245, 245, 248))
                if painter:  # свой значок (логотип) — квадрат по высоте текста
                    painter(p, QRectF(r.left() + 8, r.center().y() - 7, 14, 14))
                elif glyph:
                    p.setFont(self.f_icon)
                    p.drawText(QRectF(r.left() + 9, r.top(), gw, r.height()), Qt.AlignLeft | Qt.AlignVCenter, glyph)
                p.setFont(self.f_mono)
                p.drawText(QRectF(r.left() + 9 + gw, r.top(), keep[0], r.height()), Qt.AlignCenter, text)
                continue
            f = self.f_icon if kind == "icon" else self.f_mono if kind == "clock" else self.f_reg
            w = QFontMetricsF(f).horizontalAdvance(text) + 18
            if kind == "clock":  # ширина часов не меняется каждую секунду
                w = QFontMetricsF(f).horizontalAdvance(text.rstrip("0123456789") + "00") + 18
            x -= w
            cb = (lambda r, cls=action, key=key: self.toggle_flyout(cls, key, r)) \
                if isinstance(action, type) else action
            r = add(x, w, cb, key)
            p.setFont(f)
            p.setPen(QColor(245, 245, 248))
            p.drawText(r, Qt.AlignCenter, text)

    def show_bt(self):
        """Значок Bluetooth: включён в настройках и на компьютере есть адаптер (проверяем в фоне один раз)."""
        if not self.cfg.get("show_bluetooth", True):
            return False
        if bluetooth._has_radio is None and not getattr(MenuBar, "_bt_probe", False):
            MenuBar._bt_probe = True
            threading.Thread(target=bluetooth.has_radio, daemon=True).start()
        return bool(bluetooth._has_radio)

    def hit_at(self, pos):
        for r, cb, key in self.hits:
            if r.contains(pos):
                return r, cb, key
        return None

    def mouseMoveEvent(self, e):
        hit = self.hit_at(e.position())
        key = hit[2] if hit else None
        if key != self.hover_key:
            self.hover_key = key
            self.anim.start()

    def leaveEvent(self, e):
        self.hover_key = None
        self.anim.start()

    def mousePressEvent(self, e):
        hit = self.hit_at(e.position())
        if not hit:
            return
        r, cb, key = hit
        self.active = key
        self.anim.start()
        try:
            cb(r)
        finally:
            if self.open_flyout is None:
                self.active = None
                self.anim.start()

    def global_rect(self, r):
        tl = self.mapToGlobal(r.topLeft().toPoint())
        return QRectF(tl.x(), tl.y(), r.width(), r.height())

    def toggle_flyout(self, cls, key, r):
        fly = self.flyouts.get(cls)
        if fly is None:
            fly = self.flyouts[cls] = cls(self)
        self.open_flyout = fly
        self.active = key
        fly.open_under(self.global_rect(r))

    def toggle_widget(self, wid, r):
        from .barwidgets import WidgetFlyout
        key = ("widget", wid)
        fly = self.flyouts.get(key)
        if fly is None:
            fly = self.flyouts[key] = WidgetFlyout(self, self.app.widgets, wid)
        self.open_flyout = fly
        self.active = "w:" + wid
        fly.open_under(self.global_rect(r))

    def flyout_closed(self, fly):
        if self.open_flyout is fly:
            self.open_flyout = None
            self.active = None
            self.anim.start()

    def menu_at(self, menu, r):
        return popup_menu(menu, self.mapToGlobal(r.bottomLeft().toPoint()) + QPoint(0, 3))

    # -- menus
    def logo_menu(self, r):
        m = MacMenu(parent=self)
        m.addAction("Об этом компьютере", lambda: os.startfile("ms-settings:about"))
        m.addSeparator()
        m.addAction("Launchpad", self.app.launchpad.toggle)
        m.addAction("Параметры Windows…", lambda: os.startfile("ms-settings:"))
        m.addAction("Настройки MiniDock…", self.app.open_settings)
        m.addAction("Меню «Пуск» Windows", lambda: self.app.keys.system_start())
        m.addAction("Диспетчер задач", lambda: subprocess.Popen(["taskmgr.exe"]))
        m.addSeparator()
        m.addAction("Сон", lambda: ctypes.windll.powrprof.SetSuspendState(0, 0, 0))
        m.addAction("Перезагрузить…", lambda: self.confirm("Перезагрузить компьютер?", ["shutdown", "/r", "/t", "0"]))
        m.addAction("Выключить…", lambda: self.confirm("Выключить компьютер?", ["shutdown", "/s", "/t", "0"]))
        m.addSeparator()
        m.addAction("Заблокировать экран", lambda: user32.LockWorkStation())
        m.addAction("Завершить сеанс…", lambda: self.confirm("Выйти из системы?", ["shutdown", "/l"]))
        self.menu_at(m, r)

    def confirm(self, text, cmd):
        if QMessageBox.question(None, "MiniDock", text) == QMessageBox.Yes:
            subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)

    def app_windows(self):
        if not self.app_exe:
            return []
        return [h for exe, hs in app_windows().items() if exe == self.app_exe for h in hs]

    def app_menu(self, r):
        m = MacMenu(parent=self)
        name = self.app_name
        if self.app_exe:
            m.addAction(f"Скрыть «{name}»",
                        lambda: [self.minimize(h) for h in self.app_windows()])
            m.addAction("Скрыть остальные", self.hide_others)
            m.addSeparator()
            m.addAction("Показать в проводнике",
                        lambda: subprocess.Popen(["explorer.exe", "/select,", self.app_exe]))
            m.addSeparator()
            m.addAction(f"Завершить «{name}»",
                        lambda: [syscommand(h, win32con.SC_CLOSE) for h in self.app_windows()])
        else:
            m.addAction("Новое окно Проводника", lambda: subprocess.Popen(["explorer.exe"]))
            m.addAction("Свернуть все окна", lambda: press_keys(win32con.VK_LWIN, ord("D")))
        self.menu_at(m, r)

    def minimize(self, hwnd):
        self.app.animator.minimize(hwnd)  # сначала стоп-кадр окна, потом свёртывание — без мигания

    def hide_others(self):
        for exe, hs in app_windows().items():
            if exe != self.app_exe:
                for h in hs:
                    self.minimize(h)

    def window_menu(self, r):
        m = MacMenu(parent=self)
        h = self.target
        ok = bool(h) and win32gui.IsWindow(h)
        zoomed = ok and bool(user32.IsZoomed(h))
        for label, cmd in (("Свернуть", win32con.SC_MINIMIZE),
                           ("Восстановить" if zoomed else "Развернуть",
                            win32con.SC_RESTORE if zoomed else win32con.SC_MAXIMIZE),
                           ("Закрыть окно", win32con.SC_CLOSE)):
            a = m.addAction(label, lambda cmd=cmd: self.minimize(h) if cmd == win32con.SC_MINIMIZE else syscommand(h, cmd))
            a.setEnabled(ok)
        m.addSeparator()
        m.addAction("Все окна (Task View)", lambda: press_keys(win32con.VK_LWIN, win32con.VK_TAB))
        self.menu_at(m, r)

    # -- нативные меню классических программ (Блокнот, Paint и т.п.)
    def native_titles(self):
        hmenu = user32.GetMenu(self.target) if self.target else None
        if not hmenu:
            return []
        titles = []
        for i in range(min(max(user32.GetMenuItemCount(hmenu), 0), 12)):
            buf = ctypes.create_unicode_buffer(128)
            user32.GetMenuStringW(hmenu, i, buf, 128, 0x400)
            text = buf.value.replace("&", "").split("\t")[0].strip()
            if text and user32.GetSubMenu(hmenu, i):
                titles.append(text)
        return titles[:8]

    def native_menu(self, r, index):
        hwnd = self.target
        hmenu = user32.GetMenu(hwnd)
        subs = [user32.GetSubMenu(hmenu, i) for i in range(user32.GetMenuItemCount(hmenu))]
        subs = [s for s in subs if s]
        if index >= len(subs):
            return
        m = MacMenu(parent=self)
        self.fill_native(m, subs[index], hwnd, index)
        if m.isEmpty():
            m.addAction("(пусто)").setEnabled(False)
        self.menu_at(m, r)

    def fill_native(self, qmenu, hmenu, hwnd, index):
        res = ctypes.c_size_t()
        user32.SendMessageTimeoutW(hwnd, win32con.WM_INITMENUPOPUP, hmenu, index, 2, 200, ctypes.byref(res))
        for i in range(max(user32.GetMenuItemCount(hmenu), 0)):
            state = user32.GetMenuState(hmenu, i, 0x400)
            if state & 0x800:  # MF_SEPARATOR
                qmenu.addSeparator()
                continue
            buf = ctypes.create_unicode_buffer(256)
            user32.GetMenuStringW(hmenu, i, buf, 256, 0x400)
            text = buf.value
            if not text:
                continue
            sub = user32.GetSubMenu(hmenu, i)
            if sub:
                self.fill_native(qmenu.sub(text), sub, hwnd, i)
                continue
            cmd_id = user32.GetMenuItemID(hmenu, i)
            a = qmenu.addAction(text, lambda cmd_id=cmd_id: self.run_native(hwnd, cmd_id))
            a.setEnabled(not state & 0x3)
            if state & 0x8:
                a.setCheckable(True)
                a.setChecked(True)

    def run_native(self, hwnd, cmd_id):
        force_foreground(hwnd)
        win32gui.PostMessage(hwnd, win32con.WM_COMMAND, cmd_id, 0)

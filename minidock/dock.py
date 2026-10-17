"""Док: иконки с увеличением, автоскрытие, перетаскивание, положение снизу/слева/справа.

Раскладка считается в координатах (u, v): u — вдоль края экрана, v — от края экрана внутрь.
Перевод в координаты окна делают to_win_rect / to_win_pt / to_uv.
"""
import math
import os
import subprocess
import time
from pathlib import Path

import win32api
import win32con
import win32gui
from PySide6.QtCore import QPointF, QRect, QRectF, QSizeF, Qt, QTimer
from PySide6.QtGui import (QAction, QActionGroup, QColor, QCursor, QFont, QFontMetricsF, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

from .folders import FolderPopup, folder_pixmap
from .ui import (GLASS, TOP_FLAGS, MacMenu, lerp_rect, live_for, make_glass, paint_background, popup_menu, precise_timer,
                 smooth)
from .winapi import (appbar_release, appbar_reserve, exe_description, extract_icon, file_icon, resolve_lnk, syscommand,
                     window_monitor)


class Item:
    def __init__(self, kind, path="", name="", pixmap=None):
        self.kind = kind  # app | running | folder | sep | trash | launchpad | widget
        self.widget = None  # id виджета (kind == "widget")
        self.path = path
        self.name = name
        self.pix = pixmap
        self.pix_cache = {}
        self.exe = ""
        self.hwnds = []
        self.bounce_start = None
        self.cur = None  # текущий (анимированный) прямоугольник в координатах (u, v)
        self.children = []  # программы в папке (kind == "folder")
        self.folder = None  # папка, в которой лежит программа

    @classmethod
    def make_folder(cls, name, children):
        f = cls("folder", name=name)
        f.children = list(children)
        for c in f.children:
            c.folder, c.kind, c.cur = f, "app", None
        f.refresh_icon()
        return f

    def refresh_icon(self):
        self.pix = folder_pixmap(self.children)
        self.pix_cache = {}

    @classmethod
    def from_path(cls, path, kind="app"):
        it = cls(kind, path)
        lower = path.lower()
        it.exe = (resolve_lnk(path)[0] if lower.endswith(".lnk") else path).lower()
        if lower.endswith(".exe"):
            it.name = exe_description(path) or Path(path).stem
        else:
            it.name = Path(path).stem or path
        it.pix = file_icon(path)
        return it

    def pix_for(self, size_px):
        """Иконка, заранее уменьшенная качественным алгоритмом до нужного размера (без «пикселей»)."""
        s = max(int(round(size_px / 2)) * 2, 2)
        pix = self.pix_cache.get(s)
        if pix is None:
            if len(self.pix_cache) > 48:
                self.pix_cache.clear()
            pix = self.pix.scaled(s, s, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.pix_cache[s] = pix
        return pix

    def owns_exe(self, exe):
        if self.kind == "folder":
            return any(c.owns_exe(exe) for c in self.children)
        if not self.exe:
            return False
        if exe == self.exe:
            return True
        # Лаунчеры (Discord Update.exe, Steam) открывают окна из других exe в своей папке
        folder = os.path.dirname(self.exe) + os.sep
        sysroot = os.environ.get("SystemRoot", r"C:\Windows").lower()
        return not folder.startswith(sysroot) and exe.startswith(folder)


def make_trash():
    shell32_dll = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "shell32.dll")
    return Item("trash", name="Корзина", pixmap=extract_icon(shell32_dll, 31))


def launchpad_pixmap():
    """Иконка Launchpad: сетка цветных квадратов на тёмном скруглённом фоне."""
    pix = QPixmap(256, 256)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    grad = QLinearGradient(0, 0, 0, 256)
    grad.setColorAt(0, QColor(70, 70, 82))
    grad.setColorAt(1, QColor(30, 30, 38))
    path = QPainterPath()
    path.addRoundedRect(QRectF(16, 16, 224, 224), 52, 52)
    p.fillPath(path, grad)
    colors = ["#ff5f57", "#febc2e", "#28c840", "#0a84ff", "#bf5af2", "#ff9f0a", "#64d2ff", "#ff375f", "#30d158"]
    for i, c in enumerate(colors):
        x, y = 52 + (i % 3) * 56, 52 + (i // 3) * 56
        sq = QPainterPath()
        sq.addRoundedRect(QRectF(x, y, 40, 40), 11, 11)
        p.fillPath(sq, QColor(c))
    p.end()
    return pix


class Dock(QWidget):
    def __init__(self, app, screen=None):
        super().__init__(None, TOP_FLAGS)
        self.app = app
        self.home = screen        # None — основной монитор, иначе дополнительный (настройка «Док на всех мониторах»)
        self.reserved = False
        self.cfg = app.cfg
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        self.setWindowTitle("MiniDock")

        self.widget_flyouts = {}  # id виджета -> его окно над доком
        app.widgets.changed.connect(self.on_widget_data)
        self.pinned = self.load_pinned(self.cfg["pinned"])
        self.folder_popup = FolderPopup(self)
        self.merge_target = None  # иконка, на которую сейчас «бросят» перетаскиваемую — получится папка
        self.running = []
        self.sep1, self.sep2 = Item("sep"), Item("sep")
        self.trash = make_trash()
        self.launchpad = Item("launchpad", name="Launchpad", pixmap=launchpad_pixmap())
        self.icon_cache = {}
        self.glass = make_glass(self, part="dock")  # размытый фон: снимается в момент, когда док начинает выезжать
        self.glass.region = self.glass_region
        self.glass.can_capture = self.glass_allowed

        self.hover = 0.0
        self.hide_k = 0.0          # 0 = док виден, 1 = спрятан
        self.last_seen = time.monotonic()
        self.reveal_until = 0.0
        self.mouse_u = -1.0
        self.base_span = (0.0, 0.0)
        self.rects = []
        self.targets = {}
        self.bg = QRectF()
        self.bg_cur = None
        self.zone = QRectF()
        self.press_item = None
        self.press_pos = None
        self.dragging = False
        self.menu_open = False
        self.poofs = []
        self.first_layout = True
        self.font_label = QFont("Segoe UI", 9)

        # частый таймер работает только пока что-то движется; в простое — редкая проверка курсора
        self.anim = precise_timer(self, 14, self.tick)
        self.still = 0
        self.last_cursor = None
        self.idle_timer = QTimer(self, interval=50, timeout=self.idle_check)
        self.idle_timer.start()
        self.anim.start()

    # -- geometry
    @property
    def B(self):
        return float(self.cfg["icon_size"])

    @property
    def mag(self):
        return max(float(self.cfg["magnify"]), 1.0)

    @property
    def pad(self):
        return self.B * 0.16

    @property
    def margin(self):
        return float(self.cfg["margin"])

    @property
    def side(self):
        return self.cfg["position"]

    def bar_height(self):
        """Толщина самой полоски дока (без увеличения)."""
        return self.B + 2 * self.pad

    def hide_distance(self):
        return self.bar_height() + self.margin + 6

    def reserve_thickness(self):
        """Сколько места у края занимает видимый док (для рабочей области)."""
        return self.bar_height() + self.margin + 2

    def place(self):
        scr = self.home or QApplication.primaryScreen()
        g = scr.geometry()
        if self.side == "bottom":
            base = g if self.cfg["replace_taskbar"] else scr.availableGeometry()
            depth = int(self.B * self.mag + self.pad * 2 + self.B + 40 + self.margin)
            self.area = QRect(base.x(), base.y() + base.height() - depth, base.width(), depth)
        else:
            top = self.app.bar_height() if self.cfg["menubar"] else 0
            depth = int(self.B * self.mag + self.pad * 2 + self.margin + 260)
            x = g.x() if self.side == "left" else g.x() + g.width() - depth
            self.area = QRect(x, g.y() + top, depth, g.height() - top)
        self.setGeometry(self.fitted(self.area))
        self.reserve()
        self.first_layout = True
        self.update()
        self.wake()

    def monitor(self):
        return window_monitor(int(self.winId()))[0]

    def reserve(self):
        """На дополнительном мониторе место под док (без автоскрытия) резервирует сам док —
        на основном это делает рабочая область."""
        if self.home is None:
            return
        if self.cfg["autohide"]:
            self.release()
            return
        _, (l, t, r, b) = window_monitor(int(self.winId()))
        thick = round(self.reserve_thickness() * self.home.devicePixelRatio())
        edge = {"bottom": 3, "left": 0, "right": 2}[self.side]
        rect = {3: (l, b - thick, r, b), 0: (l, t, l + thick, b), 2: (r - thick, t, r, b)}[edge]
        appbar_reserve(int(self.winId()), rect, edge)
        self.reserved = True

    def release(self):
        if self.reserved:
            appbar_release(int(self.winId()))
            self.reserved = False

    def reload_pinned(self):
        """Закреплённые поменяли в доке на другом мониторе — перечитать."""
        if self.folder_popup.isVisible():
            self.folder_popup.close()
        self.pinned = self.load_pinned(self.cfg["pinned"])
        self.wake()

    def span_needed(self):
        """Длина окна вдоль края: сам док + запас на увеличение иконок и подписи. Окно не тянется на весь
        экран — каждый кадр Windows копирует окно целиком, узкое окно рисуется в такт монитору."""
        items = self.items()
        gap = self.B * float(self.cfg["spacing"])
        base = sum(self.B * 0.3 if it.kind == "sep" else self.B for it in items) + gap * max(len(items) - 1, 0)
        return int(base + 2 * self.pad + self.B * (self.mag - 1) * 3.5 + 2 * 170)

    def fitted(self, area):
        span = self.span_needed()
        if self.side == "bottom":
            w = min(area.width(), span)
            return QRect(area.x() + (area.width() - w) // 2, area.y(), w, area.height())
        h = min(area.height(), span)
        return QRect(area.x(), area.y() + (area.height() - h) // 2, area.width(), h)

    def refit(self):
        """Иконок стало больше/меньше — подогнать длину окна, не сдвигая док на экране."""
        if not hasattr(self, "area"):
            return
        new = self.fitted(self.area)
        if new == self.geometry():
            return
        old = self.geometry()
        du = (old.x() - new.x()) if self.side == "bottom" else (old.y() - new.y())
        self.setGeometry(new)
        for it in self.items() + self.running:  # анимированные позиции — в координатах окна
            if it.cur is not None:
                it.cur = it.cur.translated(du, 0)
        if self.bg_cur is not None:
            self.bg_cur = self.bg_cur.translated(du, 0)
        self.wake()

    def length(self):
        return float(self.width() if self.side == "bottom" else self.height())

    def to_win_rect(self, r):
        u, v, du, dv = r.x(), r.y(), r.width(), r.height()
        if self.side == "bottom":
            return QRectF(u, self.height() - v - dv, du, dv)
        if self.side == "left":
            return QRectF(v, u, dv, du)
        return QRectF(self.width() - v - dv, u, dv, du)

    def to_win_pt(self, u, v):
        if self.side == "bottom":
            return QPointF(u, self.height() - v)
        if self.side == "left":
            return QPointF(v, u)
        return QPointF(self.width() - v, u)

    def to_uv(self, p):
        if self.side == "bottom":
            return QPointF(p.x(), self.height() - p.y())
        if self.side == "left":
            return QPointF(p.y(), p.x())
        return QPointF(p.y(), self.width() - p.x())

    def items(self):
        items = [self.launchpad] if self.cfg["show_launchpad"] else []
        items += self.pinned
        if self.cfg["show_running"] and self.running:
            items += [self.sep1] + self.running
        if self.cfg["show_trash"]:
            items += [self.sep2, self.trash]
        return items

    def layout(self):
        B, mag, pad = self.B, self.mag, self.pad
        gap = B * float(self.cfg["spacing"])
        L = self.length()
        items = self.items()
        if self.dragging and self.drag_remove():
            items = [it for it in items if it is not self.press_item]
        base = [B * 0.3 if it.kind == "sep" else B for it in items]
        base_total = sum(base) + gap * (len(items) - 1) + 2 * pad
        u = (L - base_total) / 2 + pad
        self.base_span = ((L - base_total) / 2, (L + base_total) / 2)
        sizes = []
        for it, w in zip(items, base):
            c = u + w / 2
            u += w + gap
            s = 1.0
            if it.kind != "sep" and self.hover > 0 and mag > 1:
                d, reach = abs(self.mouse_u - c), B * 2.6
                if d < reach:
                    s += (mag - 1) * (math.cos(math.pi * d / reach) + 1) / 2 * self.hover
            sizes.append(w * s)
        total = sum(sizes) + gap * (len(items) - 1) + 2 * pad
        bg_h = self.bar_height()
        v0 = self.margin - self.hide_k * self.hide_distance()
        bg = QRectF((L - total) / 2, v0, total, bg_h)
        rects, u = [], bg.left() + pad
        for it, s in zip(items, sizes):
            if it.kind == "sep":
                rects.append((it, QRectF(u, v0 + pad * 1.4, s, B - pad * 0.8)))
            else:
                rects.append((it, QRectF(u, v0 + pad, s, s)))
            u += s + gap
        zone = QRectF(bg.left(), -2, bg.width(), self.margin + bg_h + B * (mag - 1) + 6)
        return bg, rects, zone

    def item_at(self, uv):
        for it, r in self.rects:
            if it.kind == "sep" or (self.dragging and it is self.press_item):
                continue
            if r.left() - 3 <= uv.x() <= r.right() + 3 and -2 <= uv.y() <= r.bottom():
                return it
        return None

    def all_apps(self):
        """Закреплённые и запущенные программы, включая лежащие в папках (без самих папок)."""
        out = []
        for it in self.pinned:
            if it.kind != "widget":
                out += it.children if it.kind == "folder" else [it]
        return out + self.running

    def item_for_hwnd(self, hwnd, exe=None):
        apps = self.all_apps()
        for it in apps:
            if hwnd in it.hwnds:
                return it
        if exe:
            for it in apps:
                if it.owns_exe(exe):
                    return it
        return None

    def icon_screen_rect(self, item):
        """Прямоугольник иконки в физических пикселях — там, где она будет у видимого дока."""
        dist = self.hide_k * self.hide_distance()
        if item is not None and item.folder is not None:  # программа из папки — анимация к иконке папки
            item = item.folder
        if item is not None and item.cur is not None:
            r = item.cur.translated(0, dist)
            side = max(r.width(), self.B * 0.6)
            r = QRectF(r.center().x() - side / 2, r.top(), side, side)
        else:
            L = self.length()
            r = QRectF(L / 2 - self.B / 2, self.margin + self.pad, self.B, self.B)
        w = self.to_win_rect(r)
        dpr = self.devicePixelRatioF()
        wl, wt = win32gui.GetWindowRect(int(self.winId()))[:2]  # от настоящего положения окна — на любом мониторе
        return (round(wl + w.left() * dpr), round(wt + w.top() * dpr), round(wl + w.right() * dpr),
                round(wt + w.bottom() * dpr))

    def icon_global_rect(self, item):
        """Иконка в логических координатах экрана (для всплывающих окон Qt)."""
        if item is not None and item.folder is not None:
            item = item.folder
        r = item.cur if item is not None and item.cur is not None else QRectF(self.length() / 2 - self.B / 2,
                                                                                self.margin + self.pad, self.B, self.B)
        w = self.to_win_rect(r.translated(0, self.hide_k * self.hide_distance()))
        tl = self.mapToGlobal(w.topLeft().toPoint())
        return QRectF(tl.x(), tl.y(), w.width(), w.height())

    def reveal(self, seconds=1.2):
        self.reveal_until = time.monotonic() + seconds
        self.wake()

    # -- animation
    def at_screen_edge(self, gpos):
        # курсор должен быть на мониторе этого дока: иначе у мониторов одной высоты, стоящих рядом,
        # нижний край одного «будит» доки на всех
        scr = (self.home or QApplication.primaryScreen()).geometry()
        if self.side == "bottom":
            return gpos.y() >= scr.bottom() - 1 and scr.left() <= gpos.x() <= scr.right()
        if self.side == "left":
            return gpos.x() <= scr.left() and scr.top() <= gpos.y() <= scr.bottom()
        return gpos.x() >= scr.right() - 1 and scr.top() <= gpos.y() <= scr.bottom()

    def tick(self):
        now = time.monotonic()
        gpos = QCursor.pos()
        uv = self.to_uv(QPointF(self.mapFromGlobal(gpos)))

        target_hide = 0.0
        preview = self.app.preview
        if self.cfg["autohide"]:
            in_zone = self.zone.contains(uv) and self.hide_k < 0.6
            if self.at_screen_edge(gpos) or in_zone or self.dragging or self.menu_open or now < self.reveal_until \
                    or preview.mouse_inside():
                self.last_seen = now
            if now - self.last_seen > float(self.cfg["autohide_delay"]):
                target_hide = 1.0
        if self.blocked():
            target_hide = 1.0  # открыт Launchpad — док уезжает за край и не отзывается
            self.last_seen = 0.0
        changed = False
        if self.glass.enabled and self.hide_k < 0.99 and self.isVisible():
            if not self.glass.live_on and self.glass.start_live():
                pass  # живой фон: обновляется сам, пока док на экране
        elif self.glass.live_on:
            self.glass.stop_live()  # док спрятан — фон под ним снимать незачем
        if target_hide == 0.0 and self.hide_k >= 0.99 and self.glass.enabled and not self.glass.live_on:
            self.glass.refresh()  # док начинает выезжать — свежий фон, пока его ещё нет на экране
        if self.hide_k != target_hide:
            self.hide_k += (target_hide - self.hide_k) * smooth(0.2, self.anim.dt)
            if abs(self.hide_k - target_hide) < 0.01:
                self.hide_k = target_hide
            changed = True

        inside = (self.zone.contains(uv) and self.hide_k < 0.3) or self.dragging
        hover_target = 1.0 if inside else 0.0
        if inside and uv.x() != self.mouse_u:
            self.mouse_u = uv.x()
            changed = True
        if self.hover != hover_target:
            self.hover += (hover_target - self.hover) * smooth(0.22, self.anim.dt)
            if abs(self.hover - hover_target) < 0.01:
                self.hover = hover_target
            changed = True

        if not self.dragging and abs(self.span_needed() - self.length()) > 2:
            self.refit()
        bg, rects, self.zone = self.layout()
        if self.first_layout:
            self.bg_cur = bg
            for it, r in rects:
                it.cur = QRectF(r)
            self.first_layout = False
            changed = True
        k = smooth(0.38, self.anim.dt)
        self.bg_cur, c = lerp_rect(self.bg_cur, bg, k)
        changed |= c
        self.targets = {}
        for it, r in rects:
            if it.cur is None:  # новая иконка «вырастает» из точки
                it.cur = QRectF(r.center(), QSizeF(0, 0)) if it.kind != "sep" else QRectF(r)
            it.cur, c = lerp_rect(it.cur, r, k)
            changed |= c
            self.targets[it] = r
        self.rects = [(it, it.cur) for it, _ in rects]
        self.bg = self.bg_cur

        for it in self.items():
            if it.bounce_start is not None:
                changed = True
                if now - it.bounce_start > 1.6 or (it.hwnds and now - it.bounce_start > 0.5):
                    it.bounce_start = None
        self.poofs = [(p, t) for p, t in self.poofs if now - t < 0.4]
        changed |= bool(self.poofs) or self.dragging

        # предпросмотр окон при наведении на запущенное приложение
        hovered = self.item_at(uv) if self.hover > 0.5 and self.hide_k < 0.1 and not self.dragging else None
        if hovered is not None and hovered.kind in ("app", "running") and hovered.hwnds:
            w = self.to_win_rect(self.targets.get(hovered, hovered.cur))
            tl = self.mapToGlobal(w.topLeft().toPoint())
            preview.request(hovered, QRectF(tl.x(), tl.y(), w.width(), w.height()))
        elif preview.isVisible() or preview.pending:
            preview.leave()
        if changed:
            self.update()
            self.still = 0
        else:
            # всё на месте несколько кадров подряд — останавливаем частый таймер, дальше следит idle_check
            self.still += 1
            if self.still > 6:
                self.anim.stop()

    def wake(self):
        """Включить плавную анимацию (наведение, выезд, перетаскивание, изменения иконок)."""
        self.still = 0
        if not self.anim.isActive():
            self.anim.start()

    def blocked(self):
        """Пока открыт Launchpad, доком пользоваться нельзя."""
        lp = self.app.launchpad
        return lp.isVisible() and not lp.closing

    def idle_check(self):
        """Лёгкая проверка в простое (20 раз в секунду): не пора ли проснуться."""
        if self.anim.isActive() or not self.isVisible() or self.blocked():
            return
        now = time.monotonic()
        gpos = QCursor.pos()
        moved = gpos != self.last_cursor
        self.last_cursor = gpos
        uv = self.to_uv(QPointF(self.mapFromGlobal(gpos)))
        in_zone = self.zone.contains(uv) and self.hide_k < 0.6
        at_edge = self.at_screen_edge(gpos)
        need = (moved and (in_zone or self.hover > 0)) or (at_edge and self.hide_k > 0) \
            or (now < self.reveal_until and self.hide_k > 0)
        if self.cfg["autohide"]:
            if at_edge or in_zone or self.menu_open or self.app.preview.mouse_inside() or self.widget_open():
                self.last_seen = now
            elif self.hide_k < 1 and now - self.last_seen > float(self.cfg["autohide_delay"]):
                need = True  # пора прятать док
        if need:
            self.wake()

    # -- «стекло» под доком
    def glass_region(self):
        """Полоса, где может оказаться фон дока (с запасом на увеличение иконок при наведении)."""
        lo, hi = self.base_span
        extra = self.B * (self.mag - 1) * 2.5 + 20
        r = QRectF(lo - extra, self.margin - 2, (hi - lo) + 2 * extra, self.bar_height() + 4)
        return self.to_win_rect(r).intersected(QRectF(self.rect()))

    def glass_allowed(self):
        # фон можно снимать, только пока дока на этом месте нет — иначе он попадёт в собственный снимок
        return self.hide_k >= 0.99 or not self.isVisible()

    def apply_glass(self):
        # без автоскрытия док не уходит с экрана и фон под ним не переснять — тогда только тонировка
        # снимок фона возможен только пока дока нет на экране (значит, нужно автоскрытие); живой фон — всегда
        live = live_for("dock")
        self.glass.enabled = GLASS["enabled"] and bool(self.cfg["dock_blur"]) and (bool(self.cfg["autohide"]) or live)
        if not self.glass.enabled or not live:
            self.glass.stop_live()
        if not self.glass.enabled:
            self.glass.pix = None
            self.update()

    def hideEvent(self, e):
        self.glass.stop()
        self.glass.stop_live()

    def showEvent(self, e):
        self.apply_glass()
        self.glass.start()

    # -- polling
    def poll_windows(self, wins):
        claimed = set()
        for it in self.all_apps():
            if it.kind != "app":
                continue
            it.hwnds = []
            for exe, hwnds in wins.items():
                if it.owns_exe(exe):
                    it.hwnds += hwnds
                    claimed.add(exe)
        for f in self.pinned:
            if f.kind == "folder":
                f.hwnds = [h for c in f.children for h in c.hwnds]
        old = {it.exe: it for it in self.running}
        running = []
        now = time.monotonic()
        for exe, hwnds in wins.items():
            if exe in claimed or not os.path.exists(exe):
                continue
            it = old.get(exe)
            if it is None:
                it = self.icon_cache.get(exe) or Item.from_path(exe, kind="running")
                self.icon_cache[exe] = it
                it.cur = None
            it.hwnds = hwnds
            it.seen_at = now
            running.append(it)
        # окна на миг пропали (программа Windows «уснула», окно перестраивается) — иконку не убираем сразу,
        # иначе она мигает в доке
        for exe, it in old.items():
            if it not in running and exe not in claimed and now - getattr(it, "seen_at", 0) < 3:
                it.hwnds = [h for h in it.hwnds if win32gui.IsWindow(h)]
                running.insert(min(self.running.index(it), len(running)), it)
        self.running = running
        # перерисовываем, только если что-то видимое поменялось (иконки или точки запущенных)
        signature = tuple((id(it), bool(it.hwnds)) for it in self.items())
        if signature != getattr(self, "_signature", None):
            self._signature = signature
            self.wake()

    # -- painting
    def paintEvent(self, e):
        p = QPainter(self)
        # Стираем прошлый кадр сами: если Qt этого не сделал, старые кадры наслаиваются «шлейфом»,
        # а размытый фон снимает этот шлейф и размножает его.
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(e.rect(), Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        B = self.B
        dpr = self.devicePixelRatioF()
        now = time.monotonic()
        cfg = self.cfg

        radius = B * float(cfg["dock_radius"])
        path = QPainterPath()
        path.addRoundedRect(self.to_win_rect(self.bg), radius, radius)
        self.glass.fill(p, path)  # размытый фон: заливка пути, поэтому края скругления сглажены
        color = QColor(cfg["dock_color"])
        color.setAlpha(int(cfg["dock_opacity"]))
        p.fillPath(path, color)
        paint_background(p, path, "dock")
        if cfg["dock_border"]:
            p.setPen(QPen(QColor(255, 255, 255, 50), 1))
            p.drawPath(path)

        uv_mouse = self.to_uv(QPointF(self.mapFromGlobal(QCursor.pos())))
        hovered = self.item_at(uv_mouse) if self.hover > 0.5 and self.hide_k < 0.1 else None
        for it, r in self.rects:
            if it.kind == "sep":
                p.setPen(QPen(QColor(255, 255, 255, 60), 1))
                c = r.center().x()
                p.drawLine(self.to_win_pt(c, r.top()), self.to_win_pt(c, r.bottom()))
                continue
            if self.dragging and it is self.press_item:
                continue  # рисуется под курсором ниже, слот остаётся пустым
            if it is self.merge_target and self.dragging:  # сюда бросят иконку — получится папка
                hp = QPainterPath()
                m = r.width() * 0.12
                hp.addRoundedRect(self.to_win_rect(r.adjusted(-m, -m, m, m)), r.width() * 0.28, r.width() * 0.28)
                p.fillPath(hp, QColor(255, 255, 255, 70))
            dv = 0.0
            if it.bounce_start is not None:
                dv = abs(math.sin((now - it.bounce_start) * math.pi * 1.6)) * B * 0.45
            if it.kind == "widget" and r.width() > 1:
                self.app.widgets.paint_tile(it.widget, p, self.to_win_rect(r.translated(0, dv)))
            elif it.pix and r.width() > 1:
                pix = it.pix_for(r.width() * dpr)
                p.drawPixmap(self.to_win_rect(r.translated(0, dv)), pix, QRectF(pix.rect()))
            if it.hwnds and cfg["indicator"] != "none":
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(235, 235, 240, 230))
                cu, cv = r.center().x(), self.bg.top() + self.pad * 0.45
                if cfg["indicator"] == "line":
                    p.drawRoundedRect(self.to_win_rect(QRectF(cu - B * 0.18, cv - 1.2, B * 0.36, 2.4)), 1.2, 1.2)
                else:
                    p.drawEllipse(self.to_win_pt(cu, cv), 1.8, 1.8)

        if self.dragging and self.press_item is not None and self.press_item.pix:
            s = B * (1 + (self.mag - 1) * 0.6)
            pos = QPointF(self.mapFromGlobal(QCursor.pos()))
            p.setOpacity(0.45 if self.drag_remove() else 0.9)
            pix = self.press_item.pix_for(s * dpr)
            p.drawPixmap(QRectF(pos.x() - s / 2, pos.y() - s / 2, s, s), pix, QRectF(pix.rect()))
            p.setOpacity(1.0)

        for center, t0 in self.poofs:  # «пуф» при удалении из дока
            t = (now - t0) / 0.4
            p.setPen(Qt.NoPen)
            for k in range(5):
                ang = k * math.tau / 5 + t
                rad = B * (0.2 + 0.5 * t)
                p.setBrush(QColor(230, 230, 235, int(200 * (1 - t))))
                p.drawEllipse(center + QPointF(math.cos(ang) * rad, math.sin(ang) * rad), B * 0.18 * (1 - t * 0.5),
                              B * 0.18 * (1 - t * 0.5))

        preview_open = self.app.preview.isVisible() and self.app.preview.item is hovered
        if hovered is not None and not self.dragging and cfg["show_labels"] and not preview_open:
            r = hovered.cur
            self.draw_label(p, hovered.name, self.to_win_pt(r.center().x(), r.bottom() + 9))

    def draw_label(self, p, text, anchor):
        p.setFont(self.font_label)
        fm = QFontMetricsF(self.font_label)
        w, h = fm.horizontalAdvance(text) + 18, fm.height() + 6
        if self.side == "bottom":
            x, y = anchor.x() - w / 2, anchor.y() - h
        elif self.side == "left":
            x, y = anchor.x(), anchor.y() - h / 2
        else:
            x, y = anchor.x() - w, anchor.y() - h / 2
        x = min(max(x, 2), self.width() - w - 2)
        y = min(max(y, 2), self.height() - h - 2)
        box = QRectF(x, y, w, h)
        path = QPainterPath()
        path.addRoundedRect(box, 5, 5)
        p.fillPath(path, QColor(40, 40, 46, 230))
        p.setPen(QColor(240, 240, 245))
        p.drawText(box, Qt.AlignCenter, text)

    # -- mouse
    def drag_remove(self):
        uv = self.to_uv(QPointF(self.mapFromGlobal(QCursor.pos())))
        return self.press_item in self.pinned and uv.y() > self.bg.bottom() + self.B * 1.1

    def mousePressEvent(self, e):
        if self.blocked():
            return
        self.wake()
        self.app.preview.close_preview()
        if e.button() == Qt.LeftButton:
            self.press_item = self.item_at(self.to_uv(e.position()))
            self.press_pos = e.position()
            self.dragging = False

    def mouseMoveEvent(self, e):
        it = self.press_item
        if not (e.buttons() & Qt.LeftButton) or it is None or it.kind not in ("app", "running", "folder"):
            return
        if not self.dragging:
            if (e.position() - self.press_pos).manhattanLength() < 12:  # дрогнувшая рука — ещё клик
                return
            self.dragging = True
            if it.kind == "running":  # перетаскивание запущенного = закрепить
                self.pin_item(it)
        u = self.to_uv(e.position()).x()
        others = [o for o in self.pinned if o is not it]
        # курсор над серединой другой иконки — не переставляем, а предлагаем сложить в папку
        self.merge_target = None
        if it.kind not in ("folder", "widget") and not self.drag_remove():
            for o in others:
                r = self.targets.get(o)
                if r is not None and o.kind in ("app", "folder") and abs(u - r.center().x()) < r.width() * 0.3:
                    self.merge_target = o
                    return
        idx = sum(1 for o in others if o in self.targets and self.targets[o].center().x() < u)
        if self.pinned.index(it) != idx:
            self.pinned.remove(it)
            self.pinned.insert(idx, it)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        it, was_drag = self.press_item, self.dragging
        removing = was_drag and self.drag_remove()
        target, self.merge_target = self.merge_target, None
        self.press_item, self.dragging = None, False
        if it is None:
            return
        if was_drag:
            if target is not None and not removing:
                self.add_to_folder(it, target)
            elif removing:
                self.poofs.append((QPointF(e.position()), time.monotonic()))
                self.unpin(it)
            else:
                # иконка «прилетает» из-под курсора на своё место
                s = self.B * (1 + (self.mag - 1) * 0.6)
                uv = self.to_uv(e.position())
                it.cur = QRectF(uv.x() - s / 2, uv.y() - s / 2, s, s)
                self.save_pinned()
        elif self.rect().contains(e.position().toPoint()):
            # клик засчитываем по иконке, на которую нажали: от увеличения она могла съехать из-под курсора
            self.activate(it)

    def activate(self, it, new_window=False):
        if it.kind == "widget":
            self.open_widget(it)
            return
        if it.kind == "trash":
            subprocess.Popen(["explorer.exe", "shell:RecycleBinFolder"])
            return
        if it.kind == "launchpad":
            self.app.launchpad.toggle()
            return
        if it.kind == "folder":
            if self.folder_popup.isVisible() and self.folder_popup.folder is it:
                self.folder_popup.close()
            else:
                self.folder_popup.open(it)
            return
        # окна, которые на самом деле ещё есть (список обновляется раз в секунду — программа могла закрыться)
        alive = [h for h in it.hwnds if win32gui.IsWindow(h) and (win32gui.IsWindowVisible(h) or win32gui.IsIconic(h))]
        it.hwnds = alive
        if it.hwnds and not new_window and not (QApplication.keyboardModifiers() & Qt.ShiftModifier):
            self.app.focus_windows(it, it.hwnds)
            return
        if self.cfg["bounce"]:
            it.bounce_start = time.monotonic()
            self.wake()
        self.app.animator.expect_launch(it.exe, self.icon_screen_rect(it))
        try:
            folder = os.path.dirname(it.path) if os.path.isfile(it.path) else None
            win32api.ShellExecute(0, "open", it.path, None, folder, win32con.SW_SHOWNORMAL)
        except Exception:
            it.bounce_start = None

    # -- pin management
    def load_pinned(self, entries):
        """В настройках: путь программы, {"folder": имя, "items": [пути]} или {"widget": id}."""
        out = []
        for e in entries:
            if isinstance(e, dict) and "widget" in e:
                it = self.make_widget_item(e["widget"])
                if it is not None:
                    out.append(it)
            elif isinstance(e, dict):
                kids = [Item.from_path(p) for p in e.get("items", []) if os.path.exists(p)]
                out.append(Item.make_folder(e.get("folder") or "Папка", kids))
            elif os.path.exists(e):
                out.append(Item.from_path(e))
        return out

    def save_pinned(self):
        self.cfg["pinned"] = [{"folder": it.name, "items": [c.path for c in it.children]} if it.kind == "folder"
                              else {"widget": it.widget} if it.kind == "widget"
                              else it.path for it in self.pinned]
        self.app.save_soon()
        self.app.pinned_changed(self)
        self.wake()  # иконки переставились — доанимировать

    # -- виджеты в доке
    def make_widget_item(self, wid):
        cls = self.app.widgets.classes.get(wid)
        if cls is None:
            return None
        it = Item("widget", name=cls.name.split(" — ")[0] or wid)
        it.widget = wid
        return it

    def add_widget(self, wid):
        it = self.make_widget_item(wid)
        if it is not None:
            self.pinned.append(it)
            self.save_pinned()

    def open_widget(self, it):
        from .barwidgets import WidgetFlyout
        fly = self.widget_flyouts.get(it.widget)
        if fly is None:
            fly = self.widget_flyouts[it.widget] = WidgetFlyout(self, self.app.widgets, it.widget)
        if fly.isVisible():
            fly.close()
            return
        fly.open_under(self.icon_global_rect(it), self.side)

    def flyout_closed(self, fly):
        self.wake()

    def widget_open(self):
        return any(f.isVisible() for f in self.widget_flyouts.values())

    def on_widget_data(self):
        if self.isVisible() and any(it.kind == "widget" for it in self.pinned):
            self.update()

    # -- папки
    def folders(self):
        return [it for it in self.pinned if it.kind == "folder"]

    def add_to_folder(self, it, target):
        """Положить программу в папку target или сделать папку из двух программ (target — программа)."""
        if it in self.pinned:
            self.pinned.remove(it)
        if it in self.running:
            self.running.remove(it)
        it.kind, it.cur = "app", None
        if target.kind == "folder":
            it.folder = target
            target.children.append(it)
            target.refresh_icon()
        else:
            idx = self.pinned.index(target)
            folder = Item.make_folder("Папка", [target, it])
            folder.cur = target.cur
            self.pinned[idx] = folder
        self.save_pinned()
        self.app.poll()

    def new_folder(self, it=None):
        """Новая папка: с программой it (на её месте) или пустая в конце закреплённых."""
        folder = Item.make_folder("Новая папка", [])
        if it is not None and it in self.pinned:
            self.pinned[self.pinned.index(it)] = folder
        else:
            if it is not None and it in self.running:
                self.running.remove(it)
            self.pinned.append(folder)
        if it is not None:
            it.kind, it.folder, it.cur = "app", folder, None
            folder.children.append(it)
            folder.refresh_icon()
        self.save_pinned()
        self.app.poll()
        QTimer.singleShot(250, lambda: self.folder_popup.open(folder, edit_title=True))

    def take_out_of_folder(self, it):
        """Вынуть программу из папки в док — сразу за папкой."""
        f = it.folder
        if f is None:
            return
        f.children.remove(it)
        f.refresh_icon()
        it.folder, it.cur = None, None
        self.pinned.insert(self.pinned.index(f) + 1, it)
        self.save_pinned()
        self.app.poll()

    def ungroup(self, f):
        idx = self.pinned.index(f)
        self.pinned[idx:idx + 1] = f.children
        for c in f.children:
            c.folder, c.cur = None, None
        self.save_pinned()
        self.app.poll()

    def pin_item(self, it):
        if it in self.running:
            self.running.remove(it)
        it.kind = "app"
        if it not in self.pinned:
            self.pinned.append(it)
        self.save_pinned()

    def pin_path(self, path):
        path = os.path.normpath(path)
        if any(it.path.lower() == path.lower() for it in self.all_apps() if it.kind == "app"):
            return
        self.pinned.append(Item.from_path(path))
        self.save_pinned()
        self.app.poll()

    def unpin(self, it):
        if self.folder_popup.isVisible() and self.folder_popup.folder is it:
            self.folder_popup.close()
        if it in self.pinned:
            self.pinned.remove(it)
        it.cur = None
        self.save_pinned()
        self.app.poll()

    # -- drag & drop files
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            if url.isLocalFile():
                self.pin_path(url.toLocalFile())

    # -- context menu
    def contextMenuEvent(self, e):
        if self.blocked():
            return
        it = self.item_at(self.to_uv(QPointF(e.pos())))
        m = MacMenu(parent=self)
        if it is not None and it.kind == "folder":
            m.addAction("Открыть", lambda: self.folder_popup.open(it))
            m.addAction("Переименовать…", lambda: self.folder_popup.open(it, edit_title=True))
            if it.children:
                m.addAction("Разгруппировать", lambda: self.ungroup(it))
            m.addAction("Убрать папку из дока", lambda: self.unpin(it))
            m.addSeparator()
        elif it is not None and it.kind in ("app", "running"):
            m.addAction("Открыть новое окно", lambda: self.activate(it, new_window=True))
            if it.kind == "app":
                m.addAction("Убрать из дока", lambda: self.unpin(it))
            else:
                m.addAction("Закрепить в доке", lambda: self.pin_item(it))
            to_folder = m.sub("Переместить в папку")
            for f in self.folders():
                to_folder.addAction(f.name, lambda f=f: self.add_to_folder(it, f))
            if self.folders():
                to_folder.addSeparator()
            to_folder.addAction("Новая папка…", lambda: self.new_folder(it))
            m.addAction("Показать в проводнике",
                        lambda: subprocess.Popen(["explorer.exe", "/select,", it.path]))
            if it.hwnds:
                m.addAction("Закрыть все окна",
                            lambda: [syscommand(h, win32con.SC_CLOSE) for h in it.hwnds])
            m.addSeparator()
        elif it is not None and it.kind == "trash":
            m.addAction("Открыть корзину", lambda: self.activate(it))
            m.addSeparator()
        elif it is not None and it.kind == "widget":
            m.addAction("Открыть", lambda: self.open_widget(it))
            m.addAction("Убрать из дока", lambda: self.unpin(it))
            m.addSeparator()

        m.addAction("Добавить приложение…", self.add_dialog)
        m.addAction("Новая папка", self.new_folder)
        in_dock = {o.widget for o in self.pinned if o.kind == "widget"}
        widgets = m.sub("Добавить виджет")
        for wid, name in self.app.widgets.available():
            a = widgets.addAction(name, lambda wid=wid: self.add_widget(wid))
            a.setEnabled(wid not in in_dock)
        pos_menu = m.sub("Положение на экране")
        grp = QActionGroup(pos_menu)
        for label, value in (("Снизу", "bottom"), ("Слева", "left"), ("Справа", "right")):
            a = QAction(label, pos_menu, checkable=True, checked=self.side == value)
            a.triggered.connect(lambda _=False, v=value: self.app.set_cfg("position", v))
            grp.addAction(a)
            pos_menu.addAction(a)
        a = m.addAction("Автоматически скрывать док")
        a.setCheckable(True)
        a.setChecked(bool(self.cfg["autohide"]))
        a.triggered.connect(lambda c: self.app.set_cfg("autohide", c))
        m.addSeparator()
        m.addAction("Настройки MiniDock…", self.app.open_settings)
        m.addAction("Выход", QApplication.quit)
        self.menu_open = True
        try:
            popup_menu(m, e.globalPos())
        finally:
            self.menu_open = False
            self.wake()

    def add_dialog(self):
        self.menu_open = True
        files, _ = QFileDialog.getOpenFileNames(self, "Добавить в док", str(Path.home() / "Desktop"),
                                                "Программы и ярлыки (*.exe *.lnk *.url);;Все файлы (*)")
        self.menu_open = False
        for f in files:
            self.pin_path(f)

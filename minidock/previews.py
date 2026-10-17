"""Живые миниатюры окон (DWM) для Alt+Tab и предпросмотра в доке.

Миниатюры рисует сам DWM — они живые (видео продолжает играть) и почти ничего не стоят. Но DWM умеет
только прямоугольники и только в обычном нативном окне. Поэтому слоёв три:
  1. панель Qt (размытый фон, подсветка, подписи);
  2. прозрачное нативное окно-хост, в котором DWM рисует миниатюры;
  3. прозрачный слой Qt поверх, который закрывает уголки миниатюр тем же фоном панели (её же рендером)
     и рисует сглаженную обводку — так миниатюры выглядят скруглёнными, оставаясь живыми.
Оба верхних слоя пропускают клики насквозь — их обрабатывает панель.
"""
import ctypes
import time
from ctypes import wintypes

import win32con
import win32gui
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget

from .animations import THUMB_PROPS
from .ui import TOP_FLAGS, animate, fade_in, make_glass, prepare_glass, rounded_path
from .winapi import dwmapi, force_foreground, native_overlay_window, syscommand, window_aspect, window_geometry

THUMB_RADIUS = 8.0


def window_title(hwnd):
    try:
        return win32gui.GetWindowText(hwnd) or "Окно"
    except Exception:
        return "Окно"


def row_layout(hwnds, height, gap, max_width):
    """Ширины миниатюр одной высоты по пропорциям окон; если ряд шире max_width — всё уменьшается."""
    aspects = [window_aspect(h) for h in hwnds]
    widths = [height * a for a in aspects]
    total = sum(widths) + gap * (len(widths) - 1)
    k = min(1.0, max_width / total) if total > 0 else 1.0
    return [w * k for w in widths], height * k


class ThumbHost:
    """Прозрачное нативное окно, в котором DWM рисует миниатюры окон."""

    def __init__(self):
        self.hwnd, self._proc = native_overlay_window(
            "MiniDockThumbs", win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT)
        win32gui.SetLayeredWindowAttributes(self.hwnd, 0, 255, win32con.LWA_ALPHA)
        self.thumbs = {}       # hwnd окна -> handle миниатюры
        self.visible = False
        self.opacity = 255
        self.boxes = []        # [(hwnd, (l, t, r, b) относительно хоста)]

    @staticmethod
    def source_rect(hwnd, size):
        """Видимая часть окна без невидимых рамок изменения размера."""
        g = window_geometry(hwnd)
        il, it, ir, ib = g[1] if g else (7, 0, 7, 7)
        if size.cx <= il + ir + 8 or size.cy <= it + ib + 8:
            il = it = ir = ib = 0
        return il, it, size.cx - ir, size.cy - ib

    def show(self, screen_rect, boxes):
        """screen_rect — (l, t, r, b) хоста в физических пикселях; boxes — [(hwnd, (l, t, r, b) в хосте)]."""
        wanted = {h for h, _ in boxes}
        for h in list(self.thumbs):
            if h not in wanted:
                dwmapi.DwmUnregisterThumbnail(self.thumbs.pop(h))
        for h in wanted:
            if h not in self.thumbs:
                th = ctypes.c_void_p()
                if dwmapi.DwmRegisterThumbnail(self.hwnd, h, ctypes.byref(th)) == 0:
                    self.thumbs[h] = th
        self.boxes = boxes
        l, t, r, b = screen_rect
        win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST, l, t, r - l, b - t,
                              win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)
        self.visible = True
        self.update()

    def update(self):
        props = THUMB_PROPS()
        props.dwFlags = 1 | 2 | 4 | 8 | 0x10
        props.fSourceClientAreaOnly = False
        for hwnd, box in self.boxes:
            th = self.thumbs.get(hwnd)
            if th is None:
                continue
            size = wintypes.SIZE()
            dwmapi.DwmQueryThumbnailSourceSize(th, ctypes.byref(size))
            props.rcSource = wintypes.RECT(*self.source_rect(hwnd, size))
            props.rcDestination = wintypes.RECT(*box)  # плитка уже в пропорциях окна — растягивать не нужно
            props.opacity = self.opacity
            props.fVisible = True
            dwmapi.DwmUpdateThumbnailProperties(th, ctypes.byref(props))

    def set_opacity(self, value):
        self.opacity = int(max(0, min(255, value)))
        self.update()

    def hide(self):
        for th in self.thumbs.values():
            dwmapi.DwmUnregisterThumbnail(th)
        self.thumbs = {}
        self.boxes = []
        if self.visible:
            win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
            self.visible = False


class CornerMask(QWidget):
    """Слой поверх миниатюр: закрывает уголки рендером панели под ними и рисует обводку."""

    def __init__(self):
        super().__init__(None, TOP_FLAGS | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.bg = None
        self.rects = []
        self.overlay = None

    def sync(self, panel, rects, overlay=None):
        """rects — QRectF миниатюр в координатах панели; overlay(painter) — что дорисовать поверх (крестик)."""
        self.rects, self.overlay = rects, overlay
        self.bg = panel.grab()  # тот же фон, подсветка и тонировка, что под миниатюрами
        if self.geometry() != panel.geometry():
            self.setGeometry(panel.geometry())
        if not self.isVisible():
            self.show()
        self.raise_()
        self.update()

    def paintEvent(self, _):
        if self.bg is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        brush = QBrush(self.bg)
        for r in self.rects:
            square = QPainterPath()
            square.addRect(r)
            round_ = QPainterPath()
            round_.addRoundedRect(r, THUMB_RADIUS, THUMB_RADIUS)
            p.fillPath(square.subtracted(round_), brush)
            p.setPen(QPen(QColor(255, 255, 255, 42), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), THUMB_RADIUS, THUMB_RADIUS)
        if self.overlay:
            self.overlay(p)


class LiveThumbs:
    """Живые скруглённые миниатюры поверх панели: хост DWM + слой скругления."""

    def __init__(self):
        self.host = ThumbHost()
        self.mask = CornerMask()

    def show(self, panel, tiles, overlay=None):
        """tiles — [(hwnd, QRectF в координатах панели)]."""
        dpr = panel.devicePixelRatioF()
        g = panel.geometry()
        rect = (round(g.x() * dpr), round(g.y() * dpr), round((g.x() + g.width()) * dpr),
                round((g.y() + g.height()) * dpr))
        boxes = [(h, (round(t.x() * dpr), round(t.y() * dpr), round(t.right() * dpr), round(t.bottom() * dpr)))
                 for h, t in tiles]
        self.host.show(rect, boxes)
        self.mask.sync(panel, [t for _, t in tiles], overlay)

    def refresh_mask(self, panel, tiles, overlay=None):
        """Под миниатюрами что-то поменялось (подсветка) — перерисовать уголки."""
        if self.mask.isVisible():
            self.mask.sync(panel, [t for _, t in tiles], overlay)

    def fade_in(self, owner, ms=170):
        self.host.opacity = 0
        self.mask.setWindowOpacity(0.0)

        def step(v):
            self.host.set_opacity(255 * v)
            self.mask.setWindowOpacity(v)
        owner._thumb_fade = animate(owner, ms, step)

    def hide(self):
        self.host.hide()
        self.mask.hide()


class WindowPreview(QWidget):
    """Предпросмотр окон приложения при наведении на иконку в доке."""
    GAP, TITLE_H = 16.0, 22.0

    @property
    def PAD(self):
        """Прозрачный ободок вокруг миниатюр (настройка «Толщина рамки»)."""
        return float(self.cfg["dock_preview_pad"])

    def __init__(self, app):
        super().__init__(None, TOP_FLAGS)
        self.app = app
        self.cfg = app.cfg
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.glass = make_glass(self)
        self.thumbs = LiveThumbs()
        self.item = None
        self.hwnds = []
        self.tiles = []            # [(hwnd, QRectF миниатюры)]
        self.hover = None          # (index, "thumb" | "close")
        self.font = QFont("Segoe UI", 9)
        self.font_x = QFont("Segoe MDL2 Assets", 7)
        self.show_timer = QTimer(self, singleShot=True, timeout=self._show_pending)
        self.hide_timer = QTimer(self, singleShot=True, interval=280, timeout=self._hide_if_away)
        self.pending = None
        self.hold_until = 0.0  # для команды --test-preview: не прятать, пока не истечёт

    # -- geometry
    def layout_tiles(self):
        """Плитки одной высоты в пропорциях окон, с одинаковыми промежутками."""
        h = float(self.cfg["dock_preview"]) * 0.5625
        scr = QApplication.primaryScreen().geometry()
        widths, h = row_layout(self.hwnds, h, self.GAP, scr.width() - 80 - 2 * self.PAD)
        x, tiles = self.PAD, []
        for hwnd, w in zip(self.hwnds, widths):
            tiles.append((hwnd, QRectF(round(x), self.PAD, round(w), round(h))))
            x += w + self.GAP
        self.tiles = tiles
        return x - self.GAP + self.PAD, self.PAD + h + self.PAD  # только картинки, без подписей

    def close_rect(self, thumb):
        return QRectF(thumb.right() - 24, thumb.top() + 6, 18, 18)

    # -- show / hide (вызывается доком)
    def request(self, item, anchor):
        """Курсор на иконке item (anchor — QRectF иконки в глобальных координатах)."""
        if not self.cfg["dock_previews"] or not item.hwnds:
            self.leave()
            return
        self.hide_timer.stop()
        if self.isVisible():
            if item is not self.item:
                self.open(item, anchor)  # уже показан — переключаемся на другое приложение сразу
            return
        if self.pending and self.pending[0] is item:
            return
        self.pending = (item, anchor)
        self.show_timer.start(int(float(self.cfg["dock_preview_delay"]) * 1000))

    def leave(self):
        """Курсор ушёл с иконки — прячем чуть позже (вдруг он идёт на сам предпросмотр)."""
        if time.monotonic() < self.hold_until:
            return
        self.show_timer.stop()
        self.pending = None
        if self.isVisible() and not self.hide_timer.isActive():
            self.hide_timer.start()

    def _show_pending(self):
        if self.pending:
            item, anchor = self.pending
            self.pending = None
            self.open(item, anchor)

    def _hide_if_away(self):
        if not self.geometry().contains(QCursor.pos()):
            self.close_preview()

    def mouse_inside(self):
        return self.isVisible() and self.geometry().adjusted(-6, -6, 6, 6).contains(QCursor.pos())

    def open(self, item, anchor):
        hwnds = [h for h in item.hwnds if win32gui.IsWindow(h)][:6]
        if not hwnds:
            return
        self.thumbs.hide()
        if self.isVisible():
            self.hide()
        self.item, self.hwnds, self.hover = item, hwnds, None
        w, h = self.layout_tiles()
        scr = (QApplication.screenAt(anchor.center().toPoint()) or QApplication.primaryScreen()).geometry()
        side = self.cfg["position"]
        if side == "bottom":
            x, y = anchor.center().x() - w / 2, anchor.top() - h - 12
        elif side == "left":
            x, y = anchor.right() + 12, anchor.center().y() - h / 2
        else:
            x, y = anchor.left() - w - 12, anchor.center().y() - h / 2
        x = min(max(x, scr.left() + 6), scr.right() - w - 6)
        y = min(max(y, scr.top() + 6), scr.bottom() - h - 6)
        self.setGeometry(int(x), int(y), int(w), int(h))
        prepare_glass(self.glass)
        self.show()
        self.raise_()
        self.thumbs.show(self, self.tiles, self.paint_overlay)
        self.thumbs.fade_in(self)

    def close_preview(self):
        self.show_timer.stop()
        self.hide_timer.stop()
        self.thumbs.hide()
        self.hide()
        self.item = None

    def showEvent(self, e):
        fade_in(self)

    def hideEvent(self, e):
        self.thumbs.hide()

    # -- painting
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        path = rounded_path(self.rect(), 16)
        self.glass.fill(p, path)
        p.fillPath(path, QColor(30, 30, 36, 165))
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.drawPath(path)
        for i, (h, thumb) in enumerate(self.tiles):
            if self.hover is not None and self.hover[0] == i:
                # подсветка вокруг миниатюры (её уголки слой скругления повторит)
                hp = QPainterPath()
                hp.addRoundedRect(thumb.adjusted(-5, -5, 5, 5), THUMB_RADIUS + 4, THUMB_RADIUS + 4)
                p.fillPath(hp, QColor(255, 255, 255, 40))

    def paint_overlay(self, p):
        """Поверх миниатюры (в слое скругления): кружок «закрыть» у наведённого окна, как на Mac."""
        if self.hover is None or self.hover[0] >= len(self.tiles):
            return
        i, part = self.hover
        r = self.close_rect(self.tiles[i][1])
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(232, 17, 35) if part == "close" else QColor(20, 20, 24, 200))
        p.drawEllipse(r)
        p.setFont(self.font_x)
        p.setPen(QColor(255, 255, 255))
        p.drawText(r, Qt.AlignCenter, "")

    def set_hover(self, hover):
        if hover != self.hover:
            self.hover = hover
            self.update()
            self.thumbs.refresh_mask(self, self.tiles, self.paint_overlay)

    # -- mouse
    def hit(self, pos):
        for i, (h, thumb) in enumerate(self.tiles):
            if self.close_rect(thumb).contains(pos):
                return i, "close"
            if thumb.adjusted(-6, -6, 6, 6).contains(pos):
                return i, "thumb"
        return None

    def mouseMoveEvent(self, e):
        self.set_hover(self.hit(e.position()))

    def leaveEvent(self, e):
        self.set_hover(None)
        self.leave()

    def enterEvent(self, e):
        self.hide_timer.stop()

    def mouseReleaseEvent(self, e):
        hit = self.hit(e.position())
        if hit is None:
            return
        i, part = hit
        hwnd = self.hwnds[i]
        if part == "close":
            syscommand(hwnd, win32con.SC_CLOSE)
            item = self.item
            QTimer.singleShot(250, lambda: item is self.item and self.refresh_after_close())
            return
        item = self.item
        self.close_preview()
        if win32gui.IsIconic(hwnd):
            self.app.animator.restore(hwnd, self.app.dock_of(item).icon_screen_rect(item))
        else:
            force_foreground(hwnd)

    def refresh_after_close(self):
        if not self.isVisible() or self.item is None:
            return
        self.hwnds = [h for h in self.hwnds if win32gui.IsWindow(h)]
        if not self.hwnds:
            self.close_preview()
            return
        w, h = self.layout_tiles()
        # левый край не двигаем: размытый фон снят под панелью на прежнем месте
        self.setGeometry(self.x(), self.y(), int(w), int(h))
        self.hover = None
        self.update()
        self.thumbs.show(self, self.tiles, self.paint_overlay)

"""«Стекло» как в macOS: размытый фон под панелью, нарисованный самой панелью.

Системное размытие Windows 10 всегда прямоугольное (DWM игнорирует форму окна), поэтому фон
делаем сами: снимаем участок экрана под панелью сразу уменьшенным, размываем гауссом и заливаем
им скруглённую форму со сглаживанием. Окно одно, углы — настоящие и гладкие.

Полупрозрачные окна Qt нельзя исключить из захвата экрана, поэтому фон снимается, пока панели
на этом месте нет: всплывающие окна — перед показом, док — пока спрятан и в момент появления.
"""
import threading

import numpy as np
from PySide6.QtCore import QObject, QRectF, QTimer, Signal
from PySide6.QtGui import QBrush, QImage, QPixmap, QTransform

import win32gui

from .winapi import grab_screen_scaled, window_monitor

DOWNSCALE = 4      # размываем копию в 4 раза меньше — быстро, а после размытия разницы не видно
MARGIN = 28        # снимаем чуть больше, чтобы у краёв размытие не темнело


def _box_blur(a, r, axis):
    """Box-размытие по одной оси через накопленные суммы (края — повтор крайних пикселей)."""
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float32)
    n = a.shape[axis]
    if axis == 0:
        return (c[2 * r + 1:2 * r + 1 + n] - c[:n]) / (2 * r + 1)
    return (c[:, 2 * r + 1:2 * r + 1 + n] - c[:, :n]) / (2 * r + 1)


def blur_small(data, w, h, radius, saturation=1.3):
    """Размытие уменьшенной копии: 3 прохода box ≈ гаусс, плюс немного насыщенности (vibrancy)."""
    a = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)[:, :, :3].astype(np.float32)
    r = max(int(radius / 1.7), 1)
    for _ in range(3):
        a = _box_blur(a, r, 1)
        a = _box_blur(a, r, 0)
    if saturation != 1.0:
        lum = a @ np.array([0.114, 0.587, 0.299], dtype=np.float32)  # порядок BGR
        a = lum[..., None] + (a - lum[..., None]) * saturation
    out = np.empty((h, w, 4), dtype=np.uint8)
    out[..., :3] = np.clip(a, 0, 255)
    out[..., 3] = 255
    return QImage(out.data, w, h, w * 4, QImage.Format_RGB32).copy()


class Glass(QObject):
    """Размытый фон под окном. live_ms > 0 — обновлять в фоне, пока окно видно (для дока)."""
    ready = Signal(QImage, tuple)

    def __init__(self, widget, live_ms=0, radius=18.0):
        super().__init__(widget)
        self.w = widget
        self.radius = radius
        self.pix = None
        self.transform = QTransform()
        self.enabled = True
        self.region = None      # функция -> QRectF в координатах окна, что именно снимать (по умолчанию всё)
        self.can_capture = None  # функция -> bool: можно ли сейчас снимать (док — только пока спрятан)
        self.live = live_ms > 0
        self.busy = False
        self.timer = QTimer(self, interval=live_ms or 1000, timeout=self.refresh_async)
        self.ready.connect(self._apply)

    def start(self):
        if self.live:
            self.timer.start()

    def stop(self):
        self.timer.stop()

    def target(self):
        """Какой участок экрана снимать: (x0, y0, x1, y1) в физических пикселях или None."""
        if not self.enabled or (self.can_capture and not self.can_capture()):
            return None
        r = self.region() if self.region else QRectF(self.w.rect())
        if r is None or r.width() < 2 or r.height() < 2:
            return None
        dpr = self.w.devicePixelRatioF()
        hwnd = int(self.w.winId())
        wl, wt = win32gui.GetWindowRect(hwnd)[:2]
        _, (ml, mt, mr, mb) = window_monitor(hwnd)
        x0 = max(round(wl + r.left() * dpr) - MARGIN, ml)
        y0 = max(round(wt + r.top() * dpr) - MARGIN, mt)
        x1 = min(round(wl + r.right() * dpr) + MARGIN, mr)
        y1 = min(round(wt + r.bottom() * dpr) + MARGIN, mb)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        return x0, y0, x1, y1

    def work(self, rect, dpr):
        data, sw, sh = grab_screen_scaled(rect, DOWNSCALE)
        return blur_small(data, sw, sh, self.radius * dpr / DOWNSCALE)

    def refresh(self):
        """Сразу (синхронно) снять и размыть фон — для всплывающих окон перед показом."""
        rect = self.target()
        if rect:
            self._apply(self.work(rect, self.w.devicePixelRatioF()), rect)

    def refresh_async(self):
        """То же в фоновом потоке — для фона дока, чтобы интерфейс не ждал захвата экрана."""
        if self.busy:
            return
        rect = self.target()
        if not rect:
            return
        self.busy = True
        dpr = self.w.devicePixelRatioF()

        def run():
            try:
                self.ready.emit(self.work(rect, dpr), rect)
            except Exception:
                pass
            finally:
                self.busy = False
        threading.Thread(target=run, daemon=True).start()

    def _apply(self, img, rect):
        x0, y0, x1, y1 = rect
        dpr = self.w.devicePixelRatioF()
        self.pix = QPixmap.fromImage(img)
        # маленький снимок растягивается при отрисовке: сдвиг на место + масштаб обратно
        wl, wt = win32gui.GetWindowRect(int(self.w.winId()))[:2]
        sx, sy = (x1 - x0) / dpr / img.width(), (y1 - y0) / dpr / img.height()
        self.transform = QTransform(sx, 0, 0, sy, (x0 - wl) / dpr, (y0 - wt) / dpr)
        self.w.update()

    def fill(self, painter, path):
        """Залить форму размытым фоном (края сглажены, как у любой заливки пути)."""
        if self.pix is None or not self.enabled:
            return False
        brush = QBrush(self.pix)
        brush.setTransform(self.transform)
        painter.fillPath(path, brush)
        return True

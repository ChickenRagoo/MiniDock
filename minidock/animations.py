"""Анимации окон: «джинн» и «масштаб» на живых DWM-миниатюрах, закрытие, открытие из иконки.

Разворачивание: окно делается невидимым (layered, alpha=0), разворачивается и отрисовывается,
а пользователь видит его живую миниатюру, которая «вытекает» из иконки. В конце окно снова
становится видимым ровно под последним кадром — без мигания и даже для давно свёрнутых окон.
"""
import ctypes
import os
import time
from ctypes import wintypes

import win32con
import win32gui
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from .gpu_genie import GpuGenie
from .ui import TOP_FLAGS, clamp, ease, lerp, precise_timer
from .winapi import (dwmapi, force_foreground, grab_screen, is_app_window, make_invisible, make_visible, native_overlay_window,
                     point_monitor, primary_monitor_rect, restored_frame, snapshot, user32, window_exe,
                     window_geometry, window_monitor)

DEBUG = bool(os.environ.get("MINIDOCK_DEBUG"))
SNAP_LIMIT = 6  # снимков окон для анимации закрытия (каждый до ~4 МБ)
MIN_LIMIT = 4   # кадров свёрнутых окон — с них разворачивание начинается мгновенно (каждый до ~15 МБ)


class THUMB_PROPS(ctypes.Structure):
    _fields_ = [("dwFlags", wintypes.DWORD), ("rcDestination", wintypes.RECT), ("rcSource", wintypes.RECT),
                ("opacity", ctypes.c_ubyte), ("fVisible", wintypes.BOOL), ("fSourceClientAreaOnly", wintypes.BOOL)]


dwmapi.DwmRegisterThumbnail.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.POINTER(ctypes.c_void_p)]
dwmapi.DwmUpdateThumbnailProperties.argtypes = [ctypes.c_void_p, ctypes.POINTER(THUMB_PROPS)]
dwmapi.DwmUnregisterThumbnail.argtypes = [ctypes.c_void_p]
dwmapi.DwmQueryThumbnailSourceSize.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.SIZE)]


def to_flow(r, flow):
    """(l, t, r, b) → (a0, f0, a1, f1): f растёт в сторону дока, a — поперёк."""
    l, t, rr, b = r
    if flow == "down":
        return l, t, rr, b
    if flow == "right":
        return t, l, b, rr
    return t, -rr, b, -l


def from_flow(a0, f0, a1, f1, flow):
    if flow == "down":
        return a0, f0, a1, f1
    if flow == "right":
        return f0, a0, f1, a1
    return -f1, a0, -f0, a1


class GenieOverlay:
    """Прозрачное окно поверх экрана, на котором DWM рисует полосы-миниатюры окна."""

    def __init__(self):
        self.hwnd, self._proc = native_overlay_window(
            "MiniDockGenie", win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT)
        win32gui.SetLayeredWindowAttributes(self.hwnd, 0, 255, win32con.LWA_ALPHA)
        self.mon = None
        self.anims = []
        self.visible = False
        self.frames = []
        self.held = None  # миниатюра от capture(), пока её не закрыла анимация
        self.timer = precise_timer(None, 7, self.step)

    def busy_with(self, hwnd):
        return any(a["hwnd"] == hwnd for a in self.anims)

    def play(self, hwnd, src, dst, insets, reverse, duration, flow="down", mode="genie", on_done=None):
        """src/dst — (l, t, r, b) в физических пикселях. reverse=True — окно появляется из иконки."""
        if self.busy_with(hwnd):
            return False
        a0, f0, a1, f1 = to_flow(src, flow)
        n = 1 if mode == "scale" else int(clamp((f1 - f0) / 7, 40, 110))
        thumbs = []
        for _ in range(n):
            th = ctypes.c_void_p()
            if dwmapi.DwmRegisterThumbnail(self.hwnd, hwnd, ctypes.byref(th)) != 0:
                break
            thumbs.append(th)
        if len(thumbs) < n:
            for th in thumbs:
                dwmapi.DwmUnregisterThumbnail(th)
            return False
        size = wintypes.SIZE()
        dwmapi.DwmQueryThumbnailSourceSize(thumbs[0], ctypes.byref(size))
        il, it, ir, ib = insets
        source = (il, it, max(size.cx - ir, il + 1), max(size.cy - ib, it + 1))
        a = dict(hwnd=hwnd, thumbs=thumbs, src=src, dst=dst, source=source, reverse=reverse, flow=flow,
                 mode=mode, start=time.monotonic(), dur=duration, done=on_done, release_at=None)
        self.ensure_shown()
        self.render(a, 1.0 if reverse else 0.0)
        self.anims.append(a)
        self.timer.start()
        return True

    def capture(self, hwnd, frame, insets, below=None):
        """Снимок уже свёрнутого окна: его DWM-миниатюра рисуется на прежнем месте и снимается с экрана.

        Миниатюра остаётся на экране (под окном below), пока не вызовут release_capture(), —
        так между исчезновением окна и первым кадром анимации нет пустого кадра.
        """
        self.release_capture()
        th = ctypes.c_void_p()
        if dwmapi.DwmRegisterThumbnail(self.hwnd, hwnd, ctypes.byref(th)) != 0:
            return None
        size = wintypes.SIZE()
        dwmapi.DwmQueryThumbnailSourceSize(th, ctypes.byref(size))
        il, it, ir, ib = insets
        self.ensure_shown()
        if below:  # под окном GPU-анимации: её первый кадр сразу ляжет поверх снимка
            win32gui.SetWindowPos(self.hwnd, below, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
        ox, oy = self.mon[0], self.mon[1]
        props = THUMB_PROPS()
        props.dwFlags = 1 | 2 | 4 | 8 | 0x10
        props.rcSource = wintypes.RECT(il, it, max(size.cx - ir, il + 1), max(size.cy - ib, it + 1))
        props.rcDestination = wintypes.RECT(frame[0] - ox, frame[1] - oy, frame[2] - ox, frame[3] - oy)
        props.opacity = 255
        props.fVisible = True
        dwmapi.DwmUpdateThumbnailProperties(th, ctypes.byref(props))
        dwmapi.DwmFlush()
        dwmapi.DwmFlush()
        self.held = th
        return grab_screen(frame)

    def release_capture(self):
        th, self.held = self.held, None
        if th is None:
            return
        dwmapi.DwmUnregisterThumbnail(th)
        if not self.anims:
            win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
            self.visible = False

    def ensure_shown(self):
        mon = primary_monitor_rect()
        if not self.visible or mon != self.mon:
            self.mon = mon
            l, t, r, b = mon
            win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST, l, t, r - l, b - t,
                                  win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)
            self.visible = True

    def render(self, a, g):
        """g: 0 — окно на своём месте, 1 — окно втянуто в иконку."""
        ox, oy = self.mon[0], self.mon[1]
        S = (a["src"][0] - ox, a["src"][1] - oy, a["src"][2] - ox, a["src"][3] - oy)
        T = (a["dst"][0] - ox, a["dst"][1] - oy, a["dst"][2] - ox, a["dst"][3] - oy)
        sl, st, sr, sb = a["source"]
        props = THUMB_PROPS()
        props.dwFlags = 1 | 2 | 4 | 8 | 0x10
        props.fSourceClientAreaOnly = False

        if a["mode"] == "scale":
            k = ease(g)
            rect = [round(lerp(s, t, k)) for s, t in zip(S, T)]
            props.rcSource = wintypes.RECT(sl, st, sr, sb)
            props.rcDestination = wintypes.RECT(rect[0], rect[1], max(rect[2], rect[0] + 1), max(rect[3], rect[1] + 1))
            props.opacity = int(255 * clamp((1 - g) / 0.35)) if g > 0.65 else 255
            props.fVisible = True
            dwmapi.DwmUpdateThumbnailProperties(a["thumbs"][0], ctypes.byref(props))
            return

        flow = a["flow"]
        Sa0, Sf0, Sa1, Sf1 = to_flow(S, flow)
        Ta0, Tf0, Ta1, Tf1 = to_flow(T, flow)
        sw, sh = sr - sl, sb - st
        n = len(a["thumbs"])
        ph1 = ease(clamp(g / 0.4))            # фаза 1: край окна у дока сужается к иконке
        ph2 = ease(clamp((g - 0.25) / 0.75))  # фаза 2: окно «стекает» в иконку
        span = Tf0 - Sf0 if abs(Tf0 - Sf0) > 1 else 1.0
        opacity = int(255 * clamp((1 - g) / 0.12)) if g > 0.88 else 255

        def f_at(v):
            bv = min(ph2 + 0.7 * v * ph2 * (1 - ph2), 1.0)
            return lerp(Sf0 + v * (Sf1 - Sf0), Tf0 + v * (Tf1 - Tf0), bv)

        for i, th in enumerate(a["thumbs"]):
            v0, v1 = i / n, (i + 1) / n
            y0, y1 = f_at(v0), f_at(v1)
            s = ease(clamp(((y0 + y1) / 2 - Sf0) / span))
            left = lerp(Sa0, lerp(Sa0, Ta0, s), ph1)
            right = lerp(Sa1, lerp(Sa1, Ta1, s), ph1)
            if flow == "down":
                props.rcSource = wintypes.RECT(sl, st + round(v0 * sh), sr, st + round(v1 * sh))
            elif flow == "right":
                props.rcSource = wintypes.RECT(sl + round(v0 * sw), st, sl + round(v1 * sw), sb)
            else:
                props.rcSource = wintypes.RECT(sr - round(v1 * sw), st, sr - round(v0 * sw), sb)
            l, t, r, b = from_flow(round(left), round(y0), max(round(right), round(left) + 1),
                                   max(round(y1), round(y0) + 1), flow)
            props.rcDestination = wintypes.RECT(l, t, r, b)
            props.opacity = opacity
            props.fVisible = right - left >= 0.5
            dwmapi.DwmUpdateThumbnailProperties(th, ctypes.byref(props))

    def release(self, a):
        for th in a["thumbs"]:
            dwmapi.DwmUnregisterThumbnail(th)
        self.anims.remove(a)

    def step(self):
        now = time.monotonic()
        if DEBUG:
            self.frames.append(now)
        for a in list(self.anims):
            if a["release_at"] is not None:
                if now >= a["release_at"]:
                    self.release(a)
                continue
            t = clamp((now - a["start"]) / a["dur"])
            self.render(a, 1 - t if a["reverse"] else t)
            if t >= 1:
                if a["done"]:
                    a["done"]()
                # пару кадров держим последний кадр, пока настоящее окно не появится на его месте
                a["release_at"] = now + (0.04 if a["reverse"] else 0)
        if not self.anims:
            self.timer.stop()
            win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
            self.visible = False
            if DEBUG and len(self.frames) > 2:
                span = self.frames[-1] - self.frames[0]
                print(f"genie: {len(self.frames)} frames, {len(self.frames) / span:.0f} fps", flush=True)
            self.frames = []


class CloseOverlay(QWidget):
    """Анимация закрытия окна: снимок окна уменьшается и растворяется."""

    def __init__(self):
        super().__init__(None, TOP_FLAGS | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.anims = []
        self.timer = precise_timer(self, 10, self.step)

    def play(self, img, src, duration=0.22):
        g = QApplication.primaryScreen().geometry()
        if self.geometry() != g:
            self.setGeometry(g)
        self.anims.append((QPixmap.fromImage(img), src, time.monotonic(), duration))
        if not self.isVisible():
            self.show()
        self.raise_()
        self.timer.start()

    def step(self):
        now = time.monotonic()
        self.anims = [a for a in self.anims if now - a[2] < a[3]]
        if not self.anims:
            self.timer.stop()
            self.hide()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        now = time.monotonic()
        origin = QPointF(self.geometry().topLeft())
        for pix, src, start, dur in self.anims:
            k = ease(clamp((now - start) / dur))
            src = src.translated(-origin)
            s = 1 - 0.12 * k
            r = QRectF(0, 0, src.width() * s, src.height() * s)
            r.moveCenter(src.center() + QPointF(0, 12 * k))
            p.setOpacity(1 - k)
            p.drawPixmap(r, pix, QRectF(pix.rect()))
        p.setOpacity(1)


class WindowAnimator:
    def __init__(self, app):
        self.app = app
        self.gpu = GpuGenie()          # основной: снимок окна на сетке, без «лесенок»
        self.gpus = [self.gpu]         # по слою на каждый монитор (дополнительные создаются при надобности)
        self.genie = GenieOverlay()    # запасной: живые DWM-миниатюры, если снимок не получился
        self.closer = CloseOverlay()
        self.hidden = {}      # hwnd -> исходный exstyle, пока окно невидимо ради анимации
        self.pre_min = {}     # hwnd -> когда снят полный кадр перед свёртыванием (кнопка «свернуть», клик в доке)
        self.ours = set()     # окна, которые сейчас разворачиваем мы сами
        self.launches = []    # [exe | None, icon_rect, deadline] — ждём окно запущенной программы
        self.geo = {}         # hwnd -> (рамка, отступы)
        self.snaps = {}       # hwnd -> (QImage, QRectF логические) — для закрытия и запасного снимка
        self.min_imgs = {}    # hwnd -> QImage: последний кадр окна перед свёртыванием

    @property
    def cfg(self):
        return self.app.cfg

    def flow(self):
        return {"bottom": "down", "left": "left", "right": "right"}[self.cfg["position"]]

    def dur(self, base):
        return base / max(float(self.cfg["anim_speed"]), 0.1)

    def to_logical(self, rect):
        dpr = QApplication.primaryScreen().devicePixelRatio()
        l, t, r, b = rect
        return QRectF(l / dpr, t / dpr, (r - l) / dpr, (b - t) / dpr)

    # -- bookkeeping
    def remember(self, wins):
        alive = set()
        for hwnds in wins.values():
            for h in hwnds:
                alive.add(h)
                if h not in self.hidden:
                    g = window_geometry(h)
                    if g:
                        self.geo[h] = g
        self.geo = {h: g for h, g in self.geo.items() if h in alive or win32gui.IsWindow(h)}
        self.snaps = {h: s for h, s in self.snaps.items() if win32gui.IsWindow(h)}
        self.min_imgs = {h: s for h, s in self.min_imgs.items() if win32gui.IsWindow(h)}

    def take_snapshot(self, hwnd, max_w=1280):
        if not self.cfg["animations"] or self.app.fullscreen or hwnd in self.hidden:
            return
        if not is_app_window(hwnd) or win32gui.IsIconic(hwnd):
            return
        g = window_geometry(hwnd)
        if g:
            self.geo[hwnd] = g
        snap = snapshot(hwnd, max_w=max_w)
        if snap:
            self.snaps.pop(hwnd, None)
            self.snaps[hwnd] = (snap[0], self.to_logical(snap[1]))
            while len(self.snaps) > SNAP_LIMIT:  # храним только последние окна (dict помнит порядок)
                self.snaps.pop(next(iter(self.snaps)))

    def prepare_minimize(self, hwnd):
        """Окно вот-вот свернётся: снять его полный кадр, пока оно целиком на экране. Тогда анимация
        свёртывания начнётся сразу, без медленного снимка в момент, когда Windows уже прячет окно (мигание)."""
        if not self.cfg["animations"]:
            return
        self.take_snapshot(hwnd, max_w=4096)
        if hwnd in self.snaps:
            self.pre_min[hwnd] = time.monotonic()

    def hold_minimize(self, hwnd):
        """Поверх окна — его неподвижный кадр (первый кадр анимации свёртывания). GpuGenie или None.
        Когда Windows спрячет окно, анимация пойдёт с этого кадра — без пустого кадра между ними."""
        if not self.cfg["animations"] or self.app.fullscreen or self.busy(hwnd) or win32gui.IsIconic(hwnd):
            return None
        self.prepare_minimize(hwnd)
        snap, g = self.snaps.get(hwnd), window_geometry(hwnd)
        if not snap or not g:
            return None
        if not self.play(hwnd, snap[0], g[0], self.icon_for(hwnd), g[1], reverse=False, duration=self.dur(0.5),
                         mode=self.cfg["minimize_effect"]):
            return None
        gpu = self.gpu_with(hwnd)
        if gpu is None or not gpu.hold(hwnd):
            return None
        self.geo[hwnd] = g
        if snap[0].width() >= g[0][2] - g[0][0] - 2:
            self.min_imgs.pop(hwnd, None)
            self.min_imgs[hwnd] = snap[0]
        return gpu

    def minimize(self, hwnd):
        """Свернуть окно самим (клик в доке, меню панели): сначала стоп-кадр, потом свёртывание."""
        gpu = self.hold_minimize(hwnd)
        send = lambda: win32gui.PostMessage(hwnd, win32con.WM_SYSCOMMAND, win32con.SC_MINIMIZE, 0)
        if gpu is None:
            send()
            return
        gpu.after_frames(2, send)  # кадр уже на экране — теперь можно прятать окно
        QTimer.singleShot(1500, lambda: gpu.drop_held(hwnd))  # страховка: окно не свернулось

    def on_press(self, hwnd):
        """Кнопка мыши нажата в окне. Если это кнопки в правом верхнем углу — окно, скорее всего, сейчас
        свернётся или закроется: снимаем его в полном размере для анимации. Нажата именно «свернуть» —
        сразу кладём поверх окна его стоп-кадр (окно свернётся, когда кнопку отпустят)."""
        if not self.cfg["animations"]:
            return
        root = user32.GetAncestor(hwnd, 2)
        g = window_geometry(root) if root else None
        if not g:
            return
        x, y = win32gui.GetCursorPos()
        (l, t, r, b), _ = g
        if not (r - 220 <= x <= r and t <= y <= t + 60):
            return
        res = ctypes.c_size_t()
        lp = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
        hit = user32.SendMessageTimeoutW(root, 0x0084, 0, lp, 2, 60, ctypes.byref(res)) and res.value  # WM_NCHITTEST
        if hit == 8:  # HTMINBUTTON
            gpu = self.hold_minimize(root)
            if gpu is not None:
                self._watch_release(root, gpu)
                return
        self.prepare_minimize(root)

    def _watch_release(self, hwnd, gpu, started=None):
        """Кнопку мыши отпустили, а окно не свернулось (увели мышь с кнопки) — убрать стоп-кадр."""
        started = started or time.monotonic()
        if user32.GetAsyncKeyState(0x01) & 0x8000 and time.monotonic() - started < 10:
            QTimer.singleShot(30, lambda: self._watch_release(hwnd, gpu, started))
            return
        QTimer.singleShot(250, lambda: gpu.drop_held(hwnd))  # свернулось — стоп-кадр уже пошёл в анимацию

    def gpu_for(self, rect):
        """Слой анимаций монитора, на котором центр rect (физические пиксели)."""
        mon = point_monitor((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
        for g in self.gpus:
            if g.physical() and g.mon == mon:
                return g
        for scr in QApplication.screens():
            if scr is QApplication.primaryScreen() or any(g.screen is scr for g in self.gpus):
                continue
            g = GpuGenie(scr)
            self.gpus.append(g)
            if g.physical() and g.mon == mon:
                return g
        return self.gpu

    def gpu_with(self, hwnd):
        return next((g for g in self.gpus if g.busy_with(hwnd)), None)

    def set_enabled(self, on):
        for g in self.gpus:
            g.set_enabled(on)

    def busy(self, hwnd):
        return self.gpu_with(hwnd) is not None or self.genie.busy_with(hwnd)

    def play(self, hwnd, img, frame, icon, insets, reverse, duration, mode, on_done=None):
        """Сначала GPU-«джинн» по снимку, если снимка нет — DWM-миниатюры."""
        if self.busy(hwnd):
            return False
        dpr = QApplication.primaryScreen().devicePixelRatio()
        dock = int(self.dock_for(hwnd).winId())
        if img is not None and self.gpu_for(icon).play(hwnd, img, frame, icon, reverse, duration, self.flow(), mode,
                                                       on_done, dpr, below=dock):
            return True
        if DEBUG:
            print(f"fallback to DWM: img={'none' if img is None else img.size()} reverse={reverse}", flush=True)
        return self.genie.play(hwnd, frame, icon, insets, reverse, duration, self.flow(), mode, on_done)

    def unhide(self, hwnd):
        ex = self.hidden.pop(hwnd, None)
        if ex is not None:
            make_visible(hwnd, ex)

    def hide_for_animation(self, hwnd):
        if hwnd in self.hidden:
            return True
        ex = make_invisible(hwnd)
        if ex is None:
            return False
        self.hidden[hwnd] = ex
        QTimer.singleShot(3000, lambda: self.unhide(hwnd))  # страховка: окно никогда не останется невидимым
        return True

    def cleanup(self):
        for hwnd in list(self.hidden):
            self.unhide(hwnd)

    def dock_for(self, hwnd):
        """Док на мониторе окна (если там есть свой док), иначе основной."""
        try:
            mon = window_monitor(hwnd)[0]
        except Exception:
            return self.app.dock
        return next((d for d in self.app.docks() if d.monitor() == mon), self.app.dock)

    def icon_for(self, hwnd):
        dock = self.dock_for(hwnd)
        return dock.icon_screen_rect(dock.item_for_hwnd(hwnd, window_exe(hwnd)))

    # -- minimize
    def on_minimize_start(self, hwnd):
        if not self.cfg["animations"] or self.app.fullscreen or hwnd in self.hidden:
            return
        if not is_app_window(hwnd, need_visible=False):
            return
        if any(gp.begin_held(hwnd) for gp in self.gpus):  # стоп-кадр уже на месте окна — просто поехали
            self.dock_for(hwnd).reveal(1.0)
            return
        g = window_geometry(hwnd) or self.geo.get(hwnd)
        if not g:
            return
        self.geo[hwnd] = g
        self.dock_for(hwnd).reveal(1.0)
        ready = self.snaps.get(hwnd) if time.monotonic() - self.pre_min.pop(hwnd, 0) < 2.0 else None
        # кадр, снятый перед свёртыванием, — анимация стартует сразу; иначе снимаем сейчас (медленнее)
        snap = (ready[0], None) if ready else snapshot(hwnd)
        img = snap[0] if snap else None
        if img is None:  # уже свёрнуто — берём кадр из DWM
            img = self.genie.capture(hwnd, g[0], g[1], below=self.gpu_for(self.icon_for(hwnd)).hwnd)
        if img is None:
            img = (self.snaps.get(hwnd) or (None,))[0]
        elif img.width() >= g[0][2] - g[0][0] - 2:  # полноразмерный кадр — с него потом и развернём
            self.min_imgs.pop(hwnd, None)
            self.min_imgs[hwnd] = img
            while len(self.min_imgs) > MIN_LIMIT:
                self.min_imgs.pop(next(iter(self.min_imgs)))
        if DEBUG:
            print(f"minimize: fresh={bool(snap)} image={img is not None}", flush=True)
        on_gpu = self.play(hwnd, img, g[0], self.icon_for(hwnd), g[1], reverse=False, duration=self.dur(0.5),
                           mode=self.cfg["minimize_effect"]) and self.gpu_with(hwnd)
        if on_gpu:
            on_gpu.after_frames(2, self.genie.release_capture)  # снимок под анимацией больше не нужен
        else:
            self.genie.release_capture()

    # -- restore
    def restore(self, hwnd, icon_rect, mode=None):
        """Разворачивает окно с анимацией из icon_rect (физические пиксели)."""
        if not self.cfg["animations"] or not self.hide_for_animation(hwnd):
            force_foreground(hwnd)
            return
        self.ours.add(hwnd)
        mode = mode or self.cfg["minimize_effect"]
        if self._play_cached(hwnd, icon_rect, mode, self.dur(0.45), restored_frame(hwnd, self.geo.get(hwnd))):
            force_foreground(hwnd)
            return
        force_foreground(hwnd)
        QTimer.singleShot(45, lambda: self._play_appear(hwnd, icon_rect, mode, self.dur(0.45)))

    def _play_cached(self, hwnd, icon_rect, mode, duration, g):
        """Разворачивание сразу с кадра, снятого при свёртывании, — без ожидания, пока окно отрисуется.
        Когда окно (невидимое) уже развернулось, снимок подменяется свежим."""
        img = self.min_imgs.pop(hwnd, None)
        if DEBUG:
            print(f"cached: img={img is not None} g={g} hidden={hwnd in self.hidden}", flush=True)
        if img is None or not g or not self.play(hwnd, img, g[0], icon_rect, g[1], reverse=True, duration=duration,
                                                 mode=mode, on_done=lambda: self.unhide(hwnd)):
            return False
        if not self.gpu_with(hwnd):
            return True

        def refresh():
            self.ours.discard(hwnd)
            fresh = window_geometry(hwnd) if hwnd in self.hidden else None
            snap = snapshot(hwnd) if fresh else None
            if DEBUG:
                print(f"refresh: fresh={fresh} snap={bool(snap)}", flush=True)
            if snap:
                g = self.gpu_with(hwnd)
                if g:
                    g.update_image(hwnd, snap[0], fresh[0])
        QTimer.singleShot(50, refresh)
        return True

    def _play_appear(self, hwnd, icon_rect, mode, duration):
        self.ours.discard(hwnd)
        if not win32gui.IsWindow(hwnd):
            self.unhide(hwnd)
            return
        g = window_geometry(hwnd) or restored_frame(hwnd, self.geo.get(hwnd))
        # окно уже развёрнуто, но невидимо — снимаем его настоящий текущий вид
        snap = snapshot(hwnd) if g else None
        img = snap[0] if snap else None
        if not g or not self.play(hwnd, img, g[0], icon_rect, g[1], reverse=True, duration=duration, mode=mode,
                                  on_done=lambda: self.unhide(hwnd)):
            self.unhide(hwnd)

    def on_minimize_end(self, hwnd):
        """Окно развернули не мы (системный Alt+Tab, клик по уведомлению) — всё равно анимируем."""
        if hwnd in self.ours or hwnd in self.hidden or not self.cfg["animations"] or self.app.fullscreen:
            return
        if not is_app_window(hwnd, need_visible=False) or not self.hide_for_animation(hwnd):
            return
        icon = self.icon_for(hwnd)
        if self._play_cached(hwnd, icon, self.cfg["minimize_effect"], self.dur(0.42), window_geometry(hwnd)):
            return
        QTimer.singleShot(30, lambda: self._play_appear(hwnd, icon, self.cfg["minimize_effect"], self.dur(0.42)))

    # -- open (program launched from the dock / Launchpad)
    def expect_launch(self, exe, icon_rect):
        if self.cfg["animations"] and self.cfg["open_animation"]:
            self.launches.append([exe or None, icon_rect, time.monotonic() + 15])

    def on_show(self, hwnd):
        now = time.monotonic()
        self.launches = [x for x in self.launches if x[2] > now]
        if not self.launches or user32.GetAncestor(hwnd, 2) != hwnd or not is_app_window(hwnd):
            return
        exe = window_exe(hwnd)
        sysroot = os.environ.get("SystemRoot", r"C:\Windows").lower()
        for entry in self.launches:
            want = entry[0]
            folder = os.path.dirname(want) + os.sep if want else ""
            if want is None or exe == want or (folder and not folder.startswith(sysroot) and exe.startswith(folder)):
                self.launches.remove(entry)
                if self.hide_for_animation(hwnd):
                    QTimer.singleShot(110, lambda: self._play_appear(hwnd, entry[1], "scale", self.dur(0.38)))
                return

    # -- close
    def on_hide(self, hwnd):
        if not self.cfg["animations"] or not self.cfg["close_animation"] or hwnd in self.hidden:
            return
        snap = self.snaps.get(hwnd)
        if not snap or win32gui.IsIconic(hwnd) or not is_app_window(hwnd, need_visible=False):
            return
        if win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd):
            return
        del self.snaps[hwnd]
        dpr = QApplication.primaryScreen().devicePixelRatio()
        r = snap[1]
        frame = (round(r.left() * dpr), round(r.top() * dpr), round(r.right() * dpr), round(r.bottom() * dpr))
        if not self.gpu_for(frame).play(hwnd, snap[0], frame, frame, False, self.dur(0.24), self.flow(), "close",
                                        None, dpr):
            self.closer.play(snap[0], snap[1], duration=self.dur(0.22))

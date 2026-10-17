"""Общие элементы интерфейса: анимации, «стеклянные» панели и меню в стиле macOS, переключатель."""
import ctypes
import threading
import time
from ctypes import wintypes

from PySide6.QtCore import QEasingCurve, QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QAbstractButton, QMenu

from . import glass
from .winapi import try_foreground

TOP_FLAGS = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
MDL2 = "Segoe MDL2 Assets"
ACCENT = QColor(10, 132, 255)

# Общие настройки «стекла»
GLASS = {"enabled": True, "live": True, "radius": 18.0}


def clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


def ease(t):
    return t * t * (3 - 2 * t)


def ease_out(t):
    return 1 - (1 - t) ** 3


def lerp(a, b, t):
    return a + (b - a) * t


def lerp_rect(cur, tgt, k):
    """Плавно двигает прямоугольник к цели. Возвращает (новый, изменился_ли)."""
    vals = [cur.x(), cur.y(), cur.width(), cur.height()]
    goal = [tgt.x(), tgt.y(), tgt.width(), tgt.height()]
    if all(abs(a - b) < 0.25 for a, b in zip(vals, goal)):
        return QRectF(tgt), cur != tgt
    return QRectF(*(lerp(a, b, k) for a, b in zip(vals, goal))), True


_winmm = ctypes.windll.winmm
_hires_users = 0


class PreciseTimer(QTimer):
    """Таймер для анимаций. Пока хоть один такой таймер запущен, системный таймер Windows работает
    с точностью 1 мс (иначе ~15 мс и анимация дёргается); когда все остановлены — возвращаем как было,
    чтобы в простое не будить процессор всей системе."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTimerType(Qt.PreciseTimer)
        self._hires = False

    def start(self, *args):
        global _hires_users
        if not self._hires:
            self._hires = True
            _hires_users += 1
            if _hires_users == 1:
                _winmm.timeBeginPeriod(1)
        super().start(*args)

    def stop(self):
        global _hires_users
        super().stop()
        if self._hires:
            self._hires = False
            _hires_users -= 1
            if _hires_users == 0:
                _winmm.timeEndPeriod(1)


# -- кадры в такт монитору (180 Гц — 180 кадров) ---------------------------------------------------

class _OpenAdapter(ctypes.Structure):
    _fields_ = [("hDc", wintypes.HDC), ("hAdapter", wintypes.UINT), ("LuidLow", wintypes.DWORD),
                ("LuidHigh", wintypes.LONG), ("VidPnSourceId", wintypes.UINT)]


class _WaitVBlank(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT), ("hDevice", wintypes.UINT), ("VidPnSourceId", wintypes.UINT)]


class VSyncClock(QObject):
    """Отдельный поток ждёт обновления экрана (вертикальной развёртки) и будит анимации ровно
    в такт монитору. Пока анимаций нет — поток спит и ничего не тратит."""
    tick = Signal()

    def __init__(self):
        super().__init__()
        self.timers = set()
        self.pending = False
        self.last_tick = 0.0
        self.wake = threading.Event()
        # Запасной ход: на некоторых видеокартах и при уснувшем мониторе ожидание развёртки может не вернуться —
        # тогда кадры идут от обычного таймера, и анимации не встают.
        self.backup = QTimer(self, timeout=self._backup_tick)
        self.backup.setTimerType(Qt.PreciseTimer)
        self.backup.setInterval(4)
        self.wait = self._make_wait()
        self.tick.connect(self._on_tick)
        threading.Thread(target=self._run, daemon=True, name="MiniDockVSync").start()

    @staticmethod
    def _make_wait():
        try:
            gdi = ctypes.WinDLL("gdi32")
            o = _OpenAdapter()
            o.hDc = ctypes.windll.user32.GetDC(None)
            if gdi.D3DKMTOpenAdapterFromHdc(ctypes.byref(o)) == 0:
                w = _WaitVBlank(o.hAdapter, 0, o.VidPnSourceId)
                if gdi.D3DKMTWaitForVerticalBlankEvent(ctypes.byref(w)) == 0:
                    return lambda: gdi.D3DKMTWaitForVerticalBlankEvent(ctypes.byref(w))
        except Exception:
            pass
        dwm = ctypes.windll.dwmapi
        return lambda: dwm.DwmFlush() == 0 or time.sleep(1 / 120)

    def _run(self):
        while True:
            self.wake.wait()
            t = time.perf_counter()
            self.wait()
            if time.perf_counter() - t < 0.001:  # ожидание не сработало — не крутимся вхолостую
                time.sleep(1 / 240)
            if self.timers and not self.pending:
                self.pending = True  # следующий кадр не ставим в очередь, пока не нарисован этот
                self.tick.emit()

    def _backup_tick(self):
        if time.perf_counter() - self.last_tick > 0.03:  # от развёртки давно ничего — тикаем сами
            self._on_tick()

    def _on_tick(self):
        self.pending = False
        now = time.perf_counter()
        self.last_tick = now
        for t in list(self.timers):
            if t in self.timers:
                t._fire(now)

    def add(self, t):
        self.timers.add(t)
        self.wake.set()
        if not self.backup.isActive():
            self.last_tick = time.perf_counter()
            self.backup.start()

    def remove(self, t):
        self.timers.discard(t)
        if not self.timers:
            self.wake.clear()
            self.backup.stop()


_clock = None


def vsync_clock():
    global _clock
    if _clock is None:
        _clock = VSyncClock()
    return _clock


FRAME_REF = 0.014  # длительность кадра, под которую подбирались коэффициенты плавности


class FrameTimer(QObject):
    """Как QTimer для анимаций, но срабатывает на каждом кадре монитора. dt — секунды с прошлого кадра."""
    timeout = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.active = False
        self.last = 0.0
        self.dt = FRAME_REF

    def start(self, *_):
        if not self.active:
            self.active = True
            self.last = time.perf_counter()
            vsync_clock().add(self)

    def stop(self):
        if self.active:
            self.active = False
            vsync_clock().remove(self)

    def isActive(self):
        return self.active

    def _fire(self, now):
        self.dt = min(max(now - self.last, 0.001), 0.05)
        self.last = now
        self.timeout.emit()


def smooth(k, dt):
    """Коэффициент «догоняния» k (подобран для кадра 14 мс) для кадра длиной dt — скорость не зависит от частоты."""
    return 1 - (1 - k) ** (dt / FRAME_REF)


def per_frame(step, dt):
    """Шаг step (подобран для кадра 14 мс) для кадра длиной dt."""
    return step * dt / FRAME_REF


def precise_timer(parent, interval, slot):
    """Таймер анимации: тикает в такт монитору (interval оставлен для совместимости)."""
    t = FrameTimer(parent)
    t.timeout.connect(slot)
    return t


class Tween(QObject):
    """Анимация 0→1 за duration мс по кривой, кадры — в такт монитору."""

    def __init__(self, owner, duration, on_value, on_done, curve):
        super().__init__(owner)
        self.curve = QEasingCurve(curve)
        self.duration = max(duration / 1000, 0.001)
        self.on_value, self.on_done = on_value, on_done
        self.start = time.perf_counter()
        self.timer = FrameTimer(self)
        self.timer.timeout.connect(self.step)
        on_value(0.0)
        self.timer.start()

    def step(self):
        t = clamp((time.perf_counter() - self.start) / self.duration)
        self.on_value(self.curve.valueForProgress(t))
        if t >= 1:
            self.stop()
            if self.on_done:
                self.on_done()

    def stop(self):
        self.timer.stop()
        self.deleteLater()


def animate(owner, duration, on_value, on_done=None, curve=QEasingCurve.OutCubic):
    """Короткая анимация 0→1; ссылка хранится в owner, чтобы её не собрал сборщик мусора."""
    return Tween(owner, duration, on_value, on_done, curve)


def fade_in(widget, slide=0):
    """Плавное появление окна (прозрачность + небольшой сдвиг)."""
    final = widget.pos()
    widget.setWindowOpacity(0.0)

    def step(v):
        widget.setWindowOpacity(v)
        if slide:
            widget.move(final.x(), round(final.y() - slide * (1 - v)))

    widget._fade = animate(widget, 170, step)


def make_glass(widget, live_ms=0):
    g = glass.Glass(widget, live_ms)
    g.enabled = GLASS["enabled"]
    return g


class _StopLiveOnHide(QObject):
    def eventFilter(self, obj, e):
        if e.type() == e.Type.Hide:
            self.parent().stop_live()
        return False


def prepare_glass(g):
    """Перед показом окна снять размытый фон под ним (самого окна на экране ещё нет); дальше, если можно, —
    живой фон, пока окно открыто."""
    g.enabled = GLASS["enabled"]
    if g.enabled:
        g.refresh()
        if g.start_live() and not hasattr(g, "_hide_filter"):
            g._hide_filter = _StopLiveOnHide(g)
            g.w.installEventFilter(g._hide_filter)


def rounded_path(rect, radius):
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    return path


MENU_QSS = """
QMenu { background: rgba(38, 38, 44, 150); border: 1px solid rgba(255, 255, 255, 38); border-radius: 9px;
        padding: 5px; color: #f2f2f5; font: 9pt 'Segoe UI'; }
QMenu::item { padding: 4px 26px 4px 14px; border-radius: 5px; margin: 0 1px; }
QMenu::item:selected { background: rgba(10, 132, 255, 225); color: white; }
QMenu::item:disabled { color: rgba(255, 255, 255, 85); }
QMenu::separator { height: 1px; background: rgba(255, 255, 255, 30); margin: 4px 9px; }
QMenu::indicator { width: 12px; height: 12px; margin-left: 3px; }
"""


class MacMenu(QMenu):
    """QMenu со «стеклом», скруглением и плавным появлением."""

    def __init__(self, title="", parent=None):
        super().__init__(title, parent)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(MENU_QSS)
        self.glass = make_glass(self)

    def sub(self, title):
        m = MacMenu(title, self)
        self.addMenu(m)
        return m

    def showEvent(self, e):
        prepare_glass(self.glass)
        super().showEvent(e)
        fade_in(self)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        self.glass.fill(p, rounded_path(self.rect(), 9))
        p.end()
        super().paintEvent(e)


def popup_menu(menu, global_pos):
    """Показ меню из окна без фокуса: меню получает фокус, чтобы закрываться по клику мимо."""
    QTimer.singleShot(30, lambda: menu.isVisible() and try_foreground(int(menu.winId())))
    return menu.exec(global_pos)


class Toggle(QAbstractButton):
    """Переключатель в стиле macOS с анимацией кружка."""

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.k = 1.0 if checked else 0.0
        self.toggled.connect(self._animate)

    def sizeHint(self):
        return QSize(40, 24)

    def _animate(self, on):
        start = self.k
        end = 1.0 if on else 0.0

        def step(v):
            self.k = lerp(start, end, v)
            self.update()
        self._anim = animate(self, 160, step)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(1, 2, 38, 20)
        path = QPainterPath()
        path.addRoundedRect(r, 10, 10)
        off, on = QColor(255, 255, 255, 45), ACCENT
        col = QColor(round(lerp(off.red(), on.red(), self.k)), round(lerp(off.green(), on.green(), self.k)),
                     round(lerp(off.blue(), on.blue(), self.k)), round(lerp(off.alpha(), 255, self.k)))
        p.fillPath(path, col)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QPointF(lerp(12, 28, self.k), 12), 8, 8)

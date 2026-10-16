"""«Джинн» и «масштаб» на видеокарте: снимок окна натягивается на деформируемую сетку (Qt Quick, Direct3D 11).

В отличие от полос-миниатюр DWM, края сетки — наклонные линии, поэтому «лесенок» нет,
а сотни рядов сетки для видеокарты ничего не стоят.
"""
import ctypes
import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QSurfaceFormat
from PySide6.QtQuick import (QQuickItem, QQuickWindow, QSGGeometry, QSGGeometryNode, QSGNode, QSGOpacityNode,
                             QSGTexture, QSGTextureMaterial)
from PySide6.QtWidgets import QApplication

from .ui import clamp, ease, ease_out, lerp, precise_timer

ROWS = 160
SLOTS = 4
PAD = 2  # прозрачная рамка снимка для сглаживания краёв


def genie_rows(S, T, g, flow, rows=ROWS):
    """Ряды сетки: [(x_left, y_left, x_right, y_right, tx_l, ty_l, tx_r, ty_r)] для прогресса g (0 — окно, 1 — иконка).

    S, T — (l, t, r, b). flow: down | left | right — в какую сторону от окна находится док.
    """
    def to_flow(r):
        l, t, rr, b = r
        if flow == "down":
            return l, t, rr, b
        if flow == "right":
            return t, l, b, rr
        return t, -rr, b, -l

    Sa0, Sf0, Sa1, Sf1 = to_flow(S)
    Ta0, Tf0, Ta1, Tf1 = to_flow(T)
    ph1 = ease(clamp(g / 0.4))            # фаза 1: край окна у дока сужается к иконке
    ph2 = ease(clamp((g - 0.25) / 0.75))  # фаза 2: окно «стекает» в иконку
    span = Tf0 - Sf0 if abs(Tf0 - Sf0) > 1 else 1.0
    out = []
    for i in range(rows + 1):
        v = i / rows
        bv = min(ph2 + 0.7 * v * ph2 * (1 - ph2), 1.0)
        f = lerp(Sf0 + v * (Sf1 - Sf0), Tf0 + v * (Tf1 - Tf0), bv)
        s = ease(clamp((f - Sf0) / span))
        a0 = lerp(Sa0, lerp(Sa0, Ta0, s), ph1)
        a1 = lerp(Sa1, lerp(Sa1, Ta1, s), ph1)
        if flow == "down":
            out.append((a0, f, a1, f, 0.0, v, 1.0, v))
        elif flow == "right":
            out.append((f, a0, f, a1, v, 0.0, v, 1.0))
        else:
            out.append((-f, a0, -f, a1, 1.0 - v, 0.0, 1.0 - v, 1.0))
    return out


class GenieItem(QQuickItem):
    """Рисует до SLOTS анимаций одновременно. updatePaintNode вызывается в потоке рендера,
    пока поток интерфейса стоит, поэтому читать список анимаций здесь безопасно."""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setFlag(QQuickItem.ItemHasContents, True)
        self.slots = []
        self.blank = QImage(1, 1, QImage.Format_ARGB32_Premultiplied)
        self.blank.fill(Qt.transparent)

    def make_slot(self, root):
        # ссылки на геометрию, атрибуты, материал и текстуру держим сами: иначе Python удалит их раньше сцены
        attrs = QSGGeometry.defaultAttributes_TexturedPoint2D()
        geo = QSGGeometry(attrs, 0)
        geo.setDrawingMode(QSGGeometry.DrawTriangleStrip)
        gn = QSGGeometryNode()
        gn.setGeometry(geo)
        mat = QSGTextureMaterial()
        mat.setFiltering(QSGTexture.Linear)
        mat.setMipmapFiltering(QSGTexture.Linear)
        tex = self.window().createTextureFromImage(self.blank)
        mat.setTexture(tex)
        gn.setMaterial(mat)
        op = QSGOpacityNode()
        op.appendChildNode(gn)
        root.appendChildNode(op)
        return dict(attrs=attrs, geo=geo, gn=gn, mat=mat, tex=tex, blank=tex, op=op, anim=None, tex_for=None,
                    tex_ver=0, buf=None)

    def updatePaintNode(self, node, data):
        if node is None:
            node = QSGNode()
            self.root = node
            self.slots = [self.make_slot(node) for _ in range(SLOTS)]
        anims = list(self.owner.anims)
        for s in self.slots:
            if s["anim"] is not None and s["anim"] not in anims:
                s["anim"] = None
        for a in anims:
            if not any(s["anim"] is a for s in self.slots):
                free = next((s for s in self.slots if s["anim"] is None), None)
                if free is not None:
                    free["anim"] = a
        now = time.monotonic()
        for s in self.slots:
            a = s["anim"]
            geo = s["geo"]
            if a is None:
                if geo.vertexCount():
                    geo.allocate(0)
                    s["gn"].markDirty(QSGNode.DirtyGeometry)
                if s["tex_for"] is not None:  # отпускаем большую текстуру снимка
                    s["mat"].setTexture(s["blank"])
                    s["tex"], s["tex_for"] = s["blank"], None
                    s["gn"].markDirty(QSGNode.DirtyMaterial)
                continue
            if s["tex_for"] is not a or s["tex_ver"] != a["ver"]:  # новая анимация или свежий снимок окна
                tex = self.window().createTextureFromImage(a["img"], QQuickWindow.TextureHasMipmaps)
                s["mat"].setTexture(tex)
                s["tex"], s["tex_for"], s["tex_ver"] = tex, a, a["ver"]
                s["gn"].markDirty(QSGNode.DirtyMaterial)
            g, opacity = self.owner.progress(a, now)
            rows = self.owner.rows(a, g)
            count = len(rows) * 2
            if geo.vertexCount() != count:
                geo.allocate(count)
                s["buf"] = (ctypes.c_float * (count * 4))()
            buf = s["buf"]
            for i, r in enumerate(rows):
                buf[8 * i:8 * i + 8] = [r[0], r[1], r[4], r[5], r[2], r[3], r[6], r[7]]
            ctypes.memmove(int(geo.vertexData()), buf, ctypes.sizeof(buf))
            s["gn"].markDirty(QSGNode.DirtyGeometry)
            s["op"].setOpacity(opacity)
        return node


class GpuGenie:
    def __init__(self):
        self.anims = []
        self.win = QQuickWindow()
        fmt = QSurfaceFormat(self.win.format())
        fmt.setAlphaBufferSize(8)
        self.win.setFormat(fmt)
        self.win.setFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
                          | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.win.setColor(QColor(0, 0, 0, 0))
        self.item = GenieItem(self)
        self.item.setParentItem(self.win.contentItem())
        self.place()
        self.timer = precise_timer(None, 6, self.step)
        self.enabled = True
        self.swaps = 0        # кадров, выведенных на экран (считает поток рендера)
        self.waiters = []     # [номер кадра, колбэк] — «когда анимация уже видна на экране»
        self.win.frameSwapped.connect(self._swapped, Qt.DirectConnection)
        self.win.show()  # окно постоянно открыто и прозрачно: первая анимация не ждёт создания swapchain

    def _swapped(self):
        self.swaps += 1

    @property
    def hwnd(self):
        return int(self.win.winId())

    def after_frames(self, n, fn):
        """fn() в потоке интерфейса, когда на экран выйдет ещё n кадров анимации (но не позже чем через 0,2 с)."""
        self.waiters.append([self.swaps + n, time.monotonic() + 0.2, fn])
        self.timer.start()

    def place(self):
        g = QApplication.primaryScreen().geometry()
        self.win.setGeometry(g)
        self.item.setSize(self.win.size())
        self.origin = (g.x(), g.y())

    def set_enabled(self, on):
        """Прячем окно в полноэкранных играх, чтобы не мешать их выводу."""
        if on == self.enabled:
            return
        self.enabled = on
        if on:
            self.win.show()
        elif not self.anims:
            self.win.hide()

    def busy_with(self, hwnd):
        return any(a["hwnd"] == hwnd for a in self.anims)

    def play(self, hwnd, img, src, dst, reverse, duration, flow="down", mode="genie", on_done=None, dpr=1.0):
        """img — QImage окна; src/dst — (l, t, r, b) в физических пикселях экрана."""
        if self.busy_with(hwnd) or len(self.anims) >= SLOTS or img is None or img.isNull():
            return False
        a = dict(hwnd=hwnd, reverse=reverse, flow=flow, mode=mode, start=time.monotonic(), dur=duration, done=on_done,
                 release_at=None, ver=0, dpr=dpr, icon=dst)
        self._set_image(a, img, src, dst)
        if not self.win.isVisible():
            self.win.show()
        self.win.raise_()
        self.anims.append(a)
        self.item.update()
        self.timer.start()
        return True

    def _set_image(self, a, img, src, dst):
        ox, oy = self.origin
        dpr = a["dpr"]

        def logical(r):
            return (r[0] / dpr - ox, r[1] / dpr - oy, r[2] / dpr - ox, r[3] / dpr - oy)

        # Сглаживание краёв без MSAA (MSAA стоил ~160 МБ): у снимка прозрачная рамка в PAD пикселей,
        # сетка шире на столько же — линейная фильтрация текстуры сама смягчает наклонный край.
        padded = QImage(img.width() + 2 * PAD, img.height() + 2 * PAD, QImage.Format_ARGB32_Premultiplied)
        padded.fill(Qt.transparent)
        painter = QPainter(padded)
        painter.drawImage(PAD, PAD, img)
        painter.end()

        def expand(r):
            sx, sy = (r[2] - r[0]) / img.width() * PAD, (r[3] - r[1]) / img.height() * PAD
            return (r[0] - sx, r[1] - sy, r[2] + sx, r[3] + sy)

        a["img"], a["src"], a["dst"] = padded, expand(logical(src)), expand(logical(dst))
        a["ver"] += 1

    def update_image(self, hwnd, img, src):
        """Подменяет снимок у идущей анимации (например, свежий кадр уже развёрнутого окна)."""
        for a in self.anims:
            if a["hwnd"] == hwnd and a["release_at"] is None and img is not None and not img.isNull():
                self._set_image(a, img, src, a["icon"])
                self.item.update()
                return True
        return False

    def progress(self, a, now):
        if a["release_at"] is not None:
            t = 1.0
        else:
            t = clamp((now - a["start"]) / a["dur"])
        g = 1 - t if a["reverse"] else t
        if a["mode"] == "close":
            return g, 1 - ease(g)
        if a["mode"] == "scale":
            opacity = clamp((1 - g) / 0.35) if g > 0.65 else 1.0
        else:
            opacity = clamp((1 - g) / 0.12) if g > 0.88 else 1.0
        return g, opacity

    def rows(self, a, g):
        S, T = a["src"], a["dst"]
        if a["mode"] == "close":
            k = 1 - 0.16 * ease_out(g)  # как закрытие окна в macOS / Windows 11: чуть уменьшается и тает
            cx, cy = (S[0] + S[2]) / 2, (S[1] + S[3]) / 2 + 10 * ease_out(g)
            hw, hh = (S[2] - S[0]) / 2 * k, (S[3] - S[1]) / 2 * k
            l, t, r, b = cx - hw, cy - hh, cx + hw, cy + hh
            return [(l, t, r, t, 0.0, 0.0, 1.0, 0.0), (l, b, r, b, 0.0, 1.0, 1.0, 1.0)]
        if a["mode"] == "scale":
            k = ease(g)
            l, t, r, b = (lerp(s, d, k) for s, d in zip(S, T))
            return [(l, t, r, t, 0.0, 0.0, 1.0, 0.0), (l, b, r, b, 0.0, 1.0, 1.0, 1.0)]
        return genie_rows(S, T, g, a["flow"])

    def step(self):
        now = time.monotonic()
        for w in [w for w in self.waiters if self.swaps >= w[0] or now >= w[1]]:
            self.waiters.remove(w)
            w[2]()
        for a in list(self.anims):
            if a["release_at"] is not None:
                if now >= a["release_at"]:
                    self.anims.remove(a)
                continue
            if now - a["start"] >= a["dur"]:
                if a["done"]:
                    a["done"]()
                # пару кадров держим последний кадр, пока настоящее окно не появится на его месте
                a["release_at"] = now + (0.05 if a["reverse"] else 0.0)
        self.item.update()
        if not self.anims and not self.waiters:
            self.timer.stop()
            if not self.enabled:
                self.win.hide()

"""Папки в доке (как в MyDockFinder): иконка-сетка из программ внутри, по клику — окно с программами."""
import math
import subprocess

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

from .ui import MacMenu, fade_in, make_glass, paint_background, popup_menu, prepare_glass, rounded_path
from .winapi import try_foreground


def folder_image(images):
    """Иконка папки: полупрозрачный скруглённый квадрат с сеткой 3×3 из иконок программ (QImage)."""
    img = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(14, 14, 228, 228), 54, 54)
    p.fillPath(bg, QColor(255, 255, 255, 62))
    p.setPen(QPen(QColor(255, 255, 255, 70), 3))
    p.drawPath(bg)
    images = [im for im in images if im is not None and not im.isNull()]
    n = 2 if len(images) <= 4 else 3  # до 4 программ — крупная сетка 2×2
    cell, gap = (80, 16) if n == 2 else (56, 12)
    x0 = 128 - (n * cell + (n - 1) * gap) / 2
    for i, im in enumerate(images[:n * n]):
        r = QRectF(x0 + (i % n) * (cell + gap), x0 + (i // n) * (cell + gap), cell, cell)
        small = im.scaled(cell * 2, cell * 2, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p.drawImage(r, small, QRectF(small.rect()))
    p.end()
    return img


def folder_pixmap(children):
    return QPixmap.fromImage(folder_image([it.pix.toImage() for it in children if it.pix]))


class FolderPopup(QWidget):
    """Окно открытой папки над её иконкой: название (можно переименовать) и сетка программ."""
    CELL_W, CELL_H, PAD, TITLE_H = 92.0, 96.0, 18.0, 40.0

    def __init__(self, dock):
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.dock = dock
        self.folder = None
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.glass = make_glass(self, part="folders")
        self.hover = None
        self.font_label = QFont("Segoe UI", 9)
        self.title = QLineEdit(self)
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setFrame(False)
        self.title.setStyleSheet("QLineEdit { background: transparent; color: #f2f2f5; font: 600 12pt 'Segoe UI';"
                                 " border: none; border-radius: 6px; padding: 2px 6px; }"
                                 "QLineEdit:focus { background: rgba(255, 255, 255, 30); }")
        self.title.editingFinished.connect(self.rename)

    # -- layout
    def cols(self):
        n = len(self.folder.children)
        return max(1, min(5, math.ceil(math.sqrt(max(n, 1)))))

    def cell_rect(self, i):
        c = self.cols()
        return QRectF(self.PAD + (i % c) * self.CELL_W, self.PAD + self.TITLE_H + (i // c) * self.CELL_H,
                      self.CELL_W, self.CELL_H)

    def index_at(self, pos):
        for i in range(len(self.folder.children)):
            if self.cell_rect(i).contains(pos):
                return i
        return None

    def open(self, folder, edit_title=False):
        self.folder = folder
        n = len(folder.children)
        c = self.cols()
        rows = max(1, math.ceil(n / c))
        w = max(self.PAD * 2 + c * self.CELL_W, 220)
        h = self.PAD * 2 + self.TITLE_H + rows * self.CELL_H if n else self.PAD * 2 + self.TITLE_H + 60
        self.resize(int(w), int(h))
        self.title.setGeometry(int(self.PAD), int(self.PAD - 4), int(w - 2 * self.PAD), 30)
        self.title.setText(folder.name)
        self.hover = None
        # над иконкой папки (или рядом с ней, если док слева/справа)
        icon = self.dock.icon_global_rect(folder)
        scr = self.dock.screen().geometry()
        side = self.dock.side
        if side == "bottom":
            x, y = icon.center().x() - w / 2, icon.top() - h - 14
        elif side == "left":
            x, y = icon.right() + 14, icon.center().y() - h / 2
        else:
            x, y = icon.left() - w - 14, icon.center().y() - h / 2
        x = min(max(x, scr.left() + 6), scr.right() - w - 6)
        y = min(max(y, scr.top() + 6), scr.bottom() - h - 6)
        self.move(int(x), int(y))
        self.dock.menu_open = True  # док не прячется, пока папка открыта
        self.show()
        QTimer.singleShot(20, lambda: self.isVisible() and try_foreground(int(self.winId())))
        if edit_title:
            QTimer.singleShot(60, lambda: (self.title.setFocus(), self.title.selectAll()))

    def rename(self):
        if self.folder is None:
            return
        name = self.title.text().strip() or "Папка"
        if name != self.folder.name:
            self.folder.name = name
            self.dock.save_pinned()
        self.title.clearFocus()

    # -- events
    def showEvent(self, e):
        prepare_glass(self.glass)
        fade_in(self, slide=-10 if self.dock.side == "bottom" else 0)

    def hideEvent(self, e):
        self.rename()
        self.dock.menu_open = False
        self.dock.wake()

    def mouseMoveEvent(self, e):
        i = self.index_at(e.position())
        if i != self.hover:
            self.hover = i
            self.update()

    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def mouseReleaseEvent(self, e):
        i = self.index_at(e.position())
        if i is None:
            return
        child = self.folder.children[i]
        if e.button() == Qt.LeftButton:
            self.close()
            self.dock.activate(child)
        elif e.button() == Qt.RightButton:
            self.child_menu(child, e.globalPosition().toPoint())

    def child_menu(self, child, pos):
        m = MacMenu(parent=self)
        m.addAction("Открыть новое окно", lambda: (self.close(), self.dock.activate(child, new_window=True)))
        m.addAction("Убрать из папки", lambda: self.take_out(child))
        m.addAction("Показать в проводнике", lambda: subprocess.Popen(["explorer.exe", "/select,", child.path]))
        popup_menu(m, pos)

    def take_out(self, child):
        self.dock.take_out_of_folder(child)
        if self.isVisible():
            self.open(self.folder)
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        path = rounded_path(self.rect(), 16)
        self.glass.fill(p, path)
        p.fillPath(path, QColor(34, 34, 40, 165))
        paint_background(p, path, "folders")
        p.setPen(QPen(QColor(255, 255, 255, 45), 1))
        p.drawPath(path)
        dpr = self.devicePixelRatioF()
        fm = QFontMetricsF(self.font_label)
        p.setFont(self.font_label)
        if not self.folder.children:
            p.setPen(QColor(255, 255, 255, 130))
            p.drawText(QRectF(0, self.PAD + self.TITLE_H, self.width(), 60), Qt.AlignCenter,
                       "Перетащите сюда программы\nиз дока")
            return
        icon = 60.0
        for i, it in enumerate(self.folder.children):
            r = self.cell_rect(i)
            if i == self.hover:
                hp = QPainterPath()
                hp.addRoundedRect(r.adjusted(4, 2, -4, -2), 12, 12)
                p.fillPath(hp, QColor(255, 255, 255, 34))
            ir = QRectF(r.center().x() - icon / 2, r.top() + 8, icon, icon)
            if it.pix:
                pix = it.pix_for(icon * dpr)
                p.drawPixmap(ir, pix, QRectF(pix.rect()))
            if it.hwnds:  # точка «запущено»
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(235, 235, 240, 230))
                p.drawEllipse(QPointF(r.center().x(), ir.bottom() + 4), 1.8, 1.8)
            p.setPen(QColor(240, 240, 245))
            text = QRectF(r.left() + 4, ir.bottom() + 8, r.width() - 8, fm.height() + 2)
            p.drawText(text, Qt.AlignHCenter | Qt.AlignTop, fm.elidedText(it.name, Qt.ElideRight, text.width()))

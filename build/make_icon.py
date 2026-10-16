"""Иконка MiniDock (.ico с размерами 16–256): тёмный скруглённый квадрат с «доком» из цветных иконок."""
import sys

from PIL import Image
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication

out_ico, out_png = sys.argv[1], sys.argv[2]
app = QApplication([])
S = 256
img = QImage(S, S, QImage.Format_ARGB32)
img.fill(Qt.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)

# фон — скруглённый квадрат с градиентом, как иконки macOS
bg = QPainterPath()
bg.addRoundedRect(QRectF(12, 12, 232, 232), 54, 54)
grad = QLinearGradient(0, 12, 0, 244)
grad.setColorAt(0, QColor("#3b3f54"))
grad.setColorAt(1, QColor("#16171f"))
p.fillPath(bg, grad)
p.setPen(QColor(255, 255, 255, 40))
p.drawPath(bg)

# «стеклянная» полоса дока
dock = QPainterPath()
dock.addRoundedRect(QRectF(34, 148, 188, 60), 20, 20)
p.fillPath(dock, QColor(255, 255, 255, 46))

# иконки на доке (средняя увеличена — как при наведении)
icons = [("#0a84ff", 52, 160, 36), ("#30d158", 96, 156, 40), ("#ff9f0a", 140, 140, 52), ("#ff375f", 196, 160, 36)]
for color, cx, y, s in [(c, x, y, s) for c, x, y, s in icons]:
    r = QPainterPath()
    r.addRoundedRect(QRectF(cx - s / 2 + (0 if s != 52 else 6), y, s, s), s * 0.26, s * 0.26)
    p.fillPath(r, QColor(color))
# верхняя панель
bar = QPainterPath()
bar.addRoundedRect(QRectF(40, 46, 176, 16), 8, 8)
p.fillPath(bar, QColor(255, 255, 255, 70))
p.end()

img.save(out_png)
Image.open(out_png).save(out_ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon saved")

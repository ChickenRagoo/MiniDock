"""Окно настроек MiniDock в стиле «Системных настроек» macOS. Все изменения применяются сразу."""
import os

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QColorDialog, QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QScrollArea, QSlider, QStackedWidget, QVBoxLayout, QWidget)

from . import elevation
from .barwidgets import WIDGETS_DIR
from .config import CFG_DIR, DEFAULT_CFG
from .updater import PAGE
from .version import __version__
from .ui import BG_PARTS, GLASS_PARTS, MDL2, Toggle, fade_in, rounded_path
from .winapi import autostart_enabled, enable_blur, round_window, set_autostart

QSS = """
QWidget { color: #f2f2f5; font: 9pt 'Segoe UI'; background: transparent; }
QLabel#h1 { font: 15pt 'Segoe UI Semibold'; }
QLabel#desc { color: rgba(255, 255, 255, 130); font: 8pt 'Segoe UI'; }
QLabel#value { color: rgba(255, 255, 255, 170); min-width: 52px; }
QFrame#group { background: rgba(255, 255, 255, 14); border: 1px solid rgba(255, 255, 255, 22); border-radius: 10px; }
QFrame#line { background: rgba(255, 255, 255, 20); max-height: 1px; min-height: 1px; border: none; }
QListWidget { border: none; outline: 0; padding: 4px; }
QListWidget::item { padding: 7px 10px; border-radius: 7px; margin: 1px 0; }
QListWidget::item:selected { background: rgba(10, 132, 255, 210); color: white; }
QListWidget::item:hover:!selected { background: rgba(255, 255, 255, 22); }
QPushButton { background: rgba(255, 255, 255, 26); border: 1px solid rgba(255, 255, 255, 30); border-radius: 7px;
              padding: 5px 14px; }
QPushButton:hover { background: rgba(255, 255, 255, 44); }
QComboBox { background: rgba(255, 255, 255, 26); border: 1px solid rgba(255, 255, 255, 30); border-radius: 7px;
            padding: 4px 10px; min-width: 130px; }
QComboBox QAbstractItemView { background: #2a2a30; border: 1px solid rgba(255, 255, 255, 40);
                              selection-background-color: #0a84ff; outline: 0; }
QComboBox::drop-down { border: none; width: 18px; }
QSlider::groove:horizontal { height: 4px; background: rgba(255, 255, 255, 40); border-radius: 2px; }
QSlider::sub-page:horizontal { background: #0a84ff; border-radius: 2px; }
QSlider::handle:horizontal { background: white; width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; }
QScrollArea { border: none; }
QScrollBar:vertical { width: 8px; background: transparent; }
QScrollBar::handle:vertical { background: rgba(255, 255, 255, 50); border-radius: 4px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""

PAGES = [
    ("", "Основные"),
    ("", "Док"),
    ("", "Внешний вид"),
    ("", "Фоны"),
    ("", "Анимации"),
    ("", "Верхняя панель"),
    ("", "Alt+Tab"),
    ("", "Launchpad"),
    ("", "Обновления"),
    ("", "О программе"),
]


class TrafficLight(QPushButton):
    def __init__(self, color, cb):
        super().__init__()
        self.color = QColor(color)
        self.setFixedSize(14, 14)
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(cb)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(self.color.darker(130), 0.8))
        p.setBrush(self.color)
        p.drawEllipse(QRectF(1, 1, 12, 12))


class SettingsWindow(QWidget):
    def __init__(self, app):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint)
        self.app = app
        self.cfg = app.cfg
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("Настройки MiniDock")
        self.setStyleSheet(QSS)
        self.resize(880, 620)
        self.drag = None
        self.controls = {}

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        side = QWidget()
        side.setFixedWidth(210)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(12, 14, 8, 12)
        lights = QHBoxLayout()
        lights.setSpacing(8)
        lights.addWidget(TrafficLight("#ff5f57", self.close))
        lights.addWidget(TrafficLight("#febc2e", self.showMinimized))
        lights.addWidget(TrafficLight("#3a3a40", lambda: None))
        lights.addStretch()
        sl.addLayout(lights)
        sl.addSpacing(14)
        self.nav = QListWidget()
        for glyph, name in PAGES:
            it = QListWidgetItem(f"{name}")
            self.nav.addItem(it)
        sl.addWidget(self.nav, 1)
        outer.addWidget(side)

        self.stack = QStackedWidget()
        for build in (self.page_general, self.page_dock, self.page_look, self.page_backgrounds, self.page_anim,
                      self.page_bar,
                      self.page_switcher, self.page_launchpad, self.page_updates, self.page_about):
            self.stack.addWidget(self.scroll(build()))
        outer.addWidget(self.stack, 1)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

    # -- window chrome
    def showEvent(self, e):
        fade_in(self, slide=-8)

    def paintEvent(self, _):
        # окно перетаскивают, поэтому снятый фон под ним устаревал бы — делаем его почти непрозрачным
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = rounded_path(self.rect(), 12)
        p.fillPath(path, QColor(30, 30, 36, 248))
        side = QPainterPath()
        side.addRect(QRectF(0, 0, 210, self.height()))
        p.fillPath(side.intersected(path), QColor(255, 255, 255, 10))
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.drawPath(path)
        p.drawLine(210, 0, 210, self.height())

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() < 48:
            self.drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self.drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self.drag)

    def mouseReleaseEvent(self, e):
        self.drag = None

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()

    # -- builders
    def scroll(self, page):
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(page)
        area.viewport().setAutoFillBackground(False)
        return area

    def page(self, title):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(12)
        h = QLabel(title)
        h.setObjectName("h1")
        lay.addWidget(h)
        return w, lay

    def group(self, lay, rows):
        frame = QFrame()
        frame.setObjectName("group")
        gl = QVBoxLayout(frame)
        gl.setContentsMargins(14, 6, 14, 6)
        gl.setSpacing(0)
        for i, row in enumerate(rows):
            if i:
                line = QFrame()
                line.setObjectName("line")
                gl.addWidget(line)
            gl.addWidget(row)
        lay.addWidget(frame)

    def row(self, title, control, desc=None):
        w = QWidget()
        hl = QHBoxLayout(w)
        hl.setContentsMargins(0, 9, 0, 9)
        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(QLabel(title))
        if desc:
            d = QLabel(desc)
            d.setObjectName("desc")
            d.setWordWrap(True)
            text.addWidget(d)
        hl.addLayout(text, 1)
        if isinstance(control, QHBoxLayout):
            hl.addLayout(control)
        else:
            hl.addWidget(control)
        return w

    def toggle(self, key, title, desc=None):
        t = Toggle(bool(self.cfg[key]))
        t.toggled.connect(lambda v: self.app.set_cfg(key, v))
        self.controls[key] = t
        return self.row(title, t, desc)

    def slider(self, key, title, lo, hi, scale=1.0, fmt="{:.0f}", desc=None):
        s = QSlider(Qt.Horizontal)
        s.setFixedWidth(220)
        s.setRange(int(lo * scale), int(hi * scale))
        s.setValue(int(round(float(self.cfg[key]) * scale)))
        val = QLabel(fmt.format(float(self.cfg[key])))
        val.setObjectName("value")
        val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        def changed(v):
            value = v / scale
            if scale == 1.0:
                value = int(v)
            val.setText(fmt.format(value))
            self.app.set_cfg(key, value)
        s.valueChanged.connect(changed)
        box = QHBoxLayout()
        box.addWidget(s)
        box.addWidget(val)
        return self.row(title, box, desc)

    def combo(self, key, title, options, desc=None):
        c = QComboBox()
        for label, value in options:
            c.addItem(label, value)
        values = [v for _, v in options]
        if self.cfg[key] in values:
            c.setCurrentIndex(values.index(self.cfg[key]))
        c.currentIndexChanged.connect(lambda i: self.app.set_cfg(key, c.itemData(i)))
        return self.row(title, c, desc)

    def color(self, key, title, dialog_title="Цвет фона дока"):
        b = QPushButton()
        b.setFixedSize(46, 24)

        def paint():
            b.setStyleSheet(f"background: {self.cfg[key]}; border: 1px solid rgba(255,255,255,80); "
                            f"border-radius: 6px;")

        def pick():
            c = QColorDialog.getColor(QColor(self.cfg[key]), self, dialog_title)
            if c.isValid():
                self.app.set_cfg(key, c.name())
                paint()
        paint()
        b.clicked.connect(pick)
        return self.row(title, b)

    def button(self, text, cb):
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(cb)
        return b

    def set_admin(self, on, toggle):
        """Включить/выключить запуск с правами администратора и перезапустить MiniDock в нужном режиме."""
        ok = elevation.enable() if on else elevation.disable()
        if not ok:  # отказались в окне Windows или не получилось — переключатель назад
            toggle.blockSignals(True)
            toggle.setChecked(not on)
            toggle.blockSignals(False)
            return
        if on:
            set_autostart(False, self.app.script)  # теперь запуском при входе занимается задание
            elevation.restart_elevated()
        else:
            set_autostart(True, self.app.script)
            elevation.restart_normal()
        QApplication.quit()

    # -- pages
    def page_general(self):
        w, lay = self.page("Основные")
        admin_task = elevation.task_exists()
        auto = Toggle(autostart_enabled() or admin_task)
        auto.setEnabled(not admin_task)  # в режиме администратора MiniDock запускает задание Планировщика
        auto.toggled.connect(lambda v: set_autostart(v, self.app.script))
        admin = Toggle(admin_task)
        admin.toggled.connect(lambda v: self.set_admin(v, admin))
        self.group(lay, [
            self.toggle("replace_taskbar", "Заменить панель задач Windows",
                        "Док встаёт на место панели задач, она прячется. При выходе всё возвращается."),
            self.row("Запускать вместе с Windows", auto),
            self.row("Работать с правами администратора", admin,
                     "Нужно, чтобы Alt+Tab, Win и Alt+Space работали и поверх программ, запущенных от администратора "
                     "(диспетчер задач, лаунчеры игр, античиты). Windows один раз спросит разрешение. "
                     "Перетаскивать файлы из Проводника в док в этом режиме нельзя."),
        ])
        self.group(lay, [
            self.toggle("win_key_launchpad", "Клавиша Win открывает Launchpad",
                        "Одиночное нажатие Win. Сочетания вроде Win+E, Win+D работают как обычно."),
            self.toggle("spotlight", "Spotlight по Alt+Space",
                        "Поиск приложений, файлов, настроек, калькулятор. Также лупа на верхней панели."),
        ])
        self.group(lay, [
            self.row("Папка настроек", self.button("Открыть", lambda: os.startfile(str(CFG_DIR)))),
            self.row("Сбросить все настройки", self.button("Сбросить", self.reset),
                     "Закреплённые приложения сохранятся."),
        ])
        lay.addStretch()
        return w

    def page_dock(self):
        w, lay = self.page("Док")
        self.group(lay, [
            self.combo("position", "Положение на экране",
                       [("Снизу", "bottom"), ("Слева", "left"), ("Справа", "right")]),
            self.slider("icon_size", "Размер иконок", 24, 96, fmt="{:.0f} px"),
            self.slider("magnify", "Увеличение при наведении", 1.0, 2.5, scale=100, fmt="{:.2f}×"),
            self.slider("spacing", "Расстояние между иконками", 0, 0.5, scale=100, fmt="{:.2f}"),
            self.slider("margin", "Отступ от края экрана", 0, 40, fmt="{:.0f} px"),
            self.toggle("dock_all_screens", "Док на всех мониторах",
                        "Свой док на каждом мониторе: окна сворачиваются в док того экрана, где они открыты."),
        ])
        self.group(lay, [
            self.toggle("autohide", "Автоматически скрывать док",
                        "Док появляется, когда курсор касается края экрана."),
            self.slider("autohide_delay", "Задержка скрытия", 0, 2, scale=100, fmt="{:.2f} с"),
        ])
        self.group(lay, [
            self.toggle("show_launchpad", "Значок Launchpad"),
            self.toggle("show_running", "Показывать запущенные приложения"),
            self.toggle("show_trash", "Корзина"),
            self.toggle("show_labels", "Подписи при наведении"),
            self.toggle("dock_previews", "Миниатюры окон при наведении",
                        "Наведите на запущенное приложение: клик по миниатюре — открыть окно, крестик — закрыть."),
            self.slider("dock_preview", "Размер миниатюр", 140, 360, fmt="{:.0f} px"),
            self.slider("dock_preview_pad", "Толщина рамки", 2, 24, fmt="{:.0f} px"),
            self.slider("dock_preview_delay", "Задержка показа", 0, 1.5, scale=100, fmt="{:.2f} с"),
            self.toggle("dock_peek", "Показывать окно целиком при наведении на миниатюру",
                        "Как на панели задач Windows: окно встаёт на своё место, остальные становятся прозрачными. "
                        "Мышь ушла с миниатюры — всё возвращается."),
            self.toggle("bounce", "Подпрыгивание при запуске"),
            self.combo("indicator", "Индикатор запущенных",
                       [("Точка", "dot"), ("Полоска", "line"), ("Нет", "none")]),
        ])
        lay.addStretch()
        return w

    def page_look(self):
        w, lay = self.page("Внешний вид дока")
        self.group(lay, [
            self.color("dock_color", "Цвет фона"),
            self.slider("dock_opacity", "Непрозрачность фона", 0, 255, fmt="{:.0f}"),
            self.toggle("dock_blur", "Размытие под доком"),
            self.toggle("glass_live", "Живое размытие",
                        "Фон под доком и всплывающими окнами обновляется на лету — окна под ними двигаются. "
                        "Если выключить, фон снимается один раз при появлении (чуть меньше нагрузки)."),
            self.slider("glass_radius", "Сила размытия", 4, 40, fmt="{:.0f}"),
            self.slider("dock_radius", "Скругление углов", 0, 0.5, scale=100, fmt="{:.2f}"),
            self.toggle("dock_border", "Светлая обводка"),
        ])
        self.group(lay, self.live_parts_rows())
        lay.addStretch()
        return w

    def live_parts_rows(self):
        """Где включено живое размытие: отдельный переключатель для каждой части интерфейса."""
        parts = set(self.cfg["glass_live_parts"])
        rows, toggles = [], []
        for key, title in GLASS_PARTS:
            t = Toggle(key in parts)

            def changed(on, key=key):
                cur = [k for k in self.cfg["glass_live_parts"] if k != key]
                self.app.set_cfg("glass_live_parts", cur + [key] if on else cur)
            t.toggled.connect(changed)
            toggles.append(t)
            rows.append(self.row(title, t))
        head = QLabel("Где размытие живое")
        head.setObjectName("desc")
        rows.insert(0, head)

        def follow(on):  # без общего «Живое размытие» отдельные переключатели не действуют
            for t in toggles:
                t.setEnabled(bool(on))
        self.controls["glass_live"].toggled.connect(follow)
        follow(self.cfg["glass_live"])
        return rows

    def page_backgrounds(self):
        w, lay = self.page("Фоны")
        note = QLabel("Своя картинка вместо фона любой части интерфейса. Картинка заполняет фон целиком "
                      "(по центру, с обрезкой — как обои рабочего стола) и рисуется поверх размытия; "
                      "прозрачностью можно оставить размытие видным сквозь неё.")
        note.setObjectName("desc")
        note.setWordWrap(True)
        lay.addWidget(note)
        self.group(lay, [self.background_row(part, title) for part, title in BG_PARTS])
        lay.addStretch()
        return w

    def background_row(self, part, title):
        """Строка части интерфейса: имя файла, «Выбрать…», «Убрать», прозрачность картинки."""
        spec = dict(self.cfg["backgrounds"].get(part) or {})
        desc = QLabel(os.path.basename(spec["path"]) if spec.get("path") else "Без картинки")
        desc.setObjectName("desc")
        opacity = QSlider(Qt.Horizontal)
        opacity.setFixedWidth(120)
        opacity.setRange(5, 100)
        opacity.setValue(round(float(spec.get("opacity", 1.0)) * 100))
        opacity.setEnabled(bool(spec.get("path")))
        opacity.setToolTip("Непрозрачность картинки")

        def store(new):
            bgs = dict(self.cfg["backgrounds"])
            if new:
                bgs[part] = new
            else:
                bgs.pop(part, None)
            self.app.set_cfg("backgrounds", bgs)
            desc.setText(os.path.basename(new["path"]) if new else "Без картинки")
            opacity.setEnabled(bool(new))

        def choose():
            from PySide6.QtWidgets import QFileDialog
            src, _ = QFileDialog.getOpenFileName(self, f"Картинка для фона: {title}", "",
                                                 "Картинки (*.png *.jpg *.jpeg *.bmp *.webp *.gif)")
            if not src:
                return
            import shutil
            import time
            folder = CFG_DIR / "backgrounds"
            folder.mkdir(parents=True, exist_ok=True)
            # копия у себя — фон не пропадёт, если исходный файл переместят; новое имя — новая картинка в кэше
            dst = folder / f"{part}_{int(time.time())}{os.path.splitext(src)[1].lower()}"
            shutil.copyfile(src, dst)
            old = (self.cfg["backgrounds"].get(part) or {}).get("path")
            store({"path": str(dst), "opacity": opacity.value() / 100})
            if old and os.path.dirname(old) == str(folder):
                try:
                    os.remove(old)
                except OSError:
                    pass

        def remove():
            old = (self.cfg["backgrounds"].get(part) or {}).get("path")
            store(None)
            if old and os.path.dirname(old) == str(CFG_DIR / "backgrounds"):
                try:
                    os.remove(old)
                except OSError:
                    pass

        def set_opacity(v):
            cur = self.cfg["backgrounds"].get(part)
            if cur:
                store(dict(cur, opacity=v / 100))
        opacity.valueChanged.connect(set_opacity)
        box = QHBoxLayout()
        box.addWidget(opacity)
        box.addWidget(self.button("Выбрать…", choose))
        box.addWidget(self.button("Убрать", remove))
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 9, 0, 9)
        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(QLabel(title))
        text.addWidget(desc)
        hl.addLayout(text, 1)
        hl.addLayout(box)
        return row

    def page_anim(self):
        w, lay = self.page("Анимации")
        self.group(lay, [
            self.toggle("animations", "Анимации окон", "Свёртывание в док и разворачивание из него."),
            self.combo("minimize_effect", "Эффект свёртывания", [("Джинн", "genie"), ("Масштаб", "scale")]),
            self.slider("anim_speed", "Скорость анимаций", 0.5, 2.0, scale=100, fmt="{:.2f}×"),
            self.toggle("open_animation", "Открытие программ из иконки",
                        "Окно программы, запущенной из дока или Launchpad, вылетает из её иконки."),
            self.toggle("close_animation", "Анимация закрытия окон"),
            self.toggle("window_shadows", "Тени под окнами",
                        "Выключено — у окон программ нет теней, а анимации заканчиваются без «всплывающей» тени. "
                        "При выходе из MiniDock системная настройка возвращается."),
        ])
        self.group(lay, [
            self.row("Системная анимация Windows",
                     self.button("Открыть", lambda: os.startfile("SystemPropertiesPerformance.exe", "runas")),
                     "Чтобы анимации не накладывались, снимите галочку «Анимация окон при свёртывании "
                     "и развёртывании»."),
        ])
        lay.addStretch()
        return w

    def page_bar(self):
        w, lay = self.page("Верхняя панель")
        self.group(lay, [
            self.toggle("menubar", "Показывать верхнюю панель"),
            self.toggle("bar_all_screens", "На всех мониторах",
                        "Панель появится и на дополнительных мониторах; развёрнутые окна там встанут под неё."),
            self.slider("bar_height", "Высота", 20, 36, fmt="{:.0f} px"),
            self.slider("bar_opacity", "Непрозрачность", 0, 255, fmt="{:.0f}"),
            self.toggle("bar_blur", "Размытие"),
        ])
        self.group(lay, [
            self.toggle("clock_seconds", "Секунды в часах"),
            self.toggle("clock_date", "Дата рядом с часами"),
            self.toggle("show_battery", "Заряд аккумулятора", "Только на ноутбуках."),
        ])
        self.widgets_box = QVBoxLayout()
        self.widgets_box.setContentsMargins(0, 0, 0, 0)
        holder = QWidget()
        holder.setLayout(self.widgets_box)
        lay.addWidget(QLabel("Виджеты"))
        lay.addWidget(holder)
        self.fill_widgets()
        self.app.widgets.listed.connect(self.fill_widgets)
        self.group(lay, [
            self.combo("media_bg", "Фон карточек плееров в окне звука",
                       [("Размытая обложка", "blur"), ("Сплошной цвет", "solid")]),
            self.color("media_color", "Цвет фона карточек", "Цвет фона карточек плееров"),
        ])
        lay.addStretch()
        return w

    def page_switcher(self):
        w, lay = self.page("Переключатель Alt+Tab")
        self.group(lay, [
            self.toggle("custom_switcher", "Свой переключатель вместо системного",
                        "Alt+Tab / Alt+Shift+Tab, стрелки, мышь. Q — закрыть приложение, Esc — отмена."),
            self.combo("switcher_style", "Стиль", [("Значки программ и их окна (как в macOS)", "mac"),
                                                   ("Миниатюры всех окон (как в Windows)", "windows")],
                       "Во втором стиле у каждой миниатюры внизу — значок программы."),
            self.button_row("Посмотреть, как выглядит", "Показать", self.preview_switcher),
        ])
        self.group(lay, [
            self.toggle("switcher_previews", "Миниатюры окон",
                        "Живые миниатюры всех окон выбранного приложения. ↓ или ` — выбрать окно, ← → — какое, "
                        "↑ — назад к приложениям, Q — закрыть выбранное окно."),
            self.slider("switcher_preview", "Размер миниатюр", 140, 400, fmt="{:.0f} px"),
        ])
        self.group(lay, [
            self.slider("switcher_icon", "Размер иконок", 48, 160, fmt="{:.0f} px"),
            self.per_row_slider(),
            self.slider("switcher_opacity", "Непрозрачность фона", 0, 255, fmt="{:.0f}"),
            self.slider("switcher_radius", "Скругление углов", 0, 40, fmt="{:.0f} px"),
            self.combo("switcher_position", "Положение", [("По центру экрана", "center"),
                                                           ("Выше центра", "upper")]),
        ])
        self.group(lay, [
            self.toggle("switcher_titles", "Название приложения"),
            self.toggle("switcher_window_title", "Заголовок окна под названием"),
            self.slider("switcher_delay", "Задержка появления", 0, 400, fmt="{:.0f} мс",
                        desc="Быстрое нажатие Alt+Tab переключает без показа окна."),
            self.toggle("switcher_minimized", "Показывать свёрнутые приложения"),
            self.toggle("switcher_hover", "Предпросмотр при наведении мыши",
                        "Наведение показывает окна приложения, а переключает только клик."),
            self.toggle("switcher_genie", "Разворачивать «джинном» из дока",
                        "Если выключить — свёрнутое окно появляется сразу."),
        ])
        lay.addStretch()
        return w

    def fill_widgets(self):
        """Список виджетов верхней панели: встроенные и свои из папки widgets."""
        while self.widgets_box.count():
            item = self.widgets_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        host = self.app.widgets
        on = set(host.enabled())
        rows = []
        for wid, name in host.available():
            t = Toggle(wid in on)
            t.toggled.connect(lambda v, wid=wid: (host.set_enabled(wid, v), self.app.save_soon()))
            rows.append(self.row(name, t))
        tools = QHBoxLayout()
        tools.addWidget(self.button("Открыть папку", lambda: os.startfile(str(WIDGETS_DIR))))
        tools.addWidget(self.button("Перезагрузить", host.reload))
        desc = ("Клик по виджету на панели открывает подробности. Свои виджеты — файлы .py в папке виджетов: "
                "там лежит пример с описанием.")
        if host.errors:
            desc += "\n\nНе загрузились:\n" + "\n".join(host.errors)
        rows.append(self.row("Свои виджеты", tools, desc))
        frame = QFrame()
        frame.setObjectName("group")
        gl = QVBoxLayout(frame)
        gl.setContentsMargins(14, 6, 14, 6)
        gl.setSpacing(0)
        for i, row in enumerate(rows):
            if i:
                line = QFrame()
                line.setObjectName("line")
                gl.addWidget(line)
            gl.addWidget(row)
        self.widgets_box.addWidget(frame)

    def per_row_slider(self):
        """«В одной строке»: 0 показываем как «Все» — ряд не переносится."""
        row = self.slider("switcher_per_row", "Иконок и окон в одной строке", 0, 20, fmt="{:.0f}",
                          desc="Сколько иконок (или миниатюр во втором стиле) помещается в ряд, остальные — "
                               "на следующей строке. 0 — все в один ряд.")
        val = row.findChild(QLabel, "value")
        fix = lambda: val.setText("Все" if val.text() == "0" else val.text())
        row.findChild(QSlider).valueChanged.connect(lambda _: fix())
        fix()
        return row

    def button_row(self, title, text, cb, desc=None):
        return self.row(title, self.button(text, cb), desc)

    def preview_switcher(self):
        sw = self.app.switcher
        sw.begin(1)
        from PySide6.QtCore import QTimer
        QTimer.singleShot(2500, sw.cancel)

    def page_launchpad(self):
        w, lay = self.page("Launchpad")
        self.group(lay, [
            self.combo("lp_mode", "Показывать", [("Все приложения по алфавиту", "all"),
                                                 ("Закреплённые плитки из «Пуска»", "start")],
                       "Переключать можно и прямо в Launchpad (вверху или клавишей Tab)."),
        ])
        self.group(lay, [
            self.slider("lp_columns", "Столбцов", 4, 10),
            self.slider("lp_rows", "Строк", 3, 7),
            self.slider("lp_icon", "Размер иконок", 48, 128, fmt="{:.0f} px"),
        ])
        self.group(lay, [
            self.row("Список приложений", self.button("Обновить", self.app.launchpad.catalog.load),
                     "Берётся из меню «Пуск», включая приложения Microsoft Store."),
        ])
        lay.addStretch()
        return w

    def page_updates(self):
        w, lay = self.page("Обновления")
        up = self.app.updater
        status = QLabel(up.status_text())
        status.setObjectName("desc")
        status.setWordWrap(True)
        action = self.button("Проверить сейчас", lambda: up.install() if up.state == "ready" else up.check())

        def refresh():
            status.setText(up.status_text())
            action.setText("Установить сейчас" if up.state == "ready" else "Проверить сейчас")
            action.setEnabled(up.state not in ("checking", "downloading", "installing"))
        up.changed.connect(refresh)
        refresh()
        self.group(lay, [
            self.row(f"MiniDock {__version__}", action),
            status,
        ])
        self.group(lay, [
            self.toggle("auto_update", "Устанавливать обновления автоматически",
                        "Новая версия скачивается с GitHub и ставится, когда компьютер простаивает "
                        "3 минуты и не запущена игра. Док пропадёт на несколько секунд и вернётся."),
            self.row("Все версии и что в них нового", self.button("Открыть на GitHub",
                                                                   lambda: os.startfile(PAGE))),
        ])
        lay.addStretch()
        return w

    def page_about(self):
        w, lay = self.page("О программе")
        info = QLabel(
            f"<b>MiniDock {__version__}</b> — док, верхняя панель, Launchpad и переключатель окон в стиле macOS "
            "для Windows 10.<br><br>"
            "<b>Клавиши</b><br>"
            "Win — Launchpad<br>"
            "Alt+Space — Spotlight (Enter — открыть, Ctrl+Enter — показать в папке)<br>"
            "Alt+Tab, Alt+Shift+Tab — переключение приложений (Q — закрыть, Esc — отмена)<br>"
            "Shift+клик по иконке в доке — новое окно<br><br>"
            "<b>Выход</b>: правый клик по доку → «Выход» или ярлык «Выйти из MiniDock» в меню «Пуск» — "
            "панель задач и рабочая область "
            "вернутся как были.")
        info.setWordWrap(True)
        info.setTextFormat(Qt.RichText)
        frame = QFrame()
        frame.setObjectName("group")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(16, 14, 16, 14)
        fl.addWidget(info)
        lay.addWidget(frame)
        lay.addStretch()
        return w

    def reset(self):
        if QMessageBox.question(self, "MiniDock", "Сбросить все настройки по умолчанию?") != QMessageBox.Yes:
            return
        keep = {k: self.cfg[k] for k in ("pinned", "taskbar_orig_state")}
        self.cfg.clear()
        self.cfg.update(DEFAULT_CFG)
        self.cfg.update(keep)
        self.app.set_cfg("cfg_version", DEFAULT_CFG["cfg_version"])
        self.close()
        self.app.settings = None
        self.app.open_settings()

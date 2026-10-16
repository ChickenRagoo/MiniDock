"""Окно настроек MiniDock в стиле «Системных настроек» macOS. Все изменения применяются сразу."""
import os
import subprocess

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QColorDialog, QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QScrollArea, QSlider, QStackedWidget, QVBoxLayout, QWidget)

from .config import CFG_DIR, DEFAULT_CFG
from .updater import PAGE
from .version import __version__
from .ui import MDL2, Toggle, fade_in, rounded_path
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
        for build in (self.page_general, self.page_dock, self.page_look, self.page_anim, self.page_bar,
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

    def color(self, key, title):
        b = QPushButton()
        b.setFixedSize(46, 24)

        def paint():
            b.setStyleSheet(f"background: {self.cfg[key]}; border: 1px solid rgba(255,255,255,80); "
                            f"border-radius: 6px;")

        def pick():
            c = QColorDialog.getColor(QColor(self.cfg[key]), self, "Цвет фона дока")
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

    # -- pages
    def page_general(self):
        w, lay = self.page("Основные")
        auto = Toggle(autostart_enabled())
        auto.toggled.connect(lambda v: set_autostart(v, self.app.script))
        self.group(lay, [
            self.toggle("replace_taskbar", "Заменить панель задач Windows",
                        "Док встаёт на место панели задач, она прячется. При выходе всё возвращается."),
            self.row("Запускать вместе с Windows", auto),
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
            self.slider("dock_preview_delay", "Задержка показа", 0, 1.5, scale=100, fmt="{:.2f} с"),
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
            self.toggle("dock_blur", "Размытие под доком",
                        "Работает при автоскрытии: фон снимается, пока док спрятан."),
            self.slider("dock_radius", "Скругление углов", 0, 0.5, scale=100, fmt="{:.2f}"),
            self.toggle("dock_border", "Светлая обводка"),
        ])
        lay.addStretch()
        return w

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
                     self.button("Открыть", lambda: subprocess.Popen(["SystemPropertiesPerformance.exe"])),
                     "Чтобы анимации не накладывались, снимите галочку «Анимация окон при свёртывании "
                     "и развёртывании»."),
        ])
        lay.addStretch()
        return w

    def page_bar(self):
        w, lay = self.page("Верхняя панель")
        self.group(lay, [
            self.toggle("menubar", "Показывать верхнюю панель"),
            self.slider("bar_height", "Высота", 20, 36, fmt="{:.0f} px"),
            self.slider("bar_opacity", "Непрозрачность", 0, 255, fmt="{:.0f}"),
            self.toggle("bar_blur", "Размытие"),
        ])
        self.group(lay, [
            self.toggle("clock_seconds", "Секунды в часах"),
            self.toggle("clock_date", "Дата рядом с часами"),
            self.toggle("show_battery", "Заряд аккумулятора", "Только на ноутбуках."),
        ])
        lay.addStretch()
        return w

    def page_switcher(self):
        w, lay = self.page("Переключатель Alt+Tab")
        self.group(lay, [
            self.toggle("custom_switcher", "Свой переключатель вместо системного",
                        "Alt+Tab / Alt+Shift+Tab, стрелки, мышь. Q — закрыть приложение, Esc — отмена."),
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

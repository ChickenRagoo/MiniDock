"""Spotlight: поиск приложений, файлов (индекс Windows), системных команд, калькулятор и поиск в интернете."""
import ast
import ctypes
import datetime
import math
import operator
import functools
import os
import subprocess
import threading
import time
import urllib.parse

from PySide6.QtCore import QFileInfo, QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QFileIconProvider, QLineEdit, QMessageBox, QWidget

from .ui import MDL2, animate, fade_in, make_glass, paint_background, prepare_glass, rounded_path
from .winapi import enable_blur, force_foreground, launch, round_window, user32

ROW_H, HEAD_H, TOP_H, WIDTH, MAX_ROWS = 40, 24, 62, 720, 11
SKIP_PARTS = ("\\appdata\\", "\\.", "\\node_modules\\", "\\$recycle.bin\\", "\\__pycache__\\")

# -- калькулятор: только арифметика и функции math, никакого произвольного кода
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"sqrt": math.sqrt, "sin": lambda x: math.sin(math.radians(x)), "cos": lambda x: math.cos(math.radians(x)),
          "tan": lambda x: math.tan(math.radians(x)), "log": math.log10, "ln": math.log, "abs": abs, "round": round,
          "exp": math.exp}
_CONSTS = {"pi": math.pi, "e": math.e, "пи": math.pi}


def calc(text):
    expr = text.strip().replace("^", "**").replace("×", "*").replace("÷", "/").replace(",", ".")
    has_op = any(op in expr for op in "+-*/%(")
    if not any(ch.isdigit() for ch in expr) or not has_op:
        return None

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            left, right = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(right) > 1000:
                raise ValueError
            return _OPS[type(n.op)](left, right)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Name) and n.id in _CONSTS:
            return _CONSTS[n.id]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and len(n.args) == 1:
            return _FUNCS[n.func.id](ev(n.args[0]))
        raise ValueError
    try:
        value = ev(ast.parse(expr, mode="eval"))
    except Exception:
        return None
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.10g}"
    return str(value)


# -- поиск на любом языке: не та раскладка и транслит --------------------------------------------------
_EN = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./"
_RU = "ёйцукенгшщзхъфывапролджэячсмитьбю."
_TO_RU = {e: r for e, r in zip(_EN, _RU)}
_TO_EN = {r: e for e, r in zip(_EN, _RU)}
_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r",
                      "s", "t", "u", "f", "kh", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))
# звуковой «скелет»: одинаково для «телеграм» и telegram, «хром» и chrome, «стим» и steam, «ютуб» и youtube
_SOUNDS = [("sch", "sh"), ("ph", "f"), ("ck", "k"), ("qu", "kv"), ("c", "k"), ("q", "k"), ("w", "v"), ("x", "ks"),
           ("you", "iu"), ("ou", "u"), ("ee", "i"), ("ea", "i"), ("y", "i"), ("ai", "i"), ("ei", "i")]


def swap_layout(text):
    """Текст, набранный не в той раскладке: «ntktuhfv» → «телеграм», «еудупкфь» → «telegram»."""
    cyr = sum("а" <= ch <= "я" or ch == "ё" for ch in text)
    table = _TO_EN if cyr * 2 >= len(text) else _TO_RU
    return "".join(table.get(ch, ch) for ch in text)


@functools.lru_cache(maxsize=4096)
def skeleton(text):
    s = "".join(_TRANSLIT.get(ch, ch) for ch in text.casefold())
    for a, b in _SOUNDS:
        s = s.replace(a, b)
    out = []
    for ch in s:  # двойные буквы — как одна (Photoshop / фотошоп, Steam / стим)
        if not out or out[-1] != ch or not ch.isalpha():
            out.append(ch)
    return "".join(out)


def score(name, q):
    """Насколько название подходит к запросу. Находит и набранное не в той раскладке, и транслитом —
    такие совпадения чуть ниже точных."""
    best = _score(name, q)
    if len(q) < 2 or best >= 100:
        return best
    swapped = swap_layout(q)
    variants = [(swapped, 6)]
    if len(q) >= 3 and any(ch.isalpha() for ch in q):  # по звучанию — от трёх букв, иначе слишком много шума
        variants += [(q, 10), (swapped, 14)]  # «телеграм», «ntktuhfv»
    extra = 30 if best else 0  # у самого запроса уже есть совпадение — догадки идут ниже него
    for i, (v, penalty) in enumerate(variants):
        penalty += extra
        if i == 0:
            s = _score(name, v)
        else:
            sk = skeleton(v)
            s = _score(skeleton(name), sk) if len(sk) >= 2 else 0
        if s:
            best = max(best, s - penalty)
    return best


def _score(name, q):
    n = name.casefold()
    if n == q:
        return 120
    if n.startswith(q):
        return 100
    words = n.replace("-", " ").replace("_", " ").split()
    if any(w.startswith(q) for w in words):
        return 80
    if q in n:
        return 55
    if len(q) > 1 and "".join(w[0] for w in words if w).startswith(q):  # «vsc» → Visual Studio Code
        return 45
    return 0


class FileSearch(QObject):
    """Поиск файлов в индексе Windows (в фоне)."""
    found = Signal(int, list)

    def __init__(self):
        super().__init__()
        self.conn = None

    def query(self, seq, text):
        threading.Thread(target=self._run, args=(seq, text), daemon=True).start()

    def _run(self, seq, text):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        out = []
        try:
            conn = win32com.client.Dispatch("ADODB.Connection")
            conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
            q = text.replace("'", "''").replace("%", "").replace("[", "").replace("_", "[_]")
            home = os.path.expanduser("~").replace("\\", "/")
            sql = ("SELECT TOP 60 System.ItemUrl, System.ItemNameDisplay, System.ItemType, System.DateModified, "
                   "System.Size FROM SYSTEMINDEX "
                   f"WHERE System.ItemNameDisplay LIKE '%{q}%' AND SCOPE='file:{home}' "
                   "ORDER BY System.DateModified DESC")
            rs = win32com.client.Dispatch("ADODB.Recordset")
            rs.Open(sql, conn)
            while not rs.EOF:
                url = rs.Fields.Item(0).Value or ""
                path = urllib.parse.unquote(url[5:] if url.lower().startswith("file:") else url).replace("/", "\\")
                low = path.lower()
                if path and not any(s in low for s in SKIP_PARTS):
                    out.append(dict(path=path, name=rs.Fields.Item(1).Value or os.path.basename(path),
                                    kind=rs.Fields.Item(2).Value or "", modified=rs.Fields.Item(3).Value,
                                    size=rs.Fields.Item(4).Value))
                rs.MoveNext()
            rs.Close()
            conn.Close()
        except Exception:
            pass
        self.found.emit(seq, out)


class Spotlight(QWidget):
    def __init__(self, app):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.app = app
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.results = []      # [dict(section, title, sub, icon_kind, icon, action, alt)]
        self.files = []
        self.selected = 0
        self.picked = set()    # несколько строк, выделенных протягиванием мыши или Ctrl+кликом
        self.press_row = None
        self.scroll = 0
        self.seq = 0
        self.was_active = False
        self.opened_at = 0.0
        self.icons = {}
        self.provider = QFileIconProvider()
        self.files_search = FileSearch()
        self.files_search.found.connect(self.on_files)
        self.debounce = QTimer(self, singleShot=True, interval=140, timeout=self.query_files)

        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText("Поиск Spotlight")
        self.edit.setStyleSheet("QLineEdit { background: transparent; border: none; color: #f4f4f7; "
                                "font: 17pt 'Segoe UI Light'; selection-background-color: #0a84ff; }")
        pal = self.edit.palette()
        pal.setColor(QPalette.PlaceholderText, QColor(255, 255, 255, 110))
        self.edit.setPalette(pal)
        self.edit.textChanged.connect(self.on_text)
        self.edit.cursorPositionChanged.connect(lambda *_: self.update())
        self.edit.selectionChanged.connect(self.update)
        self.edit.installEventFilter(self)
        self.f_title = QFont("Segoe UI", 10)
        self.f_sub = QFont("Segoe UI", 8)
        self.f_head = QFont("Segoe UI", 8, QFont.DemiBold)
        self.f_glyph = QFont(MDL2, 13)
        self.f_big = QFont("Segoe UI Light", 22)
        self.resize(WIDTH, TOP_H)
        self.glass = make_glass(self, part="spotlight")
        self.glass.region = self.glass_region

    # -- show / hide
    def toggle(self):
        if self.isVisible():
            self.close_animated()
        else:
            self.open()

    def open(self):
        scr = QApplication.primaryScreen().geometry()
        self.was_active = False
        self.opened_at = time.monotonic()
        self.move(int(scr.center().x() - WIDTH / 2), int(scr.top() + scr.height() * 0.2))
        self.edit.selectAll()
        self.relayout()
        self.show()
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(10, lambda: force_foreground(int(self.winId())))
        self.edit.setFocus()

    def close_animated(self):
        start = self.windowOpacity()
        self._fade = animate(self, 120, lambda v: self.setWindowOpacity(start * (1 - v)), self.hide)

    def glass_region(self):
        # фон снимаем сразу под максимальную высоту — при появлении результатов его не нужно переснимать
        return QRectF(0, 0, WIDTH, TOP_H + MAX_ROWS * ROW_H + 5 * HEAD_H + 10)

    def showEvent(self, e):
        prepare_glass(self.glass)
        fade_in(self, slide=-10)

    def changeEvent(self, e):
        if e.type() != e.Type.ActivationChange or not self.isVisible():
            return
        if self.isActiveWindow():
            self.was_active = True
        elif self.was_active and time.monotonic() - self.opened_at > 0.4:
            QTimer.singleShot(120, lambda: self.isVisible() and not self.isActiveWindow() and self.close_animated())

    # -- search
    def system_items(self):
        app = self.app
        lock = lambda: user32.LockWorkStation()  # noqa: E731
        return [
            ("Launchpad", "launchpad пуск start", "", lambda: app.launchpad.open()),
            ("Настройки MiniDock", "minidock dock док настройки", "", app.open_settings),
            ("Параметры Windows", "settings параметры настройки", "", lambda: os.startfile("ms-settings:")),
            ("Wi‑Fi и сеть", "wifi сеть интернет network", "", lambda: os.startfile("ms-settings:network-wifi")),
            ("Bluetooth", "bluetooth блютуз", "", lambda: os.startfile("ms-settings:bluetooth")),
            ("Звук", "sound звук громкость audio", "", lambda: os.startfile("ms-settings:sound")),
            ("Дисплей", "display экран монитор разрешение", "", lambda: os.startfile("ms-settings:display")),
            ("Персонализация и обои", "обои wallpaper персонализация тема", "",
             lambda: os.startfile("ms-settings:personalization")),
            ("Центр обновления Windows", "update обновление", "",
             lambda: os.startfile("ms-settings:windowsupdate")),
            ("Приложения и возможности", "удалить программы apps uninstall", "",
             lambda: os.startfile("ms-settings:appsfeatures")),
            ("Диспетчер задач", "task manager диспетчер процессы", "", lambda: subprocess.Popen(["taskmgr.exe"])),
            ("Панель управления", "control panel панель управления", "", lambda: subprocess.Popen(["control"])),
            ("Корзина", "trash корзина recycle", "", lambda: os.startfile("shell:RecycleBinFolder")),
            ("Загрузки", "downloads загрузки", "", lambda: os.startfile("shell:Downloads")),
            ("Документы", "documents документы", "", lambda: os.startfile("shell:Personal")),
            ("Рабочий стол", "desktop рабочий стол", "", lambda: os.startfile("shell:Desktop")),
            ("Заблокировать экран", "lock блокировка заблокировать", "", lock),
            ("Сон", "sleep сон спящий", "", lambda: ctypes.windll.powrprof.SetSuspendState(0, 0, 0)),
            ("Перезагрузить", "restart reboot перезагрузка", "",
             lambda: self.confirm("Перезагрузить компьютер?", ["shutdown", "/r", "/t", "0"])),
            ("Выключить", "shutdown выключение выключить", "",
             lambda: self.confirm("Выключить компьютер?", ["shutdown", "/s", "/t", "0"])),
        ]

    def confirm(self, text, cmd):
        if QMessageBox.question(None, "MiniDock", text) == QMessageBox.Yes:
            subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)

    def on_text(self, text):
        self.seq += 1
        self.files = []
        self.rebuild()
        if len(text.strip()) >= 2:
            self.debounce.start()

    def query_files(self):
        self.files_search.query(self.seq, self.edit.text().strip())

    def on_files(self, seq, files):
        if seq != self.seq:
            return  # ответ на старый запрос
        q = self.edit.text().strip().casefold()
        files.sort(key=lambda f: (-score(os.path.splitext(f["name"])[0], q), not os.path.isdir(f["path"])))
        self.files = files[:8]
        self.rebuild(keep_selection=True)

    def rebuild(self, keep_selection=False):
        text = self.edit.text().strip()
        q = text.casefold()
        res = []
        if q:
            apps = []
            for e in self.app.launchpad.catalog.entries:
                s = score(e["name"], q)
                if s:
                    apps.append((s, e))
            apps.sort(key=lambda x: (-x[0], x[1]["name"].casefold()))
            system = [(score(n, q) or (40 if q in kw else 0), n, g, a) for n, kw, g, a in self.system_items()]
            system = [x for x in system if x[0]]
            system.sort(key=lambda x: -x[0])
            value = calc(text)

            best = None
            if value is not None:
                best = dict(section="Калькулятор", title=f"= {value}", sub=f"{text}   ·   Enter — скопировать",
                            icon_kind="glyph", icon="", action=lambda v=value: self.copy(v), calc=value)
            elif apps and (not system or apps[0][0] >= system[0][0]):
                best = self.app_row(apps.pop(0)[1], "Лучшее совпадение")
            elif system:
                s, n, g, a = system.pop(0)
                best = dict(section="Лучшее совпадение", title=n, sub="Система", icon_kind="glyph", icon=g, action=a)
            if best:
                res.append(best)
            res += [self.app_row(e, "Приложения") for _, e in apps[:6]]
            res += [dict(section="Система", title=n, sub="Система", icon_kind="glyph", icon=g, action=a)
                    for _, n, g, a in system[:4]]
            for f in self.files:
                folder = os.path.dirname(f["path"])
                res.append(dict(section="Документы и папки", title=f["name"], sub=folder, icon_kind="file",
                                icon=f["path"], action=lambda p=f["path"]: launch(p),
                                alt=lambda p=f["path"]: subprocess.Popen(["explorer.exe", "/select,", p]),
                                file=f))
            res.append(dict(section="Интернет", title=f"Искать «{text}» в интернете", sub="Google",
                            icon_kind="glyph", icon="",
                            action=lambda t=text: os.startfile("https://www.google.com/search?q="
                                                              + urllib.parse.quote(t))))
        self.results = res
        if not keep_selection:
            self.picked = set()
        self.picked = {i for i in self.picked if i < len(res)}
        if not keep_selection or self.selected >= len(res):
            self.selected = 0
            self.scroll = 0
        self.relayout()
        self.update()

    def completion(self):
        """(дописка, полное название) для выбранного результата — серая подсказка после курсора, Tab её принимает.
        Если название начинается с введённого — дописываем окончание, иначе показываем «— Название»."""
        text = self.edit.text()
        if not text.strip() or not self.results or self.edit.hasSelectedText() \
                or self.edit.cursorPosition() != len(text):
            return None
        r = self.results[self.selected]
        if r.get("calc") is not None or r["section"] == "Интернет":
            return None
        title = r["title"]
        if title.casefold() == text.casefold():
            return None
        if title.casefold().startswith(text.casefold()):
            return title[len(text):], title
        return f" — {title}", title

    def accept_completion(self):
        c = self.completion()
        if c is None:
            return False
        keep = self.results[self.selected]
        self.edit.setText(c[1])  # пересоберёт результаты — выбранным оставляем тот же
        for i, r in enumerate(self.results):
            if r["title"] == keep["title"] and r["section"] in (keep["section"], "Лучшее совпадение"):
                self.selected = i
                break
        self.update()
        return True

    def app_row(self, e, section):
        return dict(section=section, title=e["name"], sub="Приложение", icon_kind="app", icon=e,
                    action=lambda e=e: self.launch_app(e))

    def launch_app(self, e):
        from .launchpad import Launchpad
        Launchpad.run_entry(e)

    def copy(self, value):
        QApplication.clipboard().setText(value)

    # -- layout
    def rows_layout(self):
        """[(y, kind, index)] — заголовки разделов и строки в пределах прокрутки."""
        out, y, last = [], float(TOP_H), None
        for i, r in enumerate(self.results[self.scroll:self.scroll + MAX_ROWS], start=self.scroll):
            if r["section"] != last:
                out.append((y, "head", i))
                y += HEAD_H
                last = r["section"]
            out.append((y, "row", i))
            y += ROW_H
        return out, y

    def relayout(self):
        rows, bottom = self.rows_layout()
        h = int(bottom + 10) if self.results else TOP_H
        if h != self.height():
            self.resize(WIDTH, h)
        self.edit.setGeometry(54, 12, WIDTH - 70, 38)

    # -- painting
    def icon_pix(self, r, size):
        key = (r["icon_kind"], id(r["icon"]) if r["icon_kind"] == "app" else r["icon"], size)
        pix = self.icons.get(key)
        if pix is None:
            if r["icon_kind"] == "app":
                e = r["icon"]
                pix = QPixmap.fromImage(e["img"]).scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation) \
                    if not e["img"].isNull() else QPixmap()
            else:
                pix = self.provider.icon(QFileInfo(r["icon"])).pixmap(QSize(size, size))
            if len(self.icons) > 300:
                self.icons.clear()
            self.icons[key] = pix
        return pix

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        path = rounded_path(self.rect(), 14)
        self.glass.fill(p, path)
        p.fillPath(path, QColor(32, 32, 38, 175))
        paint_background(p, path, "spotlight")
        p.setPen(QPen(QColor(255, 255, 255, 45), 1))
        p.drawPath(path)
        p.setFont(QFont(MDL2, 15))
        p.setPen(QColor(255, 255, 255, 170))
        p.drawText(QRectF(14, 12, 36, 38), Qt.AlignCenter, "")
        comp = self.completion()
        if comp is not None:  # серая подсказка автозаполнения прямо после курсора (Tab — принять)
            x = self.edit.x() + self.edit.cursorRect().center().x() + 1
            room = self.edit.x() + self.edit.width() - x
            p.setFont(self.edit.font())
            p.setPen(QColor(255, 255, 255, 95))
            p.drawText(QRectF(x, self.edit.y(), room, self.edit.height()), Qt.AlignVCenter | Qt.AlignLeft,
                       QFontMetricsF(self.edit.font()).elidedText(comp[0], Qt.ElideRight, room))
        if not self.results:
            return
        p.setPen(QColor(255, 255, 255, 30))
        p.drawLine(0, TOP_H - 4, self.width(), TOP_H - 4)
        rows, _ = self.rows_layout()
        fm_t, fm_s = QFontMetricsF(self.f_title), QFontMetricsF(self.f_sub)
        dpr = self.devicePixelRatioF()
        for y, kind, i in rows:
            r = self.results[i]
            if kind == "head":
                p.setFont(self.f_head)
                p.setPen(QColor(255, 255, 255, 120))
                p.drawText(QRectF(18, y, WIDTH - 36, HEAD_H), Qt.AlignLeft | Qt.AlignBottom, r["section"])
                continue
            box = QRectF(8, y + 2, WIDTH - 16, ROW_H - 4)
            if i in self.picked and i != self.selected:
                hp = QPainterPath()
                hp.addRoundedRect(box, 8, 8)
                p.fillPath(hp, QColor(10, 132, 255, 120))
            if i == self.selected:
                hp = QPainterPath()
                hp.addRoundedRect(box, 8, 8)
                p.fillPath(hp, QColor(10, 132, 255, 215))
            ir = QRectF(20, y + 6, 28, 28)
            if r["icon_kind"] == "glyph":
                p.setFont(self.f_glyph)
                p.setPen(QColor(245, 245, 248))
                p.drawText(ir, Qt.AlignCenter, r["icon"])
            else:
                pix = self.icon_pix(r, int(28 * dpr))
                if not pix.isNull():
                    p.drawPixmap(ir, pix, QRectF(pix.rect()))
            p.setFont(self.f_title)
            p.setPen(QColor(250, 250, 252))
            title_w = WIDTH - 90 - (200 if r["sub"] else 0)
            p.drawText(QRectF(60, y, title_w, ROW_H), Qt.AlignVCenter | Qt.AlignLeft,
                       fm_t.elidedText(r["title"], Qt.ElideRight, title_w))
            if r["sub"]:
                p.setFont(self.f_sub)
                p.setPen(QColor(255, 255, 255, 200 if i == self.selected else 120))
                p.drawText(QRectF(WIDTH - 230, y, 205, ROW_H), Qt.AlignVCenter | Qt.AlignRight,
                           fm_s.elidedText(r["sub"], Qt.ElideMiddle, 205))

    # -- input
    def row_at(self, pos):
        rows, _ = self.rows_layout()
        for y, kind, i in rows:
            if kind == "row" and y <= pos.y() < y + ROW_H:
                return i
        return None

    def mousePressEvent(self, e):
        i = self.row_at(e.position())
        self.press_row = i if e.button() == Qt.LeftButton else None
        if i is not None and e.button() == Qt.LeftButton and e.modifiers() & Qt.ControlModifier:
            self.picked ^= {i}  # Ctrl+клик — добавить/убрать строку
            if self.picked:
                self.picked.add(self.selected)
            self.update()

    def mouseMoveEvent(self, e):
        i = self.row_at(e.position())
        if self.press_row is not None and (e.buttons() & Qt.LeftButton) and i is not None \
                and not e.modifiers() & Qt.ControlModifier:
            # протягивание с зажатой кнопкой — выделяем все строки между началом и курсором
            lo, hi = sorted((self.press_row, i))
            self.picked = set(range(lo, hi + 1)) if hi > lo else set()
        if i is not None and i != self.selected:
            self.selected = i
        self.update()

    def mouseReleaseEvent(self, e):
        i = self.row_at(e.position())
        start, self.press_row = self.press_row, None
        if e.modifiers() & Qt.ControlModifier or (len(self.picked) > 1 and e.button() == Qt.LeftButton
                                                  and start is not None and start != i):
            return  # выделение готово — открыть всё разом: Enter или клик по выделенному
        if i is not None:
            self.selected = i
            if len(self.picked) > 1 and i in self.picked and e.button() == Qt.LeftButton:
                self.activate_many()
                return
            self.picked = set()
            self.activate(alt=e.button() == Qt.RightButton)

    def wheelEvent(self, e):
        d = -1 if e.angleDelta().y() > 0 else 1
        self.scroll = max(0, min(self.scroll + d, max(0, len(self.results) - MAX_ROWS)))
        self.relayout()
        self.update()

    def move_selection(self, d):
        if not self.results:
            return
        self.selected = (self.selected + d) % len(self.results)
        if self.selected < self.scroll:
            self.scroll = self.selected
        elif self.selected >= self.scroll + MAX_ROWS:
            self.scroll = self.selected - MAX_ROWS + 1
        self.relayout()
        self.update()

    def activate_many(self):
        """Открыть все выделенные строки (калькулятор и поиск в интернете — пропускаем)."""
        rows = [self.results[i] for i in sorted(self.picked)
                if self.results[i].get("calc") is None and self.results[i]["section"] != "Интернет"]
        self.picked = set()
        self.close_animated()
        for k, r in enumerate(rows):
            QTimer.singleShot(60 + 120 * k, r["action"])

    def activate(self, alt=False):
        if not self.results:
            return
        r = self.results[self.selected]
        action = r.get("alt") if alt and r.get("alt") else r["action"]
        if r.get("calc") is not None:
            action()
            self.close_animated()
            return
        self.close_animated()
        QTimer.singleShot(60, action)

    def eventFilter(self, obj, e):
        if obj is self.edit and e.type() == e.Type.KeyPress:
            key = e.key()
            if key == Qt.Key_Escape:
                if self.picked:
                    self.picked = set()
                    self.update()
                elif self.edit.text():
                    self.edit.clear()
                else:
                    self.close_animated()
                return True
            if key in (Qt.Key_Down, Qt.Key_Up):
                self.move_selection(1 if key == Qt.Key_Down else -1)
                return True
            if key == Qt.Key_Tab:
                self.accept_completion()
                return True  # Tab не уводит фокус из поля
            if key == Qt.Key_Right and self.edit.cursorPosition() == len(self.edit.text()) \
                    and self.accept_completion():
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if len(self.picked) > 1:
                    self.activate_many()
                else:
                    self.activate(alt=bool(e.modifiers() & (Qt.ControlModifier | Qt.AltModifier)))
                return True
        return False

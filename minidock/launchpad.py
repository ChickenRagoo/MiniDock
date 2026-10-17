"""Launchpad — полноэкранное меню приложений (замена «Пуска»).

Два режима: «Все» — все приложения по алфавиту со страницами; «Закреплённые» — плитки из «Пуска»
Windows с теми же группами и расположением.

Папки (в режиме «Все», как на Mac): перетащить иконку на другую — получится папка; папка открывается
панелью поверх сетки, иконка, вытащенная за край панели, выходит из папки.
"""
import hashlib
import math
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import win32api
import win32con
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetricsF, QImage, QKeyEvent, QPainter, QPainterPath, QPalette,
                           QPixmap)
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

from .config import CFG_DIR, ICON_CACHE
from . import uninstall
from .folders import folder_image
from .ui import MacMenu, animate, clamp, ease_out, lerp, paint_background, per_frame, popup_menu, precise_timer, smooth
from .winapi import enable_blur, enum_start_apps, force_foreground, resolve_lnk, shell_icon_image

JUNK = re.compile(r"(uninstall|деинстал|удален|удалить|readme|read me|release notes|help|справк|documentation|"
                  r"документац|website|web site|веб-сайт|сайт|license|лиценз|support|поддержк|manual|руководств|"
                  r"^about |check for updates|configur|tap virtual|tap-windows|\.url$)", re.I)

KNOWN_FOLDERS = {
    "{6D809377-6AF0-444B-8957-A3773F02200E}": os.environ.get("ProgramW6432", r"C:\Program Files"),
    "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}": os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    "{F38BF404-1D43-42F2-9305-67DE0B28FC23}": os.environ.get("SystemRoot", r"C:\Windows"),
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}": os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"),
}


ICON_PX = 128  # больше в Launchpad не бывает (настройка до 128 px) — в памяти держим одну копию такого размера


def cached_icon(key, make):
    """QImage иконки (ICON_PX) из дискового кэша или созданная make() и сохранённая."""
    path = ICON_CACHE / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".png")
    img = QImage(str(path)) if path.exists() else QImage()
    if img.isNull():
        img = make() or QImage()
        if not img.isNull():
            img.save(str(path))
    if not img.isNull() and max(img.width(), img.height()) > ICON_PX:
        img = img.scaled(ICON_PX, ICON_PX, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return img


def entry(name, parse, img, **extra):
    return dict(name=name, parse=parse, img=img, cache={}, **extra)


def load_start_layout():
    """Группы плиток «Пуска»: [[{lnk|aumid, col, row, w, h}]] или None, если раскладку не получить."""
    out_file = CFG_DIR / "startlayout.xml"
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        f"Export-StartLayout -Path '{out_file}'"], capture_output=True, timeout=25,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        root = ET.parse(out_file).getroot()
    except Exception:
        return None
    groups = []
    for g in root.iter():
        if not g.tag.endswith("}Group"):
            continue
        tiles = []
        for t in g:
            tag = t.tag.split("}")[-1]
            if tag not in ("Tile", "DesktopApplicationTile"):
                continue
            w, h = (int(x) for x in (t.get("Size") or "1x1").split("x"))
            tile = dict(col=int(t.get("Column", 0)), row=int(t.get("Row", 0)), w=w, h=h)
            if t.get("DesktopApplicationLinkPath"):
                tile["lnk"] = os.path.expandvars(t.get("DesktopApplicationLinkPath"))
            elif t.get("AppUserModelID") or t.get("DesktopApplicationID"):
                tile["aumid"] = t.get("AppUserModelID") or t.get("DesktopApplicationID")
            else:
                continue
            tiles.append(tile)
        if tiles:
            min_col = min(t["col"] for t in tiles)
            for t in tiles:
                t["col"] -= min_col
            groups.append(tiles)
    return groups


class AppCatalog(QObject):
    """Все приложения меню «Пуск» + плитки «Пуска». Грузится в фоне, иконки кэшируются на диск."""
    loaded = Signal(list, list)

    def __init__(self):
        super().__init__()
        self.entries = []
        self.groups = []
        self.loading = False

    def load(self):
        if self.loading:
            return
        self.loading = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            from win32com.shell import shell, shellcon
            ICON_CACHE.mkdir(parents=True, exist_ok=True)
            seen, out, by_parse = set(), [], {}
            for name, parse, idx in enum_start_apps():
                key = name.casefold()
                if key in seen or JUNK.search(name):
                    continue
                seen.add(key)
                e = entry(name, parse, cached_icon(parse, lambda idx=idx: shell_icon_image(idx)))
                out.append(e)
                by_parse[parse.casefold()] = e
            out.sort(key=lambda e: e["name"].casefold())

            groups = []
            desktop = shell.SHGetDesktopFolder()
            for tiles in load_start_layout() or []:
                group = []
                for t in tiles:
                    if "lnk" in t:
                        path = t["lnk"]
                        if not os.path.exists(path):
                            continue
                        name = re.sub(r"\s*[—-]\s*(ярлык|shortcut)$", "", Path(path).stem, flags=re.I)
                        name = re.sub(r"\.exe$", "", name, flags=re.I)

                        def make(path=path):
                            _, info = shell.SHGetFileInfo(path, 0, shellcon.SHGFI_SYSICONINDEX)
                            return shell_icon_image(info[1])
                        e = entry(name, path, cached_icon(path, make), **t)
                    else:
                        aumid = t["aumid"]
                        known = by_parse.get(aumid.casefold())
                        if known is not None:
                            e = entry(known["name"], aumid, known["img"], **t)
                        else:
                            def make(aumid=aumid):
                                try:
                                    _, pidl, _ = desktop.ParseDisplayName(0, None, "shell:AppsFolder\\" + aumid)
                                    _, info = shell.SHGetFileInfo(pidl, 0, shellcon.SHGFI_PIDL
                                                                  | shellcon.SHGFI_SYSICONINDEX)
                                    return shell_icon_image(info[1])
                                except Exception:
                                    return None
                            e = entry(aumid.split("!")[-1], aumid, cached_icon(aumid, make), **t)
                    group.append(e)
                if group:
                    groups.append(group)
            self.entries, self.groups = out, groups
            self.loaded.emit(out, groups)
        finally:
            self.loading = False


SEARCH_QSS = """
QLineEdit { background: rgba(255, 255, 255, 38); border: 1px solid rgba(255, 255, 255, 45); border-radius: 15px;
            padding: 4px 14px; color: #f4f4f7; font: 11pt 'Segoe UI'; selection-background-color: #0a84ff; }
"""

MODES = [("all", "Все"), ("start", "Закреплённые")]

TITLE_QSS = """
QLineEdit { background: transparent; border: none; border-radius: 8px; padding: 2px 10px; color: #f4f4f7;
            font: 600 16pt 'Segoe UI'; selection-background-color: #0a84ff; }
QLineEdit:focus { background: rgba(255, 255, 255, 30); }
"""


class Launchpad(QWidget):
    uninstalled = Signal()  # деинсталлятор завершился (из фонового потока)

    def __init__(self, app):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.app = app
        self.cfg = app.cfg
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.catalog = AppCatalog()
        self.catalog.loaded.connect(self.on_loaded)
        self.entries = []
        self.groups = []
        self.filtered = []
        self.top = []               # «Все» без поиска: папки, затем приложения не из папок
        self.folder = None          # открытая папка (запись kind="folder")
        self.folder_k = 0.0         # анимация открытия папки (0 → 1)
        self.drag = None            # перетаскиваемая иконка: (индекс, запись)
        self.drop_on = None         # индекс иконки, на которую её бросят (будет папка)
        self.picked = []            # выделенные приложения (рамкой с зажатой кнопкой мыши или Ctrl+клик)
        self.band = None            # рамка выделения: (начало, текущая точка)
        self.uninstalled.connect(self.after_uninstall)
        self.capture_keys = False  # открыт, но без фокуса — хук клавиатуры отдаёт нажатия сюда
        self.page = 0
        self.page_pos = 0.0
        self.selected = 0
        self.kb_nav = False
        self.hover = None
        self.closing = False
        self.was_active = False
        self.opened_at = 0.0
        self.press = None
        self.mode_k = 1.0           # анимация смены режима (0 → 1)
        self.mode_hl = 0.0 if self.cfg["lp_mode"] == "all" else 1.0
        self.start_cache = None     # (размер окна, раскладка) для режима «Закреплённые»
        self.font_label = QFont("Segoe UI", 9)
        self.font_seg = QFont("Segoe UI", 9, QFont.DemiBold)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Поиск")
        self.search.setAlignment(Qt.AlignCenter)
        self.search.setStyleSheet(SEARCH_QSS)
        pal = self.search.palette()
        pal.setColor(QPalette.PlaceholderText, QColor(255, 255, 255, 150))
        self.search.setPalette(pal)
        self.search.textChanged.connect(self.on_search)
        self.search.installEventFilter(self)
        self.folder_title = QLineEdit(self)
        self.folder_title.setAlignment(Qt.AlignCenter)
        self.folder_title.setStyleSheet(TITLE_QSS)
        self.folder_title.hide()
        self.folder_title.editingFinished.connect(self.rename_folder)
        self.folder_title.installEventFilter(self)
        self.anim = precise_timer(self, 14, self.tick)
        QTimer.singleShot(2500, self.catalog.load)

    def on_loaded(self, entries, groups):
        self.entries, self.groups = entries, groups
        uninstall.preload()
        self.start_cache = None
        self.build_folders()

    # -- папки
    def build_folders(self):
        """Собирает записи папок из настроек; приложения из папок убирает из общей сетки."""
        hidden = {k.casefold() for k in self.cfg.get("lp_hidden") or []}
        self.visible = [e for e in self.entries if e["parse"].casefold() not in hidden]
        by_key = {e["parse"].casefold(): e for e in self.visible}
        used, folders = set(), []
        for f in self.cfg.get("lp_folders") or []:
            kids = [by_key[k.casefold()] for k in f.get("items", []) if k.casefold() in by_key]
            if not kids:
                continue
            used.update(id(k) for k in kids)
            folders.append(dict(name=f.get("name") or "Папка", parse="", kind="folder", children=kids, cache={},
                                img=folder_image([k["img"] for k in kids])))
        self.top = folders + [e for e in self.visible if id(e) not in used]
        self.picked = [e for e in self.picked if any(e is v for v in self.visible)]
        if self.folder is not None:  # открытая папка — её обновлённая запись (или закрыть, если опустела)
            self.folder = next((f for f in folders if f["name"] == self.folder["name"]), None)
            if self.folder is None:
                self.close_folder()
        self.on_search(self.search.text(), keep_page=True)

    def save_folders(self, folders):
        self.cfg["lp_folders"] = [f for f in folders if f["items"]]  # пустые папки исчезают, как на Mac
        self.app.save_soon()
        self.build_folders()

    def folder_cfg(self):
        return [dict(name=f["name"], items=[k["parse"] for k in f["children"]]) for f in self.top
                if f.get("kind") == "folder"]

    def unique_name(self, name, folders):
        names, n, out = {f["name"] for f in folders}, 2, name
        while out in names:
            out, n = f"{name} {n}", n + 1
        return out

    def put_in_folder(self, src, dst):
        """Положить приложение src в папку dst или сделать новую папку из двух приложений."""
        if src.get("kind") == "folder" or src is dst:
            return
        folders = self.folder_cfg()
        for f in folders:
            f["items"] = [k for k in f["items"] if k.casefold() != src["parse"].casefold()]
        if dst.get("kind") == "folder":
            f = next(f for f in folders if f["name"] == dst["name"])
            f["items"].append(src["parse"])
            self.save_folders(folders)
        else:
            name = self.unique_name("Папка", folders)
            folders.insert(0, dict(name=name, items=[dst["parse"], src["parse"]]))
            self.save_folders(folders)
            new = next((e for e in self.top if e.get("kind") == "folder" and e["name"] == name), None)
            if new is not None:
                self.open_folder(new, edit_title=True)

    def take_out(self, entry):
        folders = self.folder_cfg()
        for f in folders:
            f["items"] = [k for k in f["items"] if k.casefold() != entry["parse"].casefold()]
        self.save_folders(folders)

    def ungroup(self, folder):
        self.save_folders([f for f in self.folder_cfg() if f["name"] != folder["name"]])

    def rename_folder(self):
        if self.folder is None:
            return
        name = self.folder_title.text().strip() or "Папка"
        folders = self.folder_cfg()
        if name != self.folder["name"]:
            name = self.unique_name(name, [f for f in folders if f["name"] != self.folder["name"]])
            for f in folders:
                if f["name"] == self.folder["name"]:
                    f["name"] = name
            self.folder["name"] = name
            self.save_folders(folders)
        self.folder_title.setText(name)

    def open_folder(self, folder, edit_title=False):
        self.folder = folder
        self.folder_k = 0.0
        self.hover = None
        self.selected = 0
        self.folder_title.setText(folder["name"])
        r = self.folder_panel()
        self.folder_title.setGeometry(int(r.center().x() - 180), int(r.top() + 18), 360, 40)
        self.folder_title.show()
        if edit_title:
            self.folder_title.setFocus()
            self.folder_title.selectAll()
        self.anim.start()

    def close_folder(self):
        if self.folder_title.hasFocus():
            self.rename_folder()
        self.folder = None
        self.folder_title.hide()
        self.hover = None
        self.search.setFocus()
        self.update()

    def folder_cols(self):
        n = len(self.folder["children"])
        return int(clamp(math.ceil(math.sqrt(n * 1.6)), 3, int(self.cfg["lp_columns"])))

    def folder_panel(self):
        _, _, S, cw, ch, _, _ = self.grid()
        cols = self.folder_cols()
        rows = math.ceil(len(self.folder["children"]) / cols)
        w, h = cols * cw + 80, rows * ch + 110
        h = min(h, self.height() - 120)
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2 + 20, w, h)

    def folder_items(self):
        _, _, S, cw, ch, _, _ = self.grid()
        panel = self.folder_panel()
        cols = self.folder_cols()
        out = []
        for i, e in enumerate(self.folder["children"]):
            r, c = divmod(i, cols)
            x = panel.left() + 40 + c * cw + (cw - S) / 2
            y = panel.top() + 80 + r * ch
            out.append((i, e, QRectF(x, y, S, S)))
        return out

    # -- mode
    @property
    def mode(self):
        if self.search.text().strip():
            return "all"  # поиск всегда идёт по всем приложениям
        return self.cfg["lp_mode"] if self.cfg["lp_mode"] != "start" or self.groups else "all"

    def set_mode(self, mode):
        if mode == self.cfg["lp_mode"]:
            return
        self.app.set_cfg("lp_mode", mode)
        self.mode_k = 0.0
        self.selected = 0
        self.page = 0
        self.page_pos = 0.0
        self.anim.start()

    def seg_rects(self):
        w = 120
        x0 = self.width() / 2 - w
        return [QRectF(x0 + i * w, 108, w, 26) for i in range(len(MODES))]

    # -- show / hide
    def toggle(self):
        if self.isVisible() and not self.closing:
            self.close_animated()
        else:
            self.open()

    def open(self):
        if not self.entries:
            self.catalog.load()
        self.closing = False
        self.was_active = False
        self.opened_at = time.monotonic()
        self.setGeometry(QApplication.primaryScreen().geometry())
        self.search.clear()
        if self.folder is not None:
            self.close_folder()
        self.page = 0
        self.page_pos = 0.0
        self.selected = 0
        self.kb_nav = False
        self.layout_search()
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        enable_blur(int(self.winId()))
        self.capture_keys = True
        for d in self.app.docks():  # док уезжает, пока открыт Launchpad
            d.wake()
        for ms in (10, 60, 150, 300, 600, 1000):  # Windows может не отдать фокус с первого раза
            QTimer.singleShot(ms, self.ensure_focus)
        self.search.setFocus()
        self._fade = animate(self, 200, self.setWindowOpacity)

    def close_animated(self, then=None):
        if self.closing:
            if then:  # уже закрывается (например, только что потерял фокус) — запуск программы не теряем
                QTimer.singleShot(160, then)
            return
        self.closing = True
        self.capture_keys = False
        start = self.windowOpacity()

        def done():
            self.hide()
            self.closing = False
            for d in self.app.docks():  # Launchpad закрыт — док снова доступен
                d.wake()
            if then:
                then()
        self._fade = animate(self, 140, lambda v: self.setWindowOpacity(start * (1 - v)), done)

    def layout_search(self):
        w = 280
        self.search.setGeometry(int(self.width() / 2 - w / 2), 60, w, 32)

    def resizeEvent(self, e):
        self.layout_search()
        self.start_cache = None

    # -- data
    def on_search(self, text, keep_page=False):
        q = text.strip().casefold()
        # поиск — по всем приложениям, включая лежащие в папках
        visible = getattr(self, "visible", self.entries)
        self.filtered = [e for e in visible if q in e["name"].casefold()] if q else list(self.top)
        if q and self.folder is not None:
            self.close_folder()
        if not keep_page:
            self.page = 0
            self.page_pos = 0.0
            self.selected = 0
        self.page = min(self.page, self.pages() - 1)
        self.update()

    def per_page(self):
        return int(self.cfg["lp_columns"]) * int(self.cfg["lp_rows"])

    def pages(self):
        if self.mode == "start":
            return 1
        return max(1, math.ceil(len(self.filtered) / self.per_page()))

    def grid(self):
        cols, rows, S = int(self.cfg["lp_columns"]), int(self.cfg["lp_rows"]), float(self.cfg["lp_icon"])
        cw = max(S * 1.9, (self.width() - 240) / cols if cols else S)
        cw = min(cw, S * 2.6)
        ch = S + 52
        gw, gh = cols * cw, rows * ch
        x0 = (self.width() - gw) / 2
        y0 = max(160.0, (self.height() - gh) / 2 + 20)
        return cols, rows, S, cw, ch, x0, y0

    def item_rect(self, i, page_offset=0.0):
        cols, rows, S, cw, ch, x0, y0 = self.grid()
        page, k = divmod(i, self.per_page())
        r, c = divmod(k, cols)
        x = x0 + c * cw + (cw - S) / 2 + (page - page_offset) * self.width()
        return QRectF(x, y0 + r * ch, S, S)

    def start_layout(self):
        """Плитки «Пуска»: группы идут слева направо с переносом, внутри группы — как в «Пуске»."""
        key = (self.width(), self.height(), self.cfg["lp_icon"], len(self.groups))
        if self.start_cache and self.start_cache[0] == key:
            return self.start_cache[1]
        S = float(self.cfg["lp_icon"]) * 0.9
        cw, ch = S + 46, S + 44
        gap_x, gap_y = 64.0, 40.0
        blocks = []
        for g in self.groups:
            cols = max(t["col"] + t["w"] for t in g)
            rows = max(t["row"] + t["h"] for t in g)
            blocks.append((g, cols * cw, rows * ch))
        max_w = self.width() - 200
        lines, line, line_w = [], [], 0.0
        for b in blocks:
            if line and line_w + gap_x + b[1] > max_w:
                lines.append(line)
                line, line_w = [], 0.0
            line_w += (gap_x if line else 0) + b[1]
            line.append(b)
        if line:
            lines.append(line)
        total_h = sum(max(b[2] for b in ln) for ln in lines) + gap_y * (len(lines) - 1)
        y = max(170.0, (self.height() - total_h) / 2 + 30)
        items = []
        for ln in lines:
            w = sum(b[1] for b in ln) + gap_x * (len(ln) - 1)
            x = (self.width() - w) / 2
            for g, bw, bh in ln:
                for e in g:
                    tile_w, tile_h = e["w"] * cw, e["h"] * ch
                    size = S * (1.6 if e["w"] > 1 and e["h"] > 1 else 1.0)
                    cx = x + e["col"] * cw + tile_w / 2
                    top = y + e["row"] * ch + (tile_h - size - 26) / 2
                    items.append((e, QRectF(cx - size / 2, top, size, size)))
                x += bw + gap_x
            y += max(b[2] for b in ln) + gap_y
        self.start_cache = (key, items)
        return items

    def visible_items(self):
        """[(index, entry, rect)] того, с чем сейчас работает мышь/клавиатура: открытой папки или сетки."""
        if self.folder is not None:
            return self.folder_items()
        return self.grid_items()

    def grid_items(self):
        """[(index, entry, rect)] для текущего режима и страницы."""
        if self.mode == "start":
            return [(i, e, r) for i, (e, r) in enumerate(self.start_layout())]
        lo = max(0, int(math.floor(self.page_pos)) * self.per_page())
        hi = min(len(self.filtered), (int(math.ceil(self.page_pos)) + 1) * self.per_page())
        out = []
        for i in range(lo, hi):
            r = self.item_rect(i, self.page_pos)
            if r.right() >= 0 and r.left() <= self.width():
                out.append((i, self.filtered[i], r))
        return out

    def current_list(self):
        if self.folder is not None:
            return self.folder["children"]
        return [e for e, _ in self.start_layout()] if self.mode == "start" else self.filtered

    def index_at(self, pos):
        for i, e, r in self.visible_items():
            if r.adjusted(-14, -10, 14, 30).contains(pos):
                return i
        return None

    def pix(self, e, size):
        s = int(size)
        p = e["cache"].get(s)
        if p is None:
            if e["img"].isNull():
                return None
            if len(e["cache"]) > 3:  # размеры меняются только при наведении — держим пару последних
                e["cache"].clear()
            p = QPixmap.fromImage(e["img"].scaled(s, s, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            e["cache"][s] = p
        return p

    # -- animation
    def go_page(self, page):
        self.page = int(clamp(page, 0, self.pages() - 1))
        self.anim.start()

    def tick(self):
        busy = False
        if abs(self.page_pos - self.page) < 0.002:
            self.page_pos = float(self.page)
        else:
            self.page_pos = lerp(self.page_pos, self.page, smooth(0.2, self.anim.dt))
            busy = True
        if self.mode_k < 1:
            self.mode_k = min(1.0, self.mode_k + per_frame(0.08, self.anim.dt))
            busy = True
        target = 0.0 if self.cfg["lp_mode"] == "all" else 1.0
        if self.folder is not None and self.folder_k < 1:
            self.folder_k = min(1.0, self.folder_k + per_frame(0.09, self.anim.dt))
            busy = True
        if abs(self.mode_hl - target) > 0.005:
            self.mode_hl = lerp(self.mode_hl, target, smooth(0.3, self.anim.dt))
            busy = True
        else:
            self.mode_hl = target
        self.update()
        if not busy:
            self.anim.stop()

    # -- painting
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(12, 12, 16, 130))
        whole = QPainterPath()
        whole.addRect(QRectF(self.rect()))
        paint_background(p, whole, "launchpad")
        dpr = self.devicePixelRatioF()

        # переключатель режимов
        segs = self.seg_rects()
        if not self.search.text().strip():
            bg = QPainterPath()
            bg.addRoundedRect(segs[0].united(segs[-1]), 13, 13)
            p.fillPath(bg, QColor(255, 255, 255, 30))
            hl = segs[0].translated(self.mode_hl * segs[0].width(), 0).adjusted(2, 2, -2, -2)
            hp = QPainterPath()
            hp.addRoundedRect(hl, 11, 11)
            p.fillPath(hp, QColor(255, 255, 255, 70))
            p.setFont(self.font_seg)
            p.setPen(QColor(245, 245, 248))
            for r, (_, label) in zip(segs, MODES):
                p.drawText(r, Qt.AlignCenter, label)

        fm = QFontMetricsF(self.font_label)
        p.setFont(self.font_label)
        items = self.grid_items()
        if not items:
            p.setPen(QColor(255, 255, 255, 150))
            text = "Загрузка приложений…" if not self.entries else "Ничего не найдено"
            p.drawText(QRectF(0, self.height() / 2 - 20, self.width(), 40), Qt.AlignCenter, text)
        appear = 1 - (1 - self.mode_k) ** 3
        in_folder = self.folder is not None
        fk = ease_out(self.folder_k) if in_folder else 0.0
        base = appear * (1 - 0.85 * fk)  # под открытой папкой сетка притушена
        self.draw_items(p, items, fm, dpr, base, appear, active=not in_folder)
        if in_folder:
            panel = self.folder_panel()
            k = 0.92 + 0.08 * fk
            c = panel.center()
            pr = QRectF(c.x() - panel.width() * k / 2, c.y() - panel.height() * k / 2, panel.width() * k,
                        panel.height() * k)
            p.setOpacity(fk)
            pp = QPainterPath()
            pp.addRoundedRect(pr, 28, 28)
            p.fillPath(pp, QColor(58, 58, 68, 236))
            p.setPen(QColor(255, 255, 255, 40))
            p.drawPath(pp)
            p.setFont(self.font_label)
            self.draw_items(p, self.folder_items(), fm, dpr, fk, 1.0, active=True)
        if self.band is not None:  # рамка выделения
            br = QRectF(self.band[0], self.band[1]).normalized()
            p.setOpacity(1)
            p.fillRect(br, QColor(10, 132, 255, 45))
            p.setPen(QColor(10, 132, 255, 200))
            p.drawRect(br)
        if self.drag is not None:  # иконка под курсором
            pos = QPointF(self.mapFromGlobal(QCursor.pos()))
            S = float(self.cfg["lp_icon"]) * 1.05
            pm = self.pix(self.drag[1], S * dpr)
            if pm is not None:
                p.setOpacity(0.9)
                p.drawPixmap(QRectF(pos.x() - S / 2, pos.y() - S / 2, S, S), pm, QRectF(pm.rect()))
        p.setOpacity(1)
        n = self.pages()
        if n > 1:  # точки страниц
            cx, y = self.width() / 2, self.height() - 70
            for k in range(n):
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 230 if k == self.page else 90))
                p.drawEllipse(QPointF(cx + (k - (n - 1) / 2) * 18, y), 4, 4)

    def draw_items(self, p, items, fm, dpr, opacity, appear, active):
        dragged = self.drag[1] if self.drag is not None else None
        for i, e, r in items:
            if appear < 1:  # при смене режима иконки «всплывают»
                c = r.center()
                k = 0.9 + 0.1 * appear
                r = QRectF(c.x() - r.width() * k / 2, c.y() - r.height() * k / 2, r.width() * k, r.height() * k)
            p.setOpacity(opacity * (0.3 if e is dragged else 1.0))
            if active and i == self.drop_on:  # сюда бросят иконку — будет папка
                hp = QPainterPath()
                hp.addRoundedRect(r.adjusted(-10, -10, 10, 10), r.width() * 0.3, r.width() * 0.3)
                p.fillPath(hp, QColor(255, 255, 255, 60))
            elif any(e is x for x in self.picked):  # выделено рамкой / Ctrl+кликом
                hp = QPainterPath()
                hp.addRoundedRect(r.adjusted(-12, -10, 12, 30), 14, 14)
                p.fillPath(hp, QColor(10, 132, 255, 110))
            elif active and i == self.selected and (self.search.text() or self.kb_nav):
                hp = QPainterPath()
                hp.addRoundedRect(r.adjusted(-12, -10, 12, 30), 14, 14)
                p.fillPath(hp, QColor(255, 255, 255, 40))
            if e.get("removing"):
                p.setOpacity(opacity * 0.35)
            scale = 1.08 if active and i == self.hover and self.drag is None else 1.0
            ir = QRectF(0, 0, r.width() * scale, r.height() * scale)
            ir.moveCenter(r.center())
            pm = self.pix(e, ir.width() * dpr)
            if pm is not None:
                p.drawPixmap(ir, pm, QRectF(pm.rect()))
            label = fm.elidedText("Удаление…" if e.get("removing") else e["name"], Qt.ElideRight, r.width() + 40)
            lr = QRectF(r.center().x() - (r.width() + 44) / 2, r.bottom() + 8, r.width() + 44, fm.height())
            p.setPen(QColor(0, 0, 0, 120))
            p.drawText(lr.translated(0, 1), Qt.AlignCenter, label)
            p.setPen(QColor(245, 245, 248))
            p.drawText(lr, Qt.AlignCenter, label)

    # -- input
    def can_drag(self):
        return self.mode == "all" and not self.search.text().strip()

    def band_pick(self):
        br = QRectF(self.band[0], self.band[1]).normalized()
        self.picked = [en for _, en, r in self.visible_items() if br.intersects(r.adjusted(-12, -10, 12, 30))
                       and en.get("kind") != "folder"]

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self.press and self.press[1] is None and (e.buttons() & Qt.LeftButton) and self.mode == "all":
            if self.band is None and (pos - self.press[0]).manhattanLength() > 6:
                self.band = (self.press[0], pos)
            if self.band is not None:
                self.band = (self.band[0], pos)
                self.band_pick()
                self.update()
                return
        if self.press and self.press[1] is not None and (e.buttons() & Qt.LeftButton) and self.can_drag():
            if self.drag is None and (pos - self.press[0]).manhattanLength() > 8:
                self.drag = (self.press[1], self.current_list()[self.press[1]])
                self.anim.start()
            if self.drag is not None:
                self.drop_on = None
                if self.folder is None and self.drag[1].get("kind") != "folder":
                    for i, en, r in self.grid_items():
                        if en is not self.drag[1] and QRectF(r).adjusted(-r.width() * 0.15, -r.height() * 0.15,
                                                                          r.width() * 0.15, r.height() * 0.15).contains(pos):
                            self.drop_on = i
                self.update()
                return
        i = self.index_at(pos)
        if i != self.hover:
            self.hover = i
            self.kb_nav = False
            self.update()

    def mousePressEvent(self, e):
        self.press = (e.position(), self.index_at(e.position()))

    def mouseReleaseEvent(self, e):
        if not self.press:
            return
        start, idx = self.press
        self.press = None
        if self.band is not None:  # закончили выделение рамкой
            self.band = None
            self.update()
            return
        if e.button() == Qt.LeftButton and idx is not None and e.modifiers() & Qt.ControlModifier:
            en = self.current_list()[idx]
            if en.get("kind") != "folder":
                self.picked = [x for x in self.picked if x is not en] if any(x is en for x in self.picked) \
                    else self.picked + [en]
                self.update()
            return
        if e.button() == Qt.RightButton and idx is None and self.folder is None:
            self.background_menu(e.globalPosition().toPoint())
            return
        if e.button() == Qt.LeftButton and idx is None and self.picked:  # клик мимо снимает выделение
            self.picked = []
            self.update()
            return
        if self.drag is not None:
            _, src = self.drag
            target, self.drag, self.drop_on = self.drop_on, None, None
            if self.folder is not None:
                if not self.folder_panel().contains(e.position()):  # вытащили за край панели — из папки
                    self.take_out(src)
            elif target is not None:
                self.put_in_folder(src, self.current_list()[target])
            self.update()
            return
        if self.folder is not None:
            if e.button() == Qt.RightButton and idx is not None:
                self.context(idx, e.globalPosition().toPoint())
            elif idx is not None and idx == self.index_at(e.position()):
                self.launch(idx)
            elif idx is None and not self.folder_panel().contains(e.position()):
                self.close_folder()
            return
        if not self.search.text().strip():
            for r, (mode, _) in zip(self.seg_rects(), MODES):
                if r.contains(e.position()) and r.contains(start):
                    self.set_mode(mode)
                    return
        if e.button() == Qt.RightButton and idx is not None:
            self.context(idx, e.globalPosition().toPoint())
            return
        if idx is not None and idx == self.index_at(e.position()):
            self.launch(idx)
        elif idx is None:
            self.close_animated()

    def wheelEvent(self, e):
        d = e.angleDelta().y() or e.angleDelta().x()
        if d and self.mode == "all":
            self.go_page(self.page + (1 if d < 0 else -1))

    def eventFilter(self, obj, e):
        if obj is self.folder_title and e.type() == e.Type.KeyPress and e.key() in (Qt.Key_Return, Qt.Key_Enter,
                                                                                      Qt.Key_Escape):
            self.rename_folder()
            self.search.setFocus()
            return True
        if obj is self.search and e.type() == e.Type.KeyPress:
            key = e.key()
            items = self.current_list()
            if key == Qt.Key_Escape:
                if self.picked:
                    self.picked = []
                    self.update()
                elif self.folder is not None:
                    self.close_folder()
                elif self.search.text():
                    self.search.clear()
                else:
                    self.close_animated()
                return True
            if key == Qt.Key_Tab and not self.search.text() and self.folder is None:
                self.set_mode("start" if self.cfg["lp_mode"] == "all" else "all")
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter) and self.picked:
                self.launch_many(self.picked)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if items:
                    self.launch(self.selected)
                return True
            cols = self.folder_cols() if self.folder is not None else int(self.cfg["lp_columns"])
            moves = {Qt.Key_Left: -1, Qt.Key_Right: 1, Qt.Key_Up: -cols, Qt.Key_Down: cols}
            if key in moves and items:
                self.selected = int(clamp(self.selected + moves[key], 0, len(items) - 1))
                self.kb_nav = True
                if self.mode == "all" and self.folder is None:
                    self.go_page(self.selected // self.per_page())
                self.update()
                return True
            if key in (Qt.Key_PageDown, Qt.Key_PageUp):
                self.go_page(self.page + (1 if key == Qt.Key_PageDown else -1))
                return True
        return False

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            if self.folder is not None:
                self.close_folder()
            else:
                self.close_animated()

    def ensure_focus(self):
        if not self.isVisible() or self.closing:
            return
        if not self.isActiveWindow():
            force_foreground(int(self.winId()))
        if self.folder_title.hasFocus():
            return
        self.search.setFocus()
        self.capture_keys = not self.isActiveWindow()

    def feed_key(self, vk, text):
        """Нажатие из хука клавиатуры (пока у Launchpad нет фокуса) — как будто набрано в поиске."""
        if not self.isVisible() or self.closing:
            self.capture_keys = False
            return
        keys = {0x08: Qt.Key_Backspace, 0x09: Qt.Key_Tab, 0x0D: Qt.Key_Return, 0x1B: Qt.Key_Escape,
                0x21: Qt.Key_PageUp, 0x22: Qt.Key_PageDown, 0x25: Qt.Key_Left, 0x26: Qt.Key_Up, 0x27: Qt.Key_Right,
                0x28: Qt.Key_Down, 0x2E: Qt.Key_Delete}
        QApplication.sendEvent(self.search, QKeyEvent(QEvent.KeyPress, keys.get(vk, Qt.Key_unknown), Qt.NoModifier,
                                                      text))
        self.ensure_focus()

    def hideEvent(self, e):
        self.capture_keys = False

    def changeEvent(self, e):
        # клик в другое окно (или Alt+Tab) закрывает Launchpad, как на Mac —
        # но только если он уже успел получить фокус (иначе закрылся бы сразу при открытии)
        if e.type() != e.Type.ActivationChange or not self.isVisible():
            return
        self.capture_keys = not self.isActiveWindow() and not self.closing
        if self.isActiveWindow():
            self.was_active = True
        elif self.was_active and time.monotonic() - self.opened_at > 0.5:
            QTimer.singleShot(150, lambda: self.isVisible() and not self.isActiveWindow() and not self.closing
                              and QApplication.activePopupWidget() is None and self.close_animated())

    # -- actions
    @staticmethod
    def entry_exe(e):
        parse = e["parse"]
        m = re.match(r"^(\{[0-9A-Fa-f-]+\})\\(.+)$", parse)
        if m and m.group(1).upper() in KNOWN_FOLDERS:
            parse = os.path.join(KNOWN_FOLDERS[m.group(1).upper()], m.group(2))
        if parse.lower().endswith(".exe") and os.path.isfile(parse):
            return parse.lower()
        if parse.lower().endswith(".lnk") and os.path.isfile(parse):
            return resolve_lnk(parse)[0].lower() or None
        return None

    def screen_rect(self, i):
        for idx, e, r in self.visible_items():
            if idx == i:
                dpr = self.devicePixelRatioF()
                tl = self.mapToGlobal(r.topLeft().toPoint())
                return (round(tl.x() * dpr), round(tl.y() * dpr), round((tl.x() + r.width()) * dpr),
                        round((tl.y() + r.height()) * dpr))
        return None

    @staticmethod
    def run_entry(e):
        parse = e["parse"]
        try:
            if os.path.isfile(parse):
                win32api.ShellExecute(0, "open", parse, None, os.path.dirname(parse), win32con.SW_SHOWNORMAL)
                return
        except Exception:
            pass
        try:  # запасной путь — как запускает сам «Пуск»: по ID приложения
            os.startfile("shell:AppsFolder\\" + parse)
        except Exception:
            try:
                os.startfile(parse)
            except Exception:
                pass

    def launch(self, i):
        items = self.current_list()
        if not 0 <= i < len(items):
            return
        e = items[i]
        if e.get("kind") == "folder":
            self.open_folder(e)
            return
        exe = self.entry_exe(e)
        rect = self.screen_rect(i)
        if exe and rect:  # окно новой программы «вылетит» из её иконки
            self.app.animator.expect_launch(exe, rect)
        self.close_animated(lambda: self.run_entry(e))

    def context(self, i, gpos):
        e = self.current_list()[i]
        if e.get("kind") != "folder" and len(self.picked) > 1 and any(e is x for x in self.picked):
            self.context_many(list(self.picked), gpos)
            return
        m = MacMenu(parent=self)
        m.addAction("Открыть", lambda: self.launch(i))
        if e.get("kind") == "folder":
            m.addAction("Переименовать", lambda: self.open_folder(e, edit_title=True))
            m.addAction("Разгруппировать", lambda: self.ungroup(e))
            m.addSeparator()
            m.addAction("Обновить список приложений", self.catalog.load)
            popup_menu(m, gpos)
            return
        if self.folder is not None:
            m.addAction("Убрать из папки", lambda: self.take_out(e))
        elif self.mode == "all":
            sub = m.sub("Добавить в папку")
            folders = [f for f in self.top if f.get("kind") == "folder"]
            for f in folders:
                sub.addAction(f["name"], lambda f=f: self.put_in_folder(e, f))
            if folders:
                sub.addSeparator()
            sub.addAction("Новая папка", lambda: self.new_folder(e))
        if os.path.isfile(e["parse"]):
            m.addAction("Добавить в док", lambda: self.app.dock.pin_path(e["parse"]))
            m.addAction("Показать в проводнике",
                        lambda: subprocess.Popen(["explorer.exe", "/select,", e["parse"]]))
        m.addSeparator()
        m.addAction("Убрать из Launchpad", lambda: self.hide_entries([e]))
        self.add_uninstall(m, [e])
        m.addSeparator()
        m.addAction("Обновить список приложений", self.catalog.load)
        popup_menu(m, gpos)

    def context_many(self, entries, gpos):
        n = len(entries)
        m = MacMenu(parent=self)
        m.addAction(f"Открыть ({n})", lambda: self.launch_many(entries))
        if self.mode == "all" and self.folder is None:
            sub = m.sub("Добавить в папку")
            for f in [f for f in self.top if f.get("kind") == "folder"]:
                sub.addAction(f["name"], lambda f=f: self.put_many_in_folder(entries, f))
            sub.addAction("Новая папка", lambda: self.put_many_in_folder(entries, None))
        m.addSeparator()
        m.addAction(f"Убрать из Launchpad ({n})", lambda: self.hide_entries(entries))
        self.add_uninstall(m, entries)
        m.addSeparator()
        m.addAction("Снять выделение", lambda: (self.picked.clear(), self.update()))
        popup_menu(m, gpos)

    def add_uninstall(self, m, entries):
        """«Удалить с компьютера» — подтверждение в подменю (наведение + клик), без отдельных окон."""
        plans = [(e, uninstall.plan(e["parse"], e["name"])) for e in entries]
        plans = [(e, p) for e, p in plans if p is not None]
        if not plans:
            a = m.addAction("Удалить с компьютера")
            a.setEnabled(False)
            return
        sub = m.sub("Удалить с компьютера")
        if len(plans) == 1:
            sub.addAction(plans[0][1]["label"], lambda: self.uninstall_entries(plans))
        else:
            kinds = {p["kind"] for _, p in plans}
            extra = "  (портативные — в корзину)" if "portable" in kinds else ""
            sub.addAction(f"Удалить {len(plans)} программ{extra}", lambda: self.uninstall_entries(plans))

    def background_menu(self, gpos):
        m = MacMenu(parent=self)
        hidden = self.cfg.get("lp_hidden") or []
        a = m.addAction(f"Вернуть убранные приложения ({len(hidden)})", self.unhide_all)
        a.setEnabled(bool(hidden))
        m.addAction("Обновить список приложений", self.catalog.load)
        popup_menu(m, gpos)

    def launch_many(self, entries):
        self.picked = []
        for k, e in enumerate(entries):
            QTimer.singleShot(150 + 120 * k, lambda e=e: self.run_entry(e))
        self.close_animated()

    def put_many_in_folder(self, entries, folder):
        folders = self.folder_cfg()
        keys = [e["parse"] for e in entries]
        low = {k.casefold() for k in keys}
        for f in folders:
            f["items"] = [k for k in f["items"] if k.casefold() not in low]
        if folder is None:
            name = self.unique_name("Новая папка", folders)
            folders.insert(0, dict(name=name, items=keys))
        else:
            name = folder["name"]
            f = next((f for f in folders if f["name"] == name), None)
            if f is None:
                f = dict(name=name, items=[])
                folders.insert(0, f)
            f["items"] += keys
        self.picked = []
        self.save_folders(folders)
        new = next((f for f in self.top if f.get("kind") == "folder" and f["name"] == name), None)
        if new is not None and folder is None:
            self.open_folder(new, edit_title=True)

    def hide_entries(self, entries):
        hidden = list(self.cfg.get("lp_hidden") or [])
        hidden += [e["parse"] for e in entries if e["parse"] not in hidden]
        self.cfg["lp_hidden"] = hidden
        self.picked = []
        self.app.save_soon()
        self.build_folders()

    def unhide_all(self):
        self.cfg["lp_hidden"] = []
        self.app.save_soon()
        self.build_folders()

    def uninstall_entries(self, plans):
        self.picked = []
        for e, p in plans:
            if uninstall.run(p, self.uninstalled.emit):
                e["removing"] = True
        self.update()

    def after_uninstall(self):
        """Деинсталлятор закончил: перечитываем список (часть деинсталляторов доделывает работу в фоне)."""
        self.catalog.load()
        QTimer.singleShot(8000, self.catalog.load)
        QTimer.singleShot(30000, self.catalog.load)

    def new_folder(self, entry):
        folders = self.folder_cfg()
        for f in folders:
            f["items"] = [k for k in f["items"] if k.casefold() != entry["parse"].casefold()]
        name = self.unique_name("Новая папка", folders)
        folders.insert(0, dict(name=name, items=[entry["parse"]]))
        self.save_folders(folders)
        new = next((f for f in self.top if f.get("kind") == "folder" and f["name"] == name), None)
        if new is not None:
            self.open_folder(new, edit_title=True)

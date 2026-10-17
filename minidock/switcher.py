"""Перехват клавиатуры (Alt+Tab, Win) и свой переключатель приложений в стиле Cmd+Tab."""
import ctypes
import threading
from ctypes import wintypes

import win32con
import win32gui
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget

from .dock import Item
from .previews import THUMB_RADIUS, LiveThumbs, close_button_rect, paint_close_button, row_layout, window_title
from .ui import TOP_FLAGS, animate, fade_in, lerp, make_glass, precise_timer, prepare_glass, rounded_path, smooth
from .winapi import (KBDLLHOOKSTRUCT, VK_ESCAPE, VK_LEFT, VK_LMENU, VK_LWIN, VK_MENU, VK_RETURN, VK_RIGHT, VK_RMENU,
                     VK_RWIN, VK_SHIFT, VK_TAB, key_text, LowLevelKeyboardProc, app_name_for, app_windows, force_foreground,
                     inject_dummy, inject_key, kernel32, key_down, press_keys, syscommand, user32)

user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


VK_SPACE = 0x20
VK_UP, VK_DOWN = 0x26, 0x28
VK_BACKTICK = 0xC0  # клавиша ` (Ё): следующее окно того же приложения
# клавиши, которые хуку вообще могут быть интересны; остальные пропускаются сразу, без разбора
WATCHED = {VK_TAB, VK_SPACE, VK_LWIN, VK_RWIN, VK_MENU, VK_LMENU, VK_RMENU}


class KeyHook(QObject):
    """Низкоуровневый хук клавиатуры в отдельном потоке.

    Windows ждёт ответа хука на КАЖДОЕ нажатие во всей системе. Если бы хук жил в основном потоке,
    любая задержка интерфейса (снимок окна, отрисовка) задерживала бы ввод везде. Поэтому у хука свой
    поток со своим циклом сообщений: он только решает, забрать ли клавишу, а действия (показать
    переключатель, открыть Launchpad) передаёт в основной поток сигналом.
    """
    action = Signal(str, int)
    lp_key = Signal(int, str)  # клавиша для поиска Launchpad (vk, текст)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.switching = False
        self.win_down = False
        self.win_combo = False
        self.space_eaten = False
        self.hook = None
        self.thread_id = 0
        self.action.connect(self.dispatch)  # из потока хука — в основной поток (queued)
        self.lp_key.connect(lambda vk, text: self.app.launchpad.feed_key(vk, text))
        self.lp_eaten = set()  # клавиши, нажатие которых отдали Launchpad, — их отпускание тоже забираем
        started = threading.Event()
        threading.Thread(target=self.run, args=(started,), daemon=True, name="MiniDockKeyHook").start()
        started.wait(2)

    def run(self, started):
        self._proc = LowLevelKeyboardProc(self.proc)
        self.hook = user32.SetWindowsHookExW(13, self._proc, kernel32.GetModuleHandleW(None), 0)  # WH_KEYBOARD_LL
        self.thread_id = kernel32.GetCurrentThreadId()
        started.set()
        # Если программа на миг задумалась и не ответила вовремя, Windows молча снимает перехватчик — и Alt+Tab
        # становится системным. Поэтому раз в 3 с ставим его заново (заодно он снова первый в очереди).
        user32.SetTimer(None, 0, 3000, None)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0113:  # WM_TIMER
                self.rehook()
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self.hook:
            user32.UnhookWindowsHookEx(self.hook)
            self.hook = None

    def rehook(self):
        if self.switching or self.win_down:
            return  # посреди Alt+Tab или нажатой Win не трогаем
        new = user32.SetWindowsHookExW(13, self._proc, kernel32.GetModuleHandleW(None), 0)
        if new:
            old, self.hook = self.hook, new
            if old:
                user32.UnhookWindowsHookEx(old)

    def unhook(self):
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT — поток снимет хук и завершится

    def system_start(self):
        """Открыть настоящее меню «Пуск» Windows (инжектированные клавиши хук пропускает)."""
        press_keys(VK_LWIN)

    def proc(self, n, wparam, lparam):
        try:
            if n == 0:
                kb = KBDLLHOOKSTRUCT.from_address(lparam)
                vk = kb.vkCode
                # быстрый путь: обычные клавиши при обычном состоянии не разбираем вовсе
                interesting = vk in WATCHED or self.switching or self.win_down
                if interesting and not kb.flags & 0x10:  # LLKHF_INJECTED — свои же нажатия пропускаем
                    if self.handle(vk, wparam in (0x100, 0x104)):
                        return 1
                if (self.app.launchpad.capture_keys or self.lp_eaten) and not kb.flags & 0x10 \
                        and self.launchpad_key(vk, kb.scanCode, wparam in (0x100, 0x104)):
                    return 1
        except Exception:
            pass
        return user32.CallNextHookEx(None, n, wparam, lparam)

    LP_KEYS = {0x08, 0x09, 0x0D, 0x1B, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x2E}  # Backspace, Tab, Enter, Esc…

    def launchpad_key(self, vk, scan, down):
        """Launchpad на экране, но без фокуса (Windows не всегда отдаёт его сразу): набранное уходит в его
        поиск, а не в окно позади. Сочетания с Ctrl/Alt/Win не трогаем."""
        if not down:
            if vk in self.lp_eaten:
                self.lp_eaten.discard(vk)
                return True
            return False
        if not self.app.launchpad.capture_keys or any(key_down(k) for k in (0x11, 0x12, VK_LWIN, VK_RWIN)):
            return False
        text = "" if vk in self.LP_KEYS else key_text(vk, scan)
        if vk not in self.LP_KEYS and not text:
            return False
        self.lp_eaten.add(vk)
        self.lp_key.emit(vk, text)
        return True

    def later(self, name, arg=0):
        self.action.emit(name, arg)

    def dispatch(self, name, arg):
        sw = self.app.switcher
        {"begin": lambda: sw.begin(arg), "step": lambda: sw.step(arg), "commit": sw.commit,
         "arrow": lambda: sw.arrow(arg), "nextwin": sw.next_window,
         "cancel": sw.cancel, "quit": sw.quit_selected, "launchpad": self.app.launchpad.toggle,
         "spotlight": self.app.spotlight.toggle}[name]()

    def handle(self, vk, down):
        cfg = self.app.cfg
        if vk == VK_TAB and cfg["custom_switcher"]:
            if down and (self.switching or key_down(VK_MENU)):
                direction = -1 if key_down(VK_SHIFT) else 1
                if not self.switching:
                    self.switching = True
                    inject_dummy()  # чтобы отпускание Alt не открыло меню программы
                    self.later("begin", direction)
                else:
                    self.later("step", direction)
                return True
            if not down and self.switching:
                return True
        if self.switching:
            if vk in (VK_MENU, VK_LMENU, VK_RMENU):
                if not down:
                    self.switching = False
                    self.later("commit")
                return False
            if vk in (VK_SHIFT, 0xA0, 0xA1):
                return False
            if down:
                if vk == VK_ESCAPE:
                    self.switching = False
                    self.later("cancel")
                elif vk in (VK_LEFT, VK_RIGHT, VK_UP, VK_DOWN):
                    self.later("arrow", vk)
                elif vk == VK_BACKTICK:
                    self.later("nextwin")
                elif vk == ord("Q"):
                    self.later("quit")
                elif vk == VK_RETURN:
                    self.switching = False
                    self.later("commit")
            return True  # остальные клавиши во время переключения не уходят в программу

        # Alt+Space — Spotlight
        if vk == VK_SPACE and cfg["spotlight"]:
            if down and key_down(VK_MENU):
                self.space_eaten = True
                inject_dummy()  # чтобы отпускание Alt не открыло системное меню окна
                self.later("spotlight")
                return True
            if not down and self.space_eaten:
                self.space_eaten = False
                return True

        if vk in (VK_LWIN, VK_RWIN) and cfg["win_key_launchpad"]:
            if down:
                if not self.win_down:
                    self.win_down, self.win_combo = True, False
                return False
            alone = self.win_down and not self.win_combo
            self.win_down = False
            if alone:
                # одиночное нажатие Win: гасим «Пуск» (лишняя клавиша между Win↓ и Win↑) и открываем Launchpad
                inject_dummy()
                inject_key(vk, up=True)
                self.later("launchpad")
                return True
            return False
        if self.win_down and down:
            self.win_combo = True
        return False



class Switcher(QWidget):
    """Переключатель: иконки приложений в порядке последнего использования, под ними — живые миниатюры
    окон выбранного приложения. ↓ или ` — выбирать окно, ← → — какое, ↑ — назад к приложениям."""

    def __init__(self, app):
        super().__init__(None, TOP_FLAGS)
        self.app = app
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.apps = []
        self.index = 0
        self.win_index = 0   # какое окно выбранного приложения откроется
        self.win_mode = False  # стрелки выбирают окна, а не приложения
        self.hover = None      # приложение под мышью: его окна показаны, но выбор (для Alt) не меняется
        self.hover_win = None  # миниатюра под мышью
        self.close_hot = None  # крестик под мышью: ("win", номер миниатюры) | ("app", номер приложения)
        self.icon_hover = None  # иконка под мышью (у неё появляется крестик «закрыть приложение»)
        self.hl = None       # анимированная позиция подсветки (QPointF)
        self.active = False
        self.icons = {}
        self.font_name = QFont("Segoe UI", 10, QFont.DemiBold)
        self.font_title = QFont("Segoe UI", 8)
        self.anim = precise_timer(self, 14, self.tick)
        self.glass = make_glass(self)
        self.thumbs = LiveThumbs()

    @property
    def cfg(self):
        return self.app.cfg

    def radius(self):
        return float(self.cfg["switcher_radius"])

    def text_height(self):
        h = 0
        if self.cfg["switcher_titles"]:
            h += 28
            if self.cfg["switcher_window_title"] and not self.cfg["switcher_previews"]:
                h += 16
        return h

    # -- data
    def icon_for(self, exe):
        it = self.app.dock.item_for_hwnd(0, exe)
        if it is not None and it.pix:
            return it
        if exe not in self.icons:
            self.icons[exe] = Item.from_path(exe, kind="running")
        return self.icons[exe]

    def collect(self):
        """Приложения в порядке последнего использования. Свёрнутое окно остаётся на своём месте
        (в z-order Windows оно уходит в самый низ, поэтому порядок берём из истории активаций)."""
        mru = {h: i for i, h in enumerate(self.app.mru)}
        z = {}

        def rank(h):  # окна, которые ещё не активировались при работе MiniDock, — после остальных по z-order
            return mru.get(h, len(mru) + z[h])
        apps = []
        for exe, hwnds in app_windows().items():
            for h in hwnds:
                z[h] = len(z)
            if not self.cfg["switcher_minimized"]:
                hwnds = [h for h in hwnds if not win32gui.IsIconic(h)]
                if not hwnds:
                    continue
            hwnds = sorted(hwnds, key=rank)
            it = self.icon_for(exe)
            apps.append(dict(exe=exe, hwnds=hwnds[:8], item=it, name=it.name or app_name_for(exe),
                             rank=rank(hwnds[0])))
        apps.sort(key=lambda a: a["rank"])
        return apps

    def current_windows(self):
        return self.apps[self.index]["hwnds"] if self.apps else []

    def shown(self):
        """Чьи окна сейчас показаны миниатюрами: под мышью или выбранного клавиатурой."""
        return self.hover if self.hover is not None and self.hover < len(self.apps) else self.index

    # -- geometry
    def cols(self):
        """Сколько иконок в ряду: настройка «в одной строке» (0 — все в один ряд)."""
        n = max(len(self.apps), 1)
        per = int(self.cfg.get("switcher_per_row", 0) or 0)
        return min(n, per) if per > 0 else n

    def grid_rows(self):
        return -(-max(len(self.apps), 1) // self.cols())

    def cell(self):
        n = self.cols()
        scr = QApplication.primaryScreen().geometry()
        want = float(self.cfg["switcher_icon"])
        gap = want * 0.15
        size = min(want, (scr.width() - 120) / n - gap)  # не шире экрана
        size = max(size, 32.0)
        return size, size * 0.15, max(14.0, size * 0.2)  # иконка, промежуток, поля

    PREVIEW_GAP = 18.0

    def preview_height(self):
        """Высота миниатюр: настройка — это ширина миниатюры окна 16:9."""
        return float(self.cfg["switcher_preview"]) * 0.5625

    def previews_height(self):
        if not self.cfg["switcher_previews"]:
            return 0.0
        return self.preview_height() + 6 + 18 + 12  # миниатюры + подпись + отступ до иконок

    def icons_width(self, n=None):
        size, gap, pad = self.cell()
        n = self.cols() if n is None else n
        return n * size + (n - 1) * gap

    def icons_height(self):
        size, gap, pad = self.cell()
        rows = self.grid_rows()
        return rows * size + (rows - 1) * gap * 1.6

    def place(self):
        """Размер окна фиксируется на всё нажатие Alt (самый широкий ряд окон среди приложений):
        иначе размытый фон, снятый при показе, перестал бы совпадать с окном."""
        size, gap, pad = self.cell()
        w = self.icons_width()
        if self.cfg["switcher_previews"]:
            max_w = QApplication.primaryScreen().geometry().width() - 160
            for a in self.apps:
                # плитки одной высоты в пропорциях окон; слишком длинный ряд уменьшается целиком
                a["row"] = row_layout(a["hwnds"], self.preview_height(), self.PREVIEW_GAP, max_w)
                widths = a["row"][0]
                w = max(w, sum(widths) + self.PREVIEW_GAP * (len(widths) - 1))
        w += pad * 2
        h = self.icons_height() + pad * 2 + self.text_height() + self.previews_height()
        scr = QApplication.primaryScreen().geometry()
        y = scr.center().y() - h / 2 if self.cfg["switcher_position"] == "center" else scr.top() + scr.height() * 0.22
        self.setGeometry(int(scr.center().x() - w / 2), int(y), int(w), int(h))

    def icon_rect(self, i):
        """Иконки рядами по «в одной строке»; неполный последний ряд — по центру."""
        size, gap, pad = self.cell()
        cols = self.cols()
        row, col = divmod(i, cols)
        in_row = min(cols, len(self.apps) - row * cols)
        x0 = (self.width() - self.icons_width(in_row)) / 2
        return QRectF(x0 + col * (size + gap), self.icons_top() + row * (size + gap * 1.6), size, size)

    def icons_top(self):
        """Иконки — под рядом миниатюр окон (миниатюры сверху)."""
        return self.cell()[2] + self.previews_height()

    def tiles(self):
        """[(hwnd, QRectF миниатюры)] для окон выбранного приложения — ряд по центру."""
        if not self.cfg["switcher_previews"] or not self.apps or "row" not in self.apps[self.shown()]:
            return []
        pad = self.cell()[2]
        a = self.apps[self.shown()]
        widths, h = a["row"]
        total = sum(widths) + self.PREVIEW_GAP * (len(widths) - 1)
        x = (self.width() - total) / 2
        y = pad + (self.preview_height() - h) / 2
        out = []
        for hwnd, w in zip(a["hwnds"], widths):
            out.append((hwnd, QRectF(round(x), round(y), round(w), round(h))))
            x += w + self.PREVIEW_GAP
        return out

    def update_thumbs(self):
        if not self.isVisible() or not self.cfg["switcher_previews"]:
            self.thumbs.hide()
            return
        self.update()
        self.thumbs.show(self, self.tiles(), self.paint_overlay)

    def selection_changed(self):
        """Сменилось выбранное окно: подсветка под миниатюрами — уголкам нужен новый фон."""
        self.update()
        self.thumbs.refresh_mask(self, self.tiles(), self.paint_overlay)

    # -- control (вызывается из хука)
    def begin(self, direction):
        self.apps = self.collect()
        if not self.apps:
            return
        self.app.preview.close_preview()
        self.index = (1 if direction > 0 else -1) % len(self.apps) if len(self.apps) > 1 else 0
        self.win_index, self.win_mode = 0, False
        self.hover = self.hover_win = self.icon_hover = self.close_hot = None
        self.hl = None       # подсветка сразу встаёт на выбранное
        self.active = True
        # короткое Alt+Tab переключает без вспышки окна
        QTimer.singleShot(int(self.cfg["switcher_delay"]), self.show_if_active)

    def show_if_active(self):
        if not self.active:
            return
        self.place()
        self.show()
        self.raise_()
        self.thumbs.fade_in(self)  # прозрачность 0 — до показа миниатюр, иначе первый кадр они видны целиком
        self.update_thumbs()

    def step(self, d):
        if not self.active or not self.apps:
            return
        self.index = (self.index + d) % len(self.apps)
        self.win_index, self.win_mode = 0, False
        self.hover = self.hover_win = None  # клавиатура снова главная — показываем её выбор
        self.anim.start()
        self.update_thumbs()

    def arrow(self, vk):
        """Стрелки: ← → — приложения (или окна в режиме выбора окна), ↓ — к окнам, ↑ — назад."""
        if not self.active or not self.apps:
            return
        n = len(self.current_windows())
        if self.hover is not None and self.hover != self.index:
            self.hover = self.hover_win = None
            self.update_thumbs()
        if vk == 0x28 and self.cfg["switcher_previews"] and n:  # ↓
            self.win_mode = True
        elif vk == 0x26:  # ↑
            self.win_mode = False
        elif self.win_mode:
            self.win_index = (self.win_index + (1 if vk == 0x27 else -1)) % n
        else:
            self.step(1 if vk == 0x27 else -1)
            return
        self.selection_changed()

    def next_window(self):
        """` — следующее окно того же приложения (как Cmd+` на Mac)."""
        if not self.active or not self.apps:
            return
        n = len(self.current_windows())
        if self.hover is not None and self.hover != self.index:
            self.hover = self.hover_win = None
            self.update_thumbs()
        if n:
            self.win_index = (self.win_index + 1) % n if self.win_mode else (1 % n)
            self.win_mode = True
            self.selection_changed()

    def finish(self):
        self.active = False
        self.anim.stop()
        self.thumbs.hide()
        self.hide()

    def cancel(self):
        self.finish()

    def commit(self, hwnd=None):
        """Переключиться на выбранное клавиатурой (или на hwnd — клик по миниатюре/иконке)."""
        if not self.active or not self.apps:
            self.finish()
            return
        if hwnd is None:
            hwnds = self.apps[self.index]["hwnds"]
            hwnd = hwnds[min(self.win_index, len(hwnds) - 1)]
        target = next(a for a in self.apps if hwnd in a["hwnds"])
        self.finish()
        if win32gui.IsIconic(hwnd) and self.cfg["switcher_genie"]:
            # как при клике по доку: окно «джинном» вытекает из иконки программы в доке
            dock = self.app.animator.dock_for(hwnd)
            icon = dock.icon_screen_rect(dock.item_for_hwnd(hwnd, target["exe"]))
            dock.reveal(0.8)
            self.app.animator.restore(hwnd, icon)
        else:
            force_foreground(hwnd)

    def quit_selected(self):
        """Q: закрыть выбранное окно (в режиме выбора окна) или всё приложение."""
        if not self.active or not self.apps:
            return
        target = self.apps[self.index]
        if self.win_mode and target["hwnds"]:
            syscommand(target["hwnds"].pop(self.win_index), win32con.SC_CLOSE)
            if target["hwnds"]:
                self.win_index %= len(target["hwnds"])
                self.update_thumbs()
                return
        else:
            for h in target["hwnds"]:
                syscommand(h, win32con.SC_CLOSE)
        self.apps.pop(self.index)
        if not self.apps:
            self.finish()
            return
        self.index %= len(self.apps)
        self.win_index, self.win_mode = 0, False
        self.hl = None
        self.update_thumbs()

    # -- painting
    def showEvent(self, e):
        prepare_glass(self.glass)
        fade_in(self)

    def tick(self):
        """Подсветка плавно едет к выбранной иконке (и между рядами тоже)."""
        target = self.icon_rect(self.index).topLeft() if self.apps else None
        if target is None or self.hl is None:
            self.hl = target
            self.anim.stop()
        elif abs(self.hl.x() - target.x()) + abs(self.hl.y() - target.y()) < 0.5:
            self.hl = target
            self.anim.stop()
        else:
            k = smooth(0.35, self.anim.dt)
            self.hl = QPointF(lerp(self.hl.x(), target.x(), k), lerp(self.hl.y(), target.y(), k))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        path = rounded_path(self.rect(), self.radius())
        self.glass.fill(p, path)
        p.fillPath(path, QColor(30, 30, 36, int(self.cfg["switcher_opacity"])))
        p.setPen(QPen(QColor(255, 255, 255, 45), 1))
        p.drawPath(path)
        if not self.apps:
            return
        size, gap, pad = self.cell()
        m = size * 0.08
        if self.hl is None:
            self.hl = self.icon_rect(self.index).topLeft()
        hl = QRectF(self.hl.x(), self.hl.y(), size, size).adjusted(-m, -m, m, m)
        hp = QPainterPath()
        hp.addRoundedRect(hl, size * 0.16, size * 0.16)
        p.fillPath(hp, QColor(255, 255, 255, 30 if self.win_mode else 48))
        if self.hover is not None and self.hover != self.index and self.hover < len(self.apps):
            hr = self.icon_rect(self.hover).adjusted(-m, -m, m, m)
            hp2 = QPainterPath()
            hp2.addRoundedRect(hr, size * 0.16, size * 0.16)
            p.fillPath(hp2, QColor(255, 255, 255, 20))
        dpr = self.devicePixelRatioF()
        for i, a in enumerate(self.apps):
            r = self.icon_rect(i)
            it = a["item"]
            if it.pix:
                pix = it.pix_for(size * dpr)
                p.drawPixmap(r, pix, QRectF(pix.rect()))
        if self.icon_hover is not None and self.icon_hover < len(self.apps):
            paint_close_button(p, self.app_close_rect(self.icon_hover), self.close_hot == ("app", self.icon_hover))
        a = self.apps[self.shown()]
        if self.cfg["switcher_titles"]:
            fm = QFontMetricsF(self.font_name)
            y = self.icons_top() + self.icons_height() + 10
            p.setFont(self.font_name)
            p.setPen(QColor(245, 245, 248))
            p.drawText(QRectF(8, y, self.width() - 16, fm.height()), Qt.AlignCenter,
                       fm.elidedText(a["name"], Qt.ElideRight, self.width() - 16))
            if self.cfg["switcher_window_title"] and not self.cfg["switcher_previews"] and a["hwnds"]:
                p.setFont(self.font_title)
                p.setPen(QColor(255, 255, 255, 140))
                ft = QFontMetricsF(self.font_title)
                p.drawText(QRectF(8, y + fm.height(), self.width() - 16, ft.height()), Qt.AlignCenter,
                           ft.elidedText(window_title(a["hwnds"][0]), Qt.ElideRight, self.width() - 16))
        # подсветка и подписи миниатюр (сами миниатюры рисует DWM поверх, уголки скругляет слой-маска)
        ft = QFontMetricsF(self.font_title)
        own = self.shown() == self.index  # показаны окна выбранного клавиатурой приложения
        for i, (h, tile) in enumerate(self.tiles()):
            chosen = own and i == self.win_index
            if i == self.hover_win and not (chosen and self.win_mode):
                hv = QPainterPath()
                hv.addRoundedRect(tile.adjusted(-6, -6, 6, 6), THUMB_RADIUS + 5, THUMB_RADIUS + 5)
                p.fillPath(hv, QColor(255, 255, 255, 30))
            if chosen and self.win_mode:
                sel = QPainterPath()
                sel.addRoundedRect(tile.adjusted(-6, -6, 6, 6), THUMB_RADIUS + 5, THUMB_RADIUS + 5)
                p.fillPath(sel, QColor(10, 132, 255, 70))
                p.setPen(QPen(QColor(10, 132, 255), 2))
                p.setBrush(Qt.NoBrush)
                p.drawPath(sel)
            title = QRectF(tile.left() - 6, tile.bottom() + 6, tile.width() + 12, 18)
            p.setFont(self.font_title)
            p.setPen(QColor(255, 255, 255, 235 if chosen else 150))
            p.drawText(title, Qt.AlignHCenter | Qt.AlignTop,
                       ft.elidedText(window_title(h), Qt.ElideRight, title.width()))

    # -- mouse
    def index_at(self, pos):
        m = self.cell()[0] * 0.08
        for i in range(len(self.apps)):
            if self.icon_rect(i).adjusted(-m, -m, m, m).contains(pos):
                return i
        return None

    def window_at(self, pos):
        for i, (h, tile) in enumerate(self.tiles()):
            if tile.adjusted(-6, -6, 6, 24).contains(pos):
                return i
        return None

    # -- крестики «закрыть»: на миниатюре под мышью — окно, на иконке под мышью — всё приложение
    def app_close_rect(self, i):
        r = self.icon_rect(i)
        side = max(18.0, r.width() * 0.22)
        return QRectF(r.left() - side * 0.25, r.top() - side * 0.25, side, side)

    def close_at(self, pos):
        tiles = self.tiles()
        if self.hover_win is not None and self.hover_win < len(tiles)                 and close_button_rect(tiles[self.hover_win][1]).contains(pos):
            return "win", self.hover_win
        if self.icon_hover is not None and self.icon_hover < len(self.apps)                 and self.app_close_rect(self.icon_hover).contains(pos):
            return "app", self.icon_hover
        return None

    def paint_overlay(self, p):
        """Поверх миниатюр (в слое скругления): кружок «закрыть» у миниатюры под мышью."""
        tiles = self.tiles()
        if self.hover_win is not None and self.hover_win < len(tiles):
            paint_close_button(p, close_button_rect(tiles[self.hover_win][1]), self.close_hot == ("win", self.hover_win))

    def close_window(self, hwnd):
        """Закрыть одно окно мышью: убираем его из списка сразу, размер переключателя не меняем."""
        syscommand(hwnd, win32con.SC_CLOSE)
        for k, a in enumerate(self.apps):
            if hwnd in a["hwnds"]:
                a["hwnds"].remove(hwnd)
                if a["hwnds"]:
                    a["row"] = row_layout(a["hwnds"], self.preview_height(), self.PREVIEW_GAP,
                                          QApplication.primaryScreen().geometry().width() - 160)
                    if k == self.index:
                        self.win_index %= len(a["hwnds"])
                else:
                    self.drop_app(k)
                break
        self.hover_win = self.close_hot = None
        self.update_thumbs()

    def close_app(self, i):
        for h in self.apps[i]["hwnds"]:
            syscommand(h, win32con.SC_CLOSE)
        self.drop_app(i)
        self.hover_win = self.close_hot = None
        self.update_thumbs()

    def drop_app(self, i):
        self.apps.pop(i)
        if not self.apps:
            self.finish()
            return
        if i < self.index:
            self.index -= 1
        self.index %= len(self.apps)
        self.hover = self.icon_hover = None
        self.win_index, self.win_mode = 0, False
        self.hl = None

    def mouseMoveEvent(self, e):
        """Наведение только показывает окна приложения; выбор для отпускания Alt не меняется."""
        pos = e.position()
        w = self.window_at(pos)
        icon = self.index_at(pos) if w is None else None
        changed = w != self.hover_win or icon != self.icon_hover
        self.hover_win, self.icon_hover = w, icon
        hot = self.close_at(pos)
        if changed or hot != self.close_hot:
            self.close_hot = hot
            self.selection_changed()
        if w is not None or not self.cfg["switcher_hover"]:
            return
        if icon is not None and icon != self.shown():
            self.hover = icon
            self.update_thumbs()

    def leaveEvent(self, e):
        if self.hover_win is not None or self.icon_hover is not None:
            self.hover_win = self.icon_hover = self.close_hot = None
            self.selection_changed()

    def mouseReleaseEvent(self, e):
        """Переключает только клик левой кнопкой — по миниатюре окна или по иконке приложения;
        клик по крестику закрывает окно или приложение, переключатель остаётся открытым."""
        if e.button() != Qt.LeftButton:
            return
        hot = self.close_at(e.position())
        if hot is not None:
            kind, k = hot
            if kind == "win":
                self.close_window(self.tiles()[k][0])
            else:
                self.close_app(k)
            return
        w = self.window_at(e.position())
        i = self.index_at(e.position())
        if w is not None:
            hwnd = self.tiles()[w][0]
        elif i is not None:
            hwnd = self.apps[i]["hwnds"][0]
        else:
            return
        self.app.keys.switching = False
        self.commit(hwnd)


class WinSwitcher(QWidget):
    """Второй стиль Alt+Tab — как в Windows: крупные живые миниатюры всех окон (по последнему использованию),
    у каждой внизу значок программы, под ней — заголовок окна. Tab/стрелки — по окнам, Q — закрыть окно."""
    GAP = 22.0       # между миниатюрами
    LABEL = 40.0     # под миниатюрой: половина значка + заголовок
    ICON = 34.0

    def __init__(self, app, base):
        super().__init__(None, TOP_FLAGS)
        self.app = app
        self.base = base          # обычный переключатель — у него берём список окон и значки
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.wins = []            # [(hwnd, приложение)] в порядке последнего использования
        self.layout_rows = []     # [(индексы окон, ширины, высота)]
        self.index = 0
        self.hover = None
        self.close_hot = self._drawn_hover = None  # крестик под мышью / что уже нарисовано
        self.active = False
        self.pad = 16.0
        self.font_title = QFont("Segoe UI", 9)
        self.glass = make_glass(self)
        self.thumbs = LiveThumbs()

    @property
    def cfg(self):
        return self.app.cfg

    # -- данные и раскладка
    def collect(self):
        apps = self.base.collect()
        mru = {h: i for i, h in enumerate(self.app.mru)}
        wins = [(h, a) for a in apps for h in a["hwnds"]]
        order = {h: i for i, (h, _) in enumerate(wins)}
        wins.sort(key=lambda x: mru.get(x[0], len(mru) + order[x[0]]))
        return wins

    def tile_height(self):
        return float(self.cfg["switcher_preview"]) * 0.5625 * 1.15

    def place(self):
        """Окна раскладываются рядами (до 4 или по настройке «в одной строке»), каждый ряд — не шире экрана."""
        scr = QApplication.primaryScreen().geometry()
        max_w = scr.width() - 200
        hwnds = [h for h, _ in self.wins]
        self.pad = max(16.0, float(self.cfg["switcher_radius"]))
        layout = []
        limit = int(self.cfg.get("switcher_per_row", 0) or 0)  # «в одной строке»: 0 — сколько влезет
        first = -(-len(hwnds) // limit) if limit > 0 else 1
        for rows in range(first, max(first, 4) + 1):
            per = min(-(-len(hwnds) // rows), limit) if limit > 0 else -(-len(hwnds) // rows)
            chunks = [list(range(i, min(i + per, len(hwnds)))) for i in range(0, len(hwnds), per)]
            layout = [(idx, *row_layout([hwnds[i] for i in idx], self.tile_height(), self.GAP, max_w))
                      for idx in chunks]
            if all(h >= self.tile_height() - 0.5 for _, _, h in layout):
                break
        self.layout_rows = layout
        w = max(sum(ws) + self.GAP * (len(ws) - 1) for _, ws, _ in layout) + self.pad * 2
        h = self.pad * 2 + sum(rh + self.LABEL for _, _, rh in layout) + self.GAP * 0.5 * (len(layout) - 1)
        y = scr.center().y() - h / 2 if self.cfg["switcher_position"] == "center" else scr.top() + scr.height() * 0.22
        self.setGeometry(int(scr.center().x() - w / 2), int(y), int(w), int(h))

    def tiles(self):
        out = []
        y = self.pad
        for idx, widths, rh in self.layout_rows:
            total = sum(widths) + self.GAP * (len(widths) - 1)
            x = (self.width() - total) / 2
            for i, w in zip(idx, widths):
                out.append((self.wins[i][0], QRectF(round(x), round(y), round(w), round(rh))))
                x += w + self.GAP
            y += rh + self.LABEL + self.GAP * 0.5
        return out

    def row_of(self, i):
        for r, (idx, _, _) in enumerate(self.layout_rows):
            if i in idx:
                return r, idx.index(i)
        return 0, 0

    # -- управление (из хука клавиатуры)
    def begin(self, direction):
        self.wins = self.collect()
        if not self.wins:
            return
        self.app.preview.close_preview()
        self.index = (1 if direction > 0 else -1) % len(self.wins) if len(self.wins) > 1 else 0
        self.hover = None
        self.close_hot = self._drawn_hover = None  # крестик под мышью / что уже нарисовано
        self.active = True
        QTimer.singleShot(int(self.cfg["switcher_delay"]), self.show_if_active)

    def show_if_active(self):
        if not self.active:
            return
        self.place()
        self.show()
        self.raise_()
        self.thumbs.fade_in(self)  # прозрачность 0 — до показа миниатюр, иначе первый кадр они видны целиком
        self.thumbs.show(self, self.tiles(), self.paint_overlay)

    def changed(self):
        self.update()
        if self.isVisible():
            self.thumbs.refresh_mask(self, self.tiles(), self.paint_overlay)

    def step(self, d):
        if self.active and self.wins:
            self.index = (self.index + d) % len(self.wins)
            self.changed()

    def arrow(self, vk):
        if not self.active or not self.wins:
            return
        if vk in (0x25, 0x27):  # стрелки влево/вправо
            self.step(1 if vk == 0x27 else -1)
            return
        r, c = self.row_of(self.index)
        r2 = r + (1 if vk == 0x28 else -1)
        if 0 <= r2 < len(self.layout_rows):
            idx = self.layout_rows[r2][0]
            self.index = idx[min(c, len(idx) - 1)]
            self.changed()

    def next_window(self):
        """Клавиша Ё — следующее окно той же программы."""
        if not self.active or not self.wins:
            return
        exe = self.wins[self.index][1]["exe"]
        n = len(self.wins)
        for k in range(1, n):
            j = (self.index + k) % n
            if self.wins[j][1]["exe"] == exe:
                self.index = j
                self.changed()
                return

    def finish(self):
        self.active = False
        self.thumbs.hide()
        self.hide()

    def cancel(self):
        self.finish()

    def commit(self, hwnd=None):
        if not self.active or not self.wins:
            self.finish()
            return
        if hwnd is None:
            hwnd = self.wins[self.index][0]
        target = next(a for h, a in self.wins if h == hwnd)
        self.finish()
        if win32gui.IsIconic(hwnd) and self.cfg["switcher_genie"]:
            dock = self.app.animator.dock_for(hwnd)
            icon = dock.icon_screen_rect(dock.item_for_hwnd(hwnd, target["exe"]))
            dock.reveal(0.8)
            self.app.animator.restore(hwnd, icon)
        else:
            force_foreground(hwnd)

    def quit_selected(self, i=None):
        """Q: закрыть выбранное окно (или i-е — крестик на миниатюре)."""
        if not self.active or not self.wins:
            return
        i = self.index if i is None else i
        hwnd, _ = self.wins.pop(i)
        syscommand(hwnd, win32con.SC_CLOSE)
        if not self.wins:
            self.finish()
            return
        if i < self.index:
            self.index -= 1
        self.index %= len(self.wins)
        self.hover = self.close_hot = None
        self.thumbs.hide()
        self.place()
        self.thumbs.show(self, self.tiles(), self.paint_overlay)
        self.update()

    # -- рисование
    def showEvent(self, e):
        prepare_glass(self.glass)
        fade_in(self)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)  # фон — уменьшенный снимок: без сглаживания он в «квадратики»
        path = rounded_path(self.rect(), float(self.cfg["switcher_radius"]))
        self.glass.fill(p, path)
        p.fillPath(path, QColor(30, 30, 36, int(self.cfg["switcher_opacity"])))
        p.setPen(QPen(QColor(255, 255, 255, 45), 1))
        p.drawPath(path)
        fm = QFontMetricsF(self.font_title)
        p.setFont(self.font_title)
        for i, (h, tile) in enumerate(self.tiles()):
            sel = i == self.index
            if sel or i == self.hover:
                box = QPainterPath()
                box.addRoundedRect(tile.adjusted(-7, -7, 7, self.LABEL - 4), THUMB_RADIUS + 6, THUMB_RADIUS + 6)
                p.fillPath(box, QColor(10, 132, 255, 70) if sel else QColor(255, 255, 255, 26))
                if sel:
                    p.setPen(QPen(QColor(10, 132, 255), 2))
                    p.setBrush(Qt.NoBrush)
                    p.drawPath(box)
            title = QRectF(tile.left(), tile.bottom() + self.ICON / 2 + 2, tile.width(), fm.height() + 2)
            p.setPen(QColor(255, 255, 255, 240 if sel else 160))
            p.drawText(title, Qt.AlignHCenter | Qt.AlignTop,
                       fm.elidedText(window_title(h), Qt.ElideRight, title.width()))

    def paint_overlay(self, p):
        """Поверх миниатюр (в слое скругления): значок программы по центру нижнего края каждой миниатюры."""
        dpr = self.devicePixelRatioF()
        apps = {h: a for h, a in self.wins}
        for h, tile in self.tiles():
            it = apps.get(h, {}).get("item")
            if it is None or not it.pix:
                continue
            r = QRectF(tile.center().x() - self.ICON / 2, tile.bottom() - self.ICON / 2, self.ICON, self.ICON)
            pix = it.pix_for(self.ICON * dpr)
            p.drawPixmap(r, pix, QRectF(pix.rect()))
        tiles = self.tiles()
        if self.hover is not None and self.hover < len(tiles):  # крестик «закрыть» у миниатюры под мышью
            paint_close_button(p, close_button_rect(tiles[self.hover][1]), self.close_hot == self.hover)

    # -- мышь: наведение подсвечивает, клик — переключает, крестик — закрывает окно
    def tile_at(self, pos):
        for i, (h, tile) in enumerate(self.tiles()):
            if tile.adjusted(-7, -7, 7, self.LABEL).contains(pos):
                return i
        return None

    def close_at(self, pos):
        tiles = self.tiles()
        if self.hover is not None and self.hover < len(tiles) and close_button_rect(tiles[self.hover][1]).contains(pos):
            return self.hover
        return None

    def mouseMoveEvent(self, e):
        i = self.tile_at(e.position())
        if i != self.hover:
            self.hover = i
        hot = self.close_at(e.position())
        if hot != self.close_hot or self.hover != self._drawn_hover:
            self.close_hot, self._drawn_hover = hot, self.hover
            self.changed()

    def leaveEvent(self, e):
        if self.hover is not None:
            self.hover = self.close_hot = self._drawn_hover = None
            self.changed()

    def mouseReleaseEvent(self, e):
        i = self.tile_at(e.position())
        if e.button() != Qt.LeftButton or i is None:
            return
        if self.close_at(e.position()) == i:
            self.quit_selected(i)
            return
        self.app.keys.switching = False
        self.commit(self.wins[i][0])

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
from .previews import THUMB_RADIUS, LiveThumbs, row_layout, window_title
from .ui import TOP_FLAGS, animate, fade_in, lerp, make_glass, precise_timer, prepare_glass, rounded_path, smooth
from .winapi import (KBDLLHOOKSTRUCT, VK_ESCAPE, VK_LEFT, VK_LMENU, VK_LWIN, VK_MENU, VK_RETURN, VK_RIGHT, VK_RMENU,
                     VK_RWIN, VK_SHIFT, VK_TAB, LowLevelKeyboardProc, app_name_for, app_windows, force_foreground,
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
        started = threading.Event()
        threading.Thread(target=self.run, args=(started,), daemon=True, name="MiniDockKeyHook").start()
        started.wait(2)

    def run(self, started):
        self._proc = LowLevelKeyboardProc(self.proc)
        self.hook = user32.SetWindowsHookExW(13, self._proc, kernel32.GetModuleHandleW(None), 0)  # WH_KEYBOARD_LL
        self.thread_id = kernel32.GetCurrentThreadId()
        started.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self.hook:
            user32.UnhookWindowsHookEx(self.hook)
            self.hook = None

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
        except Exception:
            pass
        return user32.CallNextHookEx(None, n, wparam, lparam)

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
        self.hl = 0.0        # анимированная позиция подсветки
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
    def cell(self):
        n = max(len(self.apps), 1)
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

    def icons_width(self):
        size, gap, pad = self.cell()
        n = len(self.apps)
        return n * size + (n - 1) * gap

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
        h = size + pad * 2 + self.text_height() + self.previews_height()
        scr = QApplication.primaryScreen().geometry()
        y = scr.center().y() - h / 2 if self.cfg["switcher_position"] == "center" else scr.top() + scr.height() * 0.22
        self.setGeometry(int(scr.center().x() - w / 2), int(y), int(w), int(h))

    def icon_rect(self, i):
        size, gap, pad = self.cell()
        x0 = (self.width() - self.icons_width()) / 2
        return QRectF(x0 + i * (size + gap), self.icons_top(), size, size)

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
        self.thumbs.show(self, self.tiles())

    def selection_changed(self):
        """Сменилось выбранное окно: подсветка под миниатюрами — уголкам нужен новый фон."""
        self.update()
        self.thumbs.refresh_mask(self, self.tiles())

    # -- control (вызывается из хука)
    def begin(self, direction):
        self.apps = self.collect()
        if not self.apps:
            return
        self.app.preview.close_preview()
        self.index = (1 if direction > 0 else -1) % len(self.apps) if len(self.apps) > 1 else 0
        self.win_index, self.win_mode = 0, False
        self.hover = self.hover_win = None
        self.hl = float(self.index)
        self.active = True
        # короткое Alt+Tab переключает без вспышки окна
        QTimer.singleShot(int(self.cfg["switcher_delay"]), self.show_if_active)

    def show_if_active(self):
        if not self.active:
            return
        self.place()
        self.show()
        self.raise_()
        self.update_thumbs()
        self.thumbs.fade_in(self)

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
            item = target["item"]
            icon = self.app.dock.icon_screen_rect(item if item in self.app.dock.items() else None)
            self.app.dock.reveal(0.8)
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
        self.hl = min(self.hl, len(self.apps) - 1)
        self.update_thumbs()

    # -- painting
    def showEvent(self, e):
        prepare_glass(self.glass)
        fade_in(self)

    def tick(self):
        if abs(self.hl - self.index) < 0.01:
            self.hl = float(self.index)
            self.anim.stop()
        else:
            self.hl = lerp(self.hl, self.index, smooth(0.35, self.anim.dt))
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
        x0 = (self.width() - self.icons_width()) / 2
        hl = QRectF(x0 + self.hl * (size + gap), self.icons_top(), size, size).adjusted(-m, -m, m, m)
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
        a = self.apps[self.shown()]
        if self.cfg["switcher_titles"]:
            fm = QFontMetricsF(self.font_name)
            y = self.icons_top() + size + 10
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

    def mouseMoveEvent(self, e):
        """Наведение только показывает окна приложения; выбор для отпускания Alt не меняется."""
        if not self.cfg["switcher_hover"]:
            return
        w = self.window_at(e.position())
        if w != self.hover_win:
            self.hover_win = w
            self.selection_changed()
        if w is not None:
            return
        i = self.index_at(e.position())
        if i is not None and i != self.shown():
            self.hover = i
            self.update_thumbs()

    def mouseReleaseEvent(self, e):
        """Переключает только клик левой кнопкой — по миниатюре окна или по иконке приложения."""
        if e.button() != Qt.LeftButton:
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

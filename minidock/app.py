"""Главный объект MiniDock: связывает док, верхнюю панель, анимации, Launchpad и переключатель."""
import atexit
import faulthandler
import os
import sys
import threading
import time

import win32api
import win32event
import win32gui
import winerror
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from .animations import WindowAnimator
from .config import load_cfg, save_cfg
from .dock import Dock
from .launchpad import Launchpad
from .barwidgets import WidgetHost
from .menubar import MenuBar
from .previews import WindowPreview
from .spotlight import Spotlight
from .switcher import KeyHook, Switcher, WinSwitcher
from .elevation import open_security
from .ui import BACKGROUNDS, GLASS
from .updater import Updater
from .winapi import (EVENT_OBJECT_HIDE, EVENT_SYSTEM_CAPTURESTART, EVENT_OBJECT_SHOW, EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_MINIMIZEEND,
                     EVENT_SYSTEM_MINIMIZESTART, WINEVENTPROC, Taskbar, app_windows, default_pinned,
                     force_foreground, foreground_is_fullscreen, fullscreen_monitor, get_work_area, primary_monitor_rect, set_window_shadows,
                     set_work_area, taskbar_windows, user32, window_monitor, window_shadows)

QUIT_EVENT = "MiniDock_Quit"
# команды уже запущенному доку: MiniDock.pyw --launchpad / --settings (удобно повесить на ярлык)
COMMANDS = {"--launchpad": "MiniDock_Launchpad", "--settings": "MiniDock_Settings",
            "--spotlight": "MiniDock_Spotlight", "--test-switcher": "MiniDock_TestSwitcher",
            "--test-spotlight": "MiniDock_TestSpotlight", "--test-lp-start": "MiniDock_TestLpStart",
            "--reveal": "MiniDock_Reveal", "--test-preview": "MiniDock_TestPreview",
            "--test-restore": "MiniDock_TestRestore", "--test-folder": "MiniDock_TestFolder",
            "--test-lp-folder": "MiniDock_TestLpFolder", "--show": "MiniDock_Show",
            "--test-volume": "MiniDock_TestVolume", "--test-close": "MiniDock_TestClose",
            "--test-peek": "MiniDock_TestPeek", "--test-clock": "MiniDock_TestClock"}
SHOW_ACK = "MiniDock_ShowAck"
LAYOUT_KEYS = {"replace_taskbar", "menubar", "bar_height", "position", "icon_size", "magnify", "margin", "autohide"}


class App:
    def __init__(self, quit_event, script):
        self.script = script
        self.cfg = load_cfg(default_pinned)
        save_cfg(self.cfg)
        self.taskbar = Taskbar(self.cfg)
        self.peek_until = 0.0
        self.fullscreen = False
        self.want_area = None
        self.settings = None
        self.mru = []  # окна в порядке последней активации (свежие в начале) — для Alt+Tab

        self.save_timer = QTimer(singleShot=True, interval=400, timeout=lambda: save_cfg(self.cfg))
        self.animator = WindowAnimator(self)
        self.preview = WindowPreview(self)
        self.widgets = WidgetHost(self.cfg)
        self.dock = Dock(self)
        self.bar = MenuBar(self)
        self.extra_bars = {}  # имя монитора -> панель на дополнительном мониторе
        self.extra_docks = {}  # имя монитора -> док на дополнительном мониторе
        self.launchpad = Launchpad(self)
        self.spotlight = Spotlight(self)
        self.mac_switcher = Switcher(self)
        self.win_switcher = WinSwitcher(self, self.mac_switcher)
        self.keys = KeyHook(self)
        self.updater = Updater(self)

        self.apply_layout()
        QApplication.primaryScreen().geometryChanged.connect(self.apply_layout)
        QApplication.instance().screenAdded.connect(lambda *_: QTimer.singleShot(500, self.apply_layout))
        QApplication.instance().screenRemoved.connect(lambda *_: QTimer.singleShot(500, self.apply_layout))

        self._hook_proc = WINEVENTPROC(self.on_winevent)
        self._hooks = [user32.SetWinEventHook(lo, hi, None, self._hook_proc, 0, 0, 0x0002)  # SKIPOWNPROCESS
                       for lo, hi in ((EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND),
                                      (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND),
                                      (EVENT_SYSTEM_CAPTURESTART, EVENT_SYSTEM_CAPTURESTART),
                                      (EVENT_OBJECT_SHOW, EVENT_OBJECT_HIDE))]

        self.beat = time.monotonic()
        self.beat_timer = QTimer(interval=1000, timeout=self.heartbeat)
        self.beat_timer.start()
        self.heartbeat()
        threading.Thread(target=self.watchdog, daemon=True, name="MiniDockWatchdog").start()
        self.poll_timer = QTimer(interval=1000, timeout=self.poll)
        self.poll_timer.start()
        # команды (--quit, --launchpad, …) ждёт отдельный поток — в простое он спит и не тратит ничего
        events = {QUIT_EVENT: quit_event}
        sa = open_security()
        events.update({flag: win32event.CreateEvent(sa, False, False, name) for flag, name in COMMANDS.items()})
        self.waiter = EventWaiter(events)
        self.waiter.fired.connect(self.on_command)
        self.poll()
        self.bar.on_foreground(win32gui.GetForegroundWindow())

    def heartbeat(self):
        self.beat = time.monotonic()
        # Второй сторож — внутри Python на C: срабатывает, даже если зависший вызов держит GIL и поток
        # watchdog() стоит вместе со всеми. Каждую секунду переводим его на 30 с вперёд.
        if sys.stderr is not None:
            try:
                faulthandler.dump_traceback_later(30, file=sys.stderr)
            except Exception:
                pass

    def watchdog(self):
        """Интерфейс не отвечает больше 20 с — записываем в журнал, чем заняты все потоки (один раз)."""
        reported = False
        while True:
            time.sleep(5)
            stuck = time.monotonic() - self.beat > 20
            if stuck and not reported and sys.stderr is not None:
                try:
                    print(f"\n=== MiniDock завис ({time.strftime('%Y-%m-%d %H:%M:%S')}) ===", file=sys.stderr, flush=True)
                    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                except Exception:
                    pass
            reported = stuck

    # -- settings
    def bar_height(self):
        return int(self.cfg["bar_height"])

    def save_soon(self):
        self.save_timer.start()

    def set_cfg(self, key, value):
        self.cfg[key] = value
        self.save_soon()
        self.apply_layout()

    def open_settings(self):
        from .settings import SettingsWindow
        if self.settings is None:
            self.settings = SettingsWindow(self)
        g = QApplication.primaryScreen().geometry()
        self.settings.move(g.center().x() - self.settings.width() // 2, g.center().y() - self.settings.height() // 2)
        self.settings.showNormal()
        self.settings.raise_()
        self.settings.activateWindow()
        # Windows не даёт фоновому процессу выводить окна вперёд — без этого окно открывалось позади программ
        QTimer.singleShot(60, lambda: force_foreground(int(self.settings.winId())) if self.settings else None)

    # -- layout of dock / bar / taskbar / work area
    def apply_layout(self):
        cfg = self.cfg
        GLASS["live"] = bool(cfg["glass_live"])
        GLASS["live_parts"] = tuple(cfg["glass_live_parts"])
        BACKGROUNDS.clear()
        BACKGROUNDS.update(cfg["backgrounds"])
        GLASS["radius"] = float(cfg["glass_radius"])
        if cfg["replace_taskbar"]:
            self.taskbar.hide()
        elif self.taskbar.hidden or cfg.get("taskbar_orig_state") is not None:
            self.taskbar.restore()
        if cfg["menubar"] and not self.fullscreen:
            self.bar.place()
            self.bar.show()
        else:
            self.bar.hide()
        self.place_extra_bars()
        self.dock.place()
        self.place_extra_docks()
        if not self.fullscreen:
            self.dock.show()
        self.dock.apply_glass()
        self.apply_shadows()
        self.update_work_area()
        self.launchpad.update()

    @property
    def switcher(self):
        """Alt+Tab выбранного стиля: «как в macOS» (значки + окна) или «как в Windows» (миниатюры окон)."""
        return self.win_switcher if self.cfg["switcher_style"] == "windows" else self.mac_switcher

    def bars(self):
        return [self.bar] + list(self.extra_bars.values())

    def docks(self):
        return [self.dock] + list(self.extra_docks.values())

    def dock_of(self, item):
        """Док, которому принадлежит иконка (у каждого дока свои)."""
        for d in self.docks():
            if item in d.items() or item in d.all_apps():
                return d
        return self.dock

    def pinned_changed(self, source):
        for d in self.docks():
            if d is not source:
                d.reload_pinned()
        if source is not self.dock or self.extra_docks:
            QTimer.singleShot(0, self.poll)

    def place_extra_docks(self):
        """Док на дополнительных мониторах (настройка «Док на всех мониторах»)."""
        primary = QApplication.primaryScreen()
        want = {}
        if self.cfg["dock_all_screens"]:
            want = {s.name(): s for s in QApplication.screens() if s is not primary}
        for name in list(self.extra_docks):
            d = self.extra_docks[name]
            if name not in want or d.home not in QApplication.screens():
                d.release()
                d.hide()
                d.deleteLater()
                del self.extra_docks[name]
        for name, scr in want.items():
            d = self.extra_docks.get(name)
            if d is None:
                d = self.extra_docks[name] = Dock(self, scr)
            d.place()
            d.show()
            d.apply_glass()

    def place_extra_bars(self):
        """Верхняя панель на дополнительных мониторах (настройка «На всех мониторах»)."""
        primary = QApplication.primaryScreen()
        want = {}
        if self.cfg["menubar"] and self.cfg["bar_all_screens"]:
            want = {s.name(): s for s in QApplication.screens() if s is not primary}
        for name in list(self.extra_bars):
            bar = self.extra_bars[name]
            if name not in want or bar.home not in QApplication.screens():
                bar.release()
                bar.hide()
                bar.deleteLater()
                del self.extra_bars[name]
        for name, scr in want.items():
            bar = self.extra_bars.get(name)
            if bar is None:
                bar = self.extra_bars[name] = MenuBar(self, scr)
                bar.on_foreground(win32gui.GetForegroundWindow())
            bar.show()
            bar.place()

    def apply_shadows(self):
        cfg = self.cfg
        if cfg["shadows_orig"] is None:
            cfg["shadows_orig"] = window_shadows()
            self.save_soon()
        want = bool(cfg["window_shadows"]) and cfg["shadows_orig"]
        if window_shadows() != want:
            set_window_shadows(want)

    def update_work_area(self):
        """Рабочая область: окна разворачиваются ровно под верхнюю панель и до края дока/экрана."""
        cfg = self.cfg
        if not cfg["replace_taskbar"] and not cfg["menubar"]:
            if self.want_area is not None:
                set_work_area(Taskbar.default_work_area())
            self.want_area = None
            return
        dpr = QApplication.primaryScreen().devicePixelRatio()
        mon = primary_monitor_rect()
        l, t, r, b = mon if cfg["replace_taskbar"] else Taskbar.default_work_area()
        if cfg["menubar"]:
            t = mon[1] + round(self.bar_height() * dpr)
        if cfg["replace_taskbar"] and not cfg["autohide"]:
            thick = round(self.dock.reserve_thickness() * dpr)
            if cfg["position"] == "bottom":
                b -= thick
            elif cfg["position"] == "left":
                l += thick
            else:
                r -= thick
        self.want_area = (l, t, r, b)
        if get_work_area() != self.want_area:
            set_work_area(self.want_area)

    def cleanup(self):
        for w in list(self.extra_bars.values()) + list(self.extra_docks.values()):  # вернуть зарезервированное место
            w.release()
        self.animator.cleanup()
        self.keys.unhook()
        if self.taskbar.hidden or self.cfg.get("taskbar_orig_state") is not None:
            self.taskbar.restore()
        if self.want_area is not None:
            set_work_area(Taskbar.default_work_area())
            self.want_area = None
        if self.cfg["shadows_orig"] is not None:
            if window_shadows() != self.cfg["shadows_orig"]:
                set_window_shadows(self.cfg["shadows_orig"])
            self.cfg["shadows_orig"] = None
        save_cfg(self.cfg)

    def peek_taskbar(self):
        """Временно показывает панель задач Windows, повторный вызов — прячет."""
        if not self.cfg["replace_taskbar"]:
            return
        if self.peek_until:
            self.peek_until = 0.0
            self.taskbar.hide()
        else:
            self.peek_until = time.monotonic() + 15
            self.taskbar.restore()

    # -- commands and periodic checks
    def on_command(self, flag):
        if flag == QUIT_EVENT:
            QApplication.quit()
        elif flag == "--launchpad":
            self.launchpad.toggle()
        elif flag == "--settings":
            self.open_settings()
        elif flag == "--spotlight":
            self.spotlight.toggle()
        elif flag == "--test-volume":  # открыть окно звука (для проверки)
            from PySide6.QtCore import QRectF as _R
            from .flyouts import VolumeFlyout
            fly = self.bar.flyouts.get(VolumeFlyout)
            if fly is not None and fly.isVisible():  # уже открыто — нажать «громкость программ»
                fly.mixer_button.toggle()
            else:
                self.bar.toggle_flyout(VolumeFlyout, "vol", _R(self.bar.width() - 420, 0, 30, self.bar.H))
        elif flag == "--show":
            # MiniDock запустили ещё раз: показать док и панель (даже если считали, что идёт игра) и ответить
            self.fullscreen = False
            self.animator.set_enabled(True)
            self.apply_layout()
            self.dock.reveal(3.0)
            signal_running(SHOW_ACK)
        elif flag == "--reveal":
            self.dock.reveal(4.0)
        elif flag == "--test-lp-start":
            prev = self.cfg["lp_mode"]
            self.launchpad.open()
            self.launchpad.set_mode("start")
            QTimer.singleShot(3000, lambda: (self.launchpad.set_mode(prev), self.launchpad.close_animated()))
        elif flag == "--test-spotlight":
            self.spotlight.open()
            self.spotlight.edit.setText("tele")
            QTimer.singleShot(4000, self.spotlight.close_animated)
        elif flag == "--test-switcher":
            # открыть переключатель на самом свежем приложении и перейти к выбору его окон (как ↓, затем →)
            self.switcher.begin(1)
            QTimer.singleShot(150, lambda: self.switcher.step(-1))
            QTimer.singleShot(700, lambda: self.switcher.arrow(0x28))
            QTimer.singleShot(1100, lambda: self.switcher.arrow(0x27))
            QTimer.singleShot(4000, self.switcher.cancel)
        elif flag == "--test-clock":  # открыть календарь под часами (для проверки)
            from PySide6.QtCore import QRectF as _R
            from .flyouts import ClockFlyout
            fly = self.bar.flyouts.get(ClockFlyout)
            if fly is not None and fly.isVisible():
                fly.close()
            else:
                self.bar.toggle_flyout(ClockFlyout, "clock", _R(self.bar.width() - 160, 0, 150, self.bar.H))
        elif flag == "--test-peek":
            # миниатюры первого запущенного приложения, через секунду — его первое окно целиком, ещё через 2 с — выход
            self.on_command("--test-preview")

            def peek():
                pv = self.preview
                if pv.isVisible() and pv.hwnds:
                    pv.hover = (0, "thumb")
                    pv.update_peek()
                    pv.peek_timer.stop()
                    pv.start_peek(pv.hwnds[0])
            QTimer.singleShot(1000, peek)
            QTimer.singleShot(3000, self.preview.stop_peek)
        elif flag == "--test-close":
            # Alt+Tab: навести крестик на окно Блокнота и закрыть его, как кликом (другие окна не трогаем)
            sw = self.switcher
            sw.begin(1)

            def notepad():
                tiles = sw.tiles()
                return next((k for k, (h, _) in enumerate(tiles) if win32gui.GetClassName(h) == "Notepad"), None)

            def aim():
                if sw is self.win_switcher:
                    k = next((k for k, (h, _) in enumerate(sw.wins) if win32gui.GetClassName(h) == "Notepad"), None)
                    if k is not None:
                        sw.hover = sw.close_hot = k
                        sw.changed()
                    return
                k = next((k for k, a in enumerate(sw.apps)
                          if any(win32gui.GetClassName(h) == "Notepad" for h in a["hwnds"])), None)
                if k is not None:
                    sw.hover = k
                    sw.update_thumbs()
                    k = notepad()
                    sw.hover_win, sw.close_hot = k, ("win", k)
                    sw.selection_changed()

            def fire():
                if sw is self.win_switcher:
                    if sw.close_hot is not None:
                        sw.quit_selected(sw.close_hot)
                elif sw.close_hot is not None:
                    sw.close_window(sw.tiles()[sw.close_hot[1]][0])
            QTimer.singleShot(700, aim)
            QTimer.singleShot(1500, fire)
            QTimer.singleShot(3500, sw.cancel)
        elif flag == "--test-restore":
            # развернуть последнее свёрнутое окно так же, как по клику на иконку в доке
            for hwnd in reversed(list(self.animator.min_imgs)):
                if win32gui.IsIconic(hwnd):
                    self.dock.reveal(2)
                    self.animator.restore(hwnd, self.animator.icon_for(hwnd))
                    break
        elif flag == "--test-folder":
            # открыть первую папку дока
            folders = self.dock.folders()
            if folders:
                self.dock.reveal(5)
                QTimer.singleShot(300, lambda: self.dock.folder_popup.open(folders[0]))
                QTimer.singleShot(4000, self.dock.folder_popup.close)
        elif flag == "--test-lp-folder":
            # Launchpad «Все»: сложить два первых приложения в папку и открыть её
            lp = self.launchpad
            lp.open()
            lp.set_mode("all")
            apps = [e for e in lp.top if e.get("kind") != "folder"]
            if len(apps) >= 3 and not any(e.get("kind") == "folder" for e in lp.top):
                lp.put_in_folder(apps[1], apps[0])
                lp.put_in_folder(apps[2], next(e for e in lp.top if e.get("kind") == "folder"))
            QTimer.singleShot(2500, lambda: lp.open_folder(next(e for e in lp.top if e.get("kind") == "folder")))
            QTimer.singleShot(5000, lp.close_animated)
        elif flag == "--test-preview":
            # предпросмотр для первого запущенного приложения в доке, у которого есть окна
            items = [it for it in self.dock.items() if it.hwnds and it.cur is not None]
            # если запущен Блокнот — берём его (проверки «подглядывания» на небольшом окне)
            items.sort(key=lambda it: not any(win32gui.GetClassName(h) == "Notepad" for h in it.hwnds))
            if items:
                it = items[0]
                self.dock.reveal(5)
                w = self.dock.to_win_rect(it.cur.translated(0, self.dock.hide_k * self.dock.hide_distance()))
                tl = self.dock.mapToGlobal(w.topLeft().toPoint())
                from PySide6.QtCore import QRectF
                self.preview.hold_until = time.monotonic() + 4
                self.preview.open(it, QRectF(tl.x(), tl.y(), w.width(), w.height()))
                QTimer.singleShot(4000, self.preview.close_preview)

    def poll(self):
        """Раз в секунду: панель задач, рабочая область, полноэкранные игры, список окон для дока."""
        if self.cfg["replace_taskbar"]:
            if self.peek_until and time.monotonic() > self.peek_until:
                self.peek_until = 0.0
            if not self.peek_until and any(win32gui.IsWindowVisible(h) for h in taskbar_windows()):
                self.taskbar.hide()
        if self.want_area is not None and get_work_area() != self.want_area:
            set_work_area(self.want_area)
        full = foreground_is_fullscreen() and not self.launchpad.isVisible()
        if full != self.fullscreen:
            self.fullscreen = full
            self.animator.set_enabled(not full)
            for w in (self.dock, self.bar):
                if full:
                    w.hide()
                elif w is self.dock or self.cfg["menubar"]:
                    w.show()
        # на дополнительных мониторах — то же самое, но для игры на их собственном экране
        game = fullscreen_monitor() if self.extra_docks or self.extra_bars else None
        for w in list(self.extra_docks.values()) + list(self.extra_bars.values()):
            hide = game is not None and window_monitor(int(w.winId()))[0] == game
            if hide and w.isVisible():
                w.hide()
            elif not hide and not w.isVisible():
                w.show()
        wins = app_windows()
        for d in self.docks():
            d.poll_windows(wins)
        self.animator.remember(wins)

    # -- window events
    def on_winevent(self, hook, event, hwnd, id_object, id_child, thread, ms):
        if id_object != 0 or id_child != 0 or not hwnd:
            return
        try:
            if event == EVENT_SYSTEM_FOREGROUND:
                self.touch_mru(hwnd)
                for bar in self.bars():
                    bar.on_foreground(hwnd)
                QTimer.singleShot(400, lambda: self.animator.take_snapshot(hwnd))
            elif event == EVENT_SYSTEM_CAPTURESTART:
                self.animator.on_press(hwnd)
            elif event == EVENT_SYSTEM_MINIMIZESTART:
                self.animator.on_minimize_start(hwnd)
            elif event == EVENT_SYSTEM_MINIMIZEEND:
                self.animator.on_minimize_end(hwnd)
            elif event == EVENT_OBJECT_SHOW:
                self.animator.on_show(hwnd)
            elif event == EVENT_OBJECT_HIDE:
                self.animator.on_hide(hwnd)
        except Exception:
            pass

    def touch_mru(self, hwnd):
        if user32.GetAncestor(hwnd, 2) != hwnd:  # GA_ROOT: только окна верхнего уровня
            return
        if hwnd in self.mru:
            self.mru.remove(hwnd)
        self.mru.insert(0, hwnd)
        if len(self.mru) > 200:
            self.mru = [h for h in self.mru if win32gui.IsWindow(h)][:150]

    def focus_windows(self, item, hwnds):
        """Клик по иконке запущенной программы: развернуть / переключить окно / свернуть."""
        fg = win32gui.GetForegroundWindow()
        if fg in hwnds:
            if len(hwnds) == 1:
                win32gui.PostMessage(fg, 0x0112, 0xF020, 0)  # WM_SYSCOMMAND, SC_MINIMIZE
                return
            nxt = hwnds[(hwnds.index(fg) + 1) % len(hwnds)]
        else:
            nxt = hwnds[0]
        if win32gui.IsIconic(nxt):
            self.animator.restore(nxt, self.dock_of(item).icon_screen_rect(item))
        else:
            force_foreground(nxt)


class EventWaiter(QObject):
    """Поток, который спит до сигнала от другого процесса (MiniDock.pyw --quit / --launchpad / …)."""
    fired = Signal(str)

    def __init__(self, events):
        super().__init__()
        self.names = list(events)
        self.handles = [events[n] for n in self.names]
        threading.Thread(target=self.run, daemon=True, name="MiniDockCommands").start()

    def run(self):
        while True:
            idx = win32event.WaitForMultipleObjects(self.handles, False, win32event.INFINITE)
            i = idx - win32event.WAIT_OBJECT_0
            if 0 <= i < len(self.names):
                self.fired.emit(self.names[i])
                if self.names[i] == QUIT_EVENT:
                    return  # событие выхода не сбрасывается само — дальше ждать нечего


def signal_running(name):
    try:
        win32event.SetEvent(win32event.OpenEvent(win32event.EVENT_MODIFY_STATE, False, name))
    except Exception:
        pass


def take_over(mutex):
    """MiniDock уже запущен. Просим его показаться; если он завис и не отвечает — закрываем его.
    True — старого экземпляра больше нет, запускаемся сами."""
    ack = win32event.CreateEvent(open_security(), False, False, SHOW_ACK)
    signal_running(COMMANDS["--show"])
    if win32event.WaitForSingleObject(ack, 4000) == win32event.WAIT_OBJECT_0:
        return False  # живой — он сам показался
    import psutil
    me = os.getpid()
    mine = os.path.normcase(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]))
    for proc in psutil.process_iter(["pid", "exe", "cmdline", "cwd"]):
        try:
            if proc.info["pid"] == me:
                continue
            exe = os.path.normcase(proc.info["exe"] or "")
            # путь к скрипту в командной строке бывает относительным — считаем его от папки того процесса
            cwd = proc.info["cwd"] or ""
            cmd = [os.path.normcase(os.path.abspath(os.path.join(cwd, c))) for c in (proc.info["cmdline"] or [])[1:2]]
            if exe == mine or mine in cmd:
                proc.kill()
        except (psutil.Error, OSError, ValueError):
            pass
    # ждём, пока Windows освободит «занято» от завершённого процесса
    return win32event.WaitForSingleObject(mutex, 5000) in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED)


def main(script):
    if "--quit" in sys.argv:
        signal_running(QUIT_EVENT)
        return
    for flag, name in COMMANDS.items():
        if flag in sys.argv:
            signal_running(name)
            return
    try:
        mutex = win32event.CreateMutex(open_security(), False, "MiniDock_SingleInstance")
        exists = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    except Exception:  # «занято» создал экземпляр с другими правами — значит, MiniDock уже запущен
        signal_running(COMMANDS["--show"])
        return
    if exists and not take_over(mutex):
        return
    quit_event = win32event.CreateEvent(open_security(), True, False, QUIT_EVENT)
    win32event.ResetEvent(quit_event)
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    app = App(quit_event, script)
    # Панель задач, рабочая область и прозрачность чужих окон всегда возвращаются при выходе
    atexit.register(app.cleanup)
    qapp.aboutToQuit.connect(app.cleanup)
    code = qapp.exec()
    del mutex
    sys.exit(code)

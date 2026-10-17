"""Живой фон для «стекла»: участок экрана ПОД окнами MiniDock, снимаемый на лету.

Обычный снимок экрана захватывает и сам док, поэтому раньше фон снимался один раз — пока дока на этом месте
нет, — и окна, двигающиеся под доком, «замирали». Лупа Windows (Magnification API) умеет рисовать экран,
пропуская заданные окна: так получаем то, что под доком, до 30 раз в секунду.

Лупа нестабильна рядом с Direct3D и COM в одном процессе, поэтому она работает в отдельном маленьком
процессе-помощнике (MiniDock.exe --live-helper). Задания (какой участок снимать) идут ему через stdin,
готовые размытые кадры — обратно через общую память. Упал помощник — MiniDock перезапускает его, а после
нескольких неудач возвращается к обычному снимку фона.
"""
import ctypes
import json
import mmap
import os
import struct
import subprocess
import sys
import threading
import time
from ctypes import wintypes

import numpy as np

FPS = 30          # пока под окном что-то меняется
IDLE_FPS = 8      # когда картинка под окном стоит на месте — снимаем реже и ничего не отдаём
DOWNSCALE = 4
BUCKET = 128      # окна лупы — размером, округлённым вверх до BUCKET: один и тот же размер служит многим окнам
SLOTS = 8                     # одновременно живых окон («стёкол»)
SLOT_SIZE = 1 << 20           # 1 МБ на кадр (уменьшенный в 4 раза участок экрана)
HEADER = struct.Struct("<IIIiiii")  # номер кадра, ширина, высота, участок экрана (l, t, r, b)


def shared_name(parent_pid):
    return f"MiniDockLiveGlass_{parent_pid}"


def _box_blur(a, r, axis):
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float32)
    n = a.shape[axis]
    if axis == 0:
        return (c[2 * r + 1:2 * r + 1 + n] - c[:n]) / (2 * r + 1)
    return (c[:, 2 * r + 1:2 * r + 1 + n] - c[:, :n]) / (2 * r + 1)


def blur_bgra(a, radius, saturation=1.3):
    """a — float32 (h, w, 3) BGR, уже уменьшенный. Возвращает uint8 (h, w, 4)."""
    r = max(int(radius / 1.7), 1)
    for _ in range(3):
        a = _box_blur(a, r, 1)
        a = _box_blur(a, r, 0)
    if saturation != 1.0:
        lum = a @ np.array([0.114, 0.587, 0.299], dtype=np.float32)
        a = lum[..., None] + (a - lum[..., None]) * saturation
    out = np.empty(a.shape[:2] + (4,), dtype=np.uint8)
    out[..., :3] = np.clip(a, 0, 255)
    out[..., 3] = 255
    return out


# ============================================================ процесс-помощник: лупа Windows

class MAGIMAGEHEADER(ctypes.Structure):
    _fields_ = [("width", ctypes.c_uint), ("height", ctypes.c_uint), ("format", ctypes.c_byte * 16),
                ("stride", ctypes.c_uint), ("offset", ctypes.c_uint), ("cbSize", ctypes.c_size_t)]


SCALING_CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, ctypes.c_void_p, MAGIMAGEHEADER, ctypes.c_void_p,
                                MAGIMAGEHEADER, wintypes.RECT, wintypes.RECT, wintypes.HRGN)
ENUM_CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class Magnifier:
    def __init__(self, parent_pid):
        u = self.user32 = ctypes.WinDLL("user32")
        k = ctypes.WinDLL("kernel32")
        # все типы явно: в 64-битном Python дескрипторы и адреса не влезают в int по умолчанию
        k.GetModuleHandleW.restype = wintypes.HMODULE
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        u.CreateWindowExW.restype = wintypes.HWND
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                      wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        u.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.UINT]
        u.InvalidateRect.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
        u.UpdateWindow.argtypes = [wintypes.HWND]
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                   wintypes.UINT]
        u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.EnumWindows.argtypes = [ENUM_CB, wintypes.LPARAM]
        self.mag = mag = ctypes.WinDLL("Magnification")
        mag.MagSetImageScalingCallback.argtypes = [wintypes.HWND, SCALING_CB]
        mag.MagSetWindowFilterList.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_int,
                                               ctypes.POINTER(wintypes.HWND)]
        mag.MagSetWindowSource.argtypes = [wintypes.HWND, wintypes.RECT]
        if not mag.MagInitialize():
            raise OSError("MagInitialize")
        self.hinst = k.GetModuleHandleW(None)
        # невидимое окно-хозяин (прозрачное, без панели задач и фокуса); лупы — его дочерние окна
        self.host = u.CreateWindowExW(0x80000 | 0x80 | 0x8 | 0x8000000 | 0x20, "Static", "MiniDockLiveGlass",
                                      0x80000000, 0, 0, 64, 64, None, None, self.hinst, None)
        u.SetLayeredWindowAttributes(self.host, 0, 0, 2)
        u.ShowWindow(self.host, 4)
        self._cb = SCALING_CB(self._on_image)
        self.parent_pid = parent_pid
        self.excluded = []
        # Окна лупы по размерам (округлённым до BUCKET), никогда не уничтожаются. Размер окна лупы менять нельзя —
        # Windows падает, а уничтоженное окно лупы оставляет в памяти ~1,3 МБ (утечка в Magnification API):
        # раньше при каждом открытии/росте всплывающего окна помощник прибавлял мегабайты, пока не занимал сотни.
        self.pool = {}  # (ширина, высота) -> окно лупы
        self._got = None
        if self._window(16, 16) is None:
            raise OSError("Magnifier")

    def _window(self, w, h):
        """Окно лупы не меньше w×h из пула: (окно, ширина, высота) или None."""
        key = (-(-w // BUCKET) * BUCKET, -(-h // BUCKET) * BUCKET)
        hwnd = self.pool.get(key)
        if hwnd:
            return hwnd, *key
        hwnd = self.user32.CreateWindowExW(0, "Magnifier", "m", 0x40000000 | 0x10000000, 0, 0, key[0], key[1],
                                           self.host, None, self.hinst, None)
        if not hwnd or not self.mag.MagSetImageScalingCallback(hwnd, self._cb):
            return None
        self.pool[key] = hwnd
        self._apply_filter(hwnd)
        return hwnd, *key

    def _apply_filter(self, hwnd):
        arr = (wintypes.HWND * max(len(self.excluded), 1))(*self.excluded)
        self.mag.MagSetWindowFilterList(hwnd, 0, len(self.excluded), arr)  # MW_FILTERMODE_EXCLUDE

    def exclude_parent_windows(self):
        """Окна MiniDock (док, панель, всплывающие) на снимке быть не должны."""
        mine = []
        pid = wintypes.DWORD()

        def cb(h, _):
            self.user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if pid.value == self.parent_pid and self.user32.IsWindowVisible(h):
                mine.append(h)
            return True
        self.user32.EnumWindows(ENUM_CB(cb), 0)
        self.excluded = mine[:64]
        for hwnd in self.pool.values():
            self._apply_filter(hwnd)

    def _on_image(self, hwnd, src, srch, dst, dsth, unclipped, clipped, dirty):
        try:
            n = srch.stride * srch.height
            self._got = (np.frombuffer((ctypes.c_char * n).from_address(src), np.uint8).copy(),
                         srch.width, srch.height, srch.stride)
        except Exception:
            self._got = None
        return True

    def grab(self, slot, rect):
        """Участок экрана без окон MiniDock, уменьшенный в DOWNSCALE раз: float32 (h, w, 3) или None."""
        l, t, r, b = rect
        w, h = r - l, b - t
        if w < DOWNSCALE or h < DOWNSCALE:
            return None
        win = self._window(w, h)
        if win is None:
            return None
        hwnd, bw, bh = win
        self._got = None
        # снимаем участок размером с окно лупы и обрезаем лишнее справа и снизу
        self.mag.MagSetWindowSource(hwnd, wintypes.RECT(l, t, l + bw, t + bh))
        self.user32.InvalidateRect(hwnd, None, True)
        self.user32.UpdateWindow(hwnd)
        if self._got is None:
            return None
        data, gw, gh, stride = self._got
        gw, gh = min(gw, w), min(gh, h)
        a = data.reshape(-1, stride // 4, 4)[:gh, :gw, :3]
        hh, ww = gh // DOWNSCALE, gw // DOWNSCALE
        a = a[:hh * DOWNSCALE, :ww * DOWNSCALE].astype(np.float32)
        return a.reshape(hh, DOWNSCALE, ww, DOWNSCALE, 3).mean(axis=(1, 3))

    def pump(self):
        msg = wintypes.MSG()
        while self.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))


def helper_main(parent_pid):
    """Процесс-помощник: задания — строками JSON из stdin, кадры — в общую память. Закрылся stdin — выходим."""
    if os.environ.get("MINIDOCK_DEBUG"):
        import faulthandler
        log = open(os.path.join(os.environ.get("TEMP", "."), "minidock_helper.log"), "a", buffering=1)
        sys.stderr = log
        faulthandler.enable(file=log, all_threads=True)
    shm = mmap.mmap(-1, SLOTS * SLOT_SIZE, tagname=shared_name(parent_pid))
    mag = Magnifier(parent_pid)
    jobs = {}
    lock = threading.Lock()
    alive = threading.Event()
    alive.set()

    def read_jobs():
        for line in sys.stdin:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            with lock:
                jobs.clear()
                jobs.update({int(k): v for k, v in data.items()})
        alive.clear()  # MiniDock закрылся
    threading.Thread(target=read_jobs, daemon=True).start()
    sys.stdout.write("ready\n")
    sys.stdout.flush()

    seq = [0] * SLOTS
    last = {}
    still = 0
    excluded_at = 0.0
    while alive.is_set():
        start = time.perf_counter()
        with lock:
            current = dict(jobs)
        # новое окно со «стеклом» — сразу исключаем его из снимка, иначе первые кадры фона покажут его самого
        if start - excluded_at > 0.5 or set(current) != set(last):
            mag.exclude_parent_windows()
            excluded_at = start
        changed = False
        for slot, (l, t, r, b, radius) in current.items():
            arr = mag.grab(slot, (l, t, r, b))
            if arr is None:
                continue
            prev = last.get(slot)
            key = (l, t, r, b, radius)
            if prev is not None and prev[0] == key and prev[1].shape == arr.shape \
                    and float(np.abs(prev[1] - arr).mean()) < 0.4:
                continue  # под окном всё как было
            last[slot] = (key, arr)
            img = blur_bgra(arr, radius)
            h, w = img.shape[:2]
            if HEADER.size + img.nbytes > SLOT_SIZE:
                continue
            base = slot * SLOT_SIZE
            shm[base + HEADER.size:base + HEADER.size + img.nbytes] = img.tobytes()
            seq[slot] += 1
            shm[base:base + HEADER.size] = HEADER.pack(seq[slot], w, h, l, t, r, b)
            changed = True
        for slot in list(last):
            if slot not in current:
                del last[slot]
        still = 0 if changed else still + 1
        mag.pump()
        if not current:
            time.sleep(0.05)
            continue
        fps = FPS if still < 6 else IDLE_FPS
        time.sleep(max(0.0, 1 / fps - (time.perf_counter() - start)))


# ============================================================ сторона MiniDock

def _qt():
    from PySide6.QtCore import QObject, QTimer, Signal
    from PySide6.QtGui import QImage
    return QObject, QTimer, Signal, QImage


if "--live-helper" not in sys.argv:  # помощнику Qt не нужен — не грузим его там
    QObject, QTimer, Signal, QImage = _qt()

    class LiveCapture(QObject):
        frame = Signal(object, QImage, tuple)  # (кто просил, размытый фон, участок экрана в физических пикселях)

        def __init__(self):
            super().__init__()
            self.jobs = {}        # id «стекла» -> (участок, радиус)
            self.slots = {}       # id «стекла» -> ячейка общей памяти
            self.seen = [0] * SLOTS
            self.proc = None
            self.failures = 0
            self.ok = True
            self.shm = mmap.mmap(-1, SLOTS * SLOT_SIZE, tagname=shared_name(os.getpid()))
            self.timer = QTimer(self, interval=1000 // FPS, timeout=self.poll)
            self.start_helper()

        def start_helper(self):
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--live-helper", str(os.getpid())]
            else:
                root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                cmd = [sys.executable, os.path.join(root, "MiniDock.pyw"), "--live-helper", str(os.getpid())]
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=None if os.environ.get("MINIDOCK_DEBUG") else subprocess.DEVNULL,
                                             text=True, bufsize=1,
                                             creationflags=subprocess.CREATE_NO_WINDOW)
                ready = self.proc.stdout.readline().strip() == "ready"  # помощник смог запустить лупу
            except Exception:
                ready = False
            if not ready:
                self.failures += 1
                self.ok = self.failures < 3
                self.proc = None
                return False
            self.send()
            return True

        def alive(self):
            if self.proc is not None and self.proc.poll() is None:
                return True
            if not self.ok:
                return False
            self.failures += 1  # помощник упал — запускаем заново (всего не больше трёх раз)
            self.ok = self.failures < 3
            return self.ok and self.start_helper()

        def send(self):
            if self.proc is None:
                return
            data = {self.slots[cid]: [*rect, radius] for cid, (rect, radius) in self.jobs.items()}
            try:
                self.proc.stdin.write(json.dumps(data) + "\n")
                self.proc.stdin.flush()
            except OSError:
                pass

        def set_job(self, cid, rect, radius):
            if cid not in self.slots:
                free = [s for s in range(SLOTS) if s not in self.slots.values()]
                if not free:
                    return
                self.slots[cid] = free[0]
            new = (tuple(rect), radius)
            if self.jobs.get(cid) == new:
                return
            self.jobs[cid] = new
            if self.alive():
                self.send()
            if not self.timer.isActive():
                self.timer.start()

        def remove(self, cid):
            if self.jobs.pop(cid, None) is not None:
                self.slots.pop(cid, None)
                self.send()
            if not self.jobs:
                self.timer.stop()

        def poll(self):
            if not self.alive():
                self.timer.stop()
                return
            for cid, slot in list(self.slots.items()):
                base = slot * SLOT_SIZE
                seq, w, h, l, t, r, b = HEADER.unpack(self.shm[base:base + HEADER.size])
                if seq == self.seen[slot] or not w or not h:
                    continue
                self.seen[slot] = seq
                data = self.shm[base + HEADER.size:base + HEADER.size + w * h * 4]
                img = QImage(data, w, h, w * 4, QImage.Format_RGB32).copy()
                self.frame.emit(cid, img, (l, t, r, b))


_live = None


def live_capture():
    """Общий захватчик (создаётся при первом обращении). None — живой фон на этом компьютере недоступен."""
    global _live
    if _live is None:
        _live = LiveCapture()
    return _live if _live.ok and _live.proc is not None else None

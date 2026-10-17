"""Обёртки над Windows API: окна, панель задач, трей, иконки, размытие, рабочая область."""
import ctypes
import os
import sys
import winreg
from ctypes import wintypes
from pathlib import Path

import psutil
import win32api
import win32com.client
import win32con
import win32gui
import win32process
from PySide6.QtCore import QFileInfo, QSize, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QFileIconProvider

from .config import APP_NAME, save_cfg

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

user32.PrivateExtractIconsW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.UINT),
                                        wintypes.UINT, wintypes.UINT]
user32.PrivateExtractIconsW.restype = wintypes.UINT
user32.GetMenu.argtypes = [wintypes.HWND]
user32.GetMenu.restype = ctypes.c_void_p
user32.GetSubMenu.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetSubMenu.restype = ctypes.c_void_p
user32.GetMenuItemCount.argtypes = [ctypes.c_void_p]
user32.GetMenuItemID.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetMenuItemID.restype = wintypes.UINT
user32.GetMenuState.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
user32.GetMenuState.restype = wintypes.UINT
user32.GetMenuStringW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT]
user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                       wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetKeyboardLayout.restype = ctypes.c_void_p
user32.GetKeyboardLayoutList.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.VirtualAllocEx.restype = ctypes.c_void_p
kernel32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD,
                                    wintypes.DWORD]
kernel32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

IGNORED_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


# ---------------------------------------------------------------- files and icons

_shell = None


def resolve_lnk(path):
    """Возвращает (target, icon_file, icon_index) для ярлыка."""
    global _shell
    if _shell is None:
        _shell = win32com.client.Dispatch("WScript.Shell")
    try:
        sc = _shell.CreateShortCut(path)
        target = sc.Targetpath or ""
        loc = sc.IconLocation or ""
    except Exception:
        return "", "", 0
    icon_file, _, idx = loc.rpartition(",")
    icon_file = os.path.expandvars(icon_file)
    try:
        idx = int(idx)
    except ValueError:
        idx = 0
    if not icon_file or not os.path.exists(icon_file):
        icon_file, idx = (target, 0) if target and os.path.exists(target) else ("", 0)
    return target, icon_file, idx


def extract_icon(file, index=0, size=256):
    hicon = wintypes.HICON()
    icon_id = wintypes.UINT()
    n = user32.PrivateExtractIconsW(file, index, size, size, ctypes.byref(hicon), ctypes.byref(icon_id), 1, 0)
    if n == 0 or n == 0xFFFFFFFF or not hicon.value:
        return None
    try:
        img = QImage.fromHICON(hicon.value)
    finally:
        user32.DestroyIcon(hicon)
    return None if img.isNull() else QPixmap.fromImage(img)


def file_icon(path):
    pix = None
    lower = path.lower()
    if lower.endswith(".lnk"):
        _, icon_file, idx = resolve_lnk(path)
        if icon_file:
            pix = extract_icon(icon_file, idx)
    elif lower.endswith((".exe", ".ico", ".dll")):
        pix = extract_icon(path, 0)
    if pix is None:
        icon = QFileIconProvider().icon(QFileInfo(path))
        sizes = icon.availableSizes()
        best = max(sizes, key=lambda s: s.width()) if sizes else QSize(256, 256)
        pix = icon.pixmap(best)
    return pix


_desc_cache = {}


def exe_description(exe):
    if exe in _desc_cache:
        return _desc_cache[exe]
    desc = None
    try:
        lang, cp = win32api.GetFileVersionInfo(exe, "\\VarFileInfo\\Translation")[0]
        desc = win32api.GetFileVersionInfo(exe, f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription")
        desc = desc.strip() or None
    except Exception:
        pass
    _desc_cache[exe] = desc
    return desc


def app_name_for(exe):
    return exe_description(exe) or Path(exe).stem


def default_pinned():
    pinned = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")]
    wanted = ["Google Chrome", "Microsoft Edge", "Firefox", "Telegram", "Discord", "Steam",
              "Visual Studio Code", "Spotify"]
    roots = [Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs",
             Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs"]
    found = {}
    for root in roots:
        for lnk in root.rglob("*.lnk"):
            if lnk.stem in wanted and lnk.stem not in found:
                found[lnk.stem] = str(lnk)
    return pinned + [found[name] for name in wanted if name in found]


# -- большие иконки оболочки (jumbo image list) — для Launchpad, работает в фоновом потоке

class GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]


IID_IImageList = GUID(0x46EB5926, 0x582E, 0x4017, (ctypes.c_ubyte * 8)(0x9F, 0xDF, 0xE8, 0x99, 0x8D, 0xAA, 0x09, 0x50))
_GETICON = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint,
                              ctypes.POINTER(wintypes.HICON))


def _image_list(size_id):
    ptr = ctypes.c_void_p()
    if shell32.SHGetImageList(size_id, ctypes.byref(IID_IImageList), ctypes.byref(ptr)) != 0 or not ptr:
        return None, None
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ptr, _GETICON(vtbl[10])  # IImageList::GetIcon


def _icon_from_list(size_id, index):
    lst, get_icon = _image_list(size_id)
    if not lst:
        return None
    h = wintypes.HICON()
    if get_icon(lst, index, 1, ctypes.byref(h)) != 0 or not h.value:  # ILD_TRANSPARENT
        return None
    try:
        img = QImage.fromHICON(h.value)
    finally:
        user32.DestroyIcon(h)
    return None if img.isNull() else img


def shell_icon_image(sys_index):
    """QImage иконки из системного списка: 256 px, а если у программы маленькая иконка — 48 px."""
    img = _icon_from_list(4, sys_index)  # SHIL_JUMBO
    if img is not None:
        # маленькие иконки рисуются в левом верхнем углу 256×256 — проверяем, есть ли что-то дальше 64 px
        big = any(img.pixelColor(x, y).alpha() > 0 for x in range(72, 256, 23) for y in range(72, 256, 23))
        if big:
            return img
    return _icon_from_list(2, sys_index) or img  # SHIL_EXTRALARGE


def enum_start_apps():
    """Все приложения меню «Пуск» (включая UWP): [(name, parsing_name, QImage)]. Вызывать в фоне."""
    import pythoncom
    from win32com.shell import shell, shellcon
    pythoncom.CoInitialize()
    desktop = shell.SHGetDesktopFolder()
    _, pidl, _ = desktop.ParseDisplayName(0, None, "shell:AppsFolder")
    folder = desktop.BindToObject(pidl, None, shell.IID_IShellFolder)
    out = []
    for child in folder.EnumObjects(0, shellcon.SHCONTF_NONFOLDERS | shellcon.SHCONTF_FOLDERS):
        try:
            name = folder.GetDisplayNameOf(child, shellcon.SHGDN_NORMAL)
            parse = folder.GetDisplayNameOf(child, shellcon.SHGDN_FORPARSING)
            _, info = shell.SHGetFileInfo(pidl + child, 0, shellcon.SHGFI_PIDL | shellcon.SHGFI_SYSICONINDEX)
            out.append((name, parse, info[1]))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- windows

def window_exe(hwnd):
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).exe().lower()
    except Exception:
        return ""


def is_cloaked(hwnd):
    val = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(val), ctypes.sizeof(val))
    return val.value != 0


def is_app_window(hwnd, need_visible=True):
    """Окно, которое показывалось бы на панели задач."""
    try:
        if need_visible and not win32gui.IsWindowVisible(hwnd):
            return False
        if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
            return False
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex & win32con.WS_EX_TOOLWINDOW and not ex & win32con.WS_EX_APPWINDOW:
            return False
        if not win32gui.GetWindowText(hwnd) or win32gui.GetClassName(hwnd) in IGNORED_CLASSES:
            return False
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return pid != os.getpid()
    except Exception:
        return False


_pid_exe = {}  # pid -> путь к exe: процесс не меняет свой exe, поэтому узнаём один раз


_frame_exe = {}  # окно ApplicationFrameHost -> exe программы внутри (запоминаем: внутреннее окно то есть, то нет)


def _exe_of_pid(pid, seen):
    seen.add(pid)
    if pid not in _pid_exe:
        try:
            _pid_exe[pid] = psutil.Process(pid).exe().lower()
        except (psutil.Error, OSError):
            _pid_exe[pid] = None
    return _pid_exe[pid]


def _frame_app(hwnd, frame_pid, seen):
    """Программа Store в окне-рамке ApplicationFrameHost (Параметры, Калькулятор…): процесс её
    вложенного окна Windows.UI.Core.CoreWindow. Пока программа «спит», вложенного окна нет — берём запомненное."""
    found = []

    def child(h, _):
        _, pid = win32process.GetWindowThreadProcessId(h)
        if pid != frame_pid:
            found.append(pid)
        return True
    try:
        win32gui.EnumChildWindows(hwnd, child, None)
    except Exception:
        pass
    for pid in found:
        exe = _exe_of_pid(pid, seen)
        if exe:
            _frame_exe[hwnd] = exe
            return exe
    return _frame_exe.get(hwnd)


def app_windows():
    """{exe_path_lower: [hwnd, ...]} в порядке z-order (сверху вниз)."""
    result = {}
    seen = set()
    frames = set()

    def cb(hwnd, _):
        if not is_app_window(hwnd) or is_cloaked(hwnd):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe = _exe_of_pid(pid, seen)
        if exe and exe.endswith("\\applicationframehost.exe"):
            frames.add(hwnd)
            exe = _frame_app(hwnd, pid, seen)  # нет программы внутри — рамку не показываем вовсе
        if exe:
            result.setdefault(exe, []).append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    for pid in list(_pid_exe):  # завершившиеся процессы забываем (их номер может достаться другому)
        if pid not in seen:
            del _pid_exe[pid]
    for h in list(_frame_exe):
        if h not in frames and not win32gui.IsWindow(h):
            del _frame_exe[h]
    return result


def press_keys(*vks):
    for vk in vks:
        win32api.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)


def try_foreground(hwnd):
    try:
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def force_foreground(hwnd):
    """Выводит окно на передний план, обходя ограничения Windows на смену фокуса."""
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        return
    if try_foreground(hwnd):
        return
    fg = win32gui.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    me = kernel32.GetCurrentThreadId()
    attached = bool(fg_thread) and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True)
    try:
        win32gui.BringWindowToTop(hwnd)
        if not try_foreground(hwnd):
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            try_foreground(hwnd)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    except Exception:
        pass
    finally:
        if attached:
            user32.AttachThreadInput(me, fg_thread, False)


def syscommand(hwnd, cmd):
    win32gui.PostMessage(hwnd, win32con.WM_SYSCOMMAND, cmd, 0)


def foreground_is_fullscreen():
    fg = win32gui.GetForegroundWindow()
    if not fg or win32gui.GetClassName(fg) in IGNORED_CLASSES:
        return False
    _, pid = win32process.GetWindowThreadProcessId(fg)
    if pid == os.getpid():
        return False
    try:
        info = win32api.GetMonitorInfo(win32api.MonitorFromWindow(fg))
        if not info["Flags"] & 1:  # MONITORINFOF_PRIMARY: док и панель живут на основном мониторе
            return False
        if user32.IsZoomed(fg):  # обычное развёрнутое окно — не игра
            return False
        if win32gui.GetWindowLong(fg, win32con.GWL_STYLE) & win32con.WS_CAPTION == win32con.WS_CAPTION:
            return False  # окно с заголовком — не полноэкранное приложение
        rect = win32gui.GetWindowRect(fg)
    except Exception:
        return False
    mon = info["Monitor"]
    return rect[0] <= mon[0] and rect[1] <= mon[1] and rect[2] >= mon[2] and rect[3] >= mon[3]


def window_monitor(hwnd):
    """(дескриптор монитора, его прямоугольник в физических пикселях) для окна."""
    mon = win32api.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    return int(mon), tuple(win32api.GetMonitorInfo(mon)["Monitor"])


def point_monitor(x, y):
    return int(win32api.MonitorFromPoint((int(x), int(y)), 2))


def fullscreen_monitor():
    """Монитор, на котором сейчас полноэкранная игра/видео (окно без заголовка на весь экран), или None."""
    fg = win32gui.GetForegroundWindow()
    if not fg or win32gui.GetClassName(fg) in IGNORED_CLASSES:
        return None
    _, pid = win32process.GetWindowThreadProcessId(fg)
    if pid == os.getpid():
        return None
    try:
        if user32.IsZoomed(fg):
            return None
        if win32gui.GetWindowLong(fg, win32con.GWL_STYLE) & win32con.WS_CAPTION == win32con.WS_CAPTION:
            return None
        mon = win32api.MonitorFromWindow(fg)
        m = win32api.GetMonitorInfo(mon)["Monitor"]
        rect = win32gui.GetWindowRect(fg)
    except Exception:
        return None
    if rect[0] <= m[0] and rect[1] <= m[1] and rect[2] >= m[2] and rect[3] >= m[3]:
        return int(mon)
    return None


def primary_monitor_rect():
    return win32api.GetMonitorInfo(win32api.MonitorFromPoint((0, 0), 1))["Monitor"]


def get_work_area():
    rc = wintypes.RECT()
    user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rc), 0)  # SPI_GETWORKAREA
    return rc.left, rc.top, rc.right, rc.bottom


def set_work_area(rect):
    rc = wintypes.RECT(*rect)
    # Без SPIF_SENDCHANGE: синхронная рассылка срывается, если какая-то программа зависла.
    user32.SystemParametersInfoW(0x2F, 0, ctypes.byref(rc), 0)  # SPI_SETWORKAREA
    user32.PostMessageW(0xFFFF, win32con.WM_SETTINGCHANGE, 0x2F, 0)  # HWND_BROADCAST, асинхронно


def window_shadows():
    on = wintypes.BOOL()
    user32.SystemParametersInfoW(0x1024, 0, ctypes.byref(on), 0)  # SPI_GETDROPSHADOW
    return bool(on.value)


def set_window_shadows(on):
    """Тени под окнами (как галочка «Отображать тени, отбрасываемые окнами» в параметрах быстродействия)."""
    user32.SystemParametersInfoW(0x1025, 0, ctypes.c_void_p(1 if on else 0), 0)  # SPI_SETDROPSHADOW
    user32.PostMessageW(0xFFFF, win32con.WM_SETTINGCHANGE, 0x1025, 0)  # асинхронно, как и рабочая область


def visible_frame(hwnd):
    rect = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
        return rect.left, rect.top, rect.right, rect.bottom
    return win32gui.GetWindowRect(hwnd)


def window_geometry(hwnd):
    """(видимая рамка, отступы невидимой рамки) в физических пикселях, либо None для свёрнутого окна."""
    try:
        if win32gui.IsIconic(hwnd):
            return None
        wl, wt, wr, wb = win32gui.GetWindowRect(hwnd)
        fl, ft, fr, fb = visible_frame(hwnd)
    except Exception:
        return None
    if wl <= -30000 or fr - fl < 8 or fb - ft < 8:
        return None
    return (fl, ft, fr, fb), (fl - wl, ft - wt, wr - fr, wb - fb)


def window_aspect(hwnd):
    """Пропорции окна (ширина / высота) — и для свёрнутого (по его обычному положению)."""
    try:
        if win32gui.IsIconic(hwnd):
            flags, _, _, _, (l, t, r, b) = win32gui.GetWindowPlacement(hwnd)
            if flags & 2:  # развернётся на весь экран
                l, t, r, b = get_work_area()
        else:
            l, t, r, b = visible_frame(hwnd)
        if r - l > 10 and b - t > 10:
            return max(0.55, min((r - l) / (b - t), 2.4))
    except Exception:
        pass
    return 16 / 10


def make_invisible(hwnd):
    """Делает чужое окно полностью прозрачным (оно остаётся «живым» для DWM-миниатюр).

    Возвращает исходный расширенный стиль, чтобы потом вернуть как было, или None,
    если окно само управляет прозрачностью и трогать его нельзя.
    """
    try:
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex & win32con.WS_EX_LAYERED:
            return None
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex | win32con.WS_EX_LAYERED)
        win32gui.SetLayeredWindowAttributes(hwnd, 0, 0, win32con.LWA_ALPHA)
        return ex
    except Exception:
        return None


def make_visible(hwnd, ex):
    try:
        win32gui.SetLayeredWindowAttributes(hwnd, 0, 255, win32con.LWA_ALPHA)
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)
        win32gui.RedrawWindow(hwnd, None, None, win32con.RDW_INVALIDATE | win32con.RDW_FRAME
                              | win32con.RDW_ALLCHILDREN)
    except Exception:
        pass


def restored_frame(hwnd, known=None):
    """Где окно окажется после разворачивания: (рамка, отступы) в физических пикселях."""
    if known:
        return known
    try:
        flags, _, _, _, normal = win32gui.GetWindowPlacement(hwnd)
    except Exception:
        return None
    wl, wt, wr, wb = get_work_area()
    if flags & 2:  # WPF_RESTORETOMAXIMIZED
        return (wl, wt, wr, wb), (8, 8, 8, 8)
    l, t, r, b = normal
    return (l + wl + 7, t + wt, r + wl - 7, b + wt - 7), (7, 0, 7, 7)


# -- снимок окна (для анимации закрытия)

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def snapshot(hwnd, max_w=4096):
    """(QImage, (l, t, r, b) в физических пикселях) или None."""
    try:
        wl, wt, wr, wb = win32gui.GetWindowRect(hwnd)
        fl, ft, fr, fb = visible_frame(hwnd)
    except Exception:
        return None
    w, h = wr - wl, wb - wt
    if w <= 8 or h <= 8 or w > 12000 or h > 12000 or wl <= -30000:
        return None
    screen_dc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen_dc)
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth, bmi.biHeight, bmi.biPlanes, bmi.biBitCount = w, -h, 1, 32
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(mem, hbmp)
    try:
        ok = user32.PrintWindow(hwnd, mem, 2)  # PW_RENDERFULLCONTENT
        img = QImage(ctypes.string_at(bits, w * h * 4), w, h, w * 4, QImage.Format_RGB32).copy()
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen_dc)
    if not ok:
        return None
    img = img.copy(fl - wl, ft - wt, fr - fl, fb - ft)
    step_x, step_y = max(img.width() // 7, 1), max(img.height() // 7, 1)
    if all(img.pixel(x, y) & 0xFFFFFF == 0 for x in range(2, img.width(), step_x)
           for y in range(2, img.height(), step_y)):
        return None
    if img.width() > max_w:
        img = img.scaledToWidth(max_w, Qt.SmoothTransformation)
    return img, (fl, ft, fr, fb)


gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC,
                         ctypes.c_int, ctypes.c_int, wintypes.DWORD]


def grab_screen(rect):
    """QImage участка экрана (уже скомпонованного DWM) в физических пикселях."""
    l, t, r, b = rect
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return None
    screen_dc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen_dc)
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth, bmi.biHeight, bmi.biPlanes, bmi.biBitCount = w, -h, 1, 32
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(mem, hbmp)
    try:
        gdi32.BitBlt(mem, 0, 0, w, h, screen_dc, l, t, 0x00CC0020 | 0x40000000)  # SRCCOPY | CAPTUREBLT
        img = QImage(ctypes.string_at(bits, w * h * 4), w, h, w * 4, QImage.Format_RGB32).copy()
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen_dc)
    return img


gdi32.StretchBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetBrushOrgEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]


def grab_screen_scaled(rect, factor):
    """Участок экрана, сразу уменьшенный в factor раз (HALFTONE — усреднение, без «лесенки»).

    Возвращает (bytes BGRA, ширина, высота).
    """
    l, t, r, b = rect
    w, h = r - l, b - t
    sw, sh = max(w // factor, 2), max(h // factor, 2)
    screen_dc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen_dc)
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth, bmi.biHeight, bmi.biPlanes, bmi.biBitCount = sw, -sh, 1, 32
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(mem, hbmp)
    try:
        gdi32.SetStretchBltMode(mem, 4)  # HALFTONE
        gdi32.SetBrushOrgEx(mem, 0, 0, None)
        gdi32.StretchBlt(mem, 0, 0, sw, sh, screen_dc, l, t, w, h, 0x00CC0020 | 0x40000000)
        data = ctypes.string_at(bits, sw * sh * 4)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen_dc)
    return data, sw, sh


# ---------------------------------------------------------------- keyboard layouts

def lang_name(langid, kind=0x59):
    buf = ctypes.create_unicode_buffer(64)
    kernel32.GetLocaleInfoW(langid, kind, buf, 64)
    return buf.value


def keyboard_lang():
    fg = win32gui.GetForegroundWindow()
    tid = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
    lang = (user32.GetKeyboardLayout(tid) or 0) & 0xFFFF
    return lang_name(lang).upper() or "??"


def keyboard_layouts():
    n = user32.GetKeyboardLayoutList(0, None)
    buf = (ctypes.c_void_p * max(n, 1))()
    n = user32.GetKeyboardLayoutList(n, buf)
    return [buf[i] or 0 for i in range(n)]


# ---------------------------------------------------------------- blur (DWM accent, Windows 10)

class ACCENT_POLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_size_t)]


def enable_blur(hwnd, on=True):
    accent = ACCENT_POLICY(3 if on else 0, 0, 0, 0)  # ACCENT_ENABLE_BLURBEHIND / DISABLED
    data = WINCOMPATTRDATA(19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p), ctypes.sizeof(accent))
    user32.SetWindowCompositionAttribute(wintypes.HWND(hwnd), ctypes.byref(data))


def round_window(widget, radius):
    """Скругляет окно (и размытие под ним) регионом."""
    dpr = widget.devicePixelRatioF()
    w, h = round(widget.width() * dpr), round(widget.height() * dpr)
    r = round(radius * dpr * 2)
    win32gui.SetWindowRgn(int(widget.winId()), win32gui.CreateRoundRectRgn(0, 0, w + 1, h + 1, r, r), True)


class MARGINS(ctypes.Structure):
    _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]


def native_overlay_window(class_name, ex_extra=0, topmost=True):
    """Нативное прозрачное окно (для размытия и DWM-миниатюр), по умолчанию поверх всего."""
    proc = lambda h, m, w, l: win32gui.DefWindowProc(h, m, w, l)  # noqa: E731
    wc = win32gui.WNDCLASS()
    wc.lpszClassName = class_name
    wc.lpfnWndProc = proc
    wc.hbrBackground = win32gui.GetStockObject(win32con.BLACK_BRUSH)
    try:
        win32gui.RegisterClass(wc)
    except Exception:
        pass
    ex = (win32con.WS_EX_TOPMOST if topmost else 0) | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE | ex_extra
    hwnd = win32gui.CreateWindowEx(ex, class_name, class_name, win32con.WS_POPUP, 0, 0, 10, 10, 0, 0, 0, None)
    m = MARGINS(-1, -1, -1, -1)
    dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(m))
    return hwnd, proc


# ---------------------------------------------------------------- taskbar

class APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uCallbackMessage", wintypes.UINT),
                ("uEdge", wintypes.UINT), ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]


shell32.SHAppBarMessage.argtypes = [wintypes.DWORD, ctypes.POINTER(APPBARDATA)]
shell32.SHAppBarMessage.restype = ctypes.c_size_t
ABS_AUTOHIDE = 1


def appbar_state(hwnd, new_state=None):
    abd = APPBARDATA()
    abd.cbSize = ctypes.sizeof(APPBARDATA)
    abd.hWnd = hwnd
    if new_state is None:
        return int(shell32.SHAppBarMessage(4, ctypes.byref(abd)))  # ABM_GETSTATE
    abd.lParam = new_state
    shell32.SHAppBarMessage(10, ctypes.byref(abd))  # ABM_SETSTATE


def appbar_reserve(hwnd, rect, edge=1):
    """Зарезервировать полосу rect (l, t, r, b, физические пиксели) у верхнего края монитора:
    развёрнутые окна на этом мониторе не будут заходить под панель."""
    abd = APPBARDATA()
    abd.cbSize = ctypes.sizeof(APPBARDATA)
    abd.hWnd = hwnd
    abd.uCallbackMessage = 0x0400 + 77
    shell32.SHAppBarMessage(0, ctypes.byref(abd))  # ABM_NEW (повторная регистрация безвредна)
    abd.uEdge = edge  # 0 — слева, 1 — сверху, 2 — справа, 3 — снизу
    abd.rc = wintypes.RECT(*rect)
    shell32.SHAppBarMessage(2, ctypes.byref(abd))  # ABM_QUERYPOS
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    if edge == 0:
        abd.rc.right = abd.rc.left + w
    elif edge == 2:
        abd.rc.left = abd.rc.right - w
    elif edge == 3:
        abd.rc.top = abd.rc.bottom - h
    else:
        abd.rc.bottom = abd.rc.top + h
    shell32.SHAppBarMessage(3, ctypes.byref(abd))  # ABM_SETPOS


def appbar_release(hwnd):
    abd = APPBARDATA()
    abd.cbSize = ctypes.sizeof(APPBARDATA)
    abd.hWnd = hwnd
    shell32.SHAppBarMessage(1, ctypes.byref(abd))  # ABM_REMOVE


def taskbar_windows():
    hwnds = []
    win32gui.EnumWindows(lambda h, _: hwnds.append(h) if win32gui.GetClassName(h) in
                         ("Shell_TrayWnd", "Shell_SecondaryTrayWnd") else None, None)
    return hwnds


class Taskbar:
    """Прячет панель задач (автоскрытие + скрытие окна) и возвращает исходное состояние."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.hidden = False

    def hide(self):
        tray = win32gui.FindWindow("Shell_TrayWnd", None)
        if not tray:
            return
        if self.cfg.get("taskbar_orig_state") is None:
            self.cfg["taskbar_orig_state"] = appbar_state(tray)
            save_cfg(self.cfg)
        if not appbar_state(tray) & ABS_AUTOHIDE:
            appbar_state(tray, ABS_AUTOHIDE)
        for h in taskbar_windows():
            if win32gui.IsWindowVisible(h):
                win32gui.ShowWindow(h, win32con.SW_HIDE)
        self.hidden = True

    def restore(self):
        tray = win32gui.FindWindow("Shell_TrayWnd", None)
        orig = self.cfg.get("taskbar_orig_state")
        if tray and orig is not None:
            appbar_state(tray, orig)
            self.cfg["taskbar_orig_state"] = None
            save_cfg(self.cfg)
        for h in taskbar_windows():
            win32gui.ShowWindow(h, win32con.SW_SHOW)
        self.hidden = False

    @staticmethod
    def default_work_area():
        """Рабочая область «как у Windows»: экран минус видимая панель задач."""
        l, t, r, b = primary_monitor_rect()
        tray = win32gui.FindWindow("Shell_TrayWnd", None)
        if tray and win32gui.IsWindowVisible(tray) and not appbar_state(tray) & ABS_AUTOHIDE:
            tl, tt, tr, tb = win32gui.GetWindowRect(tray)
            if tt > (t + b) / 2:
                b = min(b, tt)
            elif tb < (t + b) / 2 and tr - tl > (r - l) / 2:
                t = max(t, tb)
            elif tl > (l + r) / 2:
                r = min(r, tl)
            else:
                l = max(l, tr)
        return l, t, r, b


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def launch_command(script):
    """Как запустить MiniDock: собранный MiniDock.exe — напрямую, из исходников — через pythonw."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw}" "{script}"'


def set_autostart(enabled, script):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, launch_command(script))
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


# ---------------------------------------------------------------- tray icons of the hidden taskbar

class TBBUTTON(ctypes.Structure):
    _fields_ = [("iBitmap", ctypes.c_int), ("idCommand", ctypes.c_int), ("fsState", ctypes.c_ubyte),
                ("fsStyle", ctypes.c_ubyte), ("bReserved", ctypes.c_ubyte * 6), ("dwData", ctypes.c_uint64),
                ("iString", ctypes.c_int64)]


class TRAYDATA(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_uint64), ("uID", ctypes.c_uint), ("uCallbackMessage", ctypes.c_uint),
                ("reserved", ctypes.c_uint * 2), ("hIcon", ctypes.c_uint64)]


def tray_toolbars():
    out = []
    tray = win32gui.FindWindow("Shell_TrayWnd", None)
    notify = win32gui.FindWindowEx(tray, 0, "TrayNotifyWnd", None) if tray else 0
    pager = win32gui.FindWindowEx(notify, 0, "SysPager", None) if notify else 0
    tb = win32gui.FindWindowEx(pager, 0, "ToolbarWindow32", None) if pager else 0
    if tb:
        out.append(tb)
    overflow = win32gui.FindWindow("NotifyIconOverflowWindow", None)
    tb2 = win32gui.FindWindowEx(overflow, 0, "ToolbarWindow32", None) if overflow else 0
    if tb2:
        out.append(tb2)
    return out


def tray_icons():
    """[(hwnd, uid, callback_msg, hicon, tooltip)] — значки области уведомлений."""
    icons, seen = [], set()
    for tb in tray_toolbars():
        _, pid = win32process.GetWindowThreadProcessId(tb)
        hp = kernel32.OpenProcess(0x0008 | 0x0010 | 0x0020 | 0x0400, False, pid)
        if not hp:
            continue
        mem = kernel32.VirtualAllocEx(hp, None, 4096, 0x1000, 0x04)
        try:
            for i in range(user32.SendMessageW(tb, 0x418, 0, 0)):  # TB_BUTTONCOUNT
                user32.SendMessageW(tb, 0x417, i, mem)  # TB_GETBUTTON
                btn = TBBUTTON()
                kernel32.ReadProcessMemory(hp, ctypes.c_void_p(mem), ctypes.byref(btn), ctypes.sizeof(btn), None)
                if btn.fsState & 0x08 or not btn.dwData:  # TBSTATE_HIDDEN
                    continue
                td = TRAYDATA()
                kernel32.ReadProcessMemory(hp, ctypes.c_void_p(btn.dwData), ctypes.byref(td), ctypes.sizeof(td), None)
                key = (td.hwnd, td.uID)
                if not td.hwnd or key in seen or not win32gui.IsWindow(td.hwnd):
                    continue
                seen.add(key)
                n = user32.SendMessageW(tb, 0x44B, btn.idCommand, mem)  # TB_GETBUTTONTEXTW
                text = ""
                if n > 0:
                    buf = ctypes.create_unicode_buffer(1024)
                    kernel32.ReadProcessMemory(hp, ctypes.c_void_p(mem), buf, min(n + 1, 1023) * 2, None)
                    text = buf.value
                icons.append((td.hwnd, td.uID, td.uCallbackMessage, td.hIcon, text))
        finally:
            kernel32.VirtualFreeEx(hp, ctypes.c_void_p(mem), 0, 0x8000)
            kernel32.CloseHandle(hp)
    return icons


def tray_click(hwnd, uid, msg, right):
    """Передаёт программе клик по её значку в трее."""
    x, y = win32api.GetCursorPos()
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        user32.AllowSetForegroundWindow(pid)
    except Exception:
        pass
    events = (0x204, 0x205) if right else (0x201, 0x202)  # WM_[RL]BUTTONDOWN/UP
    for ev in events:
        user32.PostMessageW(hwnd, msg, uid, ev)
    if right:  # программы с NOTIFYICON_VERSION_4 ждут WM_CONTEXTMENU и координаты в wParam
        user32.PostMessageW(hwnd, msg, (y << 16) | (x & 0xFFFF), 0x007B | (uid << 16))
    else:
        user32.PostMessageW(hwnd, msg, (y << 16) | (x & 0xFFFF), 0x0400 | (uid << 16))  # NIN_SELECT


# ---------------------------------------------------------------- hooks

WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
                                  wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WINEVENTPROC,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
EVENT_SYSTEM_CAPTURESTART = 0x0008
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


LowLevelKeyboardProc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, LowLevelKeyboardProc, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HANDLE
user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]

VK_TAB, VK_ESCAPE, VK_RETURN, VK_LEFT, VK_RIGHT = 0x09, 0x1B, 0x0D, 0x25, 0x27
VK_LWIN, VK_RWIN, VK_SHIFT, VK_LSHIFT, VK_RSHIFT = 0x5B, 0x5C, 0x10, 0xA0, 0xA1
VK_MENU, VK_LMENU, VK_RMENU = 0x12, 0xA4, 0xA5
VK_DUMMY = 0xE8  # неназначенная клавиша: гасит меню Alt и «Пуск» по одиночному Win


user32.ToUnicodeEx.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_char_p, wintypes.LPWSTR, ctypes.c_int,
                               wintypes.UINT, wintypes.HKL]


def key_text(vk, scan):
    """Текст клавиши vk с учётом Shift/Caps Lock и раскладки активного окна ('' — не символ)."""
    state = (ctypes.c_ubyte * 256)()
    for k in (0x10, 0xA0, 0xA1):  # Shift
        if user32.GetAsyncKeyState(k) & 0x8000:
            state[k] = 0x80
    if user32.GetKeyState(0x14) & 1:  # Caps Lock
        state[0x14] = 0x01
    fg = user32.GetForegroundWindow()
    layout = user32.GetKeyboardLayout(user32.GetWindowThreadProcessId(fg, None) if fg else 0)
    buf = ctypes.create_unicode_buffer(8)
    # флаг 4 — не трогать состояние «мёртвых» клавиш в системе (Windows 10 1607+)
    n = user32.ToUnicodeEx(vk, scan, ctypes.cast(state, ctypes.c_char_p), buf, 8, 4, layout)
    return buf.value[:n] if n > 0 and buf.value[:n].isprintable() else ""


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def inject_key(vk, up=False):
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP if up else 0, 0)


def inject_dummy():
    inject_key(VK_DUMMY)
    inject_key(VK_DUMMY, up=True)

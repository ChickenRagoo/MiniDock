//! Обёртки над Win32: окна MiniDock, система, оболочка, DWM.

pub mod appbar;
pub mod dwm;
pub mod hooks;
pub mod shell;
pub mod surface;
pub mod sys;

use windows::core::{w, HSTRING, PCWSTR};
use windows::Win32::Foundation::{HINSTANCE, HWND, LPARAM, LRESULT, POINT, RECT, WPARAM};
use windows::Win32::Graphics::Gdi::{GetStockObject, HBRUSH, BLACK_BRUSH};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::WindowsAndMessaging::*;

pub use surface::Surface;

pub const WM_MOUSELEAVE: u32 = 0x02A3;
pub const WM_APP_FRAME: u32 = WM_APP + 1;
pub const WM_APP_HOOK: u32 = WM_APP + 2; // клавиатурный хук → главный поток
pub const WM_APP_COMMAND: u32 = WM_APP + 3; // команда от другого процесса (MiniDock.exe --launchpad …)
pub const WM_APP_TASK: u32 = WM_APP + 4; // отложенная задача из фонового потока
pub const WM_APP_CLICK: u32 = WM_APP + 5; // нажатие мыши где угодно на экране, пока открыто меню или всплывающее окно
pub const WM_APP_APPBAR: u32 = WM_APP + 77; // уведомления панели (ABN_*)

pub const CLASS: PCWSTR = w!("MiniDockWnd");
pub const OVERLAY_CLASS: PCWSTR = w!("MiniDockOverlay");

pub type WndProc = unsafe extern "system" fn(HWND, u32, WPARAM, LPARAM) -> LRESULT;

pub fn hinstance() -> HINSTANCE {
    unsafe { GetModuleHandleW(None).map(|m| HINSTANCE(m.0)).unwrap_or_default() }
}

pub fn register_classes(proc_: WndProc) {
    unsafe {
        let icon = LoadIconW(Some(hinstance()), PCWSTR(1 as _)).unwrap_or_default();
        let wc = WNDCLASSEXW {
            cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32,
            style: CS_DBLCLKS,
            lpfnWndProc: Some(proc_),
            hInstance: hinstance(),
            hCursor: LoadCursorW(None, IDC_ARROW).unwrap_or_default(),
            hIcon: icon,
            lpszClassName: CLASS,
            ..Default::default()
        };
        RegisterClassExW(&wc);
        // окно-подложка для DWM-миниатюр: чёрный фон + DwmExtendFrameIntoClientArea = прозрачное «стекло»
        let wc2 = WNDCLASSEXW {
            cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32,
            lpfnWndProc: Some(proc_),
            hInstance: hinstance(),
            hbrBackground: HBRUSH(GetStockObject(BLACK_BRUSH).0),
            lpszClassName: OVERLAY_CLASS,
            ..Default::default()
        };
        RegisterClassExW(&wc2);
    }
}

/// Окно без рамки поверх всего, не забирающее фокус, с попиксельной прозрачностью.
pub fn create_layered(title: &str, topmost: bool, click_through: bool) -> HWND {
    let mut ex = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE;
    if topmost {
        ex |= WS_EX_TOPMOST;
    }
    if click_through {
        ex |= WS_EX_TRANSPARENT;
    }
    unsafe {
        CreateWindowExW(ex, CLASS, &HSTRING::from(title), WS_POPUP, 0, 0, 1, 1, None, None, Some(hinstance()), None)
            .unwrap_or_default()
    }
}

/// Обычное (активируемое) окно без рамки — для окон с вводом текста (Spotlight, настройки).
pub fn create_focusable(title: &str, topmost: bool) -> HWND {
    let mut ex = WS_EX_LAYERED | WS_EX_TOOLWINDOW;
    if topmost {
        ex |= WS_EX_TOPMOST;
    }
    unsafe {
        CreateWindowExW(ex, CLASS, &HSTRING::from(title), WS_POPUP, 0, 0, 1, 1, None, None, Some(hinstance()), None)
            .unwrap_or_default()
    }
}

/// Окно с кнопкой на панели задач (настройки MiniDock) — тоже без рамки и слоистое.
pub fn create_app_window(title: &str) -> HWND {
    unsafe {
        CreateWindowExW(
            WS_EX_LAYERED | WS_EX_APPWINDOW,
            CLASS,
            &HSTRING::from(title),
            WS_POPUP | WS_MINIMIZEBOX | WS_SYSMENU,
            0,
            0,
            1,
            1,
            None,
            None,
            Some(hinstance()),
            None,
        )
        .unwrap_or_default()
    }
}

/// Скрытое окно для сообщений (кадры, команды, хуки).
pub fn create_message_window() -> HWND {
    unsafe {
        CreateWindowExW(
            WINDOW_EX_STYLE::default(),
            CLASS,
            w!("MiniDockMessages"),
            WS_POPUP,
            0,
            0,
            0,
            0,
            Some(HWND_MESSAGE),
            None,
            Some(hinstance()),
            None,
        )
        .unwrap_or_default()
    }
}

pub fn show_noactivate(hwnd: HWND) {
    unsafe {
        let _ = ShowWindow(hwnd, SW_SHOWNOACTIVATE);
    }
}

pub fn hide(hwnd: HWND) {
    unsafe {
        let _ = ShowWindow(hwnd, SW_HIDE);
    }
}

pub fn is_visible(hwnd: HWND) -> bool {
    unsafe { IsWindowVisible(hwnd).as_bool() }
}

pub fn set_topmost(hwnd: HWND) {
    unsafe {
        let _ = SetWindowPos(hwnd, Some(HWND_TOPMOST), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
    }
}

pub fn cursor_pos() -> (i32, i32) {
    let mut p = POINT::default();
    unsafe {
        let _ = GetCursorPos(&mut p);
    }
    (p.x, p.y)
}

pub fn window_rect(hwnd: HWND) -> RECT {
    let mut r = RECT::default();
    unsafe {
        let _ = GetWindowRect(hwnd, &mut r);
    }
    r
}

pub fn post(hwnd: HWND, msg: u32, wp: usize, lp: isize) {
    unsafe {
        let _ = PostMessageW(Some(hwnd), msg, WPARAM(wp), LPARAM(lp));
    }
}

pub fn set_timer(hwnd: HWND, id: usize, ms: u32) {
    unsafe {
        SetTimer(Some(hwnd), id, ms, None);
    }
}

pub fn kill_timer(hwnd: HWND, id: usize) {
    unsafe {
        let _ = KillTimer(Some(hwnd), id);
    }
}

pub fn lparam_xy(lp: LPARAM) -> (i32, i32) {
    let x = (lp.0 & 0xFFFF) as u16 as i16 as i32;
    let y = ((lp.0 >> 16) & 0xFFFF) as u16 as i16 as i32;
    (x, y)
}

pub fn hkey(h: HWND) -> isize {
    h.0 as isize
}

pub fn hwnd_of(k: isize) -> HWND {
    HWND(k as *mut _)
}

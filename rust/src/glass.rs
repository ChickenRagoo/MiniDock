//! «Стекло»: нативное размытие Windows (Acrylic / Blur, на Windows 11 — Acrylic/Mica DWM) в отдельном
//! окне-подложке прямо под окном интерфейса. Регион подложки — скруглённая форма панели, поэтому
//! размытие повторяет форму дока/меню, а само окно интерфейса ничем не обрезано (подписи, тени).

use windows::Win32::Foundation::HWND;
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::win::{self, dwm};

pub struct Glass {
    pub hwnd: HWND,
    shown: bool,
    rect: (i32, i32, i32, i32),
    radius: i32,
    kind: Option<dwm::Blur>,
    tint: u32,
}

impl Glass {
    pub fn new() -> Glass {
        let hwnd = unsafe {
            CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
                win::OVERLAY_CLASS,
                windows::core::w!("MiniDockGlass"),
                WS_POPUP,
                0,
                0,
                1,
                1,
                None,
                None,
                Some(win::hinstance()),
                None,
            )
            .unwrap_or_default()
        };
        unsafe {
            let _ = SetLayeredWindowAttributes(hwnd, windows::Win32::Foundation::COLORREF(0), 255, LWA_ALPHA);
        }
        dwm::extend_frame(hwnd);
        dwm::exclude_from_peek(hwnd);
        dwm::disable_transitions(hwnd);
        Glass { hwnd, shown: false, rect: (0, 0, 0, 0), radius: -1, kind: None, tint: 0 }
    }

    /// Размытие под прямоугольником (экранные физические пиксели) прямо под окном above.
    pub fn place(&mut self, l: i32, t: i32, r: i32, b: i32, radius: i32, above: HWND, kind: dwm::Blur, tint: u32) {
        if r - l < 2 || b - t < 2 || kind == dwm::Blur::None {
            self.hide();
            return;
        }
        if self.kind != Some(kind) || self.tint != tint {
            self.kind = Some(kind);
            self.tint = tint;
            dwm::set_blur(self.hwnd, kind, tint);
        }
        let size_changed = (r - l, b - t) != (self.rect.2 - self.rect.0, self.rect.3 - self.rect.1) || radius != self.radius;
        if (l, t, r, b) != self.rect || !self.shown {
            unsafe {
                let _ = SetWindowPos(
                    self.hwnd,
                    Some(above),
                    l,
                    t,
                    r - l,
                    b - t,
                    SWP_NOACTIVATE | if self.shown { SWP_NOOWNERZORDER } else { SWP_SHOWWINDOW },
                );
            }
        }
        if size_changed {
            dwm::set_round_region(self.hwnd, 0, 0, r - l, b - t, radius);
        }
        self.rect = (l, t, r, b);
        self.radius = radius;
        self.shown = true;
    }

    pub fn hide(&mut self) {
        if self.shown {
            self.shown = false;
            win::hide(self.hwnd);
        }
    }

    pub fn is_shown(&self) -> bool {
        self.shown
    }
}

impl Drop for Glass {
    fn drop(&mut self) {
        unsafe {
            let _ = DestroyWindow(self.hwnd);
        }
    }
}

//! Резерв места у края экрана (SHAppBarMessage) и панель задач Windows.

use std::cell::RefCell;
use std::collections::HashMap;

use windows::core::w;
use windows::Win32::Foundation::{HWND, LPARAM, RECT};
use windows::Win32::UI::Shell::*;
use windows::Win32::UI::WindowsAndMessaging::{EnumWindows, FindWindowW, ShowWindow, SW_HIDE, SW_SHOW};

use crate::win::sys;

pub const EDGE_LEFT: u32 = 0;
pub const EDGE_TOP: u32 = 1;
pub const EDGE_RIGHT: u32 = 2;
pub const EDGE_BOTTOM: u32 = 3;

thread_local! {
    static RESERVED: RefCell<HashMap<isize, (RECT, u32)>> = RefCell::new(HashMap::new());
}

fn abd(hwnd: HWND) -> APPBARDATA {
    APPBARDATA {
        cbSize: std::mem::size_of::<APPBARDATA>() as u32,
        hWnd: hwnd,
        uCallbackMessage: crate::win::WM_APP_APPBAR,
        ..Default::default()
    }
}

/// Зарезервировать полосу rect (физические пиксели) у края edge: развёрнутые окна не заходят под панель.
pub fn reserve(hwnd: HWND, rect: RECT, edge: u32) {
    let key = hwnd.0 as isize;
    if RESERVED.with(|r| r.borrow().get(&key).is_some_and(|(rc, e)| *rc == rect && *e == edge)) {
        return; // уже ровно так — повтор только дёрнул бы окна на всех мониторах
    }
    RESERVED.with(|r| r.borrow_mut().insert(key, (rect, edge)));
    unsafe {
        let mut d = abd(hwnd);
        SHAppBarMessage(ABM_NEW, &mut d);
        d.uEdge = edge;
        d.rc = rect;
        SHAppBarMessage(ABM_QUERYPOS, &mut d);
        let (w, h) = (rect.right - rect.left, rect.bottom - rect.top);
        match edge {
            EDGE_LEFT => d.rc.right = d.rc.left + w,
            EDGE_RIGHT => d.rc.left = d.rc.right - w,
            EDGE_BOTTOM => d.rc.top = d.rc.bottom - h,
            _ => d.rc.bottom = d.rc.top + h,
        }
        SHAppBarMessage(ABM_SETPOS, &mut d);
    }
}

pub fn is_reserved(hwnd: HWND) -> bool {
    RESERVED.with(|r| r.borrow().contains_key(&(hwnd.0 as isize)))
}

/// Проводник перезапускался и забыл про панель: ABM_NEW удаётся только для незарегистрированной.
pub fn lost(hwnd: HWND) -> bool {
    unsafe {
        let mut d = abd(hwnd);
        if SHAppBarMessage(ABM_NEW, &mut d) != 0 {
            RESERVED.with(|r| r.borrow_mut().remove(&(hwnd.0 as isize)));
            return true;
        }
    }
    false
}

pub fn release(hwnd: HWND) {
    let had = RESERVED.with(|r| r.borrow_mut().remove(&(hwnd.0 as isize)).is_some());
    if had {
        unsafe {
            let mut d = abd(hwnd);
            SHAppBarMessage(ABM_REMOVE, &mut d);
        }
    }
}

pub fn any_reserved() -> bool {
    RESERVED.with(|r| !r.borrow().is_empty())
}

// ---------------------------------------------------------------- панель задач Windows

pub const ABS_AUTOHIDE: u32 = 1;

pub fn tray() -> HWND {
    unsafe { FindWindowW(w!("Shell_TrayWnd"), None).unwrap_or_default() }
}

pub fn state(hwnd: HWND) -> u32 {
    unsafe {
        let mut d = abd(hwnd);
        SHAppBarMessage(ABM_GETSTATE, &mut d) as u32
    }
}

pub fn set_state(hwnd: HWND, st: u32) {
    unsafe {
        let mut d = abd(hwnd);
        d.lParam = LPARAM(st as isize);
        SHAppBarMessage(ABM_SETSTATE, &mut d);
    }
}

pub fn taskbar_windows() -> Vec<HWND> {
    unsafe extern "system" fn cb(h: HWND, lp: LPARAM) -> windows::core::BOOL {
        let v = &mut *(lp.0 as *mut Vec<HWND>);
        let c = sys::class_name(h);
        if c == "Shell_TrayWnd" || c == "Shell_SecondaryTrayWnd" {
            v.push(h);
        }
        true.into()
    }
    let mut v = Vec::new();
    unsafe {
        let _ = EnumWindows(Some(cb), LPARAM(&mut v as *mut _ as isize));
    }
    v
}

/// Спрятать панель задач (автоскрытие + скрытие окна). orig_state — куда запомнить исходное состояние.
pub fn hide_taskbar(orig_state: &mut Option<u32>) -> bool {
    let t = tray();
    if t.is_invalid() {
        return false;
    }
    let mut changed = false;
    if orig_state.is_none() {
        *orig_state = Some(state(t));
        changed = true;
    }
    if state(t) & ABS_AUTOHIDE == 0 {
        set_state(t, ABS_AUTOHIDE);
    }
    for h in taskbar_windows() {
        if sys::is_visible(h) {
            unsafe {
                let _ = ShowWindow(h, SW_HIDE);
            }
        }
    }
    changed
}

pub fn restore_taskbar(orig_state: &mut Option<u32>) {
    let t = tray();
    if !t.is_invalid() {
        if let Some(st) = orig_state.take() {
            set_state(t, st);
        }
    }
    for h in taskbar_windows() {
        unsafe {
            let _ = ShowWindow(h, SW_SHOW);
        }
    }
}

pub fn taskbar_visible() -> bool {
    taskbar_windows().iter().any(|&h| sys::is_visible(h))
}

/// Рабочая область «как у Windows»: экран минус видимая панель задач.
pub fn default_work_area() -> RECT {
    let m = sys::primary_monitor().rect;
    let (mut l, mut t, mut r, mut b) = (m.left, m.top, m.right, m.bottom);
    let tr = tray();
    if !tr.is_invalid() && sys::is_visible(tr) && state(tr) & ABS_AUTOHIDE == 0 {
        let rc = sys::get_rect(tr);
        if rc.top > (t + b) / 2 {
            b = b.min(rc.top);
        } else if rc.bottom < (t + b) / 2 && rc.right - rc.left > (r - l) / 2 {
            t = t.max(rc.bottom);
        } else if rc.left > (l + r) / 2 {
            r = r.min(rc.left);
        } else {
            l = l.max(rc.right);
        }
    }
    RECT { left: l, top: t, right: r, bottom: b }
}

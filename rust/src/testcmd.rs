//! Команды для проверки запущенного экземпляра (minidock.exe --cmd "…"): в этой среде внедрённые клики
//! до окон не доходят, поэтому клик прогоняется через те же обработчики, что и настоящий.
//!   click X Y [right]  — нажать и отпустить в точке экрана
//!   down X Y / up X Y  — по отдельности (перетаскивание)
//!   move X Y           — курсор в точку (наведение)
//!   hook N [ARG]       — действие клавиатурного хука (1 Alt+Tab, 8 Launchpad, 9 Spotlight, 11/12 часы…)
//!   key VK             — клавиша окну с фокусом (WM_KEYDOWN), char C — символ (WM_CHAR)

use windows::Win32::Foundation::{HWND, LPARAM, POINT, WPARAM};
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::win;

fn ours_at(x: i32, y: i32) -> Option<HWND> {
    unsafe {
        let h = WindowFromPoint(POINT { x, y });
        let root = GetAncestor(h, GA_ROOT);
        (!root.is_invalid() && crate::win::sys::window_pid(root) == crate::win::sys::my_pid()).then_some(root)
    }
}

fn send(h: HWND, msg: u32, wp: usize, x: i32, y: i32) {
    let r = win::window_rect(h);
    let (lx, ly) = (x - r.left, y - r.top);
    let lp = ((ly as u32 & 0xFFFF) << 16 | (lx as u32 & 0xFFFF)) as isize;
    unsafe {
        crate::app::wndproc(h, msg, WPARAM(wp), LPARAM(lp));
    }
}

pub fn run(cmd: &str) {
    let parts: Vec<&str> = cmd.split_whitespace().collect();
    let num = |i: usize| parts.get(i).and_then(|v| v.parse::<i32>().ok()).unwrap_or(0);
    crate::dbg_log(&format!("testcmd {cmd}"));
    match parts.first().copied() {
        Some("click") | Some("down") | Some("up") | Some("move") => {
            let (x, y) = (num(1), num(2));
            unsafe {
                let _ = SetCursorPos(x, y);
            }
            let Some(h) = ours_at(x, y) else {
                crate::dbg_log("testcmd: под точкой не наше окно");
                return;
            };
            let right = parts.get(3) == Some(&"right");
            match parts[0] {
                "move" => send(h, WM_MOUSEMOVE, 0, x, y),
                "down" => send(h, WM_LBUTTONDOWN, 1, x, y),
                "up" => send(h, WM_LBUTTONUP, 0, x, y),
                _ if right => {
                    send(h, WM_RBUTTONDOWN, 2, x, y);
                    send(h, WM_RBUTTONUP, 0, x, y);
                }
                _ => {
                    send(h, WM_MOUSEMOVE, 0, x, y);
                    send(h, WM_LBUTTONDOWN, 1, x, y);
                    send(h, WM_LBUTTONUP, 0, x, y);
                }
            }
        }
        Some("minimize") | Some("restore") | Some("state") => {
            // окно программы по части пути exe: minimize notepad
            let want = parts.get(1).copied().unwrap_or("").to_lowercase();
            let mut found = None;
            for (exe, hs) in crate::win::sys::app_windows().iter().chain(std::iter::empty()) {
                if exe.contains(&want) {
                    found = hs.first().map(|h| h.0 as isize);
                    break;
                }
            }
            // свёрнутые окна в app_windows тоже есть (видимые свёрнутые)
            let Some(h) = found else {
                crate::dbg_log("testcmd: окно не найдено");
                return;
            };
            crate::app::with(|a| match parts[0] {
                "minimize" => a.minimize_window(h),
                "restore" => a.restore_window(h),
                _ => {
                    let w = win::hwnd_of(h);
                    crate::log::line(&format!("testcmd state: iconic={} visible={} exstyle={:#x}", crate::win::sys::is_iconic(w), crate::win::sys::is_visible(w), crate::win::sys::exstyle(w)));
                }
            });
        }
        Some("hook") => {
            let t = crate::win::hooks::TARGET.load(std::sync::atomic::Ordering::Relaxed);
            unsafe {
                crate::app::wndproc(win::hwnd_of(t), crate::win::WM_APP_HOOK, WPARAM(num(1) as usize), LPARAM(num(2) as isize));
            }
        }
        Some("key") | Some("char") => {
            let fg = crate::win::sys::foreground();
            let (msg, v) = if parts[0] == "key" { (WM_KEYDOWN, num(1) as usize) } else { (WM_CHAR, parts.get(1).and_then(|s| s.chars().next()).map(|c| c as usize).unwrap_or(0)) };
            unsafe {
                crate::app::wndproc(fg, msg, WPARAM(v), LPARAM(0));
            }
        }
        _ => {}
    }
}

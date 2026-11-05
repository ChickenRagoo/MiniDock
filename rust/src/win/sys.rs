//! Система: мониторы, окна программ, процессы, фокус, полноэкранный режим, клавиатура, автозапуск.

use std::cell::RefCell;
use std::collections::HashMap;

use windows::core::{BOOL, HSTRING, PCWSTR, PWSTR};
use windows::Win32::Foundation::{CloseHandle, HWND, LPARAM, POINT, RECT, WPARAM};
use windows::Win32::Graphics::Dwm::{DwmGetWindowAttribute, DWMWA_CLOAKED, DWMWA_EXTENDED_FRAME_BOUNDS};
use windows::Win32::Graphics::Gdi::{
    EnumDisplayMonitors, GetMonitorInfoW, MonitorFromPoint, MonitorFromWindow, HDC, HMONITOR, MONITORINFO, MONITORINFOEXW,
    MONITOR_DEFAULTTONEAREST, MONITOR_DEFAULTTOPRIMARY,
};
use windows::Win32::System::Registry::*;
use windows::Win32::System::Threading::{
    AttachThreadInput, GetCurrentProcessId, GetCurrentThreadId, OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32,
    PROCESS_QUERY_LIMITED_INFORMATION,
};
use windows::Win32::UI::HiDpi::{GetDpiForMonitor, MDT_EFFECTIVE_DPI};
use windows::Win32::UI::Input::KeyboardAndMouse::*;
use windows::Win32::UI::WindowsAndMessaging::*;

// ---------------------------------------------------------------- мониторы

#[derive(Clone, Debug, PartialEq)]
pub struct Monitor {
    pub handle: isize,
    pub rect: RECT,
    pub work: RECT,
    pub primary: bool,
    pub scale: f32,
    pub name: String,
}

impl Monitor {
    pub fn w(&self) -> i32 {
        self.rect.right - self.rect.left
    }
    pub fn h(&self) -> i32 {
        self.rect.bottom - self.rect.top
    }
}

fn monitor_info(h: HMONITOR) -> Option<Monitor> {
    unsafe {
        let mut mi = MONITORINFOEXW::default();
        mi.monitorInfo.cbSize = std::mem::size_of::<MONITORINFOEXW>() as u32;
        if !GetMonitorInfoW(h, &mut mi as *mut _ as *mut MONITORINFO).as_bool() {
            return None;
        }
        let (mut dx, mut dy) = (96u32, 96u32);
        let _ = GetDpiForMonitor(h, MDT_EFFECTIVE_DPI, &mut dx, &mut dy);
        let len = mi.szDevice.iter().position(|&c| c == 0).unwrap_or(mi.szDevice.len());
        Some(Monitor {
            handle: h.0 as isize,
            rect: mi.monitorInfo.rcMonitor,
            work: mi.monitorInfo.rcWork,
            primary: mi.monitorInfo.dwFlags & 1 != 0,
            scale: dx as f32 / 96.0,
            name: String::from_utf16_lossy(&mi.szDevice[..len]),
        })
    }
}

pub fn monitors() -> Vec<Monitor> {
    unsafe extern "system" fn cb(h: HMONITOR, _: HDC, _: *mut RECT, lp: LPARAM) -> BOOL {
        let v = &mut *(lp.0 as *mut Vec<Monitor>);
        if let Some(m) = monitor_info(h) {
            v.push(m);
        }
        true.into()
    }
    let mut v: Vec<Monitor> = Vec::new();
    unsafe {
        let _ = EnumDisplayMonitors(None, None, Some(cb), LPARAM(&mut v as *mut _ as isize));
    }
    v.sort_by_key(|m| !m.primary);
    v
}

pub fn primary_monitor() -> Monitor {
    unsafe { monitor_info(MonitorFromPoint(POINT { x: 0, y: 0 }, MONITOR_DEFAULTTOPRIMARY)) }.unwrap_or(Monitor {
        handle: 0,
        rect: RECT { left: 0, top: 0, right: 1920, bottom: 1080 },
        work: RECT { left: 0, top: 0, right: 1920, bottom: 1040 },
        primary: true,
        scale: 1.0,
        name: String::new(),
    })
}

pub fn window_monitor(hwnd: HWND) -> Monitor {
    unsafe { monitor_info(MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)) }.unwrap_or_else(primary_monitor)
}

pub fn point_monitor(x: i32, y: i32) -> Monitor {
    unsafe { monitor_info(MonitorFromPoint(POINT { x, y }, MONITOR_DEFAULTTONEAREST)) }.unwrap_or_else(primary_monitor)
}

pub fn get_work_area() -> RECT {
    let mut rc = RECT::default();
    unsafe {
        let _ = SystemParametersInfoW(SPI_GETWORKAREA, 0, Some(&mut rc as *mut _ as *mut _), SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS(0));
    }
    rc
}

pub fn set_work_area(rc: RECT) {
    unsafe {
        let mut rc = rc;
        // без SPIF_SENDCHANGE: синхронная рассылка срывается, если какая-то программа зависла
        let _ = SystemParametersInfoW(SPI_SETWORKAREA, 0, Some(&mut rc as *mut _ as *mut _), SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS(0));
        let _ = PostMessageW(Some(HWND_BROADCAST), WM_SETTINGCHANGE, WPARAM(SPI_SETWORKAREA.0 as usize), LPARAM(0));
    }
}

pub fn window_shadows() -> bool {
    let mut on = BOOL(0);
    unsafe {
        let _ = SystemParametersInfoW(SPI_GETDROPSHADOW, 0, Some(&mut on as *mut _ as *mut _), SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS(0));
    }
    on.as_bool()
}

pub fn set_window_shadows(on: bool) {
    unsafe {
        let _ = SystemParametersInfoW(SPI_SETDROPSHADOW, 0, Some(on as usize as *mut _), SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS(0));
        let _ = PostMessageW(Some(HWND_BROADCAST), WM_SETTINGCHANGE, WPARAM(SPI_SETDROPSHADOW.0 as usize), LPARAM(0));
    }
}

// ---------------------------------------------------------------- окна

pub const IGNORED_CLASSES: &[&str] = &["Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"];

pub fn class_name(hwnd: HWND) -> String {
    let mut buf = [0u16; 256];
    let n = unsafe { GetClassNameW(hwnd, &mut buf) };
    String::from_utf16_lossy(&buf[..n.max(0) as usize])
}

pub fn window_text(hwnd: HWND) -> String {
    let mut buf = [0u16; 512];
    let n = unsafe { GetWindowTextW(hwnd, &mut buf) };
    String::from_utf16_lossy(&buf[..n.max(0) as usize])
}

pub fn has_title(hwnd: HWND) -> bool {
    unsafe { GetWindowTextLengthW(hwnd) > 0 }
}

pub fn window_pid(hwnd: HWND) -> u32 {
    let mut pid = 0u32;
    unsafe {
        GetWindowThreadProcessId(hwnd, Some(&mut pid));
    }
    pid
}

pub fn window_thread(hwnd: HWND) -> u32 {
    unsafe { GetWindowThreadProcessId(hwnd, None) }
}

pub fn my_pid() -> u32 {
    unsafe { GetCurrentProcessId() }
}

pub fn exstyle(hwnd: HWND) -> u32 {
    unsafe { GetWindowLongPtrW(hwnd, GWL_EXSTYLE) as u32 }
}

pub fn style(hwnd: HWND) -> u32 {
    unsafe { GetWindowLongPtrW(hwnd, GWL_STYLE) as u32 }
}

pub fn is_window(hwnd: HWND) -> bool {
    unsafe { IsWindow(Some(hwnd)).as_bool() }
}

pub fn is_iconic(hwnd: HWND) -> bool {
    unsafe { IsIconic(hwnd).as_bool() }
}

pub fn is_zoomed(hwnd: HWND) -> bool {
    unsafe { IsZoomed(hwnd).as_bool() }
}

pub fn is_visible(hwnd: HWND) -> bool {
    unsafe { IsWindowVisible(hwnd).as_bool() }
}

pub fn owner(hwnd: HWND) -> HWND {
    unsafe { GetWindow(hwnd, GW_OWNER).unwrap_or_default() }
}

pub fn root(hwnd: HWND) -> HWND {
    unsafe { GetAncestor(hwnd, GA_ROOT) }
}

pub fn foreground() -> HWND {
    unsafe { GetForegroundWindow() }
}

pub fn is_cloaked(hwnd: HWND) -> bool {
    let mut v = 0u32;
    unsafe {
        let _ = DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, &mut v as *mut _ as *mut _, 4);
    }
    v != 0
}

/// Окно, которое показывалось бы на панели задач.
pub fn is_app_window(hwnd: HWND, need_visible: bool) -> bool {
    if need_visible && !is_visible(hwnd) {
        return false;
    }
    if !owner(hwnd).is_invalid() {
        return false;
    }
    let ex = exstyle(hwnd);
    if ex & WS_EX_TOOLWINDOW.0 != 0 && ex & WS_EX_APPWINDOW.0 == 0 {
        return false;
    }
    if !has_title(hwnd) {
        return false;
    }
    let cls = class_name(hwnd);
    if IGNORED_CLASSES.contains(&cls.as_str()) {
        return false;
    }
    window_pid(hwnd) != my_pid()
}

thread_local! {
    static PID_EXE: RefCell<HashMap<u32, Option<String>>> = RefCell::new(HashMap::new());
    static FRAME_EXE: RefCell<HashMap<isize, String>> = RefCell::new(HashMap::new());
}

pub fn pid_exe(pid: u32) -> Option<String> {
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid).ok()?;
        let mut buf = [0u16; 1024];
        let mut n = buf.len() as u32;
        let ok = QueryFullProcessImageNameW(h, PROCESS_NAME_WIN32, PWSTR(buf.as_mut_ptr()), &mut n).is_ok();
        let _ = CloseHandle(h);
        ok.then(|| String::from_utf16_lossy(&buf[..n as usize]).to_lowercase())
    }
}

fn cached_pid_exe(pid: u32) -> Option<String> {
    PID_EXE.with(|c| c.borrow_mut().entry(pid).or_insert_with(|| pid_exe(pid)).clone())
}

pub fn window_exe(hwnd: HWND) -> String {
    let pid = window_pid(hwnd);
    let exe = cached_pid_exe(pid).unwrap_or_default();
    if exe.ends_with("\\applicationframehost.exe") {
        return frame_app(hwnd, pid).unwrap_or(exe);
    }
    exe
}

/// Программа Store в окне-рамке ApplicationFrameHost: процесс её вложенного окна.
fn frame_app(hwnd: HWND, frame_pid: u32) -> Option<String> {
    unsafe extern "system" fn cb(h: HWND, lp: LPARAM) -> BOOL {
        let v = &mut *(lp.0 as *mut (u32, Vec<u32>));
        let pid = window_pid(h);
        if pid != v.0 {
            v.1.push(pid);
        }
        true.into()
    }
    let mut data = (frame_pid, Vec::new());
    unsafe {
        let _ = EnumChildWindows(Some(hwnd), Some(cb), LPARAM(&mut data as *mut _ as isize));
    }
    for pid in data.1 {
        if let Some(exe) = cached_pid_exe(pid) {
            FRAME_EXE.with(|f| f.borrow_mut().insert(hwnd.0 as isize, exe.clone()));
            return Some(exe);
        }
    }
    FRAME_EXE.with(|f| f.borrow().get(&(hwnd.0 as isize)).cloned())
}

/// Окна программ: [(exe в нижнем регистре, [hwnd…])] в порядке z-order (сверху вниз).
pub fn app_windows() -> Vec<(String, Vec<HWND>)> {
    unsafe extern "system" fn cb(h: HWND, lp: LPARAM) -> BOOL {
        let v = &mut *(lp.0 as *mut Vec<HWND>);
        v.push(h);
        true.into()
    }
    let mut all: Vec<HWND> = Vec::new();
    unsafe {
        let _ = EnumWindows(Some(cb), LPARAM(&mut all as *mut _ as isize));
    }
    let mut out: Vec<(String, Vec<HWND>)> = Vec::new();
    let mut seen_pids = std::collections::HashSet::new();
    let mut frames = std::collections::HashSet::new();
    for h in all {
        if !is_app_window(h, true) || is_cloaked(h) {
            continue;
        }
        let pid = window_pid(h);
        seen_pids.insert(pid);
        let Some(mut exe) = cached_pid_exe(pid) else { continue };
        if exe.ends_with("\\applicationframehost.exe") {
            frames.insert(h.0 as isize);
            match frame_app(h, pid) {
                Some(e) => exe = e,
                None => continue, // нет программы внутри — рамку не показываем вовсе
            }
        }
        if let Some(e) = out.iter_mut().find(|(x, _)| *x == exe) {
            e.1.push(h);
        } else {
            out.push((exe, vec![h]));
        }
    }
    // завершившиеся процессы забываем (их номер может достаться другому)
    PID_EXE.with(|c| c.borrow_mut().retain(|pid, _| seen_pids.contains(pid)));
    FRAME_EXE.with(|f| f.borrow_mut().retain(|h, _| frames.contains(h) || is_window(HWND(*h as *mut _))));
    out
}

/// Отвечает ли поток окна на сообщения прямо сейчас (ждём не дольше ms).
pub fn responds(hwnd: HWND, ms: u32) -> bool {
    let mut res = 0usize;
    unsafe { SendMessageTimeoutW(hwnd, WM_NULL, WPARAM(0), LPARAM(0), SMTO_ABORTIFHUNG, ms, Some(&mut res)).0 != 0 }
}

pub fn try_foreground(hwnd: HWND) -> bool {
    unsafe { SetForegroundWindow(hwnd).as_bool() }
}

/// Вывести окно на передний план, обходя ограничения Windows на смену фокуса.
/// Никаких синхронных вызовов в поток чужой программы: занятая программа (Photoshop) не подвесит MiniDock.
pub fn force_foreground(hwnd: HWND) {
    unsafe {
        if IsIconic(hwnd).as_bool() {
            let _ = ShowWindowAsync(hwnd, SW_RESTORE);
        }
        if try_foreground(hwnd) {
            return;
        }
        let fg = GetForegroundWindow();
        let fg_thread = if fg.is_invalid() { 0 } else { GetWindowThreadProcessId(fg, None) };
        let me = GetCurrentThreadId();
        let attached = fg_thread != 0 && fg_thread != me && AttachThreadInput(me, fg_thread, true).as_bool();
        let _ = BringWindowToTop(hwnd);
        if !try_foreground(hwnd) {
            // Windows отдаёт фокус процессу, от которого пришло последнее нажатие. Раньше здесь нажимался Alt —
            // но окно Win32 от одиночного Alt входит в «режим меню», который забирает мышь: меню MiniDock
            // зависало — ни нажать пункт, ни закрыть. Неназначенная клавиша даёт то же право без побочных эффектов.
            inject_dummy();
            try_foreground(hwnd);
        }
        if attached {
            let _ = AttachThreadInput(me, fg_thread, false);
        }
    }
}

pub fn syscommand(hwnd: HWND, cmd: u32) {
    unsafe {
        let _ = PostMessageW(Some(hwnd), WM_SYSCOMMAND, WPARAM(cmd as usize), LPARAM(0));
    }
}

pub fn close_window(hwnd: HWND) {
    syscommand(hwnd, SC_CLOSE);
}

pub fn get_rect(hwnd: HWND) -> RECT {
    let mut r = RECT::default();
    unsafe {
        let _ = GetWindowRect(hwnd, &mut r);
    }
    r
}

pub fn visible_frame(hwnd: HWND) -> RECT {
    let mut r = RECT::default();
    unsafe {
        if DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, &mut r as *mut _ as *mut _, std::mem::size_of::<RECT>() as u32)
            .is_ok()
        {
            return r;
        }
    }
    get_rect(hwnd)
}

/// (видимая рамка, отступы невидимой рамки l,t,r,b) в физических пикселях; None — свёрнуто.
pub fn window_geometry(hwnd: HWND) -> Option<(RECT, [i32; 4])> {
    if is_iconic(hwnd) {
        return None;
    }
    let w = get_rect(hwnd);
    let f = visible_frame(hwnd);
    if w.left <= -30000 || f.right - f.left < 8 || f.bottom - f.top < 8 {
        return None;
    }
    Some((f, [f.left - w.left, f.top - w.top, w.right - f.right, w.bottom - f.bottom]))
}

/// Где окно окажется после разворачивания.
pub fn restored_frame(hwnd: HWND) -> Option<(RECT, [i32; 4])> {
    let mut wp = WINDOWPLACEMENT { length: std::mem::size_of::<WINDOWPLACEMENT>() as u32, ..Default::default() };
    unsafe { GetWindowPlacement(hwnd, &mut wp).ok()? };
    let mon = window_monitor(hwnd);
    let wa = mon.work;
    if wp.flags.0 & WPF_RESTORETOMAXIMIZED.0 != 0 {
        return Some((wa, [8, 8, 8, 8]));
    }
    let n = wp.rcNormalPosition;
    // rcNormalPosition — в координатах рабочей области
    let off = (wa.left - mon.rect.left, wa.top - mon.rect.top);
    let l = n.left + mon.rect.left + off.0;
    let t = n.top + mon.rect.top + off.1;
    let r = n.right + mon.rect.left + off.0;
    let b = n.bottom + mon.rect.top + off.1;
    Some((RECT { left: l + 7, top: t, right: r - 7, bottom: b - 7 }, [7, 0, 7, 7]))
}

/// Пропорции окна (ширина / высота) — и для свёрнутого.
pub fn window_aspect(hwnd: HWND) -> f32 {
    let r = if is_iconic(hwnd) {
        restored_frame(hwnd).map(|g| g.0)
    } else {
        Some(visible_frame(hwnd))
    };
    if let Some(r) = r {
        let (w, h) = ((r.right - r.left) as f32, (r.bottom - r.top) as f32);
        if w > 10.0 && h > 10.0 {
            return (w / h).clamp(0.55, 2.4);
        }
    }
    1.6
}

fn is_fullscreen_rect(r: RECT, m: RECT, zoomed: bool) -> bool {
    let exact = (r.left - m.left).abs() <= 2
        && (r.top - m.top).abs() <= 2
        && (r.right - m.right).abs() <= 2
        && (r.bottom - m.bottom).abs() <= 2;
    let covers = r.left <= m.left && r.top <= m.top && r.right >= m.right && r.bottom >= m.bottom;
    exact || (covers && !zoomed)
}

/// Монитор, на котором сейчас полноэкранное окно (игра, видео, F11), или None.
pub fn fullscreen_monitor() -> Option<Monitor> {
    let fg = foreground();
    if fg.is_invalid() || IGNORED_CLASSES.contains(&class_name(fg).as_str()) || window_pid(fg) == my_pid() {
        return None;
    }
    let mon = window_monitor(fg);
    is_fullscreen_rect(get_rect(fg), mon.rect, is_zoomed(fg)).then_some(mon)
}

pub fn foreground_is_fullscreen() -> bool {
    fullscreen_monitor().is_some_and(|m| m.primary)
}

/// Развёрнутое окно залезло под верхнюю панель — поставить ровно в рабочую область (асинхронно).
pub fn refit_maximized(hwnd: HWND, area: RECT) -> bool {
    if !is_zoomed(hwnd) || is_iconic(hwnd) || !is_visible(hwnd) {
        return false;
    }
    let mon = window_monitor(hwnd);
    if !mon.primary {
        return false;
    }
    let r = get_rect(hwnd);
    let border = mon.rect.left - r.left;
    if border <= 0 || border > 32 || r.top >= area.top - border - 1 {
        return false;
    }
    unsafe {
        SetWindowPos(
            hwnd,
            None,
            area.left - border,
            area.top - border,
            area.right - area.left + 2 * border,
            area.bottom - area.top + 2 * border,
            SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_ASYNCWINDOWPOS,
        )
        .is_ok()
    }
}

// ---------------------------------------------------------------- пути программ

/// «…\app-1.2.3\x.exe» → (корень, версия, exe).
fn split_versioned(path: &str) -> Option<(&str, &str, &str)> {
    let lower = path.to_lowercase();
    let idx = lower.rfind("\\app-")?;
    let rest = &path[idx + 5..];
    let slash = rest.find('\\')?;
    let ver = &rest[..slash];
    let exe = &rest[slash + 1..];
    if ver.is_empty() || !ver.chars().all(|c| c.is_ascii_digit() || c == '.') || exe.contains('\\') || !exe.to_lowercase().ends_with(".exe") {
        return None;
    }
    Some((&path[..idx], ver, exe))
}

/// Путь программы без папки версии (Discord, Claude, Slack ставят версии в app-1.2.3).
pub fn app_key(path: &str) -> String {
    match split_versioned(path) {
        Some((root, _, exe)) => format!("{root}\\{exe}").to_lowercase(),
        None => path.to_lowercase(),
    }
}

/// Программа обновилась и папки её старой версии нет — путь к самой новой версии.
pub fn current_path(path: &str) -> String {
    if path.is_empty() || std::path::Path::new(path).exists() {
        return path.to_string();
    }
    let Some((root, _, exe)) = split_versioned(path) else { return path.to_string() };
    let mut best: Option<(Vec<u32>, String)> = None;
    if let Ok(rd) = std::fs::read_dir(root) {
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            let Some(ver) = name.strip_prefix("app-").or_else(|| name.strip_prefix("App-")) else { continue };
            let Ok(v) = ver.split('.').map(|x| x.parse::<u32>()).collect::<Result<Vec<_>, _>>() else { continue };
            let cand = format!("{root}\\{name}\\{exe}");
            if std::path::Path::new(&cand).is_file() && best.as_ref().is_none_or(|b| v > b.0) {
                best = Some((v, cand));
            }
        }
    }
    best.map(|b| b.1).unwrap_or_else(|| path.to_string())
}

pub fn file_stem(path: &str) -> String {
    std::path::Path::new(path).file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or_else(|| path.to_string())
}

pub fn file_name(path: &str) -> String {
    std::path::Path::new(path).file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default()
}

pub fn parent_dir(path: &str) -> String {
    std::path::Path::new(path).parent().map(|s| s.to_string_lossy().to_string()).unwrap_or_default()
}

pub fn system_root() -> String {
    std::env::var("SystemRoot").unwrap_or_else(|_| "C:\\Windows".into()).to_lowercase()
}

// ---------------------------------------------------------------- клавиатура

pub fn key_down(vk: u16) -> bool {
    unsafe { GetAsyncKeyState(vk as i32) as u16 & 0x8000 != 0 }
}

pub fn inject_key(vk: u16, up: bool) {
    let input = INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT {
                wVk: VIRTUAL_KEY(vk),
                wScan: 0,
                dwFlags: if up { KEYEVENTF_KEYUP } else { KEYBD_EVENT_FLAGS(0) },
                time: 0,
                dwExtraInfo: crate::win::hooks::INJECT_TAG,
            },
        },
    };
    unsafe {
        SendInput(&[input], std::mem::size_of::<INPUT>() as i32);
    }
}

pub const VK_DUMMY: u16 = 0xE8; // неназначенная клавиша: гасит меню Alt и «Пуск» по одиночному Win

pub fn inject_dummy() {
    inject_key(VK_DUMMY, false);
    inject_key(VK_DUMMY, true);
}

pub fn press_keys(vks: &[u16]) {
    for &vk in vks {
        inject_key(vk, false);
    }
    for &vk in vks.iter().rev() {
        inject_key(vk, true);
    }
}

/// Раскладка активного окна: короткое имя языка («RU», «EN»).
pub fn keyboard_lang() -> String {
    unsafe {
        let fg = GetForegroundWindow();
        let tid = if fg.is_invalid() { 0 } else { GetWindowThreadProcessId(fg, None) };
        let hkl = GetKeyboardLayout(tid);
        let lang = (hkl.0 as usize & 0xFFFF) as u32;
        let mut buf = [0u16; 16];
        let n = windows::Win32::Globalization::GetLocaleInfoW(lang, 0x59, Some(&mut buf)); // LOCALE_SISO639LANGNAME
        let s = String::from_utf16_lossy(&buf[..(n.max(1) - 1) as usize]).to_uppercase();
        if s.is_empty() {
            "??".into()
        } else {
            s
        }
    }
}

pub fn next_keyboard_layout() {
    unsafe {
        let fg = GetForegroundWindow();
        if !fg.is_invalid() {
            let _ = PostMessageW(Some(fg), WM_INPUTLANGCHANGEREQUEST, WPARAM(0), LPARAM(HKL_NEXT as isize));
        }
    }
}

// ---------------------------------------------------------------- автозапуск

const RUN_KEY: &str = "Software\\Microsoft\\Windows\\CurrentVersion\\Run";

pub fn autostart_enabled() -> bool {
    reg_read_string(HKEY_CURRENT_USER, RUN_KEY, "MiniDock").is_some()
}

pub fn set_autostart(on: bool) {
    let exe = std::env::current_exe().map(|p| p.to_string_lossy().to_string()).unwrap_or_default();
    unsafe {
        let mut key = HKEY::default();
        if RegOpenKeyExW(HKEY_CURRENT_USER, &HSTRING::from(RUN_KEY), Some(0), KEY_SET_VALUE, &mut key).is_err() {
            return;
        }
        if on {
            let v: Vec<u16> = format!("\"{exe}\"").encode_utf16().chain(std::iter::once(0)).collect();
            let bytes = std::slice::from_raw_parts(v.as_ptr() as *const u8, v.len() * 2);
            let _ = RegSetValueExW(key, &HSTRING::from("MiniDock"), Some(0), REG_SZ, Some(bytes));
        } else {
            let _ = RegDeleteValueW(key, &HSTRING::from("MiniDock"));
        }
        let _ = RegCloseKey(key);
    }
}

pub fn reg_read_string(root: HKEY, path: &str, name: &str) -> Option<String> {
    unsafe {
        let mut buf = vec![0u16; 2048];
        let mut size = (buf.len() * 2) as u32;
        RegGetValueW(
            root,
            &HSTRING::from(path),
            &HSTRING::from(name),
            RRF_RT_REG_SZ | RRF_RT_REG_EXPAND_SZ,
            None,
            Some(buf.as_mut_ptr() as *mut _),
            Some(&mut size),
        )
        .ok()
        .ok()?;
        let n = (size as usize / 2).saturating_sub(1);
        Some(String::from_utf16_lossy(&buf[..n.min(buf.len())]))
    }
}

pub fn reg_read_dword(root: HKEY, path: &str, name: &str) -> Option<u32> {
    unsafe {
        let mut v = 0u32;
        let mut size = 4u32;
        RegGetValueW(root, &HSTRING::from(path), &HSTRING::from(name), RRF_RT_REG_DWORD, None, Some(&mut v as *mut _ as *mut _), Some(&mut size))
            .ok()
            .ok()?;
        Some(v)
    }
}

pub fn wstr(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

pub fn pcw(v: &[u16]) -> PCWSTR {
    PCWSTR(v.as_ptr())
}

/// Собственная память процесса (private bytes) и рабочий набор, байты.
pub fn process_memory() -> (usize, usize) {
    use windows::Win32::System::ProcessStatus::{GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS, PROCESS_MEMORY_COUNTERS_EX};
    use windows::Win32::System::Threading::GetCurrentProcess;
    unsafe {
        let mut pmc = PROCESS_MEMORY_COUNTERS_EX { cb: std::mem::size_of::<PROCESS_MEMORY_COUNTERS_EX>() as u32, ..Default::default() };
        let _ = GetProcessMemoryInfo(GetCurrentProcess(), &mut pmc as *mut _ as *mut PROCESS_MEMORY_COUNTERS, pmc.cb);
        (pmc.PrivateUsage, pmc.WorkingSetSize)
    }
}

/// Освободить неиспользуемые страницы (после закрытия больших окон).
pub fn trim_working_set() {
    use windows::Win32::System::Threading::GetCurrentProcess;
    unsafe {
        let _ = windows::Win32::System::ProcessStatus::EmptyWorkingSet(GetCurrentProcess());
    }
}

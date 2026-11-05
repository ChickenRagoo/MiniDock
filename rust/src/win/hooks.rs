//! Хуки: низкоуровневый хук клавиатуры в своём потоке и события окон (SetWinEventHook).
//!
//! Windows ждёт ответа клавиатурного хука на КАЖДОЕ нажатие во всей системе, поэтому у хука свой
//! поток со своим циклом сообщений: он только решает, забрать ли клавишу (по атомарным флагам),
//! а действия (переключатель, Launchpad, часы) отправляет в главный поток сообщением — без задержки ввода.

use std::cell::RefCell;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicIsize, AtomicU32, Ordering::Relaxed};

use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::System::Threading::GetCurrentThreadId;
use windows::Win32::UI::Accessibility::{SetWinEventHook, UnhookWinEvent, HWINEVENTHOOK};
use windows::Win32::UI::Input::KeyboardAndMouse::*;
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::win::sys::{inject_dummy, inject_key, key_down};

/// Метка наших собственных нажатий (SendInput): хук их пропускает.
pub const INJECT_TAG: usize = 0x4D44_4F43; // «MDOC»

// -- настройки, которые читает поток хука
pub static CUSTOM_SWITCHER: AtomicBool = AtomicBool::new(true);
pub static SPOTLIGHT: AtomicBool = AtomicBool::new(true);
pub static WIN_LAUNCHPAD: AtomicBool = AtomicBool::new(true);
pub static LAUNCHPAD_KEYS: AtomicBool = AtomicBool::new(false); // Launchpad открыт без фокуса — набор уходит в его поиск
pub static CLOCK_HOTKEY: AtomicU32 = AtomicU32::new(0); // vk | модификаторы << 16; 0 — выключено
pub static RECORDING: AtomicBool = AtomicBool::new(false); // настройки ждут новое сочетание
pub static TARGET: AtomicIsize = AtomicIsize::new(0); // окно главного потока
pub static END_SWITCH: AtomicBool = AtomicBool::new(false); // переключились кликом — выйти из режима Alt+Tab
static THREAD: AtomicU32 = AtomicU32::new(0);

pub const MOD_CTRL: u32 = 1;
pub const MOD_ALT: u32 = 2;
pub const MOD_SHIFT: u32 = 4;
pub const MOD_WIN: u32 = 8;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(usize)]
pub enum Action {
    SwitchBegin = 1,
    SwitchStep,
    SwitchCommit,
    SwitchArrow,
    SwitchNextWin,
    SwitchCancel,
    SwitchQuit,
    Launchpad,
    Spotlight,
    LaunchpadKey,
    ClockShow,
    ClockHide,
    HotkeyRecorded,
}

impl Action {
    pub fn from(v: usize) -> Option<Action> {
        use Action::*;
        Some(match v {
            1 => SwitchBegin,
            2 => SwitchStep,
            3 => SwitchCommit,
            4 => SwitchArrow,
            5 => SwitchNextWin,
            6 => SwitchCancel,
            7 => SwitchQuit,
            8 => Launchpad,
            9 => Spotlight,
            10 => LaunchpadKey,
            11 => ClockShow,
            12 => ClockHide,
            13 => HotkeyRecorded,
            _ => return None,
        })
    }
}

fn post(a: Action, arg: isize) {
    let t = TARGET.load(Relaxed);
    if t != 0 {
        unsafe {
            let _ = PostMessageW(Some(HWND(t as *mut _)), crate::win::WM_APP_HOOK, WPARAM(a as usize), LPARAM(arg));
        }
    }
}

struct KeyState {
    switching: bool,
    win_down: bool,
    win_combo: bool,
    space_eaten: bool,
    clock_on: bool,
    clock_vk: u32,
    lp_eaten: Vec<u32>,
    rec_eaten: Vec<u32>,
}

static mut STATE: KeyState = KeyState {
    switching: false,
    win_down: false,
    win_combo: false,
    space_eaten: false,
    clock_on: false,
    clock_vk: 0,
    lp_eaten: Vec::new(),
    rec_eaten: Vec::new(),
};
static HOOK: AtomicIsize = AtomicIsize::new(0);

#[allow(static_mut_refs)]
fn st() -> &'static mut KeyState {
    // только из потока хука
    unsafe { &mut STATE }
}

fn is_alt(vk: u32) -> bool {
    vk == VK_MENU.0 as u32 || vk == VK_LMENU.0 as u32 || vk == VK_RMENU.0 as u32
}

fn is_win(vk: u32) -> bool {
    vk == VK_LWIN.0 as u32 || vk == VK_RWIN.0 as u32
}

fn is_modifier(vk: u32) -> bool {
    is_alt(vk)
        || is_win(vk)
        || [VK_SHIFT.0, VK_LSHIFT.0, VK_RSHIFT.0, VK_CONTROL.0, VK_LCONTROL.0, VK_RCONTROL.0].contains(&(vk as u16))
}

pub fn current_mods() -> u32 {
    let mut m = 0;
    if key_down(VK_CONTROL.0) {
        m |= MOD_CTRL;
    }
    if key_down(VK_MENU.0) {
        m |= MOD_ALT;
    }
    if key_down(VK_SHIFT.0) {
        m |= MOD_SHIFT;
    }
    if key_down(VK_LWIN.0) || key_down(VK_RWIN.0) {
        m |= MOD_WIN;
    }
    m
}

fn unstick() {
    let s = st();
    if s.switching && !key_down(VK_MENU.0) {
        s.switching = false;
        post(Action::SwitchCommit, 0);
    }
    if s.win_down && !(key_down(VK_LWIN.0) || key_down(VK_RWIN.0)) {
        s.win_down = false;
    }
    if s.clock_on && !key_down(s.clock_vk as u16) {
        s.clock_on = false;
        post(Action::ClockHide, 0);
    }
}

const LP_KEYS: [u32; 11] = [0x08, 0x09, 0x0D, 0x1B, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x2E];

/// Решение по одной клавише. true — забрать (не отдавать программе).
fn handle(vk: u32, scan: u32, down: bool) -> bool {
    let s = st();
    if END_SWITCH.swap(false, Relaxed) {
        s.switching = false;
    }
    if !is_alt(vk) && !is_win(vk) {
        unstick();
    }

    // запись нового сочетания для часов (настройки)
    if RECORDING.load(Relaxed) {
        if down && !is_modifier(vk) {
            RECORDING.store(false, Relaxed);
            s.rec_eaten.push(vk);
            if vk != VK_ESCAPE.0 as u32 {
                post(Action::HotkeyRecorded, (vk | current_mods() << 16) as isize);
            } else {
                post(Action::HotkeyRecorded, 0);
            }
            return true;
        }
    }
    if !down {
        if let Some(i) = s.rec_eaten.iter().position(|&k| k == vk) {
            s.rec_eaten.remove(i);
            return true;
        }
    }

    // большие часы — пока зажато сочетание
    let hk = CLOCK_HOTKEY.load(Relaxed);
    if hk != 0 {
        let (hvk, hmods) = (hk & 0xFFFF, hk >> 16);
        if vk == hvk {
            if down {
                if current_mods() == hmods {
                    if !s.clock_on {
                        s.clock_on = true;
                        s.clock_vk = vk;
                        if hmods & MOD_ALT != 0 || hmods & MOD_WIN != 0 {
                            inject_dummy(); // отпускание Alt/Win не откроет меню и «Пуск»
                        }
                        post(Action::ClockShow, 0);
                    }
                    return true; // и автоповтор тоже
                }
            } else if s.clock_on {
                s.clock_on = false;
                post(Action::ClockHide, 0);
                return true;
            }
        } else if s.clock_on && !down && is_modifier(vk) {
            // отпустили модификатор раньше клавиши — часы тоже прячем
            s.clock_on = false;
            post(Action::ClockHide, 0);
        }
    }

    if vk == VK_TAB.0 as u32 && CUSTOM_SWITCHER.load(Relaxed) {
        if down && (s.switching || key_down(VK_MENU.0)) {
            let dir = if key_down(VK_SHIFT.0) { -1 } else { 1 };
            if !s.switching {
                s.switching = true;
                inject_dummy(); // чтобы отпускание Alt не открыло меню программы
                post(Action::SwitchBegin, dir);
            } else {
                post(Action::SwitchStep, dir);
            }
            return true;
        }
        if !down && s.switching {
            return true;
        }
    }
    if s.switching {
        if is_alt(vk) {
            if !down {
                s.switching = false;
                post(Action::SwitchCommit, 0);
            }
            return false;
        }
        if [VK_SHIFT.0 as u32, 0xA0, 0xA1].contains(&vk) {
            return false;
        }
        if down {
            match vk {
                0x1B => {
                    s.switching = false;
                    post(Action::SwitchCancel, 0);
                }
                0x25..=0x28 => post(Action::SwitchArrow, vk as isize),
                0xC0 => post(Action::SwitchNextWin, 0), // `
                0x51 => post(Action::SwitchQuit, 0),    // Q
                0x0D => {
                    s.switching = false;
                    post(Action::SwitchCommit, 0);
                }
                _ => {}
            }
        }
        return true; // остальные клавиши во время переключения не уходят в программу
    }

    // Alt+Space — Spotlight
    if vk == VK_SPACE.0 as u32 && SPOTLIGHT.load(Relaxed) {
        if down && key_down(VK_MENU.0) && !key_down(VK_CONTROL.0) {
            s.space_eaten = true;
            inject_dummy();
            post(Action::Spotlight, 0);
            return true;
        }
        if !down && s.space_eaten {
            s.space_eaten = false;
            return true;
        }
    }

    if is_win(vk) && WIN_LAUNCHPAD.load(Relaxed) {
        if down {
            if !s.win_down {
                s.win_down = true;
                s.win_combo = false;
            }
            return false;
        }
        let alone = s.win_down && !s.win_combo;
        s.win_down = false;
        if alone {
            // одиночное нажатие Win: гасим «Пуск» и открываем Launchpad
            inject_dummy();
            inject_key(vk as u16, true);
            post(Action::Launchpad, 0);
            return true;
        }
        return false;
    }
    if s.win_down && down {
        s.win_combo = true;
    }

    // Launchpad на экране без фокуса: набор — в его поиск
    if !down {
        if let Some(i) = s.lp_eaten.iter().position(|&k| k == vk) {
            s.lp_eaten.remove(i);
            return true;
        }
        return false;
    }
    if LAUNCHPAD_KEYS.load(Relaxed) && current_mods() & (MOD_CTRL | MOD_ALT | MOD_WIN) == 0 && !is_modifier(vk) {
        let is_special = LP_KEYS.contains(&vk);
        let ch = if is_special { None } else { key_char(vk, scan) };
        if is_special || ch.is_some() {
            s.lp_eaten.push(vk);
            let arg = ((ch.map(|c| c as u32).unwrap_or(0) as isize) << 16) | vk as isize;
            post(Action::LaunchpadKey, arg);
            return true;
        }
    }
    false
}

/// Символ клавиши с учётом Shift/Caps Lock и раскладки активного окна.
pub fn key_char(vk: u32, scan: u32) -> Option<char> {
    unsafe {
        let mut state = [0u8; 256];
        for k in [0x10usize, 0xA0, 0xA1] {
            if key_down(k as u16) {
                state[k] = 0x80;
            }
        }
        if GetKeyState(0x14) & 1 != 0 {
            state[0x14] = 1;
        }
        let fg = GetForegroundWindow();
        let tid = if fg.is_invalid() { 0 } else { GetWindowThreadProcessId(fg, None) };
        let layout = GetKeyboardLayout(tid);
        let mut buf = [0u16; 8];
        // флаг 4 — не трогать состояние «мёртвых» клавиш в системе
        let n = ToUnicodeEx(vk, scan, &state, &mut buf, 4, Some(layout));
        if n == 1 {
            let c = char::from_u32(buf[0] as u32)?;
            (!c.is_control()).then_some(c)
        } else {
            None
        }
    }
}

unsafe extern "system" fn proc_(n: i32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    if n == HC_ACTION as i32 {
        let kb = &*(lp.0 as *const KBDLLHOOKSTRUCT);
        let injected = kb.flags.0 & LLKHF_INJECTED.0 != 0;
        if !injected || kb.dwExtraInfo != INJECT_TAG {
            let down = wp.0 as u32 == WM_KEYDOWN || wp.0 as u32 == WM_SYSKEYDOWN;
            if !injected && handle(kb.vkCode, kb.scanCode, down) {
                return LRESULT(1);
            }
        }
    }
    CallNextHookEx(None, n, wp, lp)
}

fn install() -> isize {
    unsafe {
        let hinst = GetModuleHandleW(None).ok().map(|m| windows::Win32::Foundation::HINSTANCE(m.0));
        SetWindowsHookExW(WH_KEYBOARD_LL, Some(proc_), hinst, 0).map(|h| h.0 as isize).unwrap_or(0)
    }
}

/// Запустить поток клавиатурного хука.
pub fn start_keyboard_hook() {
    std::thread::Builder::new()
        .name("MiniDockKeyHook".into())
        .stack_size(256 * 1024)
        .spawn(|| unsafe {
            THREAD.store(GetCurrentThreadId(), Relaxed);
            HOOK.store(install(), Relaxed);
            // Если программа на миг не ответила вовремя, Windows молча снимает хук — раз в 3 с ставим заново
            SetTimer(None, 0, 3000, None);
            let mut msg = MSG::default();
            while GetMessageW(&mut msg, None, 0, 0).as_bool() {
                if msg.message == WM_TIMER {
                    unstick();
                    let s = st();
                    if !(s.switching || s.win_down || s.clock_on) {
                        let new = install();
                        if new != 0 {
                            let old = HOOK.swap(new, Relaxed);
                            if old != 0 {
                                let _ = UnhookWindowsHookEx(HHOOK(old as *mut _));
                            }
                        }
                    }
                    continue;
                }
                let _ = TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
            let h = HOOK.swap(0, Relaxed);
            if h != 0 {
                let _ = UnhookWindowsHookEx(HHOOK(h as *mut _));
            }
        })
        .ok();
}

// -- клики мимо меню
//
// Меню и всплывающие окна закрываются по клику мимо. Сигнала «окно потеряло фокус» для этого мало:
// клик по доку или панели (они не берут фокус) его не даёт, а если Windows не отдала меню фокус,
// его не будет вовсе. Поэтому, пока такое окно открыто (и только тогда), стоит хук мыши: он лишь
// сообщает главному потоку, где нажали, и сразу пропускает нажатие дальше.

static MOUSE_THREAD: std::sync::Mutex<u32> = std::sync::Mutex::new(0);

unsafe extern "system" fn mouse_proc(n: i32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    if n == HC_ACTION as i32 {
        let m = wp.0 as u32;
        if m == WM_LBUTTONDOWN || m == WM_RBUTTONDOWN || m == WM_MBUTTONDOWN {
            let ms = &*(lp.0 as *const MSLLHOOKSTRUCT);
            let packed = ((ms.pt.x as u16 as u32) | ((ms.pt.y as u16 as u32) << 16)) as isize;
            let t = TARGET.load(Relaxed);
            if t != 0 {
                let _ = PostMessageW(Some(HWND(t as *mut _)), crate::win::WM_APP_CLICK, WPARAM(m as usize), LPARAM(packed));
            }
        }
    }
    CallNextHookEx(None, n, wp, lp)
}

/// Включить/выключить слежение за кликами (поток с хуком мыши живёт только пока включено).
pub fn watch_clicks(on: bool) {
    let mut t = MOUSE_THREAD.lock().unwrap_or_else(|e| e.into_inner());
    if on == (*t != 0) {
        return;
    }
    if !on {
        unsafe {
            let _ = PostThreadMessageW(*t, WM_QUIT, WPARAM(0), LPARAM(0));
        }
        *t = 0;
        return;
    }
    let (tx, rx) = std::sync::mpsc::channel();
    let ok = std::thread::Builder::new()
        .name("MiniDockMouseHook".into())
        .stack_size(128 * 1024)
        .spawn(move || unsafe {
            // очередь сообщений потока — до того, как о нём узнают (иначе WM_QUIT может потеряться)
            let mut msg = MSG::default();
            let _ = PeekMessageW(&mut msg, None, 0, 0, PM_NOREMOVE);
            let hinst = GetModuleHandleW(None).ok().map(|m| windows::Win32::Foundation::HINSTANCE(m.0));
            let hook = SetWindowsHookExW(WH_MOUSE_LL, Some(mouse_proc), hinst, 0).ok();
            let _ = tx.send(GetCurrentThreadId());
            while GetMessageW(&mut msg, None, 0, 0).as_bool() {
                DispatchMessageW(&msg);
            }
            if let Some(h) = hook {
                let _ = UnhookWindowsHookEx(h);
            }
        })
        .is_ok();
    if ok {
        *t = rx.recv_timeout(std::time::Duration::from_millis(500)).unwrap_or(0);
    }
}

pub fn stop_keyboard_hook() {
    let t = THREAD.load(Relaxed);
    if t != 0 {
        unsafe {
            let _ = PostThreadMessageW(t, WM_QUIT, WPARAM(0), LPARAM(0));
        }
    }
}

// ---------------------------------------------------------------- события окон

#[derive(Clone, Copy, Debug)]
pub struct WinEvent {
    pub event: u32,
    pub hwnd: isize,
}

thread_local! {
    static EVENTS: RefCell<VecDeque<WinEvent>> = const { RefCell::new(VecDeque::new()) };
    static POSTED: RefCell<bool> = const { RefCell::new(false) };
}

pub const EVENT_FOREGROUND: u32 = EVENT_SYSTEM_FOREGROUND;
pub const EVENT_MINSTART: u32 = EVENT_SYSTEM_MINIMIZESTART;
pub const EVENT_MINEND: u32 = EVENT_SYSTEM_MINIMIZEEND;
pub const EVENT_CAPTURE: u32 = EVENT_SYSTEM_CAPTURESTART;
pub const EVENT_CREATE: u32 = EVENT_OBJECT_CREATE;
pub const EVENT_DESTROY: u32 = EVENT_OBJECT_DESTROY;
pub const EVENT_SHOW: u32 = EVENT_OBJECT_SHOW;
pub const EVENT_HIDE: u32 = EVENT_OBJECT_HIDE;
pub const EVENT_NAMECHANGE: u32 = EVENT_OBJECT_NAMECHANGE;
pub const EVENT_CLOAKED: u32 = EVENT_OBJECT_CLOAKED;
pub const EVENT_UNCLOAKED: u32 = EVENT_OBJECT_UNCLOAKED;
pub const EVENT_LOCATION: u32 = EVENT_OBJECT_LOCATIONCHANGE;
pub const EVENT_MOVESIZEEND: u32 = EVENT_SYSTEM_MOVESIZEEND;

unsafe extern "system" fn win_event(_: HWINEVENTHOOK, event: u32, hwnd: HWND, id_object: i32, id_child: i32, _: u32, _: u32) {
    if id_object != 0 || id_child != 0 || hwnd.is_invalid() {
        return;
    }
    EVENTS.with(|q| q.borrow_mut().push_back(WinEvent { event, hwnd: hwnd.0 as isize }));
    // обрабатываем в главном цикле, а не здесь: событие может прийти посреди нашего же вызова Windows
    let need = POSTED.with(|p| !std::mem::replace(&mut *p.borrow_mut(), true));
    if need {
        let t = TARGET.load(Relaxed);
        let _ = PostMessageW(Some(HWND(t as *mut _)), crate::win::WM_APP_TASK, WPARAM(0), LPARAM(0));
    }
}

pub fn take_events() -> Vec<WinEvent> {
    POSTED.with(|p| *p.borrow_mut() = false);
    EVENTS.with(|q| q.borrow_mut().drain(..).collect())
}

pub struct WinEventHooks {
    hooks: Vec<HWINEVENTHOOK>,
    create: Option<HWINEVENTHOOK>,
    location: Option<HWINEVENTHOOK>,
}

fn set_hook(lo: u32, hi: u32) -> Option<HWINEVENTHOOK> {
    unsafe {
        let h = SetWinEventHook(lo, hi, None, Some(win_event), 0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS);
        (!h.is_invalid()).then_some(h)
    }
}

impl WinEventHooks {
    pub fn install() -> WinEventHooks {
        let ranges = [
            (EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND),
            (EVENT_SYSTEM_CAPTURESTART, EVENT_SYSTEM_CAPTURESTART),
            (EVENT_SYSTEM_MOVESIZEEND, EVENT_SYSTEM_MOVESIZEEND),
            (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND),
            (EVENT_OBJECT_DESTROY, EVENT_OBJECT_HIDE),
            (EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_NAMECHANGE),
            (EVENT_OBJECT_CLOAKED, EVENT_OBJECT_UNCLOAKED),
        ];
        WinEventHooks { hooks: ranges.iter().filter_map(|&(lo, hi)| set_hook(lo, hi)).collect(), create: None, location: None }
    }

    /// «Окно создано» — только пока ждём запуска программы из дока (событий много).
    pub fn want_create(&mut self, on: bool) {
        if on && self.create.is_none() {
            self.create = set_hook(EVENT_OBJECT_CREATE, EVENT_OBJECT_CREATE);
        } else if !on {
            if let Some(h) = self.create.take() {
                unsafe {
                    let _ = UnhookWinEvent(h);
                }
            }
        }
    }

    /// Перемещение окон — только пока открыт предпросмотр/полноэкранная проверка нужна чаще.
    pub fn want_location(&mut self, on: bool) {
        if on && self.location.is_none() {
            self.location = set_hook(EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_LOCATIONCHANGE);
        } else if !on {
            if let Some(h) = self.location.take() {
                unsafe {
                    let _ = UnhookWinEvent(h);
                }
            }
        }
    }
}

impl Drop for WinEventHooks {
    fn drop(&mut self) {
        for h in self.hooks.drain(..).chain(self.create.take()).chain(self.location.take()) {
            unsafe {
                let _ = UnhookWinEvent(h);
            }
        }
    }
}

/// Имя клавиши для подписи сочетания («Alt+Ё»).
pub fn hotkey_name(code: u32) -> String {
    if code == 0 {
        return "выключено".into();
    }
    let (vk, mods) = (code & 0xFFFF, code >> 16);
    let mut parts = Vec::new();
    if mods & MOD_CTRL != 0 {
        parts.push("Ctrl".to_string());
    }
    if mods & MOD_ALT != 0 {
        parts.push("Alt".to_string());
    }
    if mods & MOD_SHIFT != 0 {
        parts.push("Shift".to_string());
    }
    if mods & MOD_WIN != 0 {
        parts.push("Win".to_string());
    }
    parts.push(vk_name(vk));
    parts.join("+")
}

pub fn vk_name(vk: u32) -> String {
    match vk {
        0x20 => "Пробел".into(),
        0x0D => "Enter".into(),
        0x09 => "Tab".into(),
        0x08 => "Backspace".into(),
        0x70..=0x87 => format!("F{}", vk - 0x6F),
        0x30..=0x39 | 0x41..=0x5A => char::from_u32(vk).unwrap().to_string(),
        0xC0 => "`".into(),
        _ => unsafe {
            let scan = MapVirtualKeyW(vk, MAPVK_VK_TO_VSC);
            let mut buf = [0u16; 32];
            let n = GetKeyNameTextW(((scan as i32) << 16) as i32, &mut buf);
            if n > 0 {
                String::from_utf16_lossy(&buf[..n as usize])
            } else {
                format!("#{vk}")
            }
        },
    }
}

//! MiniDock — док и верхняя панель в стиле macOS для Windows. Нативная версия: Rust + Win32,
//! отрисовка процессором (tiny-skia) прямо в буферы слоистых окон, без контекстов видеокарты.
#![windows_subsystem = "windows"]

mod anim;
mod audio;
mod bluetooth;
mod flyouts;
mod media;
mod panel;
mod sysinfo;
mod tray;
mod widgets;
mod animator;
mod app;
mod bar;
mod bigclock;
mod capture;
mod catalog;
mod config;
mod dock;
mod folder;
mod gfx;
mod glass;
mod imageio;
mod launchpad;
mod lineedit;
mod log;
mod menu;
mod preview;
mod settings;
mod spotlight;
mod switcher;
mod testcmd;
mod text;
mod win;

use windows::core::HSTRING;
use windows::Win32::Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, HANDLE, WAIT_OBJECT_0};
use windows::Win32::System::Threading::*;
use windows::Win32::UI::WindowsAndMessaging::*;

/// Команды уже запущенному доку (те же имена событий, что у Python-версии: обе версии — один экземпляр).
pub const COMMANDS: [(&str, &str); 7] = [
    ("--quit", "MiniDock_Quit"),
    ("--launchpad", "MiniDock_Launchpad"),
    ("--settings", "MiniDock_Settings"),
    ("--spotlight", "MiniDock_Spotlight"),
    ("--show", "MiniDock_Show"),
    ("--reveal", "MiniDock_Reveal"),
    ("--report", "MiniDock_Report"),
];
pub const SHOW_ACK: &str = "MiniDock_ShowAck";

pub fn dbg_log(s: &str) {
    static ON: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    let on = *ON.get_or_init(|| std::env::var("MINIDOCK_DEBUG").is_ok() || std::path::Path::new(&format!("{}\\debug.flag", config::cfg_dir())).exists());
    if on {
        log::line(&format!("debug [{:.3}] {s}", anim::now()));
    }
}

pub fn signal(name: &str) {
    unsafe {
        if let Ok(h) = OpenEventW(EVENT_MODIFY_STATE, false, &HSTRING::from(name)) {
            let _ = SetEvent(h);
            let _ = CloseHandle(h);
        }
    }
}

fn create_event(name: &str, manual: bool) -> HANDLE {
    unsafe { CreateEventW(None, manual, false, &HSTRING::from(name)).unwrap_or_default() }
}

/// Поток, который спит до сигнала от другого процесса (MiniDock.exe --launchpad и т. п.).
fn start_command_waiter() {
    let handles: Vec<isize> = COMMANDS.iter().enumerate().map(|(i, (_, name))| create_event(name, i == 0).0 as isize).collect();
    std::thread::Builder::new()
        .name("MiniDockCommands".into())
        .stack_size(64 * 1024)
        .spawn(move || {
            let hs: Vec<HANDLE> = handles.iter().map(|h| HANDLE(*h as *mut _)).collect();
            loop {
                let r = unsafe { WaitForMultipleObjects(&hs, false, INFINITE) };
                let i = r.0.wrapping_sub(WAIT_OBJECT_0.0) as usize;
                if i < hs.len() {
                    let t = win::hooks::TARGET.load(std::sync::atomic::Ordering::Relaxed);
                    win::post(win::hwnd_of(t), win::WM_APP_COMMAND, i, 0);
                    if i == 0 {
                        return; // событие выхода не сбрасывается само
                    }
                }
            }
        })
        .ok();
}

/// MiniDock уже запущен: просим его показаться; не ответил за 4 с — завис, завершаем его.
fn take_over(mutex: HANDLE) -> bool {
    let ack = create_event(SHOW_ACK, false);
    signal("MiniDock_Show");
    unsafe {
        if WaitForSingleObject(ack, 4000) == WAIT_OBJECT_0 {
            return false; // живой — он сам показался
        }
        let me = std::env::current_exe().map(|p| p.to_string_lossy().to_lowercase()).unwrap_or_default();
        for pid in processes_named(&win::sys::file_name(&me)) {
            if pid != GetCurrentProcessId() {
                if let Ok(h) = OpenProcess(PROCESS_TERMINATE, false, pid) {
                    let _ = TerminateProcess(h, 1);
                    let _ = CloseHandle(h);
                }
            }
        }
        let r = WaitForSingleObject(mutex, 5000);
        r == WAIT_OBJECT_0 || r.0 == 0x80 // WAIT_ABANDONED
    }
}

fn processes_named(exe: &str) -> Vec<u32> {
    use windows::Win32::System::Diagnostics::ToolHelp::*;
    let mut out = Vec::new();
    unsafe {
        let Ok(snap) = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) else { return out };
        let mut e = PROCESSENTRY32W { dwSize: std::mem::size_of::<PROCESSENTRY32W>() as u32, ..Default::default() };
        if Process32FirstW(snap, &mut e).is_ok() {
            loop {
                let name = win::shell::from_wide(&e.szExeFile).to_lowercase();
                if name == exe {
                    out.push(e.th32ProcessID);
                }
                if Process32NextW(snap, &mut e).is_err() {
                    break;
                }
            }
        }
        let _ = CloseHandle(snap);
    }
    out
}

fn send_command(text: &str) {
    use windows::Win32::Foundation::{LPARAM, WPARAM};
    use windows::Win32::System::DataExchange::COPYDATASTRUCT;
    unsafe {
        let Ok(h) = FindWindowExW(Some(HWND_MESSAGE), None, windows::core::w!("MiniDockWnd"), windows::core::w!("MiniDockMessages")) else { return };
        let bytes = text.as_bytes();
        let cds = COPYDATASTRUCT { dwData: 0x4D44, cbData: bytes.len() as u32, lpData: bytes.as_ptr() as *mut _ };
        let _ = SendMessageW(h, WM_COPYDATA, Some(WPARAM(0)), Some(LPARAM(&cds as *const _ as isize)));
    }
}

fn main() {
    log::install_panic_hook();
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|a| a == "--log-test") {
        log::line("проверка журнала");
        return;
    }
    // команда для проверки запущенного экземпляра: --cmd "click 100 20"
    if let Some(i) = args.iter().position(|a| a == "--cmd") {
        if let Some(c) = args.get(i + 1) {
            send_command(c);
        }
        return;
    }
    for (flag, name) in COMMANDS {
        if args.iter().any(|a| a == flag) {
            signal(name);
            return;
        }
    }
    unsafe {
        let mutex = CreateMutexW(None, false, &HSTRING::from("MiniDock_SingleInstance")).unwrap_or_default();
        if GetLastError() == ERROR_ALREADY_EXISTS && !take_over(mutex) {
            return;
        }
        let _ = windows::Win32::UI::HiDpi::SetProcessDpiAwarenessContext(windows::Win32::UI::HiDpi::DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    }
    win::shell::com_init();
    win::register_classes(app::wndproc);
    let msg = win::create_message_window();
    win::hooks::TARGET.store(msg.0 as isize, std::sync::atomic::Ordering::Relaxed);
    anim::start_clock();
    win::hooks::start_keyboard_hook();
    start_command_waiter();
    let a = app::App::new(msg);
    app::App::install(a);
    unsafe {
        let mut m = MSG::default();
        while GetMessageW(&mut m, None, 0, 0).as_bool() {
            let _ = TranslateMessage(&m);
            DispatchMessageW(&m);
        }
    }
    if let Some(mut a) = app::App::take() {
        if !a.cleaned {
            a.cleanup();
        }
    }
}

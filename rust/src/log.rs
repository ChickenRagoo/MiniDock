//! Журнал: %APPDATA%\MiniDock\error.log (тот же файл, что у Python-версии — его присылают друзья).

use std::io::Write;

use windows::Win32::System::SystemInformation::GetLocalTime;

pub fn stamp(with_seconds: bool) -> String {
    let t = unsafe { GetLocalTime() };
    if with_seconds {
        format!("{:04}-{:02}-{:02} {:02}:{:02}:{:02}", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond)
    } else {
        format!("{:04}-{:02}-{:02} {:02}:{:02}", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute)
    }
}

pub fn path() -> String {
    format!("{}\\error.log", crate::config::cfg_dir())
}

pub fn raw(text: &str) {
    let _ = std::fs::create_dir_all(crate::config::cfg_dir());
    // журнал не должен расти бесконечно: больше 2 МБ — оставляем последнюю половину
    if std::fs::metadata(path()).map(|m| m.len() > 2 * 1024 * 1024).unwrap_or(false) {
        if let Ok(data) = std::fs::read(path()) {
            let half = &data[data.len() / 2..];
            let start = half.iter().position(|&b| b == b'\n').map(|p| p + 1).unwrap_or(0);
            let _ = std::fs::write(path(), &half[start..]);
        }
    }
    match std::fs::OpenOptions::new().create(true).append(true).open(path()) {
        Ok(mut f) => {
            if let Err(e) = f.write_all(text.as_bytes()) {
                eprintln!("log write: {e}");
            }
        }
        Err(e) => eprintln!("log open {}: {e}", path()),
    }
}

pub fn line(text: &str) {
    raw(&format!("[{}] {}\n", stamp(true), text));
}

/// Паника (panic = "abort") — успеть записать, где именно.
pub fn install_panic_hook() {
    std::panic::set_hook(Box::new(|info| {
        let loc = info.location().map(|l| format!("{}:{}", l.file(), l.line())).unwrap_or_default();
        let msg = info
            .payload()
            .downcast_ref::<&str>()
            .map(|s| s.to_string())
            .or_else(|| info.payload().downcast_ref::<String>().cloned())
            .unwrap_or_default();
        eprintln!("PANIC: {msg} ({loc})");
        line(&format!("MiniDock {} упал: {msg} ({loc})", env!("CARGO_PKG_VERSION")));
    }));
}

//! Сведения о системе для виджетов: процессор, память, диски, сеть, процессы, аккумулятор, время работы.

use windows::Win32::Foundation::{CloseHandle, FILETIME};
use windows::Win32::System::Diagnostics::ToolHelp::*;
use windows::Win32::System::SystemInformation::{GetTickCount64, GlobalMemoryStatusEx, MEMORYSTATUSEX};
use windows::Win32::System::Threading::{GetProcessTimes, GetSystemTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION};

fn ft(f: FILETIME) -> u64 {
    (f.dwHighDateTime as u64) << 32 | f.dwLowDateTime as u64
}

#[derive(Default)]
pub struct Cpu {
    last: Option<(u64, u64)>,
}

impl Cpu {
    /// Загрузка процессора с прошлого вызова, %.
    pub fn percent(&mut self) -> f32 {
        let (mut idle, mut kernel, mut user) = (FILETIME::default(), FILETIME::default(), FILETIME::default());
        unsafe {
            let _ = GetSystemTimes(Some(&mut idle), Some(&mut kernel), Some(&mut user));
        }
        let (idle, total) = (ft(idle), ft(kernel) + ft(user));
        let out = match self.last {
            Some((pi, pt)) if total > pt => 100.0 * (1.0 - (idle - pi) as f32 / (total - pt) as f32),
            _ => 0.0,
        };
        self.last = Some((idle, total));
        out.clamp(0.0, 100.0)
    }
}

pub fn memory() -> (f32, u64, u64) {
    let mut m = MEMORYSTATUSEX { dwLength: std::mem::size_of::<MEMORYSTATUSEX>() as u32, ..Default::default() };
    unsafe {
        let _ = GlobalMemoryStatusEx(&mut m);
    }
    (m.dwMemoryLoad as f32, m.ullTotalPhys - m.ullAvailPhys, m.ullTotalPhys)
}

/// Локальные диски: (буква, доля занятого, занято, всего).
pub fn disks() -> Vec<(String, f32, u64, u64)> {
    use windows::core::HSTRING;
    use windows::Win32::Storage::FileSystem::{GetDiskFreeSpaceExW, GetDriveTypeW, GetLogicalDrives};
    let mut out = Vec::new();
    let mask = unsafe { GetLogicalDrives() };
    for i in 0..26 {
        if mask & (1 << i) == 0 {
            continue;
        }
        let root = format!("{}:\\", (b'A' + i as u8) as char);
        let t = unsafe { GetDriveTypeW(&HSTRING::from(root.as_str())) };
        if t != 3 {
            continue; // DRIVE_FIXED
        }
        let (mut free, mut total, mut tfree) = (0u64, 0u64, 0u64);
        if unsafe { GetDiskFreeSpaceExW(&HSTRING::from(root.as_str()), Some(&mut free), Some(&mut total), Some(&mut tfree)) }.is_ok() && total > 0 {
            let used = total - tfree;
            out.push((root.trim_end_matches('\\').to_string(), used as f32 / total as f32, used, total));
        }
    }
    out
}

pub fn uptime() -> String {
    let s = unsafe { GetTickCount64() } / 1000;
    let (d, s) = (s / 86400, s % 86400);
    let (h, m) = (s / 3600, s % 3600 / 60);
    if d > 0 {
        format!("{d} д {h} ч {m} мин")
    } else {
        format!("{h} ч {m} мин")
    }
}

/// Процессы с наибольшей загрузкой между двумя замерами (pause мс): (доля процессора %, имя, память).
pub fn top_processes(pause_ms: u64, n: usize) -> Vec<(f32, String, u64)> {
    fn snapshot() -> Vec<(u32, String, u64)> {
        let mut out = Vec::new();
        unsafe {
            let Ok(snap) = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) else { return out };
            let mut e = PROCESSENTRY32W { dwSize: std::mem::size_of::<PROCESSENTRY32W>() as u32, ..Default::default() };
            if Process32FirstW(snap, &mut e).is_ok() {
                loop {
                    let name = crate::win::shell::from_wide(&e.szExeFile);
                    if e.th32ProcessID != 0 {
                        if let Ok(h) = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, e.th32ProcessID) {
                            let (mut c, mut x, mut k, mut u) = (FILETIME::default(), FILETIME::default(), FILETIME::default(), FILETIME::default());
                            if GetProcessTimes(h, &mut c, &mut x, &mut k, &mut u).is_ok() {
                                out.push((e.th32ProcessID, name, ft(k) + ft(u)));
                            }
                            let _ = CloseHandle(h);
                        }
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
    let a = snapshot();
    let t0 = std::time::Instant::now();
    std::thread::sleep(std::time::Duration::from_millis(pause_ms));
    let b = snapshot();
    let wall = t0.elapsed().as_nanos() as f64 / 100.0; // в единицах FILETIME (100 нс)
    let cores = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1) as f64;
    let mut out: Vec<(f32, String, u64)> = b
        .into_iter()
        .filter_map(|(pid, name, t)| {
            let prev = a.iter().find(|x| x.0 == pid)?.2;
            let cpu = (t.saturating_sub(prev) as f64 / wall / cores * 100.0) as f32;
            Some((cpu, name, process_memory(pid)))
        })
        .collect();
    out.sort_by(|x, y| y.0.partial_cmp(&x.0).unwrap_or(std::cmp::Ordering::Equal));
    out.truncate(n);
    out
}

fn process_memory(pid: u32) -> u64 {
    use windows::Win32::System::ProcessStatus::{GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS};
    unsafe {
        let Ok(h) = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) else { return 0 };
        let mut pmc = PROCESS_MEMORY_COUNTERS { cb: std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32, ..Default::default() };
        let _ = GetProcessMemoryInfo(h, &mut pmc, pmc.cb);
        let _ = CloseHandle(h);
        pmc.WorkingSetSize as u64
    }
}

/// Сеть: (получено, отправлено) байт по всем включённым интерфейсам.
pub fn net_bytes() -> (u64, u64) {
    use windows::Win32::NetworkManagement::IpHelper::{FreeMibTable, GetIfTable2, MIB_IF_TABLE2};
    let mut rx = 0u64;
    let mut tx = 0u64;
    unsafe {
        let mut t: *mut MIB_IF_TABLE2 = std::ptr::null_mut();
        if GetIfTable2(&mut t).is_ok() && !t.is_null() {
            let n = (*t).NumEntries as usize;
            let rows = std::slice::from_raw_parts((*t).Table.as_ptr(), n);
            for r in rows {
                // только физические и беспроводные интерфейсы, поднятые
                if r.OperStatus.0 == 1 && (r.Type == 6 || r.Type == 71) && r.InterfaceAndOperStatusFlags._bitfield & 1 != 0 {
                    rx += r.InOctets;
                    tx += r.OutOctets;
                }
            }
            FreeMibTable(t as *const _);
        }
    }
    (rx, tx)
}

/// IPv4-адреса поднятых интерфейсов: (имя, адрес).
pub fn addresses() -> Vec<(String, String)> {
    use windows::Win32::NetworkManagement::IpHelper::*;
    use windows::Win32::Networking::WinSock::{AF_INET, SOCKADDR_IN};
    let mut out = Vec::new();
    unsafe {
        let mut size = 16 * 1024u32;
        let mut buf = vec![0u8; size as usize];
        let r = GetAdaptersAddresses(AF_INET.0 as u32, GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER, None, Some(buf.as_mut_ptr() as *mut _), &mut size);
        if r != 0 {
            buf = vec![0u8; size as usize];
            if GetAdaptersAddresses(AF_INET.0 as u32, GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER, None, Some(buf.as_mut_ptr() as *mut _), &mut size) != 0 {
                return out;
            }
        }
        let mut a = buf.as_ptr() as *const IP_ADAPTER_ADDRESSES_LH;
        while !a.is_null() {
            let ad = &*a;
            if ad.OperStatus.0 == 1 {
                let name = ad.FriendlyName.to_string().unwrap_or_default();
                let mut u = ad.FirstUnicastAddress;
                while !u.is_null() {
                    let sa = (*u).Address.lpSockaddr as *const SOCKADDR_IN;
                    if !sa.is_null() && (*sa).sin_family == AF_INET {
                        let b = (*sa).sin_addr.S_un.S_un_b;
                        let ip = format!("{}.{}.{}.{}", b.s_b1, b.s_b2, b.s_b3, b.s_b4);
                        if !ip.starts_with("127.") && !ip.starts_with("169.254.") {
                            out.push((name.clone(), ip));
                        }
                    }
                    u = (*u).Next;
                }
            }
            a = ad.Next;
        }
    }
    out
}

/// Аккумулятор: (процент, на зарядке, секунд осталось) или None, если его нет.
pub fn battery() -> Option<(f32, bool, Option<u32>)> {
    use windows::Win32::System::Power::{GetSystemPowerStatus, SYSTEM_POWER_STATUS};
    let mut s = SYSTEM_POWER_STATUS::default();
    unsafe { GetSystemPowerStatus(&mut s).ok()? };
    if s.BatteryFlag == 128 || s.BatteryFlag == 255 || s.BatteryLifePercent == 255 {
        return None; // нет аккумулятора / неизвестно
    }
    let left = (s.BatteryLifeTime != u32::MAX).then_some(s.BatteryLifeTime);
    Some((s.BatteryLifePercent as f32, s.ACLineStatus == 1, left))
}

/// Wi‑Fi через netsh: (текущая сеть и сигнал, [(имя, сигнал, защищена)]); None — адаптера нет.
pub type WifiInfo = (Option<(String, u32)>, Vec<(String, u32, bool)>);

pub fn wifi() -> Option<WifiInfo> {
    let run = |args: &[&str]| -> String {
        use std::os::windows::process::CommandExt;
        std::process::Command::new("netsh")
            .args(args)
            .creation_flags(0x08000000)
            .output()
            .map(|o| oem_decode(&o.stdout))
            .unwrap_or_default()
    };
    let iface = run(&["wlan", "show", "interfaces"]);
    let nets = run(&["wlan", "show", "networks", "mode=bssid"]);
    let has = iface.lines().any(|l| {
        let t = l.trim_start();
        (t.starts_with("Name") || t.starts_with("Имя")) && t.contains(':')
    });
    if !has {
        return None;
    }
    let field = |l: &str, key: &str| -> Option<String> {
        let t = l.trim();
        let (k, v) = t.split_once(':')?;
        (k.trim() == key).then(|| v.trim().to_string())
    };
    let ssid = iface.lines().find_map(|l| field(l, "SSID"));
    let sig = iface.lines().find_map(|l| l.split_whitespace().find(|w| w.ends_with('%')).and_then(|w| w.trim_end_matches('%').parse::<u32>().ok()));
    let current = ssid.map(|s| (s, sig.unwrap_or(0)));
    let mut networks: Vec<(String, u32, bool)> = Vec::new();
    for l in nets.lines() {
        let t = l.trim();
        if let Some(rest) = t.strip_prefix("SSID ") {
            if let Some((_, name)) = rest.split_once(':') {
                networks.push((name.trim().to_string(), 0, true));
            }
            continue;
        }
        if let Some(last) = networks.last_mut() {
            if last.1 == 0 {
                if let Some(p) = t.split_whitespace().find(|w| w.ends_with('%')).and_then(|w| w.trim_end_matches('%').parse::<u32>().ok()) {
                    last.1 = p;
                }
            }
            if (t.starts_with("Authentication") || t.starts_with("Проверка подлинности")) && (t.ends_with("Open") || t.ends_with("Открыть") || t.ends_with("Открытая")) {
                last.2 = false;
            }
        }
    }
    networks.retain(|n| !n.0.is_empty());
    networks.sort_by(|a, b| b.1.cmp(&a.1));
    Some((current, networks))
}

fn oem_decode(raw: &[u8]) -> String {
    use windows::Win32::Globalization::{GetOEMCP, MultiByteToWideChar, MULTI_BYTE_TO_WIDE_CHAR_FLAGS};
    unsafe {
        let cp = GetOEMCP();
        let n = MultiByteToWideChar(cp, MULTI_BYTE_TO_WIDE_CHAR_FLAGS(0), raw, None);
        if n <= 0 {
            return String::from_utf8_lossy(raw).to_string();
        }
        let mut w = vec![0u16; n as usize];
        MultiByteToWideChar(cp, MULTI_BYTE_TO_WIDE_CHAR_FLAGS(0), raw, Some(&mut w));
        String::from_utf16_lossy(&w)
    }
}

pub fn wifi_connect(name: &str) {
    use std::os::windows::process::CommandExt;
    let ok = std::process::Command::new("netsh")
        .args(["wlan", "connect", &format!("name={name}")])
        .creation_flags(0x08000000)
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    if !ok {
        // нет сохранённого профиля — нужен пароль, открываем системный список сетей
        crate::win::shell::launch("ms-availablenetworks:", "", None);
    }
}

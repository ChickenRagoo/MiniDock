//! Значки области уведомлений спрятанной панели задач: чтение кнопок трея из памяти Проводника и клики по ним.

use windows::core::w;
use windows::Win32::Foundation::{CloseHandle, HWND, LPARAM, WPARAM};
use windows::Win32::System::Diagnostics::Debug::ReadProcessMemory;
use windows::Win32::System::Memory::{VirtualAllocEx, VirtualFreeEx, MEM_COMMIT, MEM_RELEASE, PAGE_READWRITE};
use windows::Win32::System::Threading::{OpenProcess, PROCESS_QUERY_INFORMATION, PROCESS_VM_OPERATION, PROCESS_VM_READ, PROCESS_VM_WRITE};
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::gfx::Image;
use crate::win::sys;

#[repr(C)]
#[derive(Default, Clone, Copy)]
struct TbButton {
    i_bitmap: i32,
    id_command: i32,
    fs_state: u8,
    fs_style: u8,
    reserved: [u8; 6],
    dw_data: u64,
    i_string: i64,
}

#[repr(C)]
#[derive(Default, Clone, Copy)]
struct TrayData {
    hwnd: u64,
    uid: u32,
    callback: u32,
    reserved: [u32; 2],
    hicon: u64,
}

pub struct TrayIcon {
    pub hwnd: isize,
    pub uid: u32,
    pub msg: u32,
    pub icon: Option<Image>,
    pub tip: String,
}

fn toolbars() -> Vec<HWND> {
    let mut out = Vec::new();
    unsafe {
        let tray = FindWindowW(w!("Shell_TrayWnd"), None).unwrap_or_default();
        let notify = FindWindowExW(Some(tray), None, w!("TrayNotifyWnd"), None).unwrap_or_default();
        let pager = FindWindowExW(Some(notify), None, w!("SysPager"), None).unwrap_or_default();
        if let Ok(tb) = FindWindowExW(Some(pager), None, w!("ToolbarWindow32"), None) {
            out.push(tb);
        }
        if let Ok(ov) = FindWindowW(w!("NotifyIconOverflowWindow"), None) {
            if let Ok(tb) = FindWindowExW(Some(ov), None, w!("ToolbarWindow32"), None) {
                out.push(tb);
            }
        }
    }
    out
}

fn icon_image(h: HICON) -> Option<Image> {
    unsafe {
        let mut info = ICONINFO::default();
        GetIconInfo(h, &mut info).ok()?;
        let img = crate::win::shell::hbitmap_to_image(info.hbmColor);
        let _ = windows::Win32::Graphics::Gdi::DeleteObject(info.hbmColor.into());
        let _ = windows::Win32::Graphics::Gdi::DeleteObject(info.hbmMask.into());
        img
    }
}

pub fn icons() -> Vec<TrayIcon> {
    let mut out: Vec<TrayIcon> = Vec::new();
    for tb in toolbars() {
        let pid = sys::window_pid(tb);
        unsafe {
            let Ok(hp) = OpenProcess(PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_QUERY_INFORMATION, false, pid) else { continue };
            let mem = VirtualAllocEx(hp, None, 4096, MEM_COMMIT, PAGE_READWRITE);
            if mem.is_null() {
                let _ = CloseHandle(hp);
                continue;
            }
            let count = SendMessageW(tb, 0x418, Some(WPARAM(0)), Some(LPARAM(0))).0; // TB_BUTTONCOUNT
            for i in 0..count.max(0) {
                SendMessageW(tb, 0x417, Some(WPARAM(i as usize)), Some(LPARAM(mem as isize))); // TB_GETBUTTON
                let mut b = TbButton::default();
                let _ = ReadProcessMemory(hp, mem, &mut b as *mut _ as *mut _, std::mem::size_of::<TbButton>(), None);
                if b.fs_state & 0x08 != 0 || b.dw_data == 0 {
                    continue; // TBSTATE_HIDDEN
                }
                let mut td = TrayData::default();
                let _ = ReadProcessMemory(hp, b.dw_data as *const _, &mut td as *mut _ as *mut _, std::mem::size_of::<TrayData>(), None);
                let h = td.hwnd as isize;
                if h == 0 || out.iter().any(|x| x.hwnd == h && x.uid == td.uid) || !sys::is_window(HWND(h as *mut _)) {
                    continue;
                }
                let n = SendMessageW(tb, 0x44B, Some(WPARAM(b.id_command as usize)), Some(LPARAM(mem as isize))).0; // TB_GETBUTTONTEXTW
                let mut tip = String::new();
                if n > 0 {
                    let mut buf = vec![0u16; (n as usize + 1).min(1023)];
                    let _ = ReadProcessMemory(hp, mem, buf.as_mut_ptr() as *mut _, buf.len() * 2, None);
                    tip = crate::win::shell::from_wide(&buf);
                }
                let icon = if td.hicon != 0 { icon_image(HICON(td.hicon as *mut _)) } else { None };
                if icon.is_some() {
                    out.push(TrayIcon { hwnd: h, uid: td.uid, msg: td.callback, icon, tip });
                }
            }
            let _ = VirtualFreeEx(hp, mem, 0, MEM_RELEASE);
            let _ = CloseHandle(hp);
        }
    }
    out
}

/// Передать программе клик по её значку в трее.
pub fn click(hwnd: isize, uid: u32, msg: u32, right: bool) {
    let (x, y) = crate::win::cursor_pos();
    let h = HWND(hwnd as *mut _);
    unsafe {
        let _ = AllowSetForegroundWindow(sys::window_pid(h));
        let evs: [u32; 2] = if right { [0x204, 0x205] } else { [0x201, 0x202] };
        for ev in evs {
            let _ = PostMessageW(Some(h), msg, WPARAM(uid as usize), LPARAM(ev as isize));
        }
        // программы с NOTIFYICON_VERSION_4 ждут WM_CONTEXTMENU / NIN_SELECT и координаты в wParam
        let xy = ((y as u32 & 0xFFFF) << 16 | (x as u32 & 0xFFFF)) as usize;
        let code = if right { 0x007B } else { 0x0400 };
        let _ = PostMessageW(Some(h), msg, WPARAM(xy), LPARAM((code | (uid << 16)) as isize));
    }
}

//! DWM: живые миниатюры окон (DwmRegisterThumbnail), нативное размытие (Acrylic/Mica), «подглядывание».

use windows::core::{s, w};
use windows::Win32::Foundation::{HWND, RECT, SIZE};
use windows::Win32::Graphics::Dwm::*;
use windows::Win32::Graphics::Gdi::{CreateRoundRectRgn, SetWindowRgn, HRGN};
use windows::Win32::System::LibraryLoader::{GetProcAddress, LoadLibraryW};
use windows::Win32::UI::Controls::MARGINS;

/// Живая миниатюра окна src, которую DWM рисует в окне dst (аппаратно, без копирования в наш процесс).
pub struct Thumb {
    pub id: isize,
}

impl Thumb {
    pub fn new(dst: HWND, src: HWND) -> Option<Thumb> {
        unsafe { DwmRegisterThumbnail(dst, src).ok().map(|id| Thumb { id }) }
    }

    pub fn source_size(&self) -> (i32, i32) {
        unsafe { DwmQueryThumbnailSourceSize(self.id).map(|s: SIZE| (s.cx, s.cy)).unwrap_or((0, 0)) }
    }

    /// dst — куда в окне-подложке; src — какой участок окна-источника (None — всё окно).
    pub fn update(&self, dst: RECT, src: Option<RECT>, opacity: u8, visible: bool, client_only: bool) {
        let mut flags = DWM_TNP_RECTDESTINATION | DWM_TNP_OPACITY | DWM_TNP_VISIBLE | DWM_TNP_SOURCECLIENTAREAONLY;
        if src.is_some() {
            flags |= DWM_TNP_RECTSOURCE;
        }
        let props = DWM_THUMBNAIL_PROPERTIES {
            dwFlags: flags,
            rcDestination: dst,
            rcSource: src.unwrap_or_default(),
            opacity,
            fVisible: visible.into(),
            fSourceClientAreaOnly: client_only.into(),
        };
        unsafe {
            let _ = DwmUpdateThumbnailProperties(self.id, &props);
        }
    }

    pub fn hide(&self) {
        let props = DWM_THUMBNAIL_PROPERTIES { dwFlags: DWM_TNP_VISIBLE, fVisible: false.into(), ..Default::default() };
        unsafe {
            let _ = DwmUpdateThumbnailProperties(self.id, &props);
        }
    }
}

impl Drop for Thumb {
    fn drop(&mut self) {
        unsafe {
            let _ = DwmUnregisterThumbnail(self.id);
        }
    }
}

pub fn flush() {
    unsafe {
        let _ = DwmFlush();
    }
}

/// Рамка DWM на всё окно: чёрный фон окна становится прозрачным «стеклом» (подложка для миниатюр).
pub fn extend_frame(hwnd: HWND) {
    let m = MARGINS { cxLeftWidth: -1, cxRightWidth: -1, cyTopHeight: -1, cyBottomHeight: -1 };
    unsafe {
        let _ = DwmExtendFrameIntoClientArea(hwnd, &m);
    }
}

#[repr(C)]
struct AccentPolicy {
    state: i32,
    flags: i32,
    color: u32,
    anim: i32,
}

#[repr(C)]
struct WinCompAttrData {
    attr: i32,
    data: *mut core::ffi::c_void,
    size: usize,
}

type SetWca = unsafe extern "system" fn(HWND, *mut WinCompAttrData) -> i32;

fn set_wca() -> Option<SetWca> {
    unsafe {
        let lib = LoadLibraryW(w!("user32.dll")).ok()?;
        let f = GetProcAddress(lib, s!("SetWindowCompositionAttribute"))?;
        Some(std::mem::transmute::<unsafe extern "system" fn() -> isize, SetWca>(f))
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Blur {
    None,
    /// размытие без тонировки (Windows 10)
    Blur,
    /// Acrylic: размытие + шум + тонировка цветом (Windows 10 1803+ / 11)
    Acrylic,
}

/// Нативное размытие под окном. tint — ARGB тонировки акрила.
pub fn set_blur(hwnd: HWND, kind: Blur, tint_argb: u32) {
    let Some(f) = set_wca() else { return };
    let (state, color) = match kind {
        Blur::None => (0, 0),
        Blur::Blur => (3, 0),
        // цвет в ABGR
        Blur::Acrylic => {
            let a = (tint_argb >> 24) & 0xFF;
            let r = (tint_argb >> 16) & 0xFF;
            let g = (tint_argb >> 8) & 0xFF;
            let b = tint_argb & 0xFF;
            (4, (a << 24) | (b << 16) | (g << 8) | r)
        }
    };
    let mut accent = AccentPolicy { state, flags: 2, color, anim: 0 };
    let mut data = WinCompAttrData { attr: 19, data: &mut accent as *mut _ as *mut _, size: std::mem::size_of::<AccentPolicy>() };
    unsafe {
        f(hwnd, &mut data);
    }
}

pub fn windows_build() -> u32 {
    crate::win::sys::reg_read_string(
        windows::Win32::System::Registry::HKEY_LOCAL_MACHINE,
        "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion",
        "CurrentBuildNumber",
    )
    .and_then(|s| s.parse().ok())
    .unwrap_or(19045)
}

pub fn is_win11() -> bool {
    windows_build() >= 22000
}

/// Скруглённые углы окна средствами Windows 11 (на Windows 10 ничего не делает).
pub fn round_corners(hwnd: HWND, on: bool) {
    let pref: i32 = if on { 2 } else { 1 }; // DWMWCP_ROUND / DWMWCP_DONOTROUND
    unsafe {
        let _ = DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, &pref as *const _ as *const _, 4);
    }
}

/// Mica / Acrylic системного фона (Windows 11 22H2+). kind: 2 — Mica, 3 — Acrylic, 4 — Mica Alt.
pub fn system_backdrop(hwnd: HWND, kind: i32) -> bool {
    unsafe {
        let dark: i32 = 1;
        let _ = DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, &dark as *const _ as *const _, 4);
        DwmSetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, &kind as *const _ as *const _, 4).is_ok()
    }
}

/// Область размытия: скруглённый прямоугольник в координатах окна (DWM размывает только внутри региона).
pub fn set_round_region(hwnd: HWND, l: i32, t: i32, r: i32, b: i32, radius: i32) {
    unsafe {
        let rgn: HRGN = CreateRoundRectRgn(l, t, r + 1, b + 1, radius * 2, radius * 2);
        let _ = SetWindowRgn(hwnd, Some(rgn), true);
    }
}

pub fn clear_region(hwnd: HWND) {
    unsafe {
        let _ = SetWindowRgn(hwnd, None, true);
    }
}

/// Окно остаётся видимым при «подглядывании» (DWMWA_EXCLUDED_FROM_PEEK) и не уходит в «Показать рабочий стол».
pub fn exclude_from_peek(hwnd: HWND) {
    let on: i32 = 1;
    unsafe {
        let _ = DwmSetWindowAttribute(hwnd, DWMWA_EXCLUDED_FROM_PEEK, &on as *const _ as *const _, 4);
    }
}

pub fn disable_transitions(hwnd: HWND) {
    let on: i32 = 1;
    unsafe {
        let _ = DwmSetWindowAttribute(hwnd, DWMWA_TRANSITIONS_FORCEDISABLED, &on as *const _ as *const _, 4);
    }
}

type PeekFn = unsafe extern "system" fn(i32, HWND, HWND, u32, *mut core::ffi::c_void) -> i32;

fn peek_fn() -> Option<PeekFn> {
    unsafe {
        let lib = LoadLibraryW(w!("dwmapi.dll")).ok()?;
        // DwmpActivateLivePreview — порядковый номер 113 (им пользуется панель задач Windows)
        let f = GetProcAddress(lib, windows::core::PCSTR(113 as *const u8))?;
        Some(std::mem::transmute::<unsafe extern "system" fn() -> isize, PeekFn>(f))
    }
}

/// Показать окно hwnd на его месте, остальные — прозрачными контурами. None — выйти из режима.
pub fn live_peek(hwnd: Option<HWND>, keep_on_top: HWND) -> bool {
    let Some(f) = peek_fn() else { return false };
    unsafe {
        match hwnd {
            Some(h) => f(1, h, keep_on_top, 1, std::ptr::null_mut()) == 0,
            None => f(0, HWND::default(), HWND::default(), 1, std::ptr::null_mut()) == 0,
        }
    }
}

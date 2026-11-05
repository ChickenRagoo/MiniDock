//! Слоистое окно с попиксельной прозрачностью: буфер DIB (BGRA, предумноженная альфа) + UpdateLayeredWindow.
//!
//! Никакого контекста видеокарты: кадр рисуется процессором прямо в буфер окна, а DWM выводит его сам.

use windows::Win32::Foundation::{COLORREF, HWND, POINT, SIZE};
use windows::Win32::Graphics::Gdi::{
    CreateCompatibleDC, CreateDIBSection, DeleteDC, DeleteObject, GetDC, ReleaseDC, SelectObject, AC_SRC_ALPHA, AC_SRC_OVER,
    BITMAPINFO, BITMAPINFOHEADER, BI_RGB, BLENDFUNCTION, DIB_RGB_COLORS, HBITMAP, HDC, HGDIOBJ,
};
use windows::Win32::UI::WindowsAndMessaging::{UpdateLayeredWindow, ULW_ALPHA};

use crate::gfx::{Canvas, PixmapMut};

pub struct Surface {
    dc: HDC,
    bmp: HBITMAP,
    old: HGDIOBJ,
    bits: *mut u8,
    pub w: i32,
    pub h: i32,
}

impl Default for Surface {
    fn default() -> Self {
        Surface { dc: HDC::default(), bmp: HBITMAP::default(), old: HGDIOBJ::default(), bits: std::ptr::null_mut(), w: 0, h: 0 }
    }
}

impl Surface {
    /// Буфер нужного размера (пересоздаётся только при изменении размера).
    pub fn ensure(&mut self, w: i32, h: i32) -> bool {
        let (w, h) = (w.max(1), h.max(1));
        if w == self.w && h == self.h && !self.bits.is_null() {
            return true;
        }
        self.free();
        unsafe {
            let screen = GetDC(None);
            let dc = CreateCompatibleDC(Some(screen));
            ReleaseDC(None, screen);
            let bmi = BITMAPINFO {
                bmiHeader: BITMAPINFOHEADER {
                    biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                    biWidth: w,
                    biHeight: -h,
                    biPlanes: 1,
                    biBitCount: 32,
                    biCompression: BI_RGB.0,
                    ..Default::default()
                },
                ..Default::default()
            };
            let mut bits = std::ptr::null_mut();
            let Ok(bmp) = CreateDIBSection(Some(dc), &bmi, DIB_RGB_COLORS, &mut bits, None, 0) else {
                let _ = DeleteDC(dc);
                return false;
            };
            self.old = SelectObject(dc, bmp.into());
            self.dc = dc;
            self.bmp = bmp;
            self.bits = bits as *mut u8;
            self.w = w;
            self.h = h;
        }
        true
    }

    pub fn free(&mut self) {
        if self.bits.is_null() {
            return;
        }
        unsafe {
            SelectObject(self.dc, self.old);
            let _ = DeleteObject(self.bmp.into());
            let _ = DeleteDC(self.dc);
        }
        self.dc = HDC::default();
        self.bmp = HBITMAP::default();
        self.old = HGDIOBJ::default();
        self.bits = std::ptr::null_mut();
        self.w = 0;
        self.h = 0;
    }

    pub fn is_allocated(&self) -> bool {
        !self.bits.is_null()
    }

    pub fn bytes(&self) -> usize {
        if self.bits.is_null() {
            0
        } else {
            (self.w * self.h * 4) as usize
        }
    }

    pub fn data(&mut self) -> &mut [u8] {
        unsafe { std::slice::from_raw_parts_mut(self.bits, (self.w * self.h * 4) as usize) }
    }

    pub fn canvas(&mut self) -> Canvas<'_> {
        let (w, h) = (self.w as u32, self.h as u32);
        let data = self.data();
        Canvas::new(PixmapMut::from_bytes(data, w, h).expect("буфер окна"))
    }

    /// Вывести буфер в окно: (x, y) — где окно на экране, alpha — общая прозрачность.
    pub fn present(&self, hwnd: HWND, x: i32, y: i32, alpha: u8) -> bool {
        if self.bits.is_null() {
            return false;
        }
        unsafe {
            let blend = BLENDFUNCTION {
                BlendOp: AC_SRC_OVER as u8,
                BlendFlags: 0,
                SourceConstantAlpha: alpha,
                AlphaFormat: AC_SRC_ALPHA as u8,
            };
            let pos = POINT { x, y };
            let size = SIZE { cx: self.w, cy: self.h };
            let src = POINT { x: 0, y: 0 };
            UpdateLayeredWindow(hwnd, None, Some(&pos), Some(&size), Some(self.dc), Some(&src), COLORREF(0), Some(&blend), ULW_ALPHA)
                .is_ok()
        }
    }
}

impl Drop for Surface {
    fn drop(&mut self) {
        self.free();
    }
}

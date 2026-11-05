//! Снимки: окно (PrintWindow, для программ на видеокарте — с экрана) и участки экрана.

use windows::Win32::Foundation::{HWND, RECT};
use windows::Win32::Graphics::Gdi::*;
use windows::Win32::Storage::Xps::{PrintWindow, PRINT_WINDOW_FLAGS};

use crate::gfx::Image;
use crate::win::sys;

struct Dib {
    dc: HDC,
    bmp: HBITMAP,
    old: HGDIOBJ,
    bits: *mut u8,
    w: i32,
    h: i32,
}

impl Dib {
    fn new(w: i32, h: i32) -> Option<Dib> {
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
            let bmp = CreateDIBSection(Some(dc), &bmi, DIB_RGB_COLORS, &mut bits, None, 0).ok()?;
            let old = SelectObject(dc, bmp.into());
            Some(Dib { dc, bmp, old, bits: bits as *mut u8, w, h })
        }
    }

    fn into_image(self, crop: Option<RECT>) -> Option<Image> {
        let (w, h) = (self.w, self.h);
        let src = unsafe { std::slice::from_raw_parts(self.bits, (w * h * 4) as usize) };
        let c = crop.unwrap_or(RECT { left: 0, top: 0, right: w, bottom: h });
        let (cw, ch) = ((c.right - c.left).clamp(1, w), (c.bottom - c.top).clamp(1, h));
        let mut data = vec![0u8; (cw * ch * 4) as usize];
        for y in 0..ch {
            let sy = (y + c.top).clamp(0, h - 1);
            let so = ((sy * w + c.left.clamp(0, w - 1)) * 4) as usize;
            let n = (cw.min(w - c.left.max(0)) * 4) as usize;
            data[(y * cw * 4) as usize..(y * cw * 4) as usize + n].copy_from_slice(&src[so..so + n]);
        }
        for p in data.chunks_mut(4) {
            p[3] = 255; // снимок окна непрозрачный
        }
        Image::from_bgra_premul(cw as u32, ch as u32, data)
    }
}

impl Drop for Dib {
    fn drop(&mut self) {
        unsafe {
            SelectObject(self.dc, self.old);
            let _ = DeleteObject(self.bmp.into());
            let _ = DeleteDC(self.dc);
        }
    }
}

fn is_black(img: &Image) -> bool {
    let d = img.pm.data();
    let (w, h) = (img.w as usize, img.h as usize);
    let (sx, sy) = ((w / 7).max(1), (h / 7).max(1));
    let mut y = 2;
    while y < h {
        let mut x = 2;
        while x < w {
            let i = (y * w + x) * 4;
            if d[i] | d[i + 1] | d[i + 2] != 0 {
                return false;
            }
            x += sx;
        }
        y += sy;
    }
    true
}

/// Кадр окна (видимая рамка) не шире max_w: (картинка, рамка на экране).
pub fn window_image(hwnd: HWND, max_w: u32) -> Option<(Image, RECT)> {
    let wr = sys::get_rect(hwnd);
    let f = sys::visible_frame(hwnd);
    let (w, h) = (wr.right - wr.left, wr.bottom - wr.top);
    if w <= 8 || h <= 8 || w > 12000 || h > 12000 || wr.left <= -30000 {
        return None;
    }
    let mut img = None;
    if let Some(dib) = Dib::new(w, h) {
        let ok = unsafe { PrintWindow(hwnd, dib.dc, PRINT_WINDOW_FLAGS(2)).as_bool() }; // PW_RENDERFULLCONTENT
        if ok {
            let crop = RECT { left: f.left - wr.left, top: f.top - wr.top, right: f.right - wr.left, bottom: f.bottom - wr.top };
            img = dib.into_image(Some(crop)).filter(|i| !is_black(i));
        }
    }
    // программы на видеокарте отдают PrintWindow чёрный кадр — тогда снимаем окно прямо с экрана
    if img.is_none() && sys::foreground() == hwnd && !sys::is_iconic(hwnd) {
        img = grab_screen(f);
    }
    let img = img?;
    let img = if img.w > max_w { img.scaled(max_w, (img.h as f32 * max_w as f32 / img.w as f32).round() as u32) } else { img };
    Some((img, f))
}

/// Участок экрана (уже скомпонованный DWM).
pub fn grab_screen(r: RECT) -> Option<Image> {
    let (w, h) = (r.right - r.left, r.bottom - r.top);
    if w <= 0 || h <= 0 {
        return None;
    }
    let dib = Dib::new(w, h)?;
    unsafe {
        let screen = GetDC(None);
        let _ = BitBlt(dib.dc, 0, 0, w, h, Some(screen), r.left, r.top, SRCCOPY | CAPTUREBLT);
        ReleaseDC(None, screen);
    }
    dib.into_image(None)
}

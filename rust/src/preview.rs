//! Живые миниатюры окон над доком при наведении на иконку (DWM — видео продолжает играть).
//!
//! DWM рисует только прямоугольники и только в нативном окне, поэтому слоёв три:
//!   1. панель (фон, подсветка), под ней — нативное размытие;
//!   2. окно-хост, в котором DWM рисует миниатюры;
//!   3. прозрачный слой поверх: скругляет уголки миниатюр, рисует обводку и кружок «закрыть».
//! Верхние слои пропускают клики насквозь — их обрабатывает панель.

use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::app::App;
use crate::config::Config;
use crate::gfx::*;
use crate::glass::Glass;
use crate::text::{self, Align, Font};
use crate::win::{self, dwm::Thumb, sys, Surface};

const GAP: f32 = 16.0;
const RADIUS: f32 = 16.0;

pub struct Thumbs {
    host: HWND,
    mask: HWND,
    mask_surf: Surface,
    thumbs: Vec<(isize, Thumb)>,
    pub shown: bool,
}

impl Thumbs {
    pub fn new() -> Thumbs {
        let host = unsafe {
            CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
                win::OVERLAY_CLASS,
                windows::core::w!("MiniDockThumbs"),
                WS_POPUP,
                0,
                0,
                1,
                1,
                None,
                None,
                Some(win::hinstance()),
                None,
            )
            .unwrap_or_default()
        };
        unsafe {
            let _ = SetLayeredWindowAttributes(host, windows::Win32::Foundation::COLORREF(0), 255, LWA_ALPHA);
        }
        win::dwm::extend_frame(host);
        win::dwm::exclude_from_peek(host);
        win::dwm::disable_transitions(host);
        let mask = win::create_layered("MiniDockThumbMask", true, true);
        win::dwm::exclude_from_peek(mask);
        Thumbs { host, mask, mask_surf: Surface::default(), thumbs: Vec::new(), shown: false }
    }

    /// Показать миниатюры: rect — окно панели (экран), tiles — [(окно, прямоугольник в панели, физ. пиксели)].
    pub fn show(&mut self, above: HWND, rect: RECT, tiles: &[(isize, R)], opacity: u8) {
        self.thumbs.retain(|(h, _)| tiles.iter().any(|t| t.0 == *h));
        for (h, _) in tiles {
            if !self.thumbs.iter().any(|t| t.0 == *h) {
                if let Some(t) = Thumb::new(self.host, win::hwnd_of(*h)) {
                    self.thumbs.push((*h, t));
                }
            }
        }
        for (h, r) in tiles {
            if let Some((_, th)) = self.thumbs.iter().find(|t| t.0 == *h) {
                let (sw, sh) = th.source_size();
                let ins = sys::window_geometry(win::hwnd_of(*h)).map(|g| g.1).unwrap_or([7, 0, 7, 7]);
                let ins = if sw <= ins[0] + ins[2] + 8 || sh <= ins[1] + ins[3] + 8 { [0; 4] } else { ins };
                let src = RECT { left: ins[0], top: ins[1], right: sw - ins[2], bottom: sh - ins[3] };
                let dst = RECT { left: r.x.round() as i32, top: r.y.round() as i32, right: r.right().round() as i32, bottom: r.bottom().round() as i32 };
                th.update(dst, Some(src), opacity, true, false);
            }
        }
        unsafe {
            let _ = SetWindowPos(self.host, Some(above), rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top, SWP_NOACTIVATE | SWP_SHOWWINDOW);
        }
        self.shown = true;
    }

    /// Слой поверх миниатюр: уголки цветом панели, обводка и что-нибудь ещё (кружок «закрыть»).
    pub fn mask(&mut self, rect: RECT, tiles: &[R], radius: f32, corner: Color, alpha: u8, overlay: impl FnOnce(&mut Canvas)) {
        let (w, h) = (rect.right - rect.left, rect.bottom - rect.top);
        if !self.mask_surf.ensure(w, h) {
            return;
        }
        {
            let mut cv = self.mask_surf.canvas();
            cv.clear();
            for r in tiles {
                // уголки: квадрат минус скруглённый прямоугольник
                let mut pb = PathBuilder::new();
                if let Some(rect) = r.sk() {
                    pb.push_rect(rect);
                }
                if let (Some(sq), Some(rr)) = (pb.finish(), rounded_rect(*r, radius)) {
                    let mut p = solid(corner);
                    p.anti_alias = true;
                    let mut mask = Mask::new(w as u32, h as u32).unwrap();
                    mask.fill_path(&sq, FillRule::Winding, true, Transform::identity());
                    let mut inner = Mask::new(w as u32, h as u32).unwrap();
                    inner.fill_path(&rr, FillRule::Winding, true, Transform::identity());
                    // mask = квадрат и НЕ скруглённый
                    let md = mask.data_mut();
                    for (a, b) in md.iter_mut().zip(inner.data()) {
                        *a = ((*a as u16 * (255 - *b as u16)) / 255) as u8;
                    }
                    cv.pm.fill_rect(r.adjusted(-1.0, -1.0, 1.0, 1.0).sk().unwrap(), &p, Transform::identity(), Some(&mask));
                }
                cv.stroke_rrect(r.inset(0.5), radius, rgba(255, 255, 255, 42), 1.0);
            }
            overlay(&mut cv);
        }
        self.mask_surf.present(self.mask, rect.left, rect.top, alpha);
        if !win::is_visible(self.mask) {
            win::show_noactivate(self.mask);
        }
        unsafe {
            let _ = SetWindowPos(self.mask, Some(HWND_TOPMOST), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
        }
    }

    pub fn hide(&mut self) {
        self.thumbs.clear();
        if self.shown {
            self.shown = false;
            win::hide(self.host);
            win::hide(self.mask);
            self.mask_surf.free();
        }
    }
}

/// Ширины миниатюр одной высоты по пропорциям окон; если ряд шире max_width — всё уменьшается.
pub fn row_layout(hwnds: &[isize], height: f32, gap: f32, max_width: f32) -> (Vec<f32>, f32) {
    let widths: Vec<f32> = hwnds.iter().map(|h| height * sys::window_aspect(win::hwnd_of(*h))).collect();
    let total: f32 = widths.iter().sum::<f32>() + gap * (widths.len().max(1) - 1) as f32;
    let k = if total > 0.0 { (max_width / total).min(1.0) } else { 1.0 };
    (widths.iter().map(|w| w * k).collect(), height * k)
}

/// Кружок с крестиком: красный под курсором, иначе тёмный полупрозрачный.
pub fn close_button(cv: &mut Canvas, r: R, hot: bool, s: f32) {
    cv.fill_circle(r.cx(), r.cy(), r.w / 2.0, if hot { rgba(232, 17, 35, 255) } else { rgba(20, 20, 24, 200) });
    text::draw_in(cv, "\u{E711}", Font::Icons, 9.0 * s, r, Align::Center, rgba(255, 255, 255, 255));
}

pub fn close_rect(t: R, s: f32) -> R {
    R::new(t.right() - 24.0 * s, t.y + 6.0 * s, 18.0 * s, 18.0 * s)
}

pub struct Preview {
    hwnd: HWND,
    surf: Surface,
    glass: Glass,
    thumbs: Thumbs,
    pub visible: bool,
    /// (док, иконка) — для чьих окон предпросмотр
    pub item: Option<(usize, u64)>,
    hwnds: Vec<isize>,
    tiles: Vec<(isize, R)>,
    hover: Option<(usize, bool)>,
    x: i32,
    y: i32,
    w: i32,
    h: i32,
    scale: f32,
    opened: f64,
    pending: Option<(usize, u64, RECT, f64)>,
    hide_at: Option<f64>,
    peek_at: Option<(isize, f64)>,
    peeking: Option<isize>,
    watch_at: f64,
    tracking: bool,
}

impl Preview {
    pub fn new() -> Preview {
        Preview {
            hwnd: HWND::default(),
            surf: Surface::default(),
            glass: Glass::new(),
            thumbs: Thumbs::new(),
            visible: false,
            item: None,
            hwnds: Vec::new(),
            tiles: Vec::new(),
            hover: None,
            x: 0,
            y: 0,
            w: 0,
            h: 0,
            scale: 1.0,
            opened: 0.0,
            pending: None,
            hide_at: None,
            peek_at: None,
            peeking: None,
            watch_at: 0.0,
            tracking: false,
        }
    }

    fn ensure_window(&mut self) {
        if self.hwnd.is_invalid() {
            self.hwnd = win::create_layered("MiniDockPreview", true, false);
            win::dwm::exclude_from_peek(self.hwnd);
        }
    }

    pub fn mouse_inside(&self) -> bool {
        if !self.visible {
            return false;
        }
        let (cx, cy) = win::cursor_pos();
        let m = (6.0 * self.scale) as i32;
        cx >= self.x - m && cx < self.x + self.w + m && cy >= self.y - m && cy < self.y + self.h + m
    }

    fn pad(cfg: &Config) -> f32 {
        cfg.dock_preview_pad as f32
    }

    fn thumb_radius(cfg: &Config) -> f32 {
        (RADIUS - Self::pad(cfg)).max(4.0)
    }

    fn layout(&mut self, cfg: &Config) -> (f32, f32) {
        let s = self.scale;
        let pad = Self::pad(cfg) * s;
        let h = cfg.dock_preview as f32 * 0.5625 * s;
        let mon = sys::point_monitor(self.x + self.w / 2, self.y + self.h / 2);
        let (widths, h) = row_layout(&self.hwnds, h, GAP * s, mon.w() as f32 - 80.0 * s - 2.0 * pad);
        let mut x = pad;
        self.tiles.clear();
        for (hw, w) in self.hwnds.iter().zip(widths) {
            self.tiles.push((*hw, R::new(x.round(), pad, w.round(), h.round())));
            x += w + GAP * s;
        }
        (x - GAP * s + pad, pad + h + pad)
    }

    /// Открыть для иконки (anchor — её прямоугольник на экране) с окнами hwnds.
    fn open(&mut self, cfg: &Config, item: (usize, u64), hwnds: Vec<isize>, anchor: RECT, side: crate::dock::Side) {
        let hwnds: Vec<isize> = hwnds.into_iter().filter(|h| sys::is_window(win::hwnd_of(*h))).take(6).collect();
        if hwnds.is_empty() {
            return;
        }
        self.ensure_window();
        self.stop_peek();
        let mon = sys::point_monitor((anchor.left + anchor.right) / 2, (anchor.top + anchor.bottom) / 2);
        self.scale = mon.scale;
        self.item = Some(item);
        self.hwnds = hwnds;
        self.hover = None;
        let (w, h) = self.layout(cfg);
        let s = self.scale;
        let (acx, acy) = ((anchor.left + anchor.right) as f32 / 2.0, (anchor.top + anchor.bottom) as f32 / 2.0);
        let (mut x, mut y) = match side {
            crate::dock::Side::Bottom => (acx - w / 2.0, anchor.top as f32 - h - 12.0 * s),
            crate::dock::Side::Left => (anchor.right as f32 + 12.0 * s, acy - h / 2.0),
            crate::dock::Side::Right => (anchor.left as f32 - w - 12.0 * s, acy - h / 2.0),
        };
        let m = mon.rect;
        x = x.clamp(m.left as f32 + 6.0 * s, m.right as f32 - w - 6.0 * s);
        y = y.clamp(m.top as f32 + 6.0 * s, m.bottom as f32 - h - 6.0 * s);
        self.x = x as i32;
        self.y = y as i32;
        self.w = w.ceil() as i32;
        self.h = h.ceil() as i32;
        self.opened = crate::anim::now();
        self.visible = true;
        self.hide_at = None;
        self.paint(cfg);
        win::show_noactivate(self.hwnd);
        win::set_topmost(self.hwnd);
    }

    pub fn close(&mut self) {
        self.pending = None;
        self.hide_at = None;
        self.stop_peek();
        self.thumbs.hide();
        if self.visible {
            self.visible = false;
            win::hide(self.hwnd);
            self.glass.hide();
            self.surf.free();
        }
        self.item = None;
    }

    fn paint(&mut self, cfg: &Config) {
        if !self.surf.ensure(self.w, self.h) {
            return;
        }
        let s = self.scale;
        let k = ease_out(((crate::anim::now() - self.opened) / 0.17).min(1.0) as f32);
        let full = R::new(0.0, 0.0, self.w as f32, self.h as f32);
        let tr = Self::thumb_radius(cfg) * s;
        let bg = rgba(30, 30, 36, 165);
        let hover = self.hover;
        let tiles = self.tiles.clone();
        {
            let mut cv = self.surf.canvas();
            cv.clear();
            cv.fill_rrect(full.inset(0.5), RADIUS * s, bg);
            cv.stroke_rrect(full.inset(0.5), RADIUS * s, rgba(255, 255, 255, 40), 1.0);
            if let Some((i, _)) = hover {
                if let Some((_, t)) = tiles.get(i) {
                    cv.fill_rrect(t.adjusted(-5.0 * s, -5.0 * s, 5.0 * s, 5.0 * s), tr + 4.0 * s, rgba(255, 255, 255, 40));
                }
            }
        }
        let a = (255.0 * k) as u8;
        self.surf.present(self.hwnd, self.x, self.y, a);
        self.glass.place(self.x, self.y, self.x + self.w, self.y + self.h, (RADIUS * s) as i32, self.hwnd, win::dwm::Blur::Acrylic, 0x01000000);
        let rect = RECT { left: self.x, top: self.y, right: self.x + self.w, bottom: self.y + self.h };
        self.thumbs.show(self.hwnd, rect, &tiles, a);
        let rects: Vec<R> = tiles.iter().map(|t| t.1).collect();
        // уголки — цветом панели поверх размытия (почти непрозрачно: настоящего размытия в уголках нет)
        let corner = rgba(34, 34, 40, 235);
        let close = hover.and_then(|(i, hot)| tiles.get(i).map(|t| (close_rect(t.1, s), hot)));
        self.thumbs.mask(rect, &rects, tr, corner, a, |cv| {
            if let Some((r, hot)) = close {
                close_button(cv, r, hot, s);
            }
        });
    }

    fn hit(&self, x: f32, y: f32) -> Option<(usize, bool)> {
        let s = self.scale;
        for (i, (_, t)) in self.tiles.iter().enumerate() {
            if close_rect(*t, s).contains(x, y) {
                return Some((i, true));
            }
            if t.adjusted(-6.0 * s, -6.0 * s, 6.0 * s, 6.0 * s).contains(x, y) {
                return Some((i, false));
            }
        }
        None
    }

    fn start_peek(&mut self, h: isize) {
        if win::dwm::live_peek(Some(win::hwnd_of(h)), self.hwnd) {
            self.peeking = Some(h);
        }
    }

    fn stop_peek(&mut self) {
        self.peek_at = None;
        if self.peeking.take().is_some() {
            win::dwm::live_peek(None, HWND::default());
        }
    }

    fn set_hover(&mut self, cfg: &Config, hover: Option<(usize, bool)>) {
        if hover == self.hover {
            return;
        }
        self.hover = hover;
        // окно целиком на своём месте при наведении на миниатюру (как в Windows)
        let target = hover.and_then(|(i, _)| self.hwnds.get(i).copied());
        match target {
            None => self.stop_peek(),
            Some(_) if !cfg.dock_peek => self.stop_peek(),
            Some(t) => {
                if self.peeking.is_some() {
                    if self.peeking != Some(t) {
                        self.start_peek(t); // уже подглядываем — между миниатюрами сразу
                    }
                } else if self.peek_at.map(|p| p.0) != Some(t) {
                    self.peek_at = Some((t, crate::anim::now() + 0.45));
                }
            }
        }
        self.paint(cfg);
    }

    /// Кадр: появление, таймеры показа/скрытия/подглядывания, слежение за окнами.
    pub fn frame(&mut self, cfg: &Config) -> bool {
        let now = crate::anim::now();
        let mut busy = false;
        if self.visible {
            let t = (now - self.opened) / 0.17;
            if t <= 1.05 {
                self.paint(cfg);
                busy = true;
            }
            if let Some((h, at)) = self.peek_at {
                busy = true;
                if now >= at {
                    self.peek_at = None;
                    self.start_peek(h);
                }
            }
            if let Some(at) = self.hide_at {
                busy = true;
                if now >= at {
                    self.hide_at = None;
                    if !self.mouse_inside() {
                        self.close();
                        return false;
                    }
                }
            }
            if now - self.watch_at > 0.25 {
                self.watch_at = now;
                // окна закрыли (из меню дока, самим приложением, Alt+F4) — перестроить
                if !self.hwnds.iter().all(|h| alive(*h)) {
                    self.refresh_after_close(cfg);
                }
            }
            busy = true; // пока открыт — проверяем окна
        }
        if let Some((_, _, _, at)) = self.pending {
            busy = true;
            let _ = at;
        }
        busy
    }

    fn refresh_after_close(&mut self, cfg: &Config) {
        if let Some(p) = self.peeking {
            if !alive(p) {
                self.stop_peek();
            }
        }
        self.hwnds.retain(|h| alive(*h));
        if self.hwnds.is_empty() {
            self.close();
            return;
        }
        let (w, h) = self.layout(cfg);
        // левый край не двигаем
        self.w = w.ceil() as i32;
        self.h = h.ceil() as i32;
        self.hover = None;
        self.paint(cfg);
    }
}

fn alive(h: isize) -> bool {
    let w = win::hwnd_of(h);
    sys::is_window(w) && sys::is_visible(w)
}

/// Из дока, каждый кадр/проверку простоя: курсор на иконке с окнами — показать (с задержкой) предпросмотр.
pub fn idle(app: &mut App) -> bool {
    let cfg = app.cfg.clone();
    let now = crate::anim::now();
    let mut want: Option<(usize, u64, Vec<isize>, RECT)> = None;
    for (di, d) in app.docks.iter().enumerate() {
        if !d.visible || d.dragging || app.menus.is_open() {
            continue;
        }
        if let Some(id) = d.hovered() {
            if let Some(it) = d.item(id) {
                if it.is_app() && !it.hwnds.is_empty() {
                    want = Some((di, id, it.hwnds.clone(), d.icon_screen_rect(&cfg, Some(id))));
                }
            }
        }
    }
    let pv = &mut app.preview;
    match want {
        Some((di, id, hwnds, anchor)) if cfg.dock_previews => {
            pv.hide_at = None;
            if pv.visible {
                if pv.item != Some((di, id)) {
                    let side = app.docks[di].side;
                    pv.open(&cfg, (di, id), hwnds, anchor, side); // уже показан — переключаемся сразу
                }
            } else {
                match pv.pending {
                    Some((pd, pid, _, at)) if pd == di && pid == id => {
                        if now >= at {
                            pv.pending = None;
                            let side = app.docks[di].side;
                            pv.open(&cfg, (di, id), hwnds, anchor, side);
                        }
                    }
                    _ => pv.pending = Some((di, id, anchor, now + cfg.dock_preview_delay as f64)),
                }
            }
            true
        }
        _ => {
            pv.pending = None;
            if pv.visible && pv.hide_at.is_none() && !pv.mouse_inside() {
                pv.hide_at = Some(now + 0.28);
            }
            pv.visible
        }
    }
}

pub fn on_message(app: &mut App, hwnd: HWND, msg: u32, _wp: WPARAM, lp: LPARAM) -> Option<LRESULT> {
    if app.preview.hwnd != hwnd || hwnd.is_invalid() {
        return None;
    }
    let (x, y) = win::lparam_xy(lp);
    let cfg = app.cfg.clone();
    match msg {
        WM_MOUSEACTIVATE => return Some(LRESULT(MA_NOACTIVATE as isize)),
        WM_MOUSEMOVE => {
            let pv = &mut app.preview;
            pv.hide_at = None;
            if !pv.tracking {
                pv.tracking = true;
                let mut tme = windows::Win32::UI::Input::KeyboardAndMouse::TRACKMOUSEEVENT {
                    cbSize: std::mem::size_of::<windows::Win32::UI::Input::KeyboardAndMouse::TRACKMOUSEEVENT>() as u32,
                    dwFlags: windows::Win32::UI::Input::KeyboardAndMouse::TME_LEAVE,
                    hwndTrack: hwnd,
                    dwHoverTime: 0,
                };
                unsafe {
                    let _ = windows::Win32::UI::Input::KeyboardAndMouse::TrackMouseEvent(&mut tme);
                }
            }
            let h = pv.hit(x as f32, y as f32);
            pv.set_hover(&cfg, h);
        }
        win::WM_MOUSELEAVE => {
            let pv = &mut app.preview;
            pv.tracking = false;
            pv.set_hover(&cfg, None);
            pv.hide_at = Some(crate::anim::now() + 0.28);
        }
        WM_LBUTTONUP => {
            let pv = &mut app.preview;
            let Some((i, close)) = pv.hit(x as f32, y as f32) else { return Some(LRESULT(0)) };
            let h = pv.hwnds[i];
            pv.stop_peek(); // клик — выходим из подглядывания до переключения или закрытия
            if close {
                sys::close_window(win::hwnd_of(h));
            } else {
                pv.close();
                if sys::is_iconic(win::hwnd_of(h)) {
                    app.restore_window(h);
                } else {
                    sys::force_foreground(win::hwnd_of(h));
                }
            }
        }
        _ => return None,
    }
    app.request_frame();
    Some(LRESULT(0))
}

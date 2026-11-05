//! Большие часы посреди экрана, пока зажато сочетание клавиш (настройка «Большие часы»).
//! Нажал — плавно появляются (как Spotlight), отпустил — исчезают. Окно не забирает фокус и клики.

use windows::Win32::Foundation::HWND;
use windows::Win32::System::SystemInformation::GetLocalTime;

use crate::app::App;
use crate::config::Config;
use crate::gfx::*;
use crate::glass::Glass;
use crate::text::{self, Align, Font};
use crate::win::{self, sys, Surface};

pub const WEEKDAYS: [&str; 7] = ["воскресенье", "понедельник", "вторник", "среда", "четверг", "пятница", "суббота"];
pub const MONTHS_GEN: [&str; 12] = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];

pub struct BigClock {
    hwnd: HWND,
    surf: Surface,
    glass: Option<Glass>,
    shown: bool,
    k: f32,
    target: f32,
    last: f64,
    x: i32,
    y: i32,
    w: i32,
    h: i32,
    scale: f32,
    second: u16,
}

impl BigClock {
    pub fn new() -> BigClock {
        BigClock { hwnd: HWND::default(), surf: Surface::default(), glass: None, shown: false, k: 0.0, target: 0.0, last: 0.0, x: 0, y: 0, w: 0, h: 0, scale: 1.0, second: 99 }
    }

    pub fn visible(&self) -> bool {
        self.shown
    }

    fn ensure_window(&mut self) {
        if self.hwnd.is_invalid() {
            self.hwnd = win::create_layered("MiniDockClock", true, true);
            win::dwm::exclude_from_peek(self.hwnd);
        }
    }

    pub fn frame(&mut self, cfg: &Config) -> bool {
        if !self.shown {
            return false;
        }
        let now = crate::anim::now();
        let dt = ((now - self.last) as f32).clamp(0.001, 0.05);
        self.last = now;
        let moving = self.k != self.target;
        if moving {
            let speed = if self.target > self.k { 1.0 / 0.16 } else { 1.0 / 0.12 };
            let step = dt * speed;
            self.k = if self.target > self.k { (self.k + step).min(self.target) } else { (self.k - step).max(self.target) };
        }
        let t = unsafe { GetLocalTime() };
        let tick = t.wSecond != self.second;
        if moving || tick {
            self.second = t.wSecond;
            self.paint(cfg);
        }
        if self.k <= 0.0 && self.target <= 0.0 {
            self.finish_hide();
            return false;
        }
        // кадры нужны только для появления/исчезновения; секунды — раз в секунду из опроса
        self.k != self.target
    }

    pub fn on_second(&mut self, cfg: &Config) {
        if self.shown && self.k == self.target {
            self.paint(cfg);
        }
    }

    fn paint(&mut self, cfg: &Config) {
        let s = self.scale;
        if !self.surf.ensure(self.w, self.h) {
            return;
        }
        let t = unsafe { GetLocalTime() };
        let e = ease_out(self.k);
        let full = R::new(0.0, 0.0, self.w as f32, self.h as f32);
        // появление: чуть увеличивается из 0.94 и проявляется
        let k = 0.94 + 0.06 * e;
        let panel = full.inset(24.0 * s).scaled(k);
        let radius = 28.0 * s;
        let time = format!("{:02}:{:02}", t.wHour, t.wMinute);
        let secs = format!("{:02}", t.wSecond);
        let date = format!("{}, {} {}", WEEKDAYS[t.wDayOfWeek as usize % 7], t.wDay, MONTHS_GEN[(t.wMonth as usize + 11) % 12]);
        let show_secs = cfg.clock_seconds;
        {
            let mut cv = self.surf.canvas();
            cv.clear();
            cv.shadow(panel, radius, 22.0 * s, 10.0 * s, 0.45 * e);
            cv.fill_rrect(panel, radius, rgba(28, 28, 34, 170));
            cv.stroke_rrect(panel.inset(0.5), radius, rgba(255, 255, 255, 46), 1.0);
            let big = 118.0 * s * k;
            let small = 40.0 * s * k;
            let tw = text::width(&time, Font::Light, big);
            let sw = if show_secs { text::width(&secs, Font::Light, small) + 10.0 * s } else { 0.0 };
            let (line, asc) = text::metrics(Font::Light, big);
            let x0 = panel.cx() - (tw + sw) / 2.0;
            let base = panel.y + panel.h * 0.52 - line / 2.0 + asc;
            text::draw_at(&mut cv, &time, Font::Light, big, x0.round(), base.round(), rgba(250, 250, 252, 255));
            if show_secs {
                text::draw_at(&mut cv, &secs, Font::Light, small, (x0 + tw + 10.0 * s).round(), base.round(), rgba(255, 255, 255, 150));
            }
            let dr = R::new(panel.x, base + 14.0 * s * k, panel.w, 30.0 * s * k);
            text::draw_in(&mut cv, &date, Font::Regular, 20.0 * s * k, dr, Align::Center, rgba(235, 235, 240, 200));
        }
        let alpha = (e * 255.0) as u8;
        self.surf.present(self.hwnd, self.x, self.y, alpha);
        if let Some(g) = self.glass.as_mut() {
            if e > 0.05 {
                let p = panel;
                g.place(self.x + p.x as i32, self.y + p.y as i32, self.x + p.right() as i32, self.y + p.bottom() as i32, radius as i32, self.hwnd, win::dwm::Blur::Acrylic, 0x01000000);
            } else {
                g.hide();
            }
        }
    }

    fn finish_hide(&mut self) {
        self.shown = false;
        win::hide(self.hwnd);
        self.glass = None; // окно подложки не держим, пока часы не нужны
        self.surf.free();
    }
}

pub fn show(app: &mut App) {
    let c = &mut app.clock;
    c.ensure_window();
    if !c.shown {
        // на мониторе, где курсор
        let (cx, cy) = win::cursor_pos();
        let m = sys::point_monitor(cx, cy);
        c.scale = m.scale;
        let (w, h) = ((560.0 * m.scale) as i32, (300.0 * m.scale) as i32);
        c.w = w;
        c.h = h;
        c.x = m.rect.left + (m.w() - w) / 2;
        c.y = m.rect.top + (m.h() - h) / 2 - (m.h() / 12);
        c.shown = true;
        c.k = 0.0;
        c.glass = Some(Glass::new());
        c.last = crate::anim::now();
        c.second = 99;
        let cfg = app.cfg.clone();
        app.clock.paint(&cfg);
        win::show_noactivate(app.clock.hwnd);
        win::set_topmost(app.clock.hwnd);
    }
    app.clock.target = 1.0;
    app.request_frame();
}

pub fn hide(app: &mut App) {
    app.clock.target = 0.0;
    app.request_frame();
}

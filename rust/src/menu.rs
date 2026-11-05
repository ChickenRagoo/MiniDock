//! Контекстные меню в стиле macOS: «стекло», скругление, плавное появление, подменю, клавиатура.

use windows::Win32::Foundation::HWND;
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::app::{Action, App};
use crate::gfx::*;
use crate::text::{self, Align, Font};
use crate::win::{self, sys, Surface};

#[derive(Clone)]
pub struct MenuItem {
    pub label: String,
    pub action: Option<Action>,
    pub checked: Option<bool>,
    pub radio: bool,
    pub enabled: bool,
    pub sep: bool,
    pub sub: Vec<MenuItem>,
    pub shortcut: String,
}

impl MenuItem {
    pub fn action(label: &str, action: Option<Action>) -> MenuItem {
        MenuItem { label: label.into(), action, checked: None, radio: false, enabled: true, sep: false, sub: Vec::new(), shortcut: String::new() }
    }
    pub fn check(label: &str, on: bool, action: Option<Action>) -> MenuItem {
        MenuItem { checked: Some(on), ..MenuItem::action(label, action) }
    }
    pub fn radio(label: &str, on: bool, action: Option<Action>) -> MenuItem {
        MenuItem { checked: Some(on), radio: true, ..MenuItem::action(label, action) }
    }
    pub fn disabled(label: &str) -> MenuItem {
        MenuItem { enabled: false, ..MenuItem::action(label, None) }
    }
    pub fn sep() -> MenuItem {
        MenuItem { sep: true, ..MenuItem::action("", None) }
    }
    pub fn submenu(label: &str, sub: Vec<MenuItem>) -> MenuItem {
        MenuItem { sub, ..MenuItem::action(label, None) }
    }
    pub fn with_shortcut(mut self, s: &str) -> MenuItem {
        self.shortcut = s.into();
        self
    }
}

/// Кто открыл меню: ему сообщаем о закрытии (док не прячется, пока меню открыто).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Owner {
    Dock(usize),
    Bar(usize),
    Folder,
    Other,
}

struct Level {
    hwnd: HWND,
    surf: Surface,
    glass: crate::glass::Glass,
    items: Vec<MenuItem>,
    rows: Vec<R>,
    hover: Option<usize>,
    x: i32,
    y: i32,
    w: i32,
    h: i32,
    opened: f64,
    parent_row: Option<usize>,
}

pub struct Menus {
    levels: Vec<Level>,
    pub owner: Option<Owner>,
    scale: f32,
    pub closed_at: f64,
    pool: Vec<(HWND, crate::glass::Glass)>,
}

const PAD: f32 = 5.0;
const ROW_H: f32 = 24.0;
const SEP_H: f32 = 9.0;
const FONT: f32 = 12.0;

impl Menus {
    pub fn new() -> Menus {
        Menus { levels: Vec::new(), owner: None, scale: 1.0, closed_at: 0.0, pool: Vec::new() }
    }

    pub fn is_open(&self) -> bool {
        !self.levels.is_empty()
    }

    pub fn owns(&self, hwnd: HWND) -> Option<usize> {
        self.levels.iter().position(|l| l.hwnd == hwnd)
    }

    fn take_window(&mut self) -> (HWND, crate::glass::Glass) {
        if let Some(w) = self.pool.pop() {
            return w;
        }
        let h = win::create_focusable("MiniDockMenu", true);
        crate::win::dwm::exclude_from_peek(h);
        (h, crate::glass::Glass::new())
    }

    fn layout(items: &[MenuItem], s: f32) -> (Vec<R>, f32, f32) {
        let mut w: f32 = 160.0 * s;
        for it in items {
            if !it.sep {
                let tw = text::width(&it.label, Font::Regular, FONT * s) + 40.0 * s
                    + if it.shortcut.is_empty() { 0.0 } else { text::width(&it.shortcut, Font::Regular, FONT * s) + 24.0 * s }
                    + if it.sub.is_empty() { 0.0 } else { 16.0 * s };
                w = w.max(tw);
            }
        }
        let mut y = PAD * s;
        let mut rows = Vec::new();
        for it in items {
            let h = if it.sep { SEP_H * s } else { ROW_H * s };
            rows.push(R::new(PAD * s, y, w - 2.0 * PAD * s, h));
            y += h;
        }
        (rows, w, y + PAD * s)
    }

    /// Открыть меню у точки (x, y) экрана.
    pub fn open(&mut self, items: Vec<MenuItem>, x: i32, y: i32, owner: Owner) {
        self.close_all();
        self.owner = Some(owner);
        self.scale = sys::point_monitor(x, y).scale;
        self.open_level(items, x, y, None, false);
    }

    fn open_level(&mut self, items: Vec<MenuItem>, x: i32, y: i32, parent_row: Option<usize>, left_of: bool) {
        let s = self.scale;
        let (rows, w, h) = Self::layout(&items, s);
        let (w, h) = (w.ceil() as i32, h.ceil() as i32);
        let mon = sys::point_monitor(x, y).rect;
        let mut px = if left_of { x - w } else { x };
        let mut py = y;
        if px + w > mon.right - 4 {
            px = if parent_row.is_some() { x - w - self.levels.last().map_or(0, |l| l.w) } else { mon.right - 4 - w };
        }
        if py + h > mon.bottom - 4 {
            py = (mon.bottom - 4 - h).max(mon.top + 4);
            if parent_row.is_none() && y - h >= mon.top {
                py = y - h; // меню у нижнего края (док) — открывается вверх от курсора
            }
        }
        px = px.max(mon.left + 4);
        let (hwnd, glass) = self.take_window();
        let lvl = Level {
            hwnd,
            surf: Surface::default(),
            glass,
            items,
            rows,
            hover: None,
            x: px,
            y: py,
            w,
            h,
            opened: crate::anim::now(),
            parent_row,
        };
        self.levels.push(lvl);
        let i = self.levels.len() - 1;
        self.paint(i);
        unsafe {
            let _ = SetWindowPos(hwnd, Some(HWND_TOPMOST), px, py, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW);
        }
        if i == 0 {
            // меню получает фокус, чтобы закрываться по клику мимо и слушать клавиатуру
            sys::force_foreground(hwnd);
        }
    }

    pub fn close_all(&mut self) {
        let had = !self.levels.is_empty();
        while let Some(l) = self.levels.pop() {
            self.recycle(l);
        }
        if had {
            self.closed_at = crate::anim::now();
        }
    }

    fn recycle(&mut self, mut l: Level) {
        win::hide(l.hwnd);
        l.glass.hide();
        l.surf.free();
        self.pool.push((l.hwnd, l.glass));
    }

    fn close_from(&mut self, level: usize) {
        while self.levels.len() > level {
            let l = self.levels.pop().unwrap();
            self.recycle(l);
        }
    }

    /// Кадр анимации появления; true — ещё анимируется.
    pub fn tick(&mut self) -> bool {
        let mut any = false;
        for i in 0..self.levels.len() {
            let t = (crate::anim::now() - self.levels[i].opened) as f32 / 0.17;
            if t <= 1.05 {
                any = true;
                self.paint(i);
            }
        }
        any
    }

    fn paint(&mut self, i: usize) {
        let s = self.scale;
        let l = &mut self.levels[i];
        if !l.surf.ensure(l.w, l.h) {
            return;
        }
        let k = ease_out(((crate::anim::now() - l.opened) as f32 / 0.17).min(1.0));
        let full = R::new(0.0, 0.0, l.w as f32, l.h as f32);
        let radius = 9.0 * s;
        let items = l.items.clone();
        let rows = l.rows.clone();
        let hover = l.hover;
        {
            let mut cv = l.surf.canvas();
            cv.clear();
            cv.fill_rrect(full.inset(0.5), radius, rgba(38, 38, 44, 150));
            cv.stroke_rrect(full.inset(0.5), radius, rgba(255, 255, 255, 38), 1.0);
            for (j, (it, r)) in items.iter().zip(&rows).enumerate() {
                if it.sep {
                    cv.fill_rect(R::new(r.x + 9.0 * s, r.cy().round(), r.w - 18.0 * s, 1.0), rgba(255, 255, 255, 30));
                    continue;
                }
                let hot = hover == Some(j) && it.enabled;
                if hot {
                    cv.fill_rrect(R::new(r.x + 1.0, r.y, r.w - 2.0, r.h), 5.0 * s, rgba(10, 132, 255, 225));
                }
                let color = if !it.enabled { rgba(255, 255, 255, 85) } else if hot { rgba(255, 255, 255, 255) } else { rgba(242, 242, 245, 255) };
                if let Some(on) = it.checked {
                    if on {
                        if it.radio {
                            cv.fill_circle(r.x + 12.0 * s, r.cy(), 3.2 * s, color);
                        } else {
                            let (x, y) = (r.x + 8.0 * s, r.cy());
                            cv.polyline(&[(x, y), (x + 3.5 * s, y + 3.5 * s), (x + 10.0 * s, y - 4.0 * s)], color, 1.6 * s);
                        }
                    }
                }
                let tr = R::new(r.x + 22.0 * s, r.y, r.w - 30.0 * s, r.h);
                text::draw_in(&mut cv, &it.label, Font::Regular, FONT * s, tr, Align::Left, color);
                if !it.shortcut.is_empty() {
                    text::draw_in(&mut cv, &it.shortcut, Font::Regular, FONT * s, R::new(r.x, r.y, r.w - 12.0 * s, r.h), Align::Right, with_alpha(color, 0.6));
                }
                if !it.sub.is_empty() {
                    let (x, y) = (r.right() - 12.0 * s, r.cy());
                    cv.polyline(&[(x - 3.0 * s, y - 4.0 * s), (x + 1.0 * s, y), (x - 3.0 * s, y + 4.0 * s)], color, 1.4 * s);
                }
            }
        }
        let alpha = (k * 255.0) as u8;
        l.surf.present(l.hwnd, l.x, l.y, alpha);
        l.glass.place(l.x, l.y, l.x + l.w, l.y + l.h, radius as i32, l.hwnd, crate::win::dwm::Blur::Blur, 0x01000000);
    }

    fn row_at(&self, i: usize, x: i32, y: i32) -> Option<usize> {
        let l = &self.levels[i];
        l.rows.iter().position(|r| r.contains(x as f32, y as f32)).filter(|&j| !l.items[j].sep)
    }

    /// Сообщение окну меню. Возвращает действие, которое нужно выполнить после закрытия.
    pub fn on_message(&mut self, hwnd: HWND, msg: u32, wp: usize, x: i32, y: i32) -> MenuResult {
        let Some(i) = self.owns(hwnd) else { return MenuResult::None };
        match msg {
            WM_MOUSEMOVE => {
                let r = self.row_at(i, x, y);
                if r != self.levels[i].hover {
                    self.set_hover(i, r);
                }
            }
            WM_LBUTTONUP => {
                if let Some(j) = self.row_at(i, x, y) {
                    return self.choose(i, j);
                }
            }
            WM_ACTIVATE => {
                // фокус ушёл не в наше же меню (клик мимо) — закрываем
                if wp & 0xFFFF == 0 {
                    let other = HWND(y as isize as *mut _);
                    let _ = other;
                    return MenuResult::Deactivated;
                }
            }
            WM_KEYDOWN => return self.key(wp as u32),
            _ => {}
        }
        MenuResult::None
    }

    fn set_hover(&mut self, i: usize, r: Option<usize>) {
        self.levels[i].hover = r;
        // уйти на другой пункт — закрыть подменю других пунктов
        if self.levels.len() > i + 1 && self.levels[i + 1].parent_row != r {
            self.close_from(i + 1);
        }
        if let Some(j) = r {
            if !self.levels[i].items[j].sub.is_empty() && self.levels.len() == i + 1 {
                let l = &self.levels[i];
                let row = l.rows[j];
                let sub = l.items[j].sub.clone();
                let (sx, sy) = (l.x + l.w - 3, l.y + row.y as i32 - (PAD * self.scale) as i32);
                self.open_level(sub, sx, sy, Some(j), false);
            }
        }
        self.paint(i);
    }

    fn choose(&mut self, i: usize, j: usize) -> MenuResult {
        let it = self.levels[i].items[j].clone();
        if !it.enabled || it.sep {
            return MenuResult::None;
        }
        if !it.sub.is_empty() {
            self.set_hover(i, Some(j));
            return MenuResult::None;
        }
        self.close_all();
        match it.action {
            Some(a) => MenuResult::Run(a),
            None => MenuResult::Closed,
        }
    }

    fn key(&mut self, vk: u32) -> MenuResult {
        let i = self.levels.len().saturating_sub(1);
        if self.levels.is_empty() {
            return MenuResult::None;
        }
        let n = self.levels[i].items.len();
        let step = |from: Option<usize>, d: isize, items: &[MenuItem]| -> Option<usize> {
            let mut k = from.map(|v| v as isize).unwrap_or(if d > 0 { -1 } else { n as isize });
            for _ in 0..n {
                k = (k + d).rem_euclid(n as isize);
                if !items[k as usize].sep && items[k as usize].enabled {
                    return Some(k as usize);
                }
            }
            from
        };
        match vk {
            0x28 | 0x26 => {
                let items = self.levels[i].items.clone();
                let h = step(self.levels[i].hover, if vk == 0x28 { 1 } else { -1 }, &items);
                self.levels[i].hover = h;
                self.paint(i);
            }
            0x27 => {
                if let Some(j) = self.levels[i].hover {
                    if !self.levels[i].items[j].sub.is_empty() {
                        self.set_hover(i, Some(j));
                        let k = self.levels.len() - 1;
                        if k > i {
                            let items = self.levels[k].items.clone();
                            self.levels[k].hover = step(None, 1, &items);
                            self.paint(k);
                        }
                    }
                }
            }
            0x25 => {
                if i > 0 {
                    self.close_from(i);
                }
            }
            0x0D | 0x20 => {
                if let Some(j) = self.levels[i].hover {
                    return self.choose(i, j);
                }
            }
            0x1B => {
                if i > 0 {
                    self.close_from(i);
                } else {
                    self.close_all();
                    return MenuResult::Closed;
                }
            }
            _ => {}
        }
        MenuResult::None
    }

    pub fn hwnds(&self) -> Vec<HWND> {
        self.levels.iter().map(|l| l.hwnd).collect()
    }

    pub fn contains_point(&self, x: i32, y: i32) -> bool {
        self.levels.iter().any(|l| x >= l.x && x < l.x + l.w && y >= l.y && y < l.y + l.h)
    }
}

pub enum MenuResult {
    None,
    Run(Action),
    Closed,
    Deactivated,
}

pub fn on_message(app: &mut App, hwnd: HWND, msg: u32, wp: usize, lp: isize) -> bool {
    if app.menus.owns(hwnd).is_none() {
        return false;
    }
    let (x, y) = if msg == WM_ACTIVATE { (0, lp as i32) } else { win::lparam_xy(windows::Win32::Foundation::LPARAM(lp)) };
    match app.menus.on_message(hwnd, msg, wp, x, y) {
        MenuResult::Run(a) => {
            app.menu_closed();
            // выполнить после выхода из обработчика сообщения (действие может открыть диалог)
            crate::app::defer(move || {
                crate::app::with(|app| a(app));
            });
        }
        MenuResult::Closed => app.menu_closed(),
        MenuResult::Deactivated => {
            let other = HWND(lp as *mut _);
            if app.menus.owns(other).is_none() {
                app.menus.close_all();
                app.menu_closed();
            }
        }
        MenuResult::None => {}
    }
    app.request_frame();
    true
}

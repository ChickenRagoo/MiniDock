//! Док: иконки с увеличением, автоскрытие, подскок, перетаскивание, папки, положение снизу/слева/справа.
//!
//! Раскладка считается в координатах (u, v): u — вдоль края экрана, v — от края экрана внутрь
//! (физические пиксели окна). Перевод в координаты окна — to_win_rect / to_win_pt / to_uv.

use std::collections::HashMap;
use std::rc::Rc;

use windows::Win32::Foundation::{HWND, RECT};

use crate::app::{Action, App};
use crate::config::{Config, PinEntry};
use crate::gfx::*;
use crate::menu::MenuItem;
use crate::text::{self, Align, Font};
use crate::win::{self, appbar, dwm, shell, sys, Surface};

pub const ICON_PX: i32 = 128; // размер исходных иконок (достаточно для увеличения ×1.6 на 125%)

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    App,
    Running,
    Folder,
    Sep,
    Trash,
    Launchpad,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Side {
    Bottom,
    Left,
    Right,
}

impl Side {
    pub fn from(s: &str) -> Side {
        match s {
            "left" => Side::Left,
            "right" => Side::Right,
            _ => Side::Bottom,
        }
    }
}

static NEXT_ID: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);

pub fn new_id() -> u64 {
    NEXT_ID.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
}

pub struct Item {
    pub id: u64,
    pub kind: Kind,
    pub path: String,
    pub name: String,
    pub exe: String,
    pub icon: Option<Rc<Image>>,
    pub cache: ScaledCache,
    pub hwnds: Vec<isize>,
    pub bounce_start: Option<f64>,
    pub cur: Option<R>,
    pub children: Vec<Item>,
    pub seen_at: f64,
}

impl Item {
    pub fn new(kind: Kind, path: &str, name: &str, icon: Option<Image>) -> Item {
        Item {
            id: new_id(),
            kind,
            path: path.to_string(),
            name: name.to_string(),
            exe: String::new(),
            icon: icon.map(Rc::new),
            cache: ScaledCache::default(),
            hwnds: Vec::new(),
            bounce_start: None,
            cur: None,
            children: Vec::new(),
            seen_at: 0.0,
        }
    }

    pub fn from_path(path: &str, kind: Kind) -> Item {
        let path = sys::current_path(path); // программа обновилась — её новая папка
        let lower = path.to_lowercase();
        let mut it = Item::new(kind, &path, "", None);
        it.exe = if lower.ends_with(".lnk") {
            shell::resolve_lnk(&path).map(|t| t.0).unwrap_or_default().to_lowercase()
        } else {
            lower.clone()
        };
        it.name = if lower.ends_with(".exe") {
            shell::exe_description(&path).unwrap_or_else(|| sys::file_stem(&path))
        } else {
            let s = sys::file_stem(&path);
            if s.is_empty() {
                path.clone()
            } else {
                s
            }
        };
        it.icon = shell::file_icon(&path, ICON_PX).map(Rc::new);
        it
    }

    pub fn make_folder(name: &str, children: Vec<Item>) -> Item {
        let mut f = Item::new(Kind::Folder, "", name, None);
        f.children = children;
        for c in &mut f.children {
            c.kind = Kind::App;
            c.cur = None;
        }
        f.refresh_icon();
        f
    }

    pub fn refresh_icon(&mut self) {
        let imgs: Vec<Rc<Image>> = self.children.iter().filter_map(|c| c.icon.clone()).collect();
        self.icon = Some(Rc::new(folder_image(&imgs)));
        self.cache.clear();
    }

    pub fn scaled(&mut self, size: f32) -> Option<Rc<Image>> {
        let icon = self.icon.clone()?;
        Some(self.cache.get(&icon, size))
    }

    /// Окно этого exe — от этой программы?
    pub fn owns_exe(&self, exe: &str) -> bool {
        if self.kind == Kind::Folder {
            return self.children.iter().any(|c| c.owns_exe(exe));
        }
        if self.exe.is_empty() {
            return false;
        }
        if exe == self.exe {
            return true;
        }
        let mine = sys::app_key(&self.exe);
        if sys::app_key(exe) == mine {
            return true; // та же программа, только версия новее (…\app-1.2.3\…)
        }
        // лаунчеры (Discord Update.exe, Steam) открывают окна из других exe в своей папке…
        let folder = format!("{}\\", sys::parent_dir(&mine));
        if folder.len() < 4 || folder.starts_with(&sys::system_root()) || !exe.starts_with(&folder) {
            return false;
        }
        // …но игры в библиотеке лаунчера (steamapps\common\…, games\…) — отдельные программы
        let inner: Vec<&str> = exe[folder.len()..].split('\\').collect();
        !inner[..inner.len().saturating_sub(1)].iter().any(|p| LIBRARY_DIRS.contains(p))
    }

    pub fn is_app(&self) -> bool {
        matches!(self.kind, Kind::App | Kind::Running)
    }
}

const LIBRARY_DIRS: [&str; 5] = ["steamapps", "common", "games", "game", "library"];

/// Иконка папки: полупрозрачный скруглённый квадрат с сеткой из иконок программ.
pub fn folder_image(images: &[Rc<Image>]) -> Image {
    let s = 256.0;
    let mut pm = Pixmap::new(256, 256).unwrap();
    {
        let mut cv = Canvas::new(pm.as_mut());
        let bg = R::new(14.0, 14.0, 228.0, 228.0);
        cv.fill_rrect(bg, 54.0, rgba(255, 255, 255, 62));
        cv.stroke_rrect(bg, 54.0, rgba(255, 255, 255, 70), 3.0);
        let n = if images.len() <= 4 { 2 } else { 3 };
        let (cell, gap) = if n == 2 { (80.0, 16.0) } else { (56.0, 12.0) };
        let x0 = s / 2.0 - (n as f32 * cell + (n as f32 - 1.0) * gap) / 2.0;
        for (i, im) in images.iter().take(n * n).enumerate() {
            let r = R::new(x0 + (i % n) as f32 * (cell + gap), x0 + (i / n) as f32 * (cell + gap), cell, cell);
            let small = im.scaled((cell * 1.0) as u32, (cell * 1.0) as u32);
            cv.image(&small, r);
        }
    }
    Image::from_pixmap(pm)
}

/// Иконка Launchpad: сетка цветных квадратов на тёмном скруглённом фоне.
pub fn launchpad_image() -> Image {
    let mut pm = Pixmap::new(256, 256).unwrap();
    {
        let mut cv = Canvas::new(pm.as_mut());
        let path = rounded_rect(R::new(16.0, 16.0, 224.0, 224.0), 52.0).unwrap();
        let grad = LinearGradient::new(
            Point::from_xy(0.0, 0.0),
            Point::from_xy(0.0, 256.0),
            vec![GradientStop::new(0.0, rgba(70, 70, 82, 255)), GradientStop::new(1.0, rgba(30, 30, 38, 255))],
            SpreadMode::Pad,
            Transform::identity(),
        )
        .unwrap();
        cv.fill_path_shader(&path, grad);
        let colors = ["#ff5f57", "#febc2e", "#28c840", "#0a84ff", "#bf5af2", "#ff9f0a", "#64d2ff", "#ff375f", "#30d158"];
        for (i, c) in colors.iter().enumerate() {
            let (x, y) = (52.0 + (i % 3) as f32 * 56.0, 52.0 + (i / 3) as f32 * 56.0);
            cv.fill_rrect(R::new(x, y, 40.0, 40.0), 11.0, parse_hex(c).unwrap());
        }
    }
    Image::from_pixmap(pm)
}

/// Где искать элемент дока.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Slot {
    Pinned(usize),
    Child(usize, usize),
    Running(usize),
    Sep1,
    Sep2,
    Trash,
    Launchpad,
}

pub struct Dock {
    pub hwnd: HWND,
    surf: Surface,
    /// None — основной монитор, иначе имя дополнительного (док на всех мониторах)
    pub home: Option<String>,
    pub mon: sys::Monitor,
    pub reserved: bool,
    pub pinned: Vec<Item>,
    pub running: Vec<Item>,
    sep1: Item,
    sep2: Item,
    trash: Item,
    launchpad: Item,
    icon_cache: Vec<Item>,

    pub hover: f32,
    pub hide_k: f32,
    last_seen: f64,
    reveal_until: f64,
    mouse_u: f32,
    light_pos: Option<(f32, f32)>,
    base_span: (f32, f32),
    rects: Vec<(u64, R)>,
    targets: HashMap<u64, R>,
    pub bg: R,
    bg_cur: Option<R>,
    zone: R,
    pub press: Option<u64>,
    press_pos: (f32, f32),
    pub dragging: bool,
    pub menu_open: bool,
    merge_target: Option<u64>,
    poofs: Vec<((f32, f32), f64)>,
    first_layout: bool,
    pub animating: bool,
    still: u32,
    last_frame: f64,
    last_cursor: (i32, i32),
    glass: crate::glass::Glass,
    /// что снимать для живого «стекла»: участок экрана и сила размытия (None — не нужно)
    pub glass_job: Option<(RECT, f32)>,
    /// перерисовать один раз (пришёл новый кадр фона), не запуская анимацию
    pub repaint: bool,
    pub side: Side,
    pub scale: f32,
    pub area: RECT,
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
    pub visible: bool,
    pub was_hidden: bool,
}

impl Dock {
    pub fn new(cfg: &Config, home: Option<String>) -> Dock {
        let hwnd = win::create_layered("MiniDock", true, false);
        dwm::exclude_from_peek(hwnd);
        unsafe {
            windows::Win32::UI::Shell::DragAcceptFiles(hwnd, true);
        }
        let trash = Item::new(Kind::Trash, "", "Корзина", shell::recycle_bin_icon(ICON_PX));
        let launchpad = Item::new(Kind::Launchpad, "", "Launchpad", Some(launchpad_image()));
        let mut d = Dock {
            hwnd,
            surf: Surface::default(),
            home,
            mon: sys::primary_monitor(),
            reserved: false,
            pinned: Vec::new(),
            running: Vec::new(),
            sep1: Item::new(Kind::Sep, "", "", None),
            sep2: Item::new(Kind::Sep, "", "", None),
            trash,
            launchpad,
            icon_cache: Vec::new(),
            hover: 0.0,
            hide_k: 0.0,
            last_seen: crate::anim::now(),
            reveal_until: 0.0,
            mouse_u: -1.0,
            light_pos: None,
            base_span: (0.0, 0.0),
            rects: Vec::new(),
            targets: HashMap::new(),
            bg: R::default(),
            bg_cur: None,
            zone: R::default(),
            press: None,
            press_pos: (0.0, 0.0),
            dragging: false,
            menu_open: false,
            merge_target: None,
            poofs: Vec::new(),
            first_layout: true,
            animating: true,
            still: 0,
            last_frame: crate::anim::now(),
            last_cursor: (i32::MIN, 0),
            glass: crate::glass::Glass::new(),
            glass_job: None,
            repaint: false,
            side: Side::from(&cfg.position),
            scale: 1.0,
            area: RECT::default(),
            x: 0,
            y: 0,
            w: 1,
            h: 1,
            visible: false,
            was_hidden: true,
        };
        d.pinned = load_pinned(&cfg.pins());
        d
    }

    // -- геометрия (физические пиксели)
    pub fn b(&self, cfg: &Config) -> f32 {
        cfg.icon_size * self.scale
    }
    fn mag(cfg: &Config) -> f32 {
        cfg.magnify.max(1.0)
    }
    fn pad(&self, cfg: &Config) -> f32 {
        self.b(cfg) * 0.16
    }
    fn margin(&self, cfg: &Config) -> f32 {
        cfg.margin * self.scale
    }
    pub fn bar_height(&self, cfg: &Config) -> f32 {
        self.b(cfg) + 2.0 * self.pad(cfg)
    }
    pub fn hide_distance(&self, cfg: &Config) -> f32 {
        self.bar_height(cfg) + self.margin(cfg) + 6.0 * self.scale
    }
    fn reserve_thickness(&self, cfg: &Config) -> f32 {
        self.bar_height(cfg) + self.margin(cfg) + 2.0 * self.scale
    }

    fn monitor(&self) -> sys::Monitor {
        match &self.home {
            None => sys::primary_monitor(),
            Some(name) => sys::monitors().into_iter().find(|m| &m.name == name).unwrap_or_else(sys::primary_monitor),
        }
    }

    /// Поставить док у края своего монитора.
    pub fn place(&mut self, cfg: &Config, bar_h: f32) {
        self.mon = self.monitor();
        self.scale = self.mon.scale;
        self.side = Side::from(&cfg.position);
        let g = self.mon.rect;
        let b = self.b(cfg);
        let mag = Self::mag(cfg);
        let pad = self.pad(cfg);
        let margin = self.margin(cfg);
        self.area = match self.side {
            Side::Bottom => {
                let base = if cfg.replace_taskbar || self.home.is_some() { g } else { self.mon.work };
                let depth = (b * mag + pad * 2.0 + b + 40.0 * self.scale + margin) as i32;
                RECT { left: base.left, top: base.bottom - depth, right: base.right, bottom: base.bottom }
            }
            _ => {
                let top = if cfg.menubar { (bar_h * self.scale) as i32 } else { 0 };
                let depth = (b * mag + pad * 2.0 + margin + 260.0 * self.scale) as i32;
                let x = if self.side == Side::Left { g.left } else { g.right - depth };
                RECT { left: x, top: g.top + top, right: x + depth, bottom: g.bottom }
            }
        };
        let r = self.fitted(cfg);
        self.move_window(r);
        self.reserve(cfg);
        self.first_layout = true;
        self.wake();
    }

    fn move_window(&mut self, r: RECT) {
        self.x = r.left;
        self.y = r.top;
        self.w = (r.right - r.left).max(1);
        self.h = (r.bottom - r.top).max(1);
    }

    fn span_needed(&self, cfg: &Config) -> i32 {
        let b = self.b(cfg);
        let n = self.count_items(cfg);
        let gap = b * cfg.spacing;
        let base: f32 = self.items(cfg).iter().map(|s| if self.slot_kind(*s) == Kind::Sep { b * 0.3 } else { b }).sum::<f32>()
            + gap * (n.max(1) - 1) as f32;
        (base + 2.0 * self.pad(cfg) + b * (Self::mag(cfg) - 1.0) * 3.5 + 2.0 * 170.0 * self.scale) as i32
    }

    fn count_items(&self, cfg: &Config) -> usize {
        self.items(cfg).len()
    }

    fn fitted(&self, cfg: &Config) -> RECT {
        let span = self.span_needed(cfg);
        let a = self.area;
        match self.side {
            Side::Bottom => {
                let w = (a.right - a.left).min(span);
                let x = a.left + (a.right - a.left - w) / 2;
                RECT { left: x, top: a.top, right: x + w, bottom: a.bottom }
            }
            _ => {
                let h = (a.bottom - a.top).min(span);
                let y = a.top + (a.bottom - a.top - h) / 2;
                RECT { left: a.left, top: y, right: a.right, bottom: y + h }
            }
        }
    }

    /// Иконок стало больше/меньше — подогнать длину окна, не сдвигая док на экране.
    fn refit(&mut self, cfg: &Config) {
        if self.area.right == 0 {
            return;
        }
        let new = self.fitted(cfg);
        if new.left == self.x && new.top == self.y && new.right - new.left == self.w && new.bottom - new.top == self.h {
            return;
        }
        let du = if self.side == Side::Bottom { (self.x - new.left) as f32 } else { (self.y - new.top) as f32 };
        self.move_window(new);
        let shift = |r: &mut Option<R>| {
            if let Some(c) = r.as_mut() {
                *c = c.translated(du, 0.0);
            }
        };
        for it in self.pinned.iter_mut().chain(self.running.iter_mut()) {
            shift(&mut it.cur);
        }
        for it in [&mut self.sep1, &mut self.sep2, &mut self.trash, &mut self.launchpad] {
            shift(&mut it.cur);
        }
        shift(&mut self.bg_cur);
        self.wake();
    }

    pub fn reserve(&mut self, cfg: &Config) {
        if cfg.autohide || (self.home.is_none() && !cfg.replace_taskbar) {
            self.release();
            return;
        }
        let m = self.mon.rect;
        let thick = self.reserve_thickness(cfg).round() as i32;
        let (rect, edge) = match self.side {
            Side::Bottom => (RECT { left: m.left, top: m.bottom - thick, right: m.right, bottom: m.bottom }, appbar::EDGE_BOTTOM),
            Side::Left => (RECT { left: m.left, top: m.top, right: m.left + thick, bottom: m.bottom }, appbar::EDGE_LEFT),
            Side::Right => (RECT { left: m.right - thick, top: m.top, right: m.right, bottom: m.bottom }, appbar::EDGE_RIGHT),
        };
        appbar::reserve(self.hwnd, rect, edge);
        self.reserved = true;
    }

    pub fn release(&mut self) {
        if self.reserved {
            appbar::release(self.hwnd);
            self.reserved = false;
        }
    }

    fn length(&self) -> f32 {
        if self.side == Side::Bottom {
            self.w as f32
        } else {
            self.h as f32
        }
    }

    pub fn to_win_rect(&self, r: R) -> R {
        match self.side {
            Side::Bottom => R::new(r.x, self.h as f32 - r.y - r.h, r.w, r.h),
            Side::Left => R::new(r.y, r.x, r.h, r.w),
            Side::Right => R::new(self.w as f32 - r.y - r.h, r.x, r.h, r.w),
        }
    }

    fn to_win_pt(&self, u: f32, v: f32) -> (f32, f32) {
        match self.side {
            Side::Bottom => (u, self.h as f32 - v),
            Side::Left => (v, u),
            Side::Right => (self.w as f32 - v, u),
        }
    }

    fn to_uv(&self, x: f32, y: f32) -> (f32, f32) {
        match self.side {
            Side::Bottom => (x, self.h as f32 - y),
            Side::Left => (y, x),
            Side::Right => (y, self.w as f32 - x),
        }
    }

    fn cursor_local(&self) -> (f32, f32) {
        let (cx, cy) = win::cursor_pos();
        ((cx - self.x) as f32, (cy - self.y) as f32)
    }

    // -- элементы
    pub fn items(&self, cfg: &Config) -> Vec<Slot> {
        let mut v = Vec::new();
        if cfg.show_launchpad {
            v.push(Slot::Launchpad);
        }
        v.extend((0..self.pinned.len()).map(Slot::Pinned));
        if cfg.show_running && !self.running.is_empty() {
            v.push(Slot::Sep1);
            v.extend((0..self.running.len()).map(Slot::Running));
        }
        if cfg.show_trash {
            v.push(Slot::Sep2);
            v.push(Slot::Trash);
        }
        v
    }

    pub fn get(&self, s: Slot) -> Option<&Item> {
        match s {
            Slot::Pinned(i) => self.pinned.get(i),
            Slot::Child(i, j) => self.pinned.get(i).and_then(|f| f.children.get(j)),
            Slot::Running(i) => self.running.get(i),
            Slot::Sep1 => Some(&self.sep1),
            Slot::Sep2 => Some(&self.sep2),
            Slot::Trash => Some(&self.trash),
            Slot::Launchpad => Some(&self.launchpad),
        }
    }

    pub fn get_mut(&mut self, s: Slot) -> Option<&mut Item> {
        match s {
            Slot::Pinned(i) => self.pinned.get_mut(i),
            Slot::Child(i, j) => self.pinned.get_mut(i).and_then(|f| f.children.get_mut(j)),
            Slot::Running(i) => self.running.get_mut(i),
            Slot::Sep1 => Some(&mut self.sep1),
            Slot::Sep2 => Some(&mut self.sep2),
            Slot::Trash => Some(&mut self.trash),
            Slot::Launchpad => Some(&mut self.launchpad),
        }
    }

    fn slot_kind(&self, s: Slot) -> Kind {
        self.get(s).map(|i| i.kind).unwrap_or(Kind::Sep)
    }

    pub fn find(&self, id: u64) -> Option<Slot> {
        for (i, it) in self.pinned.iter().enumerate() {
            if it.id == id {
                return Some(Slot::Pinned(i));
            }
            for (j, c) in it.children.iter().enumerate() {
                if c.id == id {
                    return Some(Slot::Child(i, j));
                }
            }
        }
        if let Some(i) = self.running.iter().position(|it| it.id == id) {
            return Some(Slot::Running(i));
        }
        for (s, it) in [(Slot::Sep1, &self.sep1), (Slot::Sep2, &self.sep2), (Slot::Trash, &self.trash), (Slot::Launchpad, &self.launchpad)] {
            if it.id == id {
                return Some(s);
            }
        }
        None
    }

    pub fn item(&self, id: u64) -> Option<&Item> {
        self.find(id).and_then(|s| self.get(s))
    }

    pub fn item_mut(&mut self, id: u64) -> Option<&mut Item> {
        let s = self.find(id)?;
        self.get_mut(s)
    }

    /// Закреплённые и запущенные программы, включая лежащие в папках (без самих папок).
    pub fn all_apps(&self) -> Vec<Slot> {
        let mut v = Vec::new();
        for (i, it) in self.pinned.iter().enumerate() {
            if it.kind == Kind::Folder {
                v.extend((0..it.children.len()).map(|j| Slot::Child(i, j)));
            } else {
                v.push(Slot::Pinned(i));
            }
        }
        v.extend((0..self.running.len()).map(Slot::Running));
        v
    }

    pub fn folders(&self) -> Vec<(u64, String)> {
        self.pinned.iter().filter(|i| i.kind == Kind::Folder).map(|i| (i.id, i.name.clone())).collect()
    }

    pub fn item_for_hwnd(&self, hwnd: isize, exe: &str) -> Option<u64> {
        let apps = self.all_apps();
        for s in &apps {
            if let Some(it) = self.get(*s) {
                if it.hwnds.contains(&hwnd) {
                    return Some(it.id);
                }
            }
        }
        if !exe.is_empty() {
            for s in &apps {
                if let Some(it) = self.get(*s) {
                    if it.owns_exe(exe) {
                        return Some(it.id);
                    }
                }
            }
        }
        None
    }

    fn folder_of(&self, id: u64) -> Option<u64> {
        match self.find(id) {
            Some(Slot::Child(i, _)) => Some(self.pinned[i].id),
            _ => None,
        }
    }

    /// Прямоугольник иконки на экране (физические пиксели) — там, где она будет у видимого дока.
    pub fn icon_screen_rect(&self, cfg: &Config, id: Option<u64>) -> RECT {
        let dist = self.hide_k * self.hide_distance(cfg);
        let b = self.b(cfg);
        let id = id.map(|i| self.folder_of(i).unwrap_or(i)); // программа из папки — к иконке папки
        let cur = id.and_then(|i| self.item(i)).and_then(|it| it.cur);
        let r = match cur {
            Some(c) => {
                let c = c.translated(0.0, dist);
                let side = c.w.max(b * 0.6);
                R::new(c.cx() - side / 2.0, c.y, side, side)
            }
            None => R::new(self.length() / 2.0 - b / 2.0, self.margin(cfg) + self.pad(cfg), b, b),
        };
        let w = self.to_win_rect(r);
        RECT {
            left: self.x + w.x.round() as i32,
            top: self.y + w.y.round() as i32,
            right: self.x + w.right().round() as i32,
            bottom: self.y + w.bottom().round() as i32,
        }
    }

    pub fn reveal(&mut self, seconds: f64) {
        self.reveal_until = crate::anim::now() + seconds;
        self.wake();
    }

    pub fn wake(&mut self) {
        self.still = 0;
        if !self.animating {
            // кадры шли из простоя: отсчёт времени заново, иначе первый шаг считался как за 50 мс — рывок
            self.last_frame = crate::anim::now();
        }
        self.animating = true;
    }

    fn at_screen_edge(&self, gx: i32, gy: i32) -> bool {
        let m = self.mon.rect;
        match self.side {
            // как в Python-версии: две нижние (правые) строки пикселей
            Side::Bottom => gy >= m.bottom - 2 && gx >= m.left && gx < m.right,
            Side::Left => gx <= m.left && gy >= m.top && gy < m.bottom,
            Side::Right => gx >= m.right - 2 && gy >= m.top && gy < m.bottom,
        }
    }

    fn layout(&self, cfg: &Config) -> (R, Vec<(Slot, R)>, R) {
        let b = self.b(cfg);
        let mag = Self::mag(cfg);
        let pad = self.pad(cfg);
        let gap = b * cfg.spacing;
        let l = self.length();
        let mut items = self.items(cfg);
        if self.dragging && self.drag_remove(cfg) {
            if let Some(p) = self.press.and_then(|id| self.find(id)) {
                items.retain(|s| *s != p);
            }
        }
        let base: Vec<f32> = items.iter().map(|s| if self.slot_kind(*s) == Kind::Sep { b * 0.3 } else { b }).collect();
        let n = items.len().max(1) as f32;
        let base_total = base.iter().sum::<f32>() + gap * (n - 1.0) + 2.0 * pad;
        let mut u = (l - base_total) / 2.0 + pad;
        let mut sizes = Vec::with_capacity(items.len());
        for (s, w) in items.iter().zip(&base) {
            let c = u + w / 2.0;
            u += w + gap;
            let mut k = 1.0;
            if self.slot_kind(*s) != Kind::Sep && self.hover > 0.0 && mag > 1.0 {
                let (d, reach) = ((self.mouse_u - c).abs(), b * 2.6);
                if d < reach {
                    k += (mag - 1.0) * ((std::f32::consts::PI * d / reach).cos() + 1.0) / 2.0 * self.hover;
                }
            }
            sizes.push(w * k);
        }
        let total = sizes.iter().sum::<f32>() + gap * (n - 1.0) + 2.0 * pad;
        let bg_h = self.bar_height(cfg);
        let v0 = self.margin(cfg) - self.hide_k * self.hide_distance(cfg);
        let bg = R::new((l - total) / 2.0, v0, total, bg_h);
        let mut rects = Vec::with_capacity(items.len());
        let mut u = bg.x + pad;
        for (s, w) in items.iter().zip(&sizes) {
            if self.slot_kind(*s) == Kind::Sep {
                rects.push((*s, R::new(u, v0 + pad * 1.4, *w, b - pad * 0.8)));
            } else {
                rects.push((*s, R::new(u, v0 + pad, *w, *w)));
            }
            u += w + gap;
        }
        let zone = R::new(bg.x, -2.0, bg.w, self.margin(cfg) + bg_h + b * (mag - 1.0) + 6.0 * self.scale);
        (bg, rects, zone)
    }

    /// Участок экрана для «стекла»: полоса, где может оказаться фон дока (с запасом на увеличение
    /// иконок), плюс поле для размытия; и сила размытия для уменьшенной копии.
    fn glass_region(&self, cfg: &Config) -> Option<(RECT, f32)> {
        let (lo, hi) = self.base_span;
        let b = self.b(cfg);
        let extra = b * (Self::mag(cfg) - 1.0) * 2.5 + 20.0 * self.scale;
        let r = self.to_win_rect(R::new(lo - extra, self.margin(cfg) - 2.0 * self.scale, (hi - lo) + 2.0 * extra, self.bar_height(cfg) + 4.0 * self.scale));
        let (x0, y0) = (r.x.max(0.0), r.y.max(0.0));
        let (x1, y1) = (r.right().min(self.w as f32), r.bottom().min(self.h as f32));
        if x1 - x0 < 2.0 || y1 - y0 < 2.0 {
            return None;
        }
        let m = self.mon.rect;
        let mg = crate::liveglass::MARGIN;
        let rect = RECT {
            left: (self.x + x0.round() as i32 - mg).max(m.left),
            top: (self.y + y0.round() as i32 - mg).max(m.top),
            right: (self.x + x1.round() as i32 + mg).min(m.right),
            bottom: (self.y + y1.round() as i32 + mg).min(m.bottom),
        };
        if rect.right - rect.left < 8 || rect.bottom - rect.top < 8 {
            return None;
        }
        Some((rect, cfg.glass_radius * self.scale / crate::liveglass::DOWNSCALE as f32))
    }

    pub fn base_span(&self) -> (f32, f32) {
        self.base_span
    }

    fn item_at(&self, u: f32, v: f32) -> Option<u64> {
        for (id, r) in &self.rects {
            let Some(it) = self.item(*id) else { continue };
            if it.kind == Kind::Sep || (self.dragging && Some(*id) == self.press) {
                continue;
            }
            if r.x - 3.0 <= u && u <= r.right() + 3.0 && -2.0 <= v && v <= r.bottom() {
                return Some(*id);
            }
        }
        None
    }

    pub fn hovered(&self) -> Option<u64> {
        if self.hover > 0.5 && self.hide_k < 0.1 {
            let (x, y) = self.cursor_local();
            let (u, v) = self.to_uv(x, y);
            self.item_at(u, v)
        } else {
            None
        }
    }

    fn drag_remove(&self, cfg: &Config) -> bool {
        let (x, y) = self.cursor_local();
        let (_, v) = self.to_uv(x, y);
        let pinned = self.press.and_then(|id| self.find(id)).is_some_and(|s| matches!(s, Slot::Pinned(_)));
        pinned && v > self.bg.bottom() + self.b(cfg) * 1.1
    }

    /// Шаг анимации. Возвращает true, пока что-то движется (нужны следующие кадры).
    pub fn tick(&mut self, cfg: &Config, ctx: &DockCtx) -> bool {
        let now = crate::anim::now();
        let dt = crate::anim::frame_dt(&mut self.last_frame);
        let (gx, gy) = win::cursor_pos();
        let (lx, ly) = ((gx - self.x) as f32, (gy - self.y) as f32);
        let (u, v) = self.to_uv(lx, ly);

        let mut target_hide = 0.0;
        if cfg.autohide || ctx.fullscreen {
            let in_zone = self.zone.contains(u, v) && self.hide_k < 0.6;
            if self.at_screen_edge(gx, gy) || in_zone || self.dragging || self.menu_open || now < self.reveal_until || ctx.preview_inside {
                self.last_seen = now;
            }
            if now - self.last_seen > cfg.autohide_delay as f64 {
                target_hide = 1.0;
            }
        }
        if ctx.blocked {
            target_hide = 1.0; // открыт Launchpad — док уезжает за край и не отзывается
            self.last_seen = 0.0;
        }
        let mut changed = false;
        if target_hide == 0.0 && self.hide_k >= 0.99 {
            // док начинает выезжать: снова поверх всех «поверх всех» окон (оверлеи игр, плееры)
            win::set_topmost(self.hwnd);
        }
        if self.hide_k != target_hide {
            self.hide_k += (target_hide - self.hide_k) * smooth(0.2, dt);
            if (self.hide_k - target_hide).abs() < 0.01 {
                self.hide_k = target_hide;
            }
            changed = true;
        }

        let inside = (self.zone.contains(u, v) && self.hide_k < 0.3) || self.dragging;
        let hover_target = if inside { 1.0 } else { 0.0 };
        if inside && u != self.mouse_u {
            self.mouse_u = u;
            changed = true;
        }
        if cfg.dock_border_light && inside && self.light_pos != Some((lx, ly)) {
            self.light_pos = Some((lx, ly));
            changed = true;
        }
        if self.hover != hover_target {
            self.hover += (hover_target - self.hover) * smooth(0.22, dt);
            if (self.hover - hover_target).abs() < 0.01 {
                self.hover = hover_target;
            }
            changed = true;
        }

        if !self.dragging && (self.span_needed(cfg) as f32 - self.length()).abs() > 2.0 {
            self.refit(cfg);
        }
        let (bg, rects, zone) = self.layout(cfg);
        self.zone = zone;
        let lo_hi = {
            let b = self.b(cfg);
            let n = self.count_items(cfg).max(1) as f32;
            let base: f32 = self.items(cfg).iter().map(|s| if self.slot_kind(*s) == Kind::Sep { b * 0.3 } else { b }).sum::<f32>()
                + b * cfg.spacing * (n - 1.0)
                + 2.0 * self.pad(cfg);
            ((self.length() - base) / 2.0, (self.length() + base) / 2.0)
        };
        self.base_span = lo_hi;
        if self.first_layout {
            self.bg_cur = Some(bg);
            for (s, r) in &rects {
                if let Some(it) = self.get_mut(*s) {
                    it.cur = Some(*r);
                }
            }
            self.first_layout = false;
            changed = true;
        }
        let k = smooth(0.38, dt);
        let cur_bg = self.bg_cur.unwrap_or(bg);
        let nb = lerp_rect(cur_bg, bg, k);
        changed |= nb.1;
        self.bg_cur = Some(nb.0);
        self.targets.clear();
        let mut out_rects = Vec::with_capacity(rects.len());
        for (s, r) in &rects {
            let Some(it) = self.get_mut(*s) else { continue };
            let cur = it.cur.unwrap_or(if it.kind != Kind::Sep { R::new(r.cx(), r.cy(), 0.0, 0.0) } else { *r });
            let (n, c) = lerp_rect(cur, *r, k);
            it.cur = Some(n);
            changed |= c;
            let id = it.id;
            self.targets.insert(id, *r);
            out_rects.push((id, n));
        }
        self.rects = out_rects;
        self.bg = self.bg_cur.unwrap_or(bg);

        let mut bouncing = false;
        for s in self.items(cfg) {
            if let Some(it) = self.get_mut(s) {
                if let Some(b0) = it.bounce_start {
                    bouncing = true;
                    if now - b0 > 1.6 || (!it.hwnds.is_empty() && now - b0 > 0.5) {
                        it.bounce_start = None;
                    }
                }
            }
        }
        changed |= bouncing;
        self.poofs.retain(|(_, t)| now - t < 0.4);
        changed |= !self.poofs.is_empty() || self.dragging;

        if changed {
            self.still = 0;
        } else {
            // всё на месте несколько кадров подряд — останавливаем кадры, дальше следит idle_check
            self.still += 1;
            if self.still > 6 {
                self.animating = false;
            }
        }
        changed
    }

    /// Лёгкая проверка в простое (20 раз в секунду): не пора ли проснуться.
    pub fn idle_check(&mut self, cfg: &Config, ctx: &DockCtx) {
        if self.animating || !self.visible || ctx.blocked {
            return;
        }
        let now = crate::anim::now();
        let (gx, gy) = win::cursor_pos();
        let moved = (gx, gy) != self.last_cursor;
        self.last_cursor = (gx, gy);
        let (u, v) = self.to_uv((gx - self.x) as f32, (gy - self.y) as f32);
        let in_zone = self.zone.contains(u, v) && self.hide_k < 0.6;
        let at_edge = self.at_screen_edge(gx, gy);
        let mut need = (moved && (in_zone || self.hover > 0.0)) || (at_edge && self.hide_k > 0.0) || (now < self.reveal_until && self.hide_k > 0.0);
        if cfg.autohide || ctx.fullscreen {
            if at_edge || in_zone || self.menu_open || ctx.preview_inside {
                self.last_seen = now;
            } else if self.hide_k < 1.0 && now - self.last_seen > cfg.autohide_delay as f64 {
                need = true; // пора прятать док
            }
        }
        if need {
            self.wake();
        }
    }

    // -- отрисовка
    pub fn paint(&mut self, cfg: &Config, ctx: &DockCtx) {
        if !self.surf.ensure(self.w, self.h) {
            return;
        }
        let b = self.b(cfg);
        let now = crate::anim::now();
        let bg_win = self.to_win_rect(self.bg);
        let radius = b * cfg.dock_radius;
        let hovered = if self.dragging { None } else { self.hovered() };
        // живое «стекло» (как в Python-версии): размытый фон рисует сам док, в своей форме и в своём кадре
        let live = cfg.dock_blur && ctx.live_glass && self.visible && self.hide_k < 0.99;
        self.glass_job = if live { self.glass_region(cfg) } else { None };
        // иначе — размытие Windows в подложке под окном дока, по форме его фона
        if live {
            self.glass.hide();
        } else if cfg.dock_blur && self.visible && self.hide_k < 0.999 {
            let (l, t) = (self.x + bg_win.x.round() as i32, self.y + bg_win.y.round() as i32);
            let (r, bt) = (self.x + bg_win.right().round() as i32, self.y + bg_win.bottom().round() as i32);
            // уехавшая за край монитора часть не размывается (иначе размытие видно на соседнем мониторе)
            let m = self.mon.rect;
            self.glass.place(l.max(m.left), t.max(m.top), r.min(m.right), bt.min(m.bottom), radius.round() as i32, self.hwnd, crate::app::blur_kind(cfg), 0x01000000);
        } else {
            self.glass.hide();
        }
        let dock_color = parse_hex(&cfg.dock_color).unwrap_or(rgba(32, 32, 38, 255));
        let rects = self.rects.clone();
        let side = self.side;
        let light = self.light_pos;
        let hover = self.hover;
        let pad = self.pad(cfg);
        let bg_top = self.bg.y;
        let mag = Self::mag(cfg);
        let drag = if self.dragging { self.press } else { None };
        let drag_remove = self.dragging && self.drag_remove(cfg);
        let merge = if self.dragging { self.merge_target } else { None };
        let (clx, cly) = self.cursor_local();
        let poofs = self.poofs.clone();
        let label = hovered
            .filter(|_| cfg.show_labels && !(ctx.preview_visible && ctx.preview_item == hovered))
            .and_then(|id| self.item(id).map(|it| (it.name.clone(), it.cur.unwrap_or_default())));
        // иконки нужного размера (кэш уменьшенных копий)
        let mut icons: Vec<(u64, Option<Rc<Image>>, R, bool, Option<f64>, Kind)> = Vec::with_capacity(rects.len());
        for (id, r) in &rects {
            let Some(it) = self.item_mut(*id) else { continue };
            let img = if it.kind != Kind::Sep && r.w > 1.0 { it.scaled(r.w) } else { None };
            icons.push((*id, img, *r, !it.hwnds.is_empty(), it.bounce_start, it.kind));
        }
        let drag_img = drag.and_then(|id| {
            let s = b * (1.0 + (mag - 1.0) * 0.6);
            self.item_mut(id).and_then(|it| it.scaled(s))
        });
        let to_win_rect = |r: R| match side {
            Side::Bottom => R::new(r.x, self.h as f32 - r.y - r.h, r.w, r.h),
            Side::Left => R::new(r.y, r.x, r.h, r.w),
            Side::Right => R::new(self.w as f32 - r.y - r.h, r.x, r.h, r.w),
        };
        let to_win_pt = |u: f32, v: f32| match side {
            Side::Bottom => (u, self.h as f32 - v),
            Side::Left => (v, u),
            Side::Right => (self.w as f32 - v, u),
        };
        let (ww, wh) = (self.w as f32, self.h as f32);
        let scale = self.scale;
        let bg_img = ctx.background.clone();
        let mut cv = self.surf.canvas();
        cv.clear();
        if let Some(path) = rounded_rect(bg_win, radius) {
            if live {
                if let Some((img, r)) = &ctx.glass_frame {
                    let dst = R::new((r.left - self.x) as f32, (r.top - self.y) as f32, (r.right - r.left) as f32, (r.bottom - r.top) as f32);
                    cv.image_in_path(img, dst, &path, 1.0);
                }
            }
            cv.fill_path(&path, with_alpha(dock_color, cfg.dock_opacity as f32 / 255.0));
            if let Some((img, op)) = &bg_img {
                draw_cover(&mut cv, img, bg_win, &path, *op);
            }
            if cfg.dock_border {
                if let Some(bp) = rounded_rect(bg_win.inset(0.5), radius) {
                    cv.stroke_path(&bp, rgba(255, 255, 255, 50), 1.0);
                    if cfg.dock_border_light && hover > 0.01 {
                        if let Some((lx, ly)) = light {
                            // курсор — как фонарик: рядом с ним обводка светлее, к краям пятна — как была
                            let k = hover * cfg.dock_light_strength.clamp(0.0, 1.0);
                            if let Some(sh) = RadialGradient::new(
                                Point::from_xy(lx, ly),
                                0.0,
                                Point::from_xy(lx, ly),
                                b * 2.4,
                                vec![
                                    GradientStop::new(0.0, rgba(255, 255, 255, (150.0 * k) as u8)),
                                    GradientStop::new(0.45, rgba(255, 255, 255, (55.0 * k) as u8)),
                                    GradientStop::new(1.0, rgba(255, 255, 255, 0)),
                                ],
                                SpreadMode::Pad,
                                Transform::identity(),
                            ) {
                                cv.stroke_path_shader(&bp, sh, 1.2);
                            }
                        }
                    }
                }
            }
        }
        for (id, img, r, running, bounce, kind) in &icons {
            if *kind == Kind::Sep {
                let c = r.cx();
                let (x0, y0) = to_win_pt(c, r.y);
                let (x1, y1) = to_win_pt(c, r.bottom());
                cv.line(x0, y0, x1, y1, rgba(255, 255, 255, 60), 1.0);
                continue;
            }
            if Some(*id) == drag {
                continue; // рисуется под курсором ниже, слот остаётся пустым
            }
            if Some(*id) == merge {
                let m = r.w * 0.12;
                cv.fill_rrect(to_win_rect(r.adjusted(-m, -m, m, m)), r.w * 0.28, rgba(255, 255, 255, 70));
            }
            let dv = bounce.map(|b0| ((now - b0) as f32 * std::f32::consts::PI * 1.6).sin().abs() * b * 0.45).unwrap_or(0.0);
            if let Some(img) = img {
                let dst = to_win_rect(r.translated(0.0, dv));
                let dst = R::centered(dst.cx(), dst.cy(), img.w as f32 * dst.w / img.w.max(img.h) as f32, img.h as f32 * dst.h / img.w.max(img.h) as f32);
                cv.image(img, dst);
            }
            if *running && cfg.indicator != "none" {
                let (cu, cvv) = (r.cx(), bg_top + pad * 0.45);
                if cfg.indicator == "line" {
                    cv.fill_rrect(to_win_rect(R::new(cu - b * 0.18, cvv - 1.2 * scale, b * 0.36, 2.4 * scale)), 1.2 * scale, rgba(235, 235, 240, 230));
                } else {
                    let (x, y) = to_win_pt(cu, cvv);
                    cv.fill_circle(x, y, 1.8 * scale, rgba(235, 235, 240, 230));
                }
            }
        }
        if let Some(img) = &drag_img {
            let s = b * (1.0 + (mag - 1.0) * 0.6);
            cv.image_alpha(img, R::new(clx - s / 2.0, cly - s / 2.0, s, s), if drag_remove { 0.45 } else { 0.9 });
        }
        for ((px, py), t0) in &poofs {
            let t = ((now - t0) / 0.4) as f32;
            for k in 0..5 {
                let ang = k as f32 * std::f32::consts::TAU / 5.0 + t;
                let rad = b * (0.2 + 0.5 * t);
                cv.fill_circle(px + ang.cos() * rad, py + ang.sin() * rad, b * 0.18 * (1.0 - t * 0.5), rgba(230, 230, 235, (200.0 * (1.0 - t)) as u8));
            }
        }
        if let Some((name, r)) = label {
            let (ax, ay) = to_win_pt(r.cx(), r.bottom() + 9.0 * scale);
            draw_label(&mut cv, &name, ax, ay, side, ww, wh, scale);
        }
        drop(cv);
        let vis = self.visible;
        if vis {
            self.surf.present(self.hwnd, self.x, self.y, 255);
        }
    }

    /// Регион на время показа подписи/перетаскивания: всё окно (иначе подпись обрежется).
    pub fn wants_full_region(&self) -> bool {
        self.dragging
    }

    pub fn show(&mut self) {
        if !self.visible {
            self.visible = true;
            win::show_noactivate(self.hwnd);
            self.wake();
        }
    }

    pub fn hide(&mut self) {
        if self.visible {
            self.visible = false;
            win::hide(self.hwnd);
            self.glass.hide();
        }
    }

    pub fn memory(&self) -> usize {
        self.surf.bytes()
            + self.pinned.iter().chain(self.running.iter()).map(|i| i.cache.bytes() + i.icon.as_ref().map_or(0, |x| x.bytes())).sum::<usize>()
    }

    // -- окна программ
    /// Раздать окна по иконкам; запущенные незакреплённые — в «запущенные».
    pub fn poll_windows(&mut self, wins: &[(String, Vec<HWND>)]) -> bool {
        let mut claimed = std::collections::HashSet::new();
        for s in self.all_apps() {
            let Some(it) = self.get_mut(s) else { continue };
            if it.kind != Kind::App {
                continue;
            }
            it.hwnds.clear();
            for (exe, hwnds) in wins {
                if it.owns_exe(exe) {
                    it.hwnds.extend(hwnds.iter().map(|h| h.0 as isize));
                    claimed.insert(exe.clone());
                }
            }
        }
        for f in self.pinned.iter_mut().filter(|f| f.kind == Kind::Folder) {
            f.hwnds = f.children.iter().flat_map(|c| c.hwnds.clone()).collect();
        }
        let now = crate::anim::now();
        let mut old: Vec<Item> = std::mem::take(&mut self.running);
        let mut running: Vec<Item> = Vec::new();
        for (exe, hwnds) in wins {
            if claimed.contains(exe) || !std::path::Path::new(exe).exists() {
                continue;
            }
            let mut it = if let Some(i) = old.iter().position(|o| &o.exe == exe) {
                old.remove(i)
            } else if let Some(i) = self.icon_cache.iter().position(|o| &o.exe == exe) {
                let mut it = self.icon_cache.remove(i);
                it.cur = None;
                it
            } else {
                let mut it = Item::from_path(exe, Kind::Running);
                it.exe = exe.clone();
                it
            };
            it.kind = Kind::Running;
            it.hwnds = hwnds.iter().map(|h| h.0 as isize).collect();
            it.seen_at = now;
            running.push(it);
        }
        // окна на миг пропали (программа «уснула», окно перестраивается) — иконку не убираем сразу
        for mut it in old {
            if !claimed.contains(&it.exe) && now - it.seen_at < 3.0 {
                it.hwnds.retain(|&h| sys::is_window(win::hwnd_of(h)));
                running.push(it);
            } else {
                it.cache.clear(); // программа закрылась — уменьшенные копии иконки больше не нужны
                it.cur = None;
                self.icon_cache.push(it);
                if self.icon_cache.len() > 48 {
                    self.icon_cache.remove(0);
                }
            }
        }
        let sig_before: Vec<(u64, bool)> = self.signature();
        self.running = running;
        let sig = self.signature();
        if sig != sig_before {
            self.wake();
            return true;
        }
        false
    }

    fn signature(&self) -> Vec<(u64, bool)> {
        self.pinned.iter().chain(self.running.iter()).map(|i| (i.id, !i.hwnds.is_empty())).collect()
    }

    pub fn pins(&self) -> Vec<PinEntry> {
        self.pinned
            .iter()
            .map(|it| {
                if it.kind == Kind::Folder {
                    PinEntry::Folder(it.name.clone(), it.children.iter().map(|c| c.path.clone()).collect())
                } else {
                    PinEntry::App(it.path.clone())
                }
            })
            .collect()
    }

    pub fn reload_pinned(&mut self, cfg: &Config) {
        self.pinned = load_pinned(&cfg.pins());
        self.wake();
    }

    // -- мышь
    pub fn on_press(&mut self, x: f32, y: f32) {
        self.wake();
        let (u, v) = self.to_uv(x, y);
        self.press = self.item_at(u, v);
        self.press_pos = (x, y);
        self.dragging = false;
    }

    /// Движение с нажатой кнопкой. true — начали перетаскивать (нужно закрепить запущенную).
    pub fn on_drag(&mut self, cfg: &Config, x: f32, y: f32) -> Option<DragStart> {
        let id = self.press?;
        let kind = self.item(id)?.kind;
        if !matches!(kind, Kind::App | Kind::Running | Kind::Folder) {
            return None;
        }
        let mut started = None;
        if !self.dragging {
            if (x - self.press_pos.0).abs() + (y - self.press_pos.1).abs() < 12.0 * self.scale {
                return None; // дрогнувшая рука — ещё клик
            }
            self.dragging = true;
            started = Some(if kind == Kind::Running { DragStart::PinRunning(id) } else { DragStart::Plain });
            if kind == Kind::Running {
                self.pin_running(id);
            }
        }
        self.wake();
        let (u, _) = self.to_uv(x, y);
        // курсор над серединой другой иконки — не переставляем, а предлагаем сложить в папку
        self.merge_target = None;
        if kind != Kind::Folder && !self.drag_remove(cfg) {
            for o in &self.pinned {
                if o.id == id || !matches!(o.kind, Kind::App | Kind::Folder) {
                    continue;
                }
                if let Some(r) = self.targets.get(&o.id) {
                    if (u - r.cx()).abs() < r.w * 0.3 {
                        self.merge_target = Some(o.id);
                        return started;
                    }
                }
            }
        }
        if let Some(Slot::Pinned(cur)) = self.find(id) {
            let idx = self.pinned.iter().filter(|o| o.id != id && self.targets.get(&o.id).is_some_and(|r| r.cx() < u)).count();
            if idx != cur && idx < self.pinned.len() {
                let it = self.pinned.remove(cur);
                self.pinned.insert(idx, it);
            }
        }
        started
    }

    fn pin_running(&mut self, id: u64) {
        if let Some(i) = self.running.iter().position(|r| r.id == id) {
            let mut it = self.running.remove(i);
            it.kind = Kind::App;
            self.pinned.push(it);
        }
    }

    pub fn pin(&mut self, id: u64) -> bool {
        if self.running.iter().any(|r| r.id == id) {
            self.pin_running(id);
            return true;
        }
        false
    }

    pub fn finish_drag(&mut self, cfg: &Config, x: f32, y: f32) -> DragEnd {
        let id = self.press.take();
        let was_drag = std::mem::replace(&mut self.dragging, false);
        let target = self.merge_target.take();
        let Some(id) = id else { return DragEnd::None };
        if !was_drag {
            return DragEnd::Click(id);
        }
        // drag_remove считается по нажатому элементу — вернём его на время проверки
        self.press = Some(id);
        let removing = self.drag_remove(cfg);
        self.press = None;
        if let Some(t) = target.filter(|_| !removing) {
            return DragEnd::Merge(id, t);
        }
        if removing {
            self.poofs.push(((x, y), crate::anim::now()));
            return DragEnd::Remove(id);
        }
        // иконка «прилетает» из-под курсора на своё место
        let s = self.b(cfg) * (1.0 + (Self::mag(cfg) - 1.0) * 0.6);
        let (u, v) = self.to_uv(x, y);
        if let Some(it) = self.item_mut(id) {
            it.cur = Some(R::new(u - s / 2.0, v - s / 2.0, s, s));
        }
        DragEnd::Reordered
    }

    pub fn item_at_point(&self, x: f32, y: f32) -> Option<u64> {
        let (u, v) = self.to_uv(x, y);
        self.item_at(u, v)
    }

    pub fn add_poof_at_cursor(&mut self) {
        let p = self.cursor_local();
        self.poofs.push((p, crate::anim::now()));
    }

    pub fn contains_point(&self, gx: i32, gy: i32) -> bool {
        let w = self.to_win_rect(self.bg);
        w.contains((gx - self.x) as f32, (gy - self.y) as f32)
    }

    // -- папки и закреплённые
    pub fn remove_item(&mut self, id: u64) -> Option<Item> {
        match self.find(id)? {
            Slot::Pinned(i) => Some(self.pinned.remove(i)),
            Slot::Child(i, j) => {
                let it = self.pinned[i].children.remove(j);
                self.pinned[i].refresh_icon();
                Some(it)
            }
            Slot::Running(i) => Some(self.running.remove(i)),
            _ => None,
        }
    }

    /// Положить программу в папку target или сделать папку из двух программ.
    pub fn add_to_folder(&mut self, id: u64, target: u64) {
        if id == target {
            return;
        }
        let Some(mut it) = self.remove_item(id) else { return };
        it.kind = Kind::App;
        it.cur = None;
        match self.find(target) {
            Some(Slot::Pinned(i)) if self.pinned[i].kind == Kind::Folder => {
                self.pinned[i].children.push(it);
                self.pinned[i].refresh_icon();
            }
            Some(Slot::Pinned(i)) => {
                let mut t = self.pinned.remove(i);
                let cur = t.cur;
                t.cur = None;
                let mut f = Item::make_folder("Папка", vec![t, it]);
                f.cur = cur;
                self.pinned.insert(i, f);
            }
            _ => self.pinned.push(it),
        }
        self.wake();
    }

    pub fn new_folder(&mut self, id: Option<u64>) -> u64 {
        let mut folder = Item::make_folder("Новая папка", Vec::new());
        let fid = folder.id;
        match id.and_then(|i| self.find(i)) {
            Some(Slot::Pinned(i)) => {
                let mut it = std::mem::replace(&mut self.pinned[i], Item::new(Kind::Sep, "", "", None));
                it.kind = Kind::App;
                it.cur = None;
                folder.children.push(it);
                folder.refresh_icon();
                self.pinned[i] = folder;
            }
            Some(_) => {
                if let Some(mut it) = self.remove_item(id.unwrap()) {
                    it.kind = Kind::App;
                    it.cur = None;
                    folder.children.push(it);
                    folder.refresh_icon();
                }
                self.pinned.push(folder);
            }
            None => self.pinned.push(folder),
        }
        self.wake();
        fid
    }

    /// Вынуть программу из папки в док — сразу за папкой.
    pub fn take_out_of_folder(&mut self, id: u64) {
        if let Some(Slot::Child(i, j)) = self.find(id) {
            let mut it = self.pinned[i].children.remove(j);
            self.pinned[i].refresh_icon();
            it.cur = None;
            self.pinned.insert(i + 1, it);
            self.wake();
        }
    }

    pub fn ungroup(&mut self, id: u64) {
        if let Some(Slot::Pinned(i)) = self.find(id) {
            let f = self.pinned.remove(i);
            for (k, mut c) in f.children.into_iter().enumerate() {
                c.cur = None;
                self.pinned.insert(i + k, c);
            }
            self.wake();
        }
    }

    pub fn unpin(&mut self, id: u64) {
        if let Some(Slot::Pinned(i)) = self.find(id) {
            self.pinned.remove(i);
            self.wake();
        }
    }

    pub fn pin_path(&mut self, path: &str) -> bool {
        let p = path.to_string();
        if self.all_apps().iter().filter_map(|s| self.get(*s)).any(|it| it.kind == Kind::App && it.path.eq_ignore_ascii_case(&p)) {
            return false;
        }
        self.pinned.push(Item::from_path(&p, Kind::App));
        self.wake();
        true
    }

    pub fn rename_folder(&mut self, id: u64, name: &str) {
        if let Some(it) = self.item_mut(id) {
            it.name = if name.trim().is_empty() { "Папка".into() } else { name.trim().to_string() };
        }
    }

    /// Прямоугольник иконки на экране в координатах окна → экрана (для всплывающих окон).
    pub fn icon_global(&self, cfg: &Config, id: u64) -> RECT {
        self.icon_screen_rect(cfg, Some(id))
    }

    pub fn trash_id(&self) -> u64 {
        self.trash.id
    }
}

pub enum DragStart {
    Plain,
    PinRunning(u64),
}

pub enum DragEnd {
    None,
    Click(u64),
    Merge(u64, u64),
    Remove(u64),
    Reordered,
}

/// Что доку нужно знать о других частях приложения.
pub struct DockCtx {
    pub blocked: bool,
    pub fullscreen: bool,
    pub preview_inside: bool,
    pub preview_visible: bool,
    pub preview_item: Option<u64>,
    pub background: Option<(Rc<Image>, f32)>,
    /// живое «стекло» включено и помощник работает
    pub live_glass: bool,
    /// последний размытый кадр фона под доком и участок экрана, который он покрывает
    pub glass_frame: Option<(Rc<Image>, RECT)>,
}

fn lerp_rect(cur: R, tgt: R, k: f32) -> (R, bool) {
    if cur.near(&tgt, 0.25) {
        return (tgt, cur != tgt);
    }
    (cur.lerp(&tgt, k), true)
}

/// Картинка «по размеру окна» (как обои: по центру, с обрезкой) внутри формы path.
pub fn draw_cover(cv: &mut Canvas, img: &Image, r: R, path: &tiny_skia::Path, opacity: f32) {
    let k = (r.w / img.w as f32).max(r.h / img.h as f32);
    let (w, h) = (img.w as f32 * k, img.h as f32 * k);
    let dst = R::new(r.cx() - w / 2.0, r.cy() - h / 2.0, w, h);
    cv.image_in_path(img, dst, path, opacity);
}

fn draw_label(cv: &mut Canvas, text_s: &str, ax: f32, ay: f32, side: Side, ww: f32, wh: f32, scale: f32) {
    let size = 12.0 * scale;
    let (line, _) = text::metrics(Font::Regular, size);
    let w = text::width(text_s, Font::Regular, size) + 18.0 * scale;
    let h = line + 6.0 * scale;
    let (mut x, mut y) = match side {
        Side::Bottom => (ax - w / 2.0, ay - h),
        Side::Left => (ax, ay - h / 2.0),
        Side::Right => (ax - w, ay - h / 2.0),
    };
    x = x.clamp(2.0, (ww - w - 2.0).max(2.0));
    y = y.clamp(2.0, (wh - h - 2.0).max(2.0));
    let b = R::new(x.round(), y.round(), w.round(), h.round());
    cv.fill_rrect(b, 5.0 * scale, rgba(40, 40, 46, 230));
    text::draw_in(cv, text_s, Font::Regular, size, b, Align::Center, rgba(240, 240, 245, 255));
}

pub fn load_pinned(entries: &[PinEntry]) -> Vec<Item> {
    let mut out = Vec::new();
    for e in entries {
        match e {
            PinEntry::Folder(name, items) => {
                let kids: Vec<Item> = items
                    .iter()
                    .map(|p| sys::current_path(p))
                    .filter(|p| std::path::Path::new(p).exists())
                    .map(|p| Item::from_path(&p, Kind::App))
                    .collect();
                out.push(Item::make_folder(if name.is_empty() { "Папка" } else { name }, kids));
            }
            PinEntry::App(p) => {
                let p = sys::current_path(p);
                if std::path::Path::new(&p).exists() {
                    out.push(Item::from_path(&p, Kind::App));
                }
            }
        }
    }
    out
}

// ---------------------------------------------------------------- действия (через App)

pub fn ctx_for(app: &App, di: usize) -> DockCtx {
    let pv = &app.preview;
    DockCtx {
        blocked: app.launchpad.blocks_dock(),
        fullscreen: app.fullscreen && app.cfg.dock_in_fullscreen,
        preview_inside: pv.mouse_inside(),
        preview_visible: pv.visible,
        preview_item: pv.item.filter(|(d, _)| *d == di).map(|(_, i)| i),
        background: app.background("dock"),
        live_glass: app.cfg.dock_blur && app.cfg.live_for("dock") && !app.live_glass.disabled,
        glass_frame: app.live_glass.frame(di as u32),
    }
}

/// Сохранить закреплённые этого дока в настройки и перечитать остальные доки.
pub fn save_pinned(app: &mut App, di: usize) {
    let pins = app.docks[di].pins();
    app.cfg.set_pins(&pins);
    app.save_soon();
    for (k, d) in app.docks.iter_mut().enumerate() {
        if k != di {
            d.reload_pinned(&app.cfg);
        }
    }
    app.docks[di].wake();
    app.request_frame();
}

pub fn on_mouse(app: &mut App, di: usize, msg: u32, x: i32, y: i32) {
    use windows::Win32::UI::WindowsAndMessaging::*;
    if app.launchpad.blocks_dock() {
        return;
    }
    let (fx, fy) = (x as f32, y as f32);
    match msg {
        WM_LBUTTONDOWN => {
            app.close_preview();
            app.docks[di].on_press(fx, fy);
            unsafe {
                windows::Win32::UI::Input::KeyboardAndMouse::SetCapture(app.docks[di].hwnd);
            }
            app.request_frame();
        }
        WM_MOUSEMOVE => {
            if app.docks[di].press.is_some() && sys::key_down(0x01) {
                let cfg = app.cfg.clone();
                if let Some(DragStart::PinRunning(_)) = app.docks[di].on_drag(&cfg, fx, fy) {
                    save_pinned(app, di);
                }
                app.request_frame();
            } else {
                app.docks[di].wake();
                app.request_frame();
            }
        }
        WM_LBUTTONUP => {
            unsafe {
                let _ = windows::Win32::UI::Input::KeyboardAndMouse::ReleaseCapture();
            }
            let cfg = app.cfg.clone();
            match app.docks[di].finish_drag(&cfg, fx, fy) {
                DragEnd::Click(id) => {
                    let inside = fx >= 0.0 && fy >= 0.0 && fx < app.docks[di].w as f32 && fy < app.docks[di].h as f32;
                    if inside {
                        activate(app, di, id, false);
                    }
                }
                DragEnd::Merge(id, t) => {
                    app.docks[di].add_to_folder(id, t);
                    save_pinned(app, di);
                    app.poll_now();
                }
                DragEnd::Remove(id) => {
                    app.docks[di].unpin(id);
                    save_pinned(app, di);
                    app.poll_now();
                }
                DragEnd::Reordered => save_pinned(app, di),
                DragEnd::None => {}
            }
            app.request_frame();
        }
        WM_RBUTTONUP => {
            let id = app.docks[di].item_at_point(fx, fy);
            context_menu(app, di, id);
        }
        _ => {}
    }
}

/// Клик по иконке.
pub fn activate(app: &mut App, di: usize, id: u64, new_window: bool) {
    let Some(it) = app.docks[di].item(id) else { return };
    match it.kind {
        Kind::Trash => {
            shell::open_recycle_bin();
            return;
        }
        Kind::Launchpad => {
            app.toggle_launchpad();
            return;
        }
        Kind::Folder => {
            app.toggle_folder(di, id);
            return;
        }
        Kind::Sep => return,
        _ => {}
    }
    // окна, которые на самом деле ещё есть (список обновляется по событиям — программа могла закрыться)
    let alive: Vec<isize> = it
        .hwnds
        .iter()
        .copied()
        .filter(|&h| {
            let w = win::hwnd_of(h);
            sys::is_window(w) && (sys::is_visible(w) || sys::is_iconic(w))
        })
        .collect();
    let shift = sys::key_down(0x10);
    let path = it.path.clone();
    let exe = it.exe.clone();
    if let Some(it) = app.docks[di].item_mut(id) {
        it.hwnds = alive.clone();
    }
    if !alive.is_empty() && !new_window && !shift {
        let icon = app.docks[di].icon_screen_rect(&app.cfg, Some(id));
        app.focus_windows(&alive, icon);
        return;
    }
    let mut path = path;
    if !std::path::Path::new(&path).exists() && !path.contains("://") && !path.to_lowercase().starts_with("shell:") {
        let new = sys::current_path(&path); // программа обновилась, пока MiniDock работал
        if new != path && std::path::Path::new(&new).exists() {
            if let Some(it) = app.docks[di].item_mut(id) {
                it.path = new.clone();
                it.exe = new.to_lowercase();
            }
            path = new;
            save_pinned(app, di);
        } else {
            crate::log::line(&format!("программы из дока больше нет: {path}"));
            return; // файла нет — не запускать: Проводник открыл бы вместо программы «Документы»
        }
    }
    if app.cfg.bounce {
        if let Some(it) = app.docks[di].item_mut(id) {
            it.bounce_start = Some(crate::anim::now());
        }
        app.docks[di].wake();
        app.request_frame();
    }
    let icon = app.docks[di].icon_screen_rect(&app.cfg, Some(id));
    app.animator.expect_launch(&app.cfg, &exe, icon);
    app.hooks.want_create(app.animator.wants_create());
    let dir = if std::path::Path::new(&path).is_file() { Some(sys::parent_dir(&path)) } else { None };
    if !shell::launch(&path, "", dir.as_deref()) {
        if let Some(it) = app.docks[di].item_mut(id) {
            it.bounce_start = None;
        }
        crate::log::line(&format!("не запустилось из дока: {path}"));
    }
}

fn act(f: impl Fn(&mut App) + 'static) -> Option<Action> {
    Some(Rc::new(f))
}

pub fn context_menu(app: &mut App, di: usize, id: Option<u64>) {
    let mut m: Vec<MenuItem> = Vec::new();
    let dock = &app.docks[di];
    if let Some(id) = id {
        let it = dock.item(id).unwrap();
        match it.kind {
            Kind::Folder => {
                m.push(MenuItem::action("Открыть", act(move |a| a.open_folder(di, id, false))));
                m.push(MenuItem::action("Переименовать…", act(move |a| a.open_folder(di, id, true))));
                if !it.children.is_empty() {
                    m.push(MenuItem::action(
                        "Разгруппировать",
                        act(move |a| {
                            a.docks[di].ungroup(id);
                            save_pinned(a, di);
                            a.poll_now();
                        }),
                    ));
                }
                m.push(MenuItem::action(
                    "Убрать папку из дока",
                    act(move |a| {
                        a.close_folder();
                        a.docks[di].unpin(id);
                        save_pinned(a, di);
                        a.poll_now();
                    }),
                ));
                m.push(MenuItem::sep());
            }
            Kind::App | Kind::Running => {
                m.push(MenuItem::action("Открыть новое окно", act(move |a| activate(a, di, id, true))));
                if it.kind == Kind::App {
                    m.push(MenuItem::action(
                        "Убрать из дока",
                        act(move |a| {
                            a.docks[di].unpin(id);
                            save_pinned(a, di);
                            a.poll_now();
                        }),
                    ));
                } else {
                    m.push(MenuItem::action(
                        "Закрепить в доке",
                        act(move |a| {
                            a.docks[di].pin(id);
                            save_pinned(a, di);
                        }),
                    ));
                }
                let mut sub = Vec::new();
                for (fid, name) in dock.folders() {
                    sub.push(MenuItem::action(
                        &name,
                        act(move |a| {
                            a.docks[di].add_to_folder(id, fid);
                            save_pinned(a, di);
                            a.poll_now();
                        }),
                    ));
                }
                if !sub.is_empty() {
                    sub.push(MenuItem::sep());
                }
                sub.push(MenuItem::action(
                    "Новая папка…",
                    act(move |a| {
                        let fid = a.docks[di].new_folder(Some(id));
                        save_pinned(a, di);
                        a.poll_now();
                        a.open_folder_later(di, fid, true);
                    }),
                ));
                m.push(MenuItem::submenu("Переместить в папку", sub));
                let path = it.path.clone();
                m.push(MenuItem::action("Показать в проводнике", act(move |_| shell::show_in_explorer(&path))));
                if !it.hwnds.is_empty() {
                    let hw = it.hwnds.clone();
                    m.push(MenuItem::action(
                        "Закрыть все окна",
                        act(move |_| {
                            for h in &hw {
                                sys::close_window(win::hwnd_of(*h));
                            }
                        }),
                    ));
                }
                m.push(MenuItem::sep());
            }
            Kind::Trash => {
                m.push(MenuItem::action("Открыть корзину", act(|_| shell::open_recycle_bin())));
                m.push(MenuItem::sep());
            }
            _ => {}
        }
    }
    m.push(MenuItem::action("Добавить приложение…", act(move |a| a.add_app_dialog(di))));
    m.push(MenuItem::action(
        "Новая папка",
        act(move |a| {
            let fid = a.docks[di].new_folder(None);
            save_pinned(a, di);
            a.poll_now();
            a.open_folder_later(di, fid, true);
        }),
    ));
    let side = app.cfg.position.clone();
    let mut pos = Vec::new();
    for (label, value) in [("Снизу", "bottom"), ("Слева", "left"), ("Справа", "right")] {
        pos.push(MenuItem::radio(label, side == value, act(move |a| a.set_cfg(|c| c.position = value.to_string()))));
    }
    m.push(MenuItem::submenu("Положение на экране", pos));
    let ah = app.cfg.autohide;
    m.push(MenuItem::check("Автоматически скрывать док", ah, act(move |a| a.set_cfg(|c| c.autohide = !ah))));
    m.push(MenuItem::sep());
    m.push(MenuItem::action("Настройки MiniDock…", act(|a| a.open_settings())));
    m.push(MenuItem::action("Выход", act(|a| a.quit())));
    app.docks[di].menu_open = true;
    let (cx, cy) = win::cursor_pos();
    let owner = crate::menu::Owner::Dock(di);
    app.show_menu(m, cx, cy, owner);
}

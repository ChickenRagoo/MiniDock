//! Launchpad — полноэкранное меню приложений (замена «Пуска»).
//! Два режима: «Все» — все приложения по алфавиту со страницами; «Закреплённые» — плитки «Пуска» Windows.
//! Папки (в «Все», как на Mac): перетащить иконку на другую — получится папка; иконка, вытащенная за край
//! открытой папки, выходит из неё.

use std::rc::Rc;

use serde_json::Value;
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::app::{Action, App};
use crate::catalog::{self, Entry};
use crate::config::Config;
use crate::gfx::*;
use crate::glass::Glass;
use crate::lineedit::{Edit, LineEdit};
use crate::menu::{MenuItem, Owner};
use crate::text::{self, Align, Font};
use crate::win::{self, sys, Surface};

#[derive(Clone)]
enum Tile {
    App(Entry),
    Folder(String, Vec<Entry>),
}

impl Tile {
    fn name(&self) -> &str {
        match self {
            Tile::App(e) => &e.name,
            Tile::Folder(n, _) => n,
        }
    }
}

pub struct Launchpad {
    hwnd: HWND,
    surf: Surface,
    glass: Option<Glass>,
    shown: bool,
    closing: Option<f64>,
    then: Option<Entry>,
    opened: f64,
    was_active: bool,
    search: LineEdit,
    top: Vec<Tile>,
    filtered: Vec<Tile>,
    folder: Option<usize>,
    folder_k: f32,
    folder_title: LineEdit,
    page: usize,
    page_pos: f32,
    selected: usize,
    kb_nav: bool,
    hover: Option<usize>,
    press: Option<((f32, f32), Option<usize>)>,
    drag: Option<usize>,
    drop_on: Option<usize>,
    mode_k: f32,
    mode_hl: f32,
    x: i32,
    y: i32,
    w: i32,
    h: i32,
    scale: f32,
    last: f64,
    folder_icons: Vec<Option<Rc<Image>>>,
    pub dirty: bool,
    blink_armed: bool,
    caret_shown: bool,
}

impl Launchpad {
    pub fn new() -> Launchpad {
        Launchpad {
            hwnd: HWND::default(),
            surf: Surface::default(),
            glass: None,
            shown: false,
            closing: None,
            then: None,
            opened: 0.0,
            was_active: false,
            search: LineEdit::default(),
            top: Vec::new(),
            filtered: Vec::new(),
            folder: None,
            folder_k: 0.0,
            folder_title: LineEdit::default(),
            page: 0,
            page_pos: 0.0,
            selected: 0,
            kb_nav: false,
            hover: None,
            press: None,
            drag: None,
            drop_on: None,
            mode_k: 1.0,
            mode_hl: 0.0,
            x: 0,
            y: 0,
            w: 0,
            h: 0,
            scale: 1.0,
            last: 0.0,
            folder_icons: Vec::new(),
            dirty: true,
            blink_armed: false,
            caret_shown: false,
        }
    }

    /// Пока открыт Launchpad, доком пользоваться нельзя (док уезжает).
    pub fn blocks_dock(&self) -> bool {
        self.shown && self.closing.is_none()
    }

    pub fn visible(&self) -> bool {
        self.shown
    }

    pub fn frame(&mut self, _cfg: &Config) -> bool {
        self.shown
    }

    fn searching(&self) -> bool {
        !self.search.text.trim().is_empty()
    }

    fn mode<'a>(&self, cfg: &'a Config, groups: bool) -> &'a str {
        if self.searching() || (cfg.lp_mode == "start" && !groups) {
            "all"
        } else {
            cfg.lp_mode.as_str()
        }
    }

    fn per_page(cfg: &Config) -> usize {
        (cfg.lp_columns * cfg.lp_rows).max(1) as usize
    }

    fn pages(&self, cfg: &Config, start: bool) -> usize {
        if start {
            1
        } else {
            self.filtered.len().div_ceil(Self::per_page(cfg)).max(1)
        }
    }

    /// (колонки, ряды, иконка, ширина ячейки, высота ячейки, x0, y0) — физические пиксели.
    fn grid(&self, cfg: &Config) -> (usize, usize, f32, f32, f32, f32, f32) {
        let s = self.scale;
        let (cols, rows, ic) = (cfg.lp_columns.max(1) as usize, cfg.lp_rows.max(1) as usize, cfg.lp_icon * s);
        let w = self.w as f32;
        let h = self.h as f32;
        let cw = (ic * 1.9).max((w - 240.0 * s) / cols as f32).min(ic * 2.6);
        let ch = ic + 52.0 * s;
        let (gw, gh) = (cols as f32 * cw, rows as f32 * ch);
        let x0 = (w - gw) / 2.0;
        let y0 = (160.0 * s).max((h - gh) / 2.0 + 20.0 * s);
        (cols, rows, ic, cw, ch, x0, y0)
    }

    fn item_rect(&self, cfg: &Config, i: usize, page_offset: f32) -> R {
        let (cols, _, ic, cw, ch, x0, y0) = self.grid(cfg);
        let per = Self::per_page(cfg);
        let (page, k) = (i / per, i % per);
        let (r, c) = (k / cols, k % cols);
        let x = x0 + c as f32 * cw + (cw - ic) / 2.0 + (page as f32 - page_offset) * self.w as f32;
        R::new(x, y0 + r as f32 * ch, ic, ic)
    }

    fn start_items(&self, cfg: &Config, groups: &[Vec<Entry>]) -> Vec<(Entry, R)> {
        let s = self.scale;
        let ic = cfg.lp_icon * 0.9 * s;
        let (cw, ch) = (ic + 46.0 * s, ic + 44.0 * s);
        let (gap_x, gap_y) = (64.0 * s, 40.0 * s);
        let blocks: Vec<(&Vec<Entry>, f32, f32)> = groups
            .iter()
            .map(|g| {
                let cols = g.iter().filter_map(|e| e.tile.map(|t| t.0 + t.2)).max().unwrap_or(1);
                let rows = g.iter().filter_map(|e| e.tile.map(|t| t.1 + t.3)).max().unwrap_or(1);
                (g, cols as f32 * cw, rows as f32 * ch)
            })
            .collect();
        let max_w = self.w as f32 - 200.0 * s;
        let mut lines: Vec<Vec<(&Vec<Entry>, f32, f32)>> = Vec::new();
        let mut line = Vec::new();
        let mut lw = 0.0;
        for b in blocks {
            if !line.is_empty() && lw + gap_x + b.1 > max_w {
                lines.push(std::mem::take(&mut line));
                lw = 0.0;
            }
            lw += if line.is_empty() { 0.0 } else { gap_x } + b.1;
            line.push(b);
        }
        if !line.is_empty() {
            lines.push(line);
        }
        let total_h: f32 = lines.iter().map(|l| l.iter().map(|b| b.2).fold(0.0, f32::max)).sum::<f32>() + gap_y * (lines.len().max(1) - 1) as f32;
        let mut y = (170.0 * s).max((self.h as f32 - total_h) / 2.0 + 30.0 * s);
        let mut items = Vec::new();
        for l in &lines {
            let w: f32 = l.iter().map(|b| b.1).sum::<f32>() + gap_x * (l.len().max(1) - 1) as f32;
            let mut x = (self.w as f32 - w) / 2.0;
            for (g, bw, _) in l {
                for e in g.iter() {
                    let (c, r, tw, th) = e.tile.unwrap_or((0, 0, 1, 1));
                    let (tile_w, tile_h) = (tw as f32 * cw, th as f32 * ch);
                    let size = ic * if tw > 1 && th > 1 { 1.6 } else { 1.0 };
                    let cx = x + c as f32 * cw + tile_w / 2.0;
                    let top = y + r as f32 * ch + (tile_h - size - 26.0 * s) / 2.0;
                    items.push((e.clone(), R::new(cx - size / 2.0, top, size, size)));
                }
                x += bw + gap_x;
            }
            y += l.iter().map(|b| b.2).fold(0.0, f32::max) + gap_y;
        }
        items
    }

    fn folder_cols(&self, cfg: &Config, n: usize) -> usize {
        ((n as f32 * 1.6).sqrt().ceil() as usize).clamp(3, cfg.lp_columns.max(3) as usize)
    }

    fn folder_panel(&self, cfg: &Config, n: usize) -> R {
        let s = self.scale;
        let (_, _, _, cw, ch, _, _) = self.grid(cfg);
        let cols = self.folder_cols(cfg, n);
        let rows = n.div_ceil(cols).max(1);
        let w = cols as f32 * cw + 80.0 * s;
        let h = (rows as f32 * ch + 110.0 * s).min(self.h as f32 - 120.0 * s);
        R::new((self.w as f32 - w) / 2.0, (self.h as f32 - h) / 2.0 + 20.0 * s, w, h)
    }

    fn folder_items(&self, cfg: &Config) -> Vec<(usize, Entry, R)> {
        let Some(fi) = self.folder else { return Vec::new() };
        let Some(Tile::Folder(_, kids)) = self.top.get(fi) else { return Vec::new() };
        let s = self.scale;
        let (_, _, ic, cw, ch, _, _) = self.grid(cfg);
        let p = self.folder_panel(cfg, kids.len());
        let cols = self.folder_cols(cfg, kids.len());
        kids.iter()
            .enumerate()
            .map(|(i, e)| {
                let (r, c) = (i / cols, i % cols);
                (i, e.clone(), R::new(p.x + 40.0 * s + c as f32 * cw + (cw - ic) / 2.0, p.y + 80.0 * s + r as f32 * ch, ic, ic))
            })
            .collect()
    }

    fn seg_rects(&self) -> [R; 2] {
        let s = self.scale;
        let w = 120.0 * s;
        let x0 = self.w as f32 / 2.0 - w;
        [R::new(x0, 108.0 * s, w, 26.0 * s), R::new(x0 + w, 108.0 * s, w, 26.0 * s)]
    }

    fn search_rect(&self) -> R {
        let s = self.scale;
        R::new(self.w as f32 / 2.0 - 140.0 * s, 60.0 * s, 280.0 * s, 32.0 * s)
    }
}

fn hidden_set(cfg: &Config) -> std::collections::HashSet<String> {
    cfg.lp_hidden.iter().map(|k| k.to_lowercase()).collect()
}

/// Папки из настроек + приложения не из папок.
fn build_top(app: &mut App) {
    let hidden = hidden_set(&app.cfg);
    let visible: Vec<Entry> = app.catalog.entries.iter().filter(|e| !hidden.contains(&e.parse.to_lowercase())).cloned().collect();
    let mut used = std::collections::HashSet::new();
    let mut folders = Vec::new();
    for f in &app.cfg.lp_folders {
        let name = f.get("name").and_then(|v| v.as_str()).unwrap_or("Папка").to_string();
        let items: Vec<String> = f.get("items").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_lowercase())).collect()).unwrap_or_default();
        let kids: Vec<Entry> = items.iter().filter_map(|k| visible.iter().find(|e| e.parse.to_lowercase() == *k).cloned()).collect();
        if kids.is_empty() {
            continue;
        }
        for k in &kids {
            used.insert(k.parse.to_lowercase());
        }
        folders.push(Tile::Folder(name, kids));
    }
    let mut top = folders;
    top.extend(visible.into_iter().filter(|e| !used.contains(&e.parse.to_lowercase())).map(Tile::App));
    app.launchpad.top = top;
    on_search(app, true);
}

fn on_search(app: &mut App, keep_page: bool) {
    let q = app.launchpad.search.text.trim().to_lowercase();
    let lp = &mut app.launchpad;
    if q.is_empty() {
        lp.filtered = lp.top.clone();
    } else {
        // поиск — по всем приложениям, включая лежащие в папках
        let hidden = hidden_set(&app.cfg);
        let mut f: Vec<(i32, Entry)> = app
            .catalog
            .entries
            .iter()
            .filter(|e| !hidden.contains(&e.parse.to_lowercase()))
            .filter_map(|e| {
                let s = crate::spotlight::score(&e.name, &q);
                (s > 0).then(|| (s, e.clone()))
            })
            .collect();
        f.sort_by(|a, b| b.0.cmp(&a.0));
        lp.filtered = f.into_iter().map(|x| Tile::App(x.1)).collect();
        lp.folder = None;
    }
    if !keep_page {
        lp.page = 0;
        lp.page_pos = 0.0;
        lp.selected = 0;
    }
    let pages = lp.pages(&app.cfg, false);
    lp.page = lp.page.min(pages - 1);
}

pub fn on_loaded(app: &mut App) {
    build_top(app);
    app.launchpad.dirty = true;
    app.request_frame();
}

fn folder_cfg(app: &App) -> Vec<(String, Vec<String>)> {
    app.launchpad
        .top
        .iter()
        .filter_map(|t| match t {
            Tile::Folder(n, kids) => Some((n.clone(), kids.iter().map(|k| k.parse.clone()).collect())),
            _ => None,
        })
        .collect()
}

fn save_folders(app: &mut App, folders: Vec<(String, Vec<String>)>) {
    app.cfg.lp_folders = folders
        .into_iter()
        .filter(|f| !f.1.is_empty()) // пустые папки исчезают, как на Mac
        .map(|(n, items)| {
            let mut o = serde_json::Map::new();
            o.insert("name".into(), Value::String(n));
            o.insert("items".into(), Value::Array(items.into_iter().map(Value::String).collect()));
            Value::Object(o)
        })
        .collect();
    app.save_soon();
    let open_name = app.launchpad.folder.and_then(|i| app.launchpad.top.get(i)).map(|t| t.name().to_string());
    build_top(app);
    // открытая папка — её обновлённая запись (или закрыть, если опустела)
    app.launchpad.folder = open_name.and_then(|n| app.launchpad.top.iter().position(|t| matches!(t, Tile::Folder(m, _) if *m == n)));
}

fn unique_name(name: &str, folders: &[(String, Vec<String>)]) -> String {
    let mut out = name.to_string();
    let mut n = 2;
    while folders.iter().any(|f| f.0 == out) {
        out = format!("{name} {n}");
        n += 1;
    }
    out
}

fn put_in_folder(app: &mut App, src: &Entry, dst: &Tile) {
    let mut folders = folder_cfg(app);
    for f in &mut folders {
        f.1.retain(|k| !k.eq_ignore_ascii_case(&src.parse));
    }
    match dst {
        Tile::Folder(name, _) => {
            if let Some(f) = folders.iter_mut().find(|f| &f.0 == name) {
                f.1.push(src.parse.clone());
            }
            save_folders(app, folders);
        }
        Tile::App(d) => {
            if d.parse == src.parse {
                return;
            }
            let name = unique_name("Папка", &folders);
            folders.insert(0, (name.clone(), vec![d.parse.clone(), src.parse.clone()]));
            save_folders(app, folders);
            if let Some(i) = app.launchpad.top.iter().position(|t| matches!(t, Tile::Folder(m, _) if *m == name)) {
                open_folder(app, i, true);
            }
        }
    }
}

fn take_out(app: &mut App, e: &Entry) {
    let mut folders = folder_cfg(app);
    for f in &mut folders {
        f.1.retain(|k| !k.eq_ignore_ascii_case(&e.parse));
    }
    save_folders(app, folders);
}

fn open_folder(app: &mut App, i: usize, edit: bool) {
    let lp = &mut app.launchpad;
    lp.folder = Some(i);
    lp.folder_k = 0.0;
    lp.hover = None;
    lp.selected = 0;
    lp.folder_title = LineEdit::new(lp.top.get(i).map(|t| t.name()).unwrap_or("Папка"));
    lp.folder_title.focused = edit;
    if edit {
        lp.folder_title.select_all();
    }
    lp.search.focused = !edit;
    lp.folder_icons.clear();
}

fn rename_folder(app: &mut App) {
    let Some(i) = app.launchpad.folder else { return };
    let Some(Tile::Folder(old, _)) = app.launchpad.top.get(i).cloned() else { return };
    let mut name = app.launchpad.folder_title.text.trim().to_string();
    if name.is_empty() {
        name = "Папка".into();
    }
    app.launchpad.folder_title.focused = false;
    app.launchpad.search.focused = true;
    if name != old {
        let mut folders = folder_cfg(app);
        let others: Vec<(String, Vec<String>)> = folders.iter().filter(|f| f.0 != old).cloned().collect();
        let name = unique_name(&name, &others);
        for f in &mut folders {
            if f.0 == old {
                f.0 = name.clone();
            }
        }
        save_folders(app, folders);
        app.launchpad.folder = app.launchpad.top.iter().position(|t| matches!(t, Tile::Folder(m, _) if *m == name));
        app.launchpad.folder_title.set(&name);
    }
}

fn close_folder(app: &mut App) {
    if app.launchpad.folder_title.focused {
        rename_folder(app);
    }
    let lp = &mut app.launchpad;
    lp.folder = None;
    lp.hover = None;
    lp.search.focused = true;
    lp.folder_icons.clear();
}

pub fn toggle(app: &mut App) {
    if app.launchpad.shown && app.launchpad.closing.is_none() {
        close(app, None);
    } else {
        open(app);
    }
}

pub fn open(app: &mut App) {
    if !app.catalog.loaded {
        app.catalog.load();
    }
    app.close_preview();
    let lp = &mut app.launchpad;
    if lp.hwnd.is_invalid() {
        lp.hwnd = win::create_focusable("MiniDockLaunchpad", true);
        win::dwm::exclude_from_peek(lp.hwnd);
    }
    if lp.glass.is_none() {
        lp.glass = Some(Glass::new());
    }
    let m = sys::primary_monitor();
    lp.scale = m.scale;
    lp.x = m.rect.left;
    lp.y = m.rect.top;
    lp.w = m.w();
    lp.h = m.h();
    lp.closing = None;
    lp.then = None;
    lp.was_active = false;
    lp.opened = crate::anim::now();
    lp.last = lp.opened;
    lp.search = LineEdit::default();
    lp.search.focused = true;
    lp.folder = None;
    lp.page = 0;
    lp.page_pos = 0.0;
    lp.selected = 0;
    lp.kb_nav = false;
    lp.mode_hl = if app.cfg.lp_mode == "all" { 0.0 } else { 1.0 };
    lp.shown = true;
    build_top(app);
    paint(app);
    let lp = &app.launchpad;
    unsafe {
        let _ = SetWindowPos(lp.hwnd, Some(HWND_TOPMOST), lp.x, lp.y, lp.w, lp.h, SWP_SHOWWINDOW);
    }
    sys::force_foreground(lp.hwnd);
    // пока у Launchpad нет фокуса (Windows не всегда отдаёт сразу), набор уходит в его поиск через хук
    win::hooks::LAUNCHPAD_KEYS.store(sys::foreground() != app.launchpad.hwnd, std::sync::atomic::Ordering::Relaxed);
    for d in &mut app.docks {
        d.wake(); // док уезжает, пока открыт Launchpad
    }
    app.request_frame();
}

pub fn close(app: &mut App, then: Option<Entry>) {
    let lp = &mut app.launchpad;
    if lp.closing.is_some() {
        if then.is_some() {
            lp.then = then;
        }
        return;
    }
    lp.closing = Some(crate::anim::now());
    lp.then = then;
    win::hooks::LAUNCHPAD_KEYS.store(false, std::sync::atomic::Ordering::Relaxed);
    app.request_frame();
}

fn finish_close(app: &mut App) {
    let lp = &mut app.launchpad;
    lp.shown = false;
    lp.closing = None;
    win::hide(lp.hwnd);
    lp.glass = None;
    lp.surf.free();
    lp.folder_icons.clear();
    let then = lp.then.take();
    app.catalog.trim(); // иконки Launchpad выгружаем из памяти
    crate::text::trim_cache();
    crate::win::sys::trim_working_set();
    for d in &mut app.docks {
        d.wake(); // Launchpad закрыт — док снова доступен
    }
    if let Some(e) = then {
        catalog::run_entry(&e);
    }
}

pub fn tick(app: &mut App) -> bool {
    if !app.launchpad.shown {
        return false;
    }
    let cfg = app.cfg.clone();
    let now = crate::anim::now();
    let lp = &mut app.launchpad;
    if let Some(t) = lp.closing {
        if now - t >= 0.14 {
            finish_close(app);
            return false;
        }
    }
    let dt = ((now - lp.last) as f32).clamp(0.001, 0.05);
    lp.last = now;
    let target = lp.page as f32;
    if (lp.page_pos - target).abs() < 0.002 {
        lp.page_pos = target;
    } else {
        lp.page_pos = lerp(lp.page_pos, target, smooth(0.2, dt));
    }
    if lp.mode_k < 1.0 {
        lp.mode_k = (lp.mode_k + 0.08 * dt / FRAME_REF).min(1.0);
    }
    if lp.folder.is_some() && lp.folder_k < 1.0 {
        lp.folder_k = (lp.folder_k + 0.09 * dt / FRAME_REF).min(1.0);
    }
    let hl_t = if cfg.lp_mode == "all" { 0.0 } else { 1.0 };
    let mut anim = lp.closing.is_some() || now - lp.opened < 0.22 || lp.drag.is_some();
    anim |= lp.page_pos != target || lp.mode_k < 1.0 || (lp.folder.is_some() && lp.folder_k < 1.0);
    if (lp.mode_hl - hl_t).abs() > 0.005 {
        lp.mode_hl = lerp(lp.mode_hl, hl_t, smooth(0.3, dt));
        anim = true;
    } else {
        lp.mode_hl = hl_t;
    }
    // фокус: Windows может не отдать его с первого раза
    let focusing = lp.closing.is_none() && now - lp.opened < 1.0;
    if focusing && sys::foreground() != lp.hwnd {
        sys::force_foreground(lp.hwnd);
    }
    win::hooks::LAUNCHPAD_KEYS.store(lp.closing.is_none() && sys::foreground() != lp.hwnd, std::sync::atomic::Ordering::Relaxed);
    // весь экран перерисовывается только когда что-то изменилось (а курсор мигает по таймеру, без кадров)
    let caret = (lp.search.focused && lp.search.caret_on()) || (lp.folder_title.focused && lp.folder_title.caret_on());
    if anim || lp.dirty || caret != lp.caret_shown {
        lp.dirty = false;
        lp.caret_shown = caret;
        paint(app);
    }
    let lp = &mut app.launchpad;
    if !anim && !lp.blink_armed && (lp.search.focused || lp.folder_title.focused) {
        lp.blink_armed = true;
        crate::app::defer_ms(265, || {
            crate::app::with(|a| {
                a.launchpad.blink_armed = false;
                a.request_frame();
            });
        });
    }
    anim || focusing
}

fn grid_items(app: &App) -> Vec<(usize, Tile, R)> {
    let cfg = &app.cfg;
    let lp = &app.launchpad;
    if lp.mode(cfg, !app.catalog.groups.is_empty()) == "start" {
        return lp.start_items(cfg, &app.catalog.groups).into_iter().enumerate().map(|(i, (e, r))| (i, Tile::App(e), r)).collect();
    }
    let per = Launchpad::per_page(cfg);
    let lo = (lp.page_pos.floor() as usize) * per;
    let hi = lp.filtered.len().min((lp.page_pos.ceil() as usize + 1) * per);
    (lo..hi)
        .filter_map(|i| {
            let r = lp.item_rect(cfg, i, lp.page_pos);
            (r.right() >= 0.0 && r.x <= lp.w as f32).then(|| (i, lp.filtered[i].clone(), r))
        })
        .collect()
}

fn current_list(app: &App) -> Vec<Tile> {
    let lp = &app.launchpad;
    if let Some(Tile::Folder(_, kids)) = lp.folder.and_then(|i| lp.top.get(i)) {
        return kids.iter().cloned().map(Tile::App).collect();
    }
    if lp.mode(&app.cfg, !app.catalog.groups.is_empty()) == "start" {
        return lp.start_items(&app.cfg, &app.catalog.groups).into_iter().map(|(e, _)| Tile::App(e)).collect();
    }
    lp.filtered.clone()
}

fn visible_items(app: &App) -> Vec<(usize, Tile, R)> {
    if app.launchpad.folder.is_some() {
        return app.launchpad.folder_items(&app.cfg).into_iter().map(|(i, e, r)| (i, Tile::App(e), r)).collect();
    }
    grid_items(app)
}

fn index_at(app: &App, x: f32, y: f32) -> Option<usize> {
    let s = app.launchpad.scale;
    visible_items(app).into_iter().find(|(_, _, r)| r.adjusted(-14.0 * s, -10.0 * s, 14.0 * s, 30.0 * s).contains(x, y)).map(|t| t.0)
}

fn tile_icon(app: &mut App, t: &Tile) -> Option<Rc<Image>> {
    match t {
        Tile::App(e) => app.catalog.icon(e),
        Tile::Folder(_, kids) => {
            let imgs: Vec<Rc<Image>> = kids.iter().take(9).filter_map(|k| app.catalog.icon(k)).collect();
            Some(Rc::new(crate::dock::folder_image(&imgs)))
        }
    }
}

fn paint(app: &mut App) {
    let cfg = app.cfg.clone();
    if !app.launchpad.shown {
        return;
    }
    let items = grid_items(app);
    let mut icons: Vec<Option<Rc<Image>>> = Vec::with_capacity(items.len());
    for (_, t, _) in &items {
        icons.push(tile_icon(app, t));
    }
    let folder_items: Vec<(usize, Entry, R)> = app.launchpad.folder_items(&cfg);
    if app.launchpad.folder_icons.len() != folder_items.len() {
        let fi: Vec<Option<Rc<Image>>> = folder_items.iter().map(|(_, e, _)| app.catalog.icon(e)).collect();
        app.launchpad.folder_icons = fi;
    }
    let groups = !app.catalog.groups.is_empty();
    let loaded = app.catalog.loaded;
    let bg = app.background("launchpad");
    let lp = &mut app.launchpad;
    if !lp.surf.ensure(lp.w, lp.h) {
        return;
    }
    let s = lp.scale;
    let now = crate::anim::now();
    let alpha = match lp.closing {
        Some(t) => 1.0 - ((now - t) / 0.14).min(1.0) as f32,
        None => ease_out(((now - lp.opened) / 0.2).min(1.0) as f32),
    };
    let full = R::new(0.0, 0.0, lp.w as f32, lp.h as f32);
    let searching = lp.searching();
    let segs = lp.seg_rects();
    let sr = lp.search_rect();
    let mode_hl = lp.mode_hl;
    let appear = 1.0 - (1.0 - lp.mode_k).powi(3);
    let in_folder = lp.folder.is_some();
    let fk = if in_folder { ease_out(lp.folder_k) } else { 0.0 };
    let base = appear * (1.0 - 0.85 * fk);
    let (hover, selected, kb_nav, drop_on, drag) = (lp.hover, lp.selected, lp.kb_nav, lp.drop_on, lp.drag);
    let pages = lp.pages(&cfg, lp.mode(&cfg, groups) == "start");
    let page = lp.page;
    let search = lp.search.clone();
    let title = lp.folder_title.clone();
    let folder_panel = lp.folder.and_then(|i| lp.top.get(i)).map(|t| if let Tile::Folder(_, k) = t { lp.folder_panel(&cfg, k.len()) } else { R::default() });
    let folder_icons = lp.folder_icons.clone();
    let (cx, cy) = win::cursor_pos();
    let (lx, ly) = ((cx - lp.x) as f32, (cy - lp.y) as f32);
    let drag_icon = drag.and_then(|i| if in_folder { folder_icons.get(i).cloned().flatten() } else { items.iter().position(|t| t.0 == i).and_then(|k| icons[k].clone()) });
    {
        let mut cv = lp.surf.canvas();
        cv.clear();
        cv.fill_rect(full, rgba(12, 12, 16, 130));
        if let Some((img, op)) = &bg {
            if let Some(p) = rounded_rect(full, 0.0) {
                crate::dock::draw_cover(&mut cv, img, full, &p, *op);
            }
        }
        // поиск
        cv.fill_rrect(sr, 15.0 * s, rgba(255, 255, 255, 38));
        cv.stroke_rrect(sr.inset(0.5), 15.0 * s, rgba(255, 255, 255, 45), 1.0);
        search.paint(&mut cv, sr.inset(14.0 * s), Font::Regular, 14.7 * s, rgba(244, 244, 247, 255), "Поиск", true);
        // переключатель режимов
        if !searching {
            let both = segs[0].union(&segs[1]);
            cv.fill_rrect(both, 13.0 * s, rgba(255, 255, 255, 30));
            let hl = segs[0].translated(mode_hl * segs[0].w, 0.0).inset(2.0 * s);
            cv.fill_rrect(hl, 11.0 * s, rgba(255, 255, 255, 70));
            for (r, l) in segs.iter().zip(["Все", "Закреплённые"]) {
                text::draw_in(&mut cv, l, Font::Semibold, 12.0 * s, *r, Align::Center, rgba(245, 245, 248, 255));
            }
        }
        if items.is_empty() {
            let t = if !loaded { "Загрузка приложений…" } else { "Ничего не найдено" };
            text::draw_in(&mut cv, t, Font::Regular, 12.0 * s, R::new(0.0, full.h / 2.0 - 20.0 * s, full.w, 40.0 * s), Align::Center, rgba(255, 255, 255, 150));
        }
        let draw_tiles = |cv: &mut Canvas, list: &[(usize, String, R, Option<Rc<Image>>)], op: f32, appear: f32, active: bool| {
            for (i, name, r, icon) in list {
                let mut r = *r;
                if appear < 1.0 {
                    r = r.scaled(0.9 + 0.1 * appear);
                }
                cv.opacity = op * if drag == Some(*i) && active { 0.3 } else { 1.0 };
                if active && drop_on == Some(*i) {
                    cv.fill_rrect(r.adjusted(-10.0 * s, -10.0 * s, 10.0 * s, 10.0 * s), r.w * 0.3, rgba(255, 255, 255, 60));
                } else if active && *i == selected && (searching || kb_nav) {
                    cv.fill_rrect(r.adjusted(-12.0 * s, -10.0 * s, 12.0 * s, 30.0 * s), 14.0 * s, rgba(255, 255, 255, 40));
                }
                let k = if active && hover == Some(*i) && drag.is_none() { 1.08 } else { 1.0 };
                let ir = r.scaled(k);
                if let Some(img) = icon {
                    let f = img.w.max(img.h) as f32;
                    cv.image(img, R::centered(ir.cx(), ir.cy(), img.w as f32 * ir.w / f, img.h as f32 * ir.h / f));
                }
                let lr = R::new(r.cx() - (r.w + 44.0 * s) / 2.0, r.bottom() + 8.0 * s, r.w + 44.0 * s, 18.0 * s);
                text::draw_in(cv, name, Font::Regular, 12.0 * s, lr.translated(0.0, 1.0), Align::Center, rgba(0, 0, 0, 120));
                text::draw_in(cv, name, Font::Regular, 12.0 * s, lr, Align::Center, rgba(245, 245, 248, 255));
            }
            cv.opacity = 1.0;
        };
        let list: Vec<(usize, String, R, Option<Rc<Image>>)> = items.iter().zip(&icons).map(|((i, t, r), ic)| (*i, t.name().to_string(), *r, ic.clone())).collect();
        draw_tiles(&mut cv, &list, base, appear, !in_folder);
        if let Some(p) = folder_panel {
            let k = 0.92 + 0.08 * fk;
            let pr = p.scaled(k);
            cv.opacity = fk;
            cv.fill_rrect(pr, 28.0 * s, rgba(58, 58, 68, 236));
            cv.stroke_rrect(pr.inset(0.5), 28.0 * s, rgba(255, 255, 255, 40), 1.0);
            let tr = R::new(p.cx() - 180.0 * s, p.y + 18.0 * s, 360.0 * s, 40.0 * s);
            if title.focused {
                cv.fill_rrect(tr, 8.0 * s, rgba(255, 255, 255, 30));
            }
            title.paint(&mut cv, tr, Font::Semibold, 21.3 * s, rgba(244, 244, 247, 255), "", true);
            cv.opacity = 1.0;
            let fl: Vec<(usize, String, R, Option<Rc<Image>>)> = folder_items.iter().zip(&folder_icons).map(|((i, e, r), ic)| (*i, e.name.clone(), *r, ic.clone())).collect();
            draw_tiles(&mut cv, &fl, fk, 1.0, true);
        }
        if let Some(img) = &drag_icon {
            let sz = cfg.lp_icon * 1.05 * s;
            cv.image_alpha(img, R::new(lx - sz / 2.0, ly - sz / 2.0, sz, sz), 0.9);
        }
        if pages > 1 {
            let (pcx, py) = (full.w / 2.0, full.h - 70.0 * s);
            for k in 0..pages {
                let x = pcx + (k as f32 - (pages - 1) as f32 / 2.0) * 18.0 * s;
                cv.fill_circle(x, py, 4.0 * s, rgba(255, 255, 255, if k == page { 230 } else { 90 }));
            }
        }
    }
    lp.surf.present(lp.hwnd, lp.x, lp.y, (alpha * 255.0) as u8);
    if let Some(g) = lp.glass.as_mut() {
        g.place(lp.x, lp.y, lp.x + lp.w, lp.y + lp.h, 0, lp.hwnd, win::dwm::Blur::Blur, 0);
    }
}

fn go_page(app: &mut App, page: i32) {
    let groups = !app.catalog.groups.is_empty();
    let lp = &mut app.launchpad;
    let n = lp.pages(&app.cfg, lp.mode(&app.cfg, groups) == "start") as i32;
    lp.page = page.clamp(0, n - 1) as usize;
}

fn screen_rect(app: &App, i: usize) -> Option<RECT> {
    let lp = &app.launchpad;
    visible_items(app).into_iter().find(|t| t.0 == i).map(|(_, _, r)| RECT {
        left: lp.x + r.x as i32,
        top: lp.y + r.y as i32,
        right: lp.x + r.right() as i32,
        bottom: lp.y + r.bottom() as i32,
    })
}

fn launch(app: &mut App, i: usize) {
    let list = current_list(app);
    let Some(t) = list.get(i).cloned() else { return };
    match t {
        Tile::Folder(..) => {
            if let Some(fi) = app.launchpad.top.iter().position(|x| x.name() == t.name()) {
                open_folder(app, fi, false);
            }
        }
        Tile::App(e) => {
            // окно новой программы «вылетит» из её иконки
            if let (Some(exe), Some(r)) = (catalog::entry_exe(&e), screen_rect(app, i)) {
                let cfg = app.cfg.clone();
                app.animator.expect_launch(&cfg, &exe, r);
                app.hooks.want_create(app.animator.wants_create());
            }
            close(app, Some(e));
        }
    }
}

fn set_mode(app: &mut App, mode: &str) {
    if app.cfg.lp_mode == mode {
        return;
    }
    let m = mode.to_string();
    app.cfg.lp_mode = m;
    app.save_soon();
    let lp = &mut app.launchpad;
    lp.mode_k = 0.0;
    lp.selected = 0;
    lp.page = 0;
    lp.page_pos = 0.0;
}

fn act(f: impl Fn(&mut App) + 'static) -> Option<Action> {
    Some(Rc::new(f))
}

fn context(app: &mut App, i: usize) {
    let list = current_list(app);
    let Some(t) = list.get(i).cloned() else { return };
    let mut m = vec![MenuItem::action("Открыть", act(move |a| launch(a, i)))];
    match &t {
        Tile::Folder(name, _) => {
            let n1 = name.clone();
            let n2 = name.clone();
            m.push(MenuItem::action("Переименовать", act(move |a| {
                if let Some(fi) = a.launchpad.top.iter().position(|x| x.name() == n1) {
                    open_folder(a, fi, true);
                }
            })));
            m.push(MenuItem::action("Разгруппировать", act(move |a| {
                let folders: Vec<(String, Vec<String>)> = folder_cfg(a).into_iter().filter(|f| f.0 != n2).collect();
                save_folders(a, folders);
            })));
        }
        Tile::App(e) => {
            let e = e.clone();
            if app.launchpad.folder.is_some() {
                let e2 = e.clone();
                m.push(MenuItem::action("Убрать из папки", act(move |a| take_out(a, &e2))));
            } else if !app.launchpad.searching() && app.launchpad.mode(&app.cfg, !app.catalog.groups.is_empty()) == "all" {
                let mut sub = Vec::new();
                for f in app.launchpad.top.iter().filter(|t| matches!(t, Tile::Folder(..))) {
                    let (f2, e2) = (f.clone(), e.clone());
                    sub.push(MenuItem::action(f.name(), act(move |a| put_in_folder(a, &e2, &f2))));
                }
                if !sub.is_empty() {
                    sub.push(MenuItem::sep());
                }
                let e2 = e.clone();
                sub.push(MenuItem::action("Новая папка", act(move |a| {
                    let mut folders = folder_cfg(a);
                    for f in &mut folders {
                        f.1.retain(|k| !k.eq_ignore_ascii_case(&e2.parse));
                    }
                    let name = unique_name("Новая папка", &folders);
                    folders.insert(0, (name.clone(), vec![e2.parse.clone()]));
                    save_folders(a, folders);
                    if let Some(fi) = a.launchpad.top.iter().position(|t| matches!(t, Tile::Folder(m, _) if *m == name)) {
                        open_folder(a, fi, true);
                    }
                })));
                m.push(MenuItem::submenu("Добавить в папку", sub));
            }
            if std::path::Path::new(&e.parse).is_file() {
                let (p1, p2) = (e.parse.clone(), e.parse.clone());
                m.push(MenuItem::action("Добавить в док", act(move |a| {
                    if a.docks[0].pin_path(&p1) {
                        crate::dock::save_pinned(a, 0);
                        a.poll_now();
                    }
                })));
                m.push(MenuItem::action("Показать в проводнике", act(move |_| crate::win::shell::show_in_explorer(&p2))));
            }
            m.push(MenuItem::sep());
            let e3 = e.clone();
            m.push(MenuItem::action("Убрать из Launchpad", act(move |a| {
                if !a.cfg.lp_hidden.contains(&e3.parse) {
                    a.cfg.lp_hidden.push(e3.parse.clone());
                }
                a.save_soon();
                build_top(a);
            })));
            m.push(MenuItem::action("Удалить с компьютера…", act(|_| {
                crate::win::shell::launch("ms-settings:appsfeatures", "", None);
            })));
        }
    }
    m.push(MenuItem::sep());
    m.push(MenuItem::action("Обновить список приложений", act(|a| a.catalog.load())));
    let (cx, cy) = win::cursor_pos();
    app.show_menu(m, cx, cy, Owner::Other);
}

fn background_menu(app: &mut App) {
    let n = app.cfg.lp_hidden.len();
    let mut back = MenuItem::action(&format!("Вернуть убранные приложения ({n})"), act(|a| {
        a.cfg.lp_hidden.clear();
        a.save_soon();
        build_top(a);
    }));
    back.enabled = n > 0;
    let items = vec![back, MenuItem::action("Обновить список приложений", act(|a| a.catalog.load()))];
    let (cx, cy) = win::cursor_pos();
    app.show_menu(items, cx, cy, Owner::Other);
}

fn key(app: &mut App, vk: u32, ch: Option<char>) {
    let cfg = app.cfg.clone();
    if app.launchpad.folder_title.focused {
        let r = match ch {
            Some(c) => app.launchpad.folder_title.on_char(c),
            None => app.launchpad.folder_title.on_key(vk),
        };
        if matches!(r, Edit::Enter | Edit::Escape) {
            rename_folder(app);
        }
        return;
    }
    if let Some(c) = ch {
        match app.launchpad.search.on_char(c) {
            Edit::Changed => on_search(app, false),
            Edit::Enter => {
                let i = app.launchpad.selected;
                if !current_list(app).is_empty() {
                    launch(app, i);
                }
            }
            Edit::Escape => escape(app),
            Edit::Tab => {
                if !app.launchpad.searching() && app.launchpad.folder.is_none() {
                    let m = if cfg.lp_mode == "all" { "start" } else { "all" };
                    set_mode(app, m);
                }
            }
            _ => {}
        }
        return;
    }
    let n = current_list(app).len();
    let cols = if let Some(Tile::Folder(_, k)) = app.launchpad.folder.and_then(|i| app.launchpad.top.get(i)) { app.launchpad.folder_cols(&cfg, k.len()) } else { cfg.lp_columns as usize };
    let mv: Option<i32> = match vk {
        0x25 => Some(-1),
        0x27 => Some(1),
        0x26 => Some(-(cols as i32)),
        0x28 => Some(cols as i32),
        _ => None,
    };
    if let Some(d) = mv {
        if n > 0 {
            let lp = &mut app.launchpad;
            lp.selected = (lp.selected as i32 + d).clamp(0, n as i32 - 1) as usize;
            lp.kb_nav = true;
            if lp.folder.is_none() && lp.mode(&cfg, !app.catalog.groups.is_empty()) == "all" {
                let p = (lp.selected / Launchpad::per_page(&cfg)) as i32;
                go_page(app, p);
            }
        }
        return;
    }
    match vk {
        0x21 => go_page(app, app.launchpad.page as i32 - 1),
        0x22 => go_page(app, app.launchpad.page as i32 + 1),
        _ => {
            if let Edit::Changed = app.launchpad.search.on_key(vk) {
                on_search(app, false);
            }
        }
    }
}

fn escape(app: &mut App) {
    if app.launchpad.folder.is_some() {
        close_folder(app);
    } else if !app.launchpad.search.text.is_empty() {
        app.launchpad.search.set("");
        on_search(app, false);
    } else {
        close(app, None);
    }
}

/// Нажатие из хука клавиатуры (пока у Launchpad нет фокуса) — как будто набрано в поиске.
pub fn feed_key(app: &mut App, vk: u32, ch: Option<char>) {
    if !app.launchpad.shown || app.launchpad.closing.is_some() {
        win::hooks::LAUNCHPAD_KEYS.store(false, std::sync::atomic::Ordering::Relaxed);
        return;
    }
    let ch = ch.or(match vk {
        0x08 => Some('\u{8}'),
        0x0D => Some('\r'),
        0x1B => Some('\u{1b}'),
        0x09 => Some('\t'),
        _ => None,
    });
    key(app, vk, ch);
    app.launchpad.dirty = true;
    sys::force_foreground(app.launchpad.hwnd);
    app.request_frame();
}

pub fn on_message(app: &mut App, hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> Option<LRESULT> {
    if app.launchpad.hwnd != hwnd || hwnd.is_invalid() {
        return None;
    }
    let cfg = app.cfg.clone();
    let (x, y) = win::lparam_xy(lp);
    let (fx, fy) = (x as f32, y as f32);
    let s = app.launchpad.scale;
    match msg {
        WM_CHAR => key(app, 0, char::from_u32(wp.0 as u32)),
        WM_KEYDOWN => {
            if !matches!(wp.0, 0x08 | 0x0D | 0x1B | 0x09) {
                key(app, wp.0 as u32, None);
            }
        }
        WM_MOUSEMOVE => {
            let can_drag = app.launchpad.mode(&cfg, !app.catalog.groups.is_empty()) == "all" && !app.launchpad.searching();
            if let Some(((px, py), Some(idx))) = app.launchpad.press {
                if sys::key_down(0x01) && can_drag {
                    if app.launchpad.drag.is_none() && (fx - px).abs() + (fy - py).abs() > 8.0 * s {
                        app.launchpad.drag = Some(idx);
                    }
                    if app.launchpad.drag.is_some() {
                        let mut drop_on = None;
                        let dragged_is_folder = matches!(current_list(app).get(idx), Some(Tile::Folder(..)));
                        if app.launchpad.folder.is_none() && !dragged_is_folder {
                            for (i, _, r) in grid_items(app) {
                                if i != idx && r.adjusted(-r.w * 0.15, -r.h * 0.15, r.w * 0.15, r.h * 0.15).contains(fx, fy) {
                                    drop_on = Some(i);
                                }
                            }
                        }
                        app.launchpad.drop_on = drop_on;
                        app.request_frame();
                        return Some(LRESULT(0));
                    }
                }
            }
            let i = index_at(app, fx, fy);
            if i == app.launchpad.hover {
                return Some(LRESULT(0)); // ничего не изменилось — не перерисовываем весь экран
            }
            app.launchpad.hover = i;
            app.launchpad.kb_nav = false;
        }
        WM_LBUTTONDOWN | WM_RBUTTONDOWN => {
            let i = index_at(app, fx, fy);
            app.launchpad.press = Some(((fx, fy), i));
            if msg == WM_LBUTTONDOWN {
                unsafe {
                    windows::Win32::UI::Input::KeyboardAndMouse::SetCapture(hwnd);
                }
                if let Some(Tile::Folder(_, k)) = app.launchpad.folder.and_then(|i| app.launchpad.top.get(i)).cloned() {
                    let p = app.launchpad.folder_panel(&cfg, k.len());
                    let tr = R::new(p.cx() - 180.0 * s, p.y + 18.0 * s, 360.0 * s, 40.0 * s);
                    if tr.contains(fx, fy) {
                        app.launchpad.folder_title.focused = true;
                        app.launchpad.search.focused = false;
                        app.launchpad.folder_title.click(fx, tr, Font::Semibold, 21.3 * s, true);
                    } else if app.launchpad.folder_title.focused {
                        rename_folder(app);
                    }
                }
            }
        }
        WM_LBUTTONUP | WM_RBUTTONUP => {
            unsafe {
                let _ = windows::Win32::UI::Input::KeyboardAndMouse::ReleaseCapture();
            }
            let Some(((px, py), idx)) = app.launchpad.press.take() else { return Some(LRESULT(0)) };
            let right = msg == WM_RBUTTONUP;
            if let Some(src) = app.launchpad.drag.take() {
                let target = app.launchpad.drop_on.take();
                let list = current_list(app);
                if let Some(Tile::App(e)) = list.get(src).cloned() {
                    if let Some(Tile::Folder(_, k)) = app.launchpad.folder.and_then(|i| app.launchpad.top.get(i)).cloned() {
                        if !app.launchpad.folder_panel(&cfg, k.len()).contains(fx, fy) {
                            take_out(app, &e); // вытащили за край панели — из папки
                        }
                    } else if let Some(t) = target.and_then(|t| list.get(t).cloned()) {
                        put_in_folder(app, &e, &t);
                    }
                }
                app.request_frame();
                return Some(LRESULT(0));
            }
            if right && idx.is_none() && app.launchpad.folder.is_none() {
                background_menu(app);
                return Some(LRESULT(0));
            }
            if let Some(Tile::Folder(_, k)) = app.launchpad.folder.and_then(|i| app.launchpad.top.get(i)).cloned() {
                if right {
                    if let Some(i) = idx {
                        context(app, i);
                    }
                } else if let Some(i) = idx.filter(|i| index_at(app, fx, fy) == Some(*i)) {
                    launch(app, i);
                } else if idx.is_none() && !app.launchpad.folder_panel(&cfg, k.len()).contains(fx, fy) {
                    close_folder(app);
                }
                app.request_frame();
                return Some(LRESULT(0));
            }
            if !app.launchpad.searching() {
                for (r, m) in app.launchpad.seg_rects().iter().zip(["all", "start"]) {
                    if r.contains(fx, fy) && r.contains(px, py) {
                        set_mode(app, m);
                        app.request_frame();
                        return Some(LRESULT(0));
                    }
                }
            }
            if app.launchpad.search_rect().contains(fx, fy) {
                let r = app.launchpad.search_rect().inset(14.0 * s);
                app.launchpad.search.click(fx, r, Font::Regular, 14.7 * s, true);
                return Some(LRESULT(0));
            }
            if right {
                if let Some(i) = idx {
                    context(app, i);
                }
            } else if let Some(i) = idx.filter(|i| index_at(app, fx, fy) == Some(*i)) {
                launch(app, i);
            } else if idx.is_none() {
                close(app, None);
            }
        }
        WM_MOUSEWHEEL | WM_MOUSEHWHEEL => {
            let d = ((wp.0 >> 16) as i16) as i32;
            if d != 0 && app.launchpad.mode(&cfg, !app.catalog.groups.is_empty()) == "all" {
                let delta = if (d < 0) == (msg == WM_MOUSEWHEEL) { 1 } else { -1 };
                go_page(app, app.launchpad.page as i32 + delta);
            }
        }
        WM_ACTIVATE => {
            // клик в другое окно (или Alt+Tab) закрывает Launchpad, как на Mac — но только если он уже был активным
            if wp.0 & 0xFFFF != 0 {
                app.launchpad.was_active = true;
            } else {
                let other = HWND(lp.0 as *mut _);
                if app.launchpad.was_active && crate::anim::now() - app.launchpad.opened > 0.5 && app.menus.owns(other).is_none() && !app.menus.is_open() {
                    close(app, None);
                }
            }
        }
        _ => return None,
    }
    app.launchpad.dirty = true;
    app.request_frame();
    Some(LRESULT(0))
}

//! Свой Alt+Tab, два стиля:
//!   • «как в macOS» — значки программ (по последнему использованию), над ними живые миниатюры окон выбранной;
//!     ↓ или ` — выбирать окно, ← → — какое, ↑ — назад, Q — закрыть;
//!   • «как в Windows» — крупные живые миниатюры всех окон со значком программы и заголовком.

use std::collections::HashMap;
use std::rc::Rc;

use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::app::App;
use crate::config::Config;
use crate::gfx::*;
use crate::glass::Glass;
use crate::preview::{close_button, close_rect, row_layout, Thumbs};
use crate::text::{self, Align, Font};
use crate::win::{self, sys, Surface};

const THUMB_RADIUS: f32 = 8.0;
const PREVIEW_GAP: f32 = 18.0;
const WGAP: f32 = 22.0;
const WLABEL: f32 = 40.0;
const WICON: f32 = 34.0;

struct Entry {
    exe: String,
    hwnds: Vec<isize>,
    name: String,
    icon: Option<Rc<Image>>,
    row: (Vec<f32>, f32),
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Style {
    Mac,
    Win,
}

pub struct Switcher {
    hwnd: HWND,
    surf: Surface,
    glass: Option<Glass>,
    thumbs: Option<Thumbs>,
    pub active: bool,
    shown: bool,
    show_at: f64,
    style: Style,
    apps: Vec<Entry>,
    index: usize,
    win_index: usize,
    win_mode: bool,
    hover: Option<usize>,
    hover_win: Option<usize>,
    icon_hover: Option<usize>,
    close_hot: Option<(bool, usize)>,
    hl: Option<(f32, f32)>,
    wins: Vec<(isize, usize)>,
    rows: Vec<(Vec<usize>, Vec<f32>, f32)>,
    x: i32,
    y: i32,
    w: i32,
    h: i32,
    scale: f32,
    opened: f64,
    last: f64,
    icons: HashMap<String, Option<Rc<Image>>>,
    tracking: bool,
}

impl Switcher {
    pub fn new() -> Switcher {
        Switcher {
            hwnd: HWND::default(),
            surf: Surface::default(),
            glass: None,
            thumbs: None,
            active: false,
            shown: false,
            show_at: 0.0,
            style: Style::Mac,
            apps: Vec::new(),
            index: 0,
            win_index: 0,
            win_mode: false,
            hover: None,
            hover_win: None,
            icon_hover: None,
            close_hot: None,
            hl: None,
            wins: Vec::new(),
            rows: Vec::new(),
            x: 0,
            y: 0,
            w: 0,
            h: 0,
            scale: 1.0,
            opened: 0.0,
            last: 0.0,
            icons: HashMap::new(),
            tracking: false,
        }
    }

    pub fn visible(&self) -> bool {
        self.shown
    }

    // -- геометрия «как в macOS»
    fn cols(&self, cfg: &Config) -> usize {
        let n = self.apps.len().max(1);
        let per = cfg.switcher_per_row as usize;
        if per > 0 {
            n.min(per)
        } else {
            n
        }
    }

    fn grid_rows(&self, cfg: &Config) -> usize {
        self.apps.len().max(1).div_ceil(self.cols(cfg))
    }

    fn cell(&self, cfg: &Config) -> (f32, f32, f32) {
        let s = self.scale;
        let n = self.cols(cfg) as f32;
        let scr = sys::primary_monitor();
        let want = cfg.switcher_icon as f32 * s;
        let gap = want * 0.15;
        let size = want.min((scr.w() as f32 - 120.0 * s) / n - gap).max(32.0 * s);
        (size, size * 0.15, (size * 0.2).max(14.0 * s))
    }

    fn preview_height(&self, cfg: &Config) -> f32 {
        cfg.switcher_preview as f32 * 0.5625 * self.scale
    }

    fn previews_height(&self, cfg: &Config) -> f32 {
        if cfg.switcher_previews {
            self.preview_height(cfg) + (6.0 + 18.0 + 12.0) * self.scale
        } else {
            0.0
        }
    }

    fn text_height(&self, cfg: &Config) -> f32 {
        let mut h = 0.0;
        if cfg.switcher_titles {
            h += 28.0;
            if cfg.switcher_window_title && !cfg.switcher_previews {
                h += 16.0;
            }
        }
        h * self.scale
    }

    fn icons_width(&self, cfg: &Config, n: usize) -> f32 {
        let (size, gap, _) = self.cell(cfg);
        n as f32 * size + (n.max(1) - 1) as f32 * gap
    }

    fn icons_height(&self, cfg: &Config) -> f32 {
        let (size, gap, _) = self.cell(cfg);
        let rows = self.grid_rows(cfg) as f32;
        rows * size + (rows - 1.0) * gap * 1.6
    }

    fn icons_top(&self, cfg: &Config) -> f32 {
        self.cell(cfg).2 + self.previews_height(cfg)
    }

    fn icon_rect(&self, cfg: &Config, i: usize) -> R {
        let (size, gap, _) = self.cell(cfg);
        let cols = self.cols(cfg);
        let (row, col) = (i / cols, i % cols);
        let in_row = cols.min(self.apps.len() - row * cols);
        let x0 = (self.w as f32 - self.icons_width(cfg, in_row)) / 2.0;
        R::new(x0 + col as f32 * (size + gap), self.icons_top(cfg) + row as f32 * (size + gap * 1.6), size, size)
    }

    fn shown_app(&self) -> usize {
        match self.hover {
            Some(h) if h < self.apps.len() => h,
            _ => self.index,
        }
    }

    fn mac_tiles(&self, cfg: &Config) -> Vec<(isize, R)> {
        if !cfg.switcher_previews || self.apps.is_empty() {
            return Vec::new();
        }
        let pad = self.cell(cfg).2;
        let a = &self.apps[self.shown_app()];
        let (widths, h) = &a.row;
        let g = PREVIEW_GAP * self.scale;
        let total: f32 = widths.iter().sum::<f32>() + g * (widths.len().max(1) - 1) as f32;
        let mut x = (self.w as f32 - total) / 2.0;
        let y = pad + (self.preview_height(cfg) - h) / 2.0;
        let mut out = Vec::new();
        for (hw, w) in a.hwnds.iter().zip(widths) {
            out.push((*hw, R::new(x.round(), y.round(), w.round(), h.round())));
            x += w + g;
        }
        out
    }

    // -- геометрия «как в Windows»
    fn tile_height(&self, cfg: &Config) -> f32 {
        cfg.switcher_preview as f32 * 0.5625 * 1.15 * self.scale
    }

    fn win_tiles(&self) -> Vec<(isize, R)> {
        let s = self.scale;
        let pad = self.pad_w();
        let mut out = Vec::new();
        let mut y = pad;
        for (idx, widths, rh) in &self.rows {
            let total: f32 = widths.iter().sum::<f32>() + WGAP * s * (widths.len().max(1) - 1) as f32;
            let mut x = (self.w as f32 - total) / 2.0;
            for (i, w) in idx.iter().zip(widths) {
                out.push((self.wins[*i].0, R::new(x.round(), y.round(), w.round(), rh.round())));
                x += w + WGAP * s;
            }
            y += rh + WLABEL * s + WGAP * s * 0.5;
        }
        out
    }

    fn pad_w(&self) -> f32 {
        16.0 * self.scale
    }

    fn tiles(&self, cfg: &Config) -> Vec<(isize, R)> {
        match self.style {
            Style::Mac => self.mac_tiles(cfg),
            Style::Win => self.win_tiles(),
        }
    }

    fn place(&mut self, cfg: &Config) {
        let scr = sys::primary_monitor();
        self.scale = scr.scale;
        let s = self.scale;
        let (w, h) = match self.style {
            Style::Mac => {
                let (_, _, pad) = self.cell(cfg);
                let mut w = self.icons_width(cfg, self.cols(cfg));
                if cfg.switcher_previews {
                    let max_w = scr.w() as f32 - 160.0 * s;
                    let ph = self.preview_height(cfg);
                    for a in &mut self.apps {
                        a.row = row_layout(&a.hwnds, ph, PREVIEW_GAP * s, max_w);
                        let ws = &a.row.0;
                        w = w.max(ws.iter().sum::<f32>() + PREVIEW_GAP * s * (ws.len().max(1) - 1) as f32);
                    }
                }
                (w + pad * 2.0, self.icons_height(cfg) + pad * 2.0 + self.text_height(cfg) + self.previews_height(cfg))
            }
            Style::Win => {
                let max_w = scr.w() as f32 - 200.0 * s;
                let hwnds: Vec<isize> = self.wins.iter().map(|w| w.0).collect();
                let th = self.tile_height(cfg);
                let limit = cfg.switcher_per_row as usize;
                let first = if limit > 0 { hwnds.len().div_ceil(limit) } else { 1 }.max(1);
                let mut layout = Vec::new();
                for rows in first..=first.max(4) {
                    let per = if limit > 0 { hwnds.len().div_ceil(rows).min(limit) } else { hwnds.len().div_ceil(rows) }.max(1);
                    layout = (0..hwnds.len())
                        .step_by(per)
                        .map(|i| {
                            let idx: Vec<usize> = (i..(i + per).min(hwnds.len())).collect();
                            let hs: Vec<isize> = idx.iter().map(|k| hwnds[*k]).collect();
                            let (ws, rh) = row_layout(&hs, th, WGAP * s, max_w);
                            (idx, ws, rh)
                        })
                        .collect::<Vec<_>>();
                    if layout.iter().all(|(_, _, rh)| *rh >= th - 0.5) {
                        break;
                    }
                }
                self.rows = layout;
                let pad = self.pad_w();
                let w = self.rows.iter().map(|(_, ws, _)| ws.iter().sum::<f32>() + WGAP * s * (ws.len().max(1) - 1) as f32).fold(0.0, f32::max) + pad * 2.0;
                let h = pad * 2.0 + self.rows.iter().map(|(_, _, rh)| rh + WLABEL * s).sum::<f32>() + WGAP * s * 0.5 * (self.rows.len().max(1) - 1) as f32;
                (w, h)
            }
        };
        let cy = (scr.rect.top + scr.rect.bottom) as f32 / 2.0;
        let y = if cfg.switcher_position == "center" { cy - h / 2.0 } else { scr.rect.top as f32 + scr.h() as f32 * 0.22 };
        self.w = w.ceil() as i32;
        self.h = h.ceil() as i32;
        self.x = (scr.rect.left + scr.rect.right) / 2 - self.w / 2;
        self.y = y as i32;
    }

    fn paint(&mut self, cfg: &Config, bg_img: &Option<(Rc<Image>, f32)>) {
        if !self.surf.ensure(self.w, self.h) {
            return;
        }
        let s = self.scale;
        let k = ease_out(((crate::anim::now() - self.opened) / 0.17).min(1.0) as f32);
        let full = R::new(0.0, 0.0, self.w as f32, self.h as f32);
        let radius = cfg.switcher_radius as f32 * s;
        let tiles = self.tiles(cfg);
        let style = self.style;
        // всё, что нужно для рисования, — заранее (холст занимает буфер окна)
        let mac = (style == Style::Mac).then(|| {
            let (size, _, _) = self.cell(cfg);
            let icons: Vec<(R, Option<Rc<Image>>)> = (0..self.apps.len()).map(|i| (self.icon_rect(cfg, i), self.apps[i].icon.clone())).collect();
            let hl = self.hl.unwrap_or_else(|| {
                let r = self.icon_rect(cfg, self.index);
                (r.x, r.y)
            });
            let hover_r = self.hover.filter(|h| *h != self.index && *h < self.apps.len()).map(|h| self.icon_rect(cfg, h));
            let app = &self.apps[self.shown_app()];
            let name = app.name.clone();
            let first_title = app.hwnds.first().map(|h| sys::window_text(win::hwnd_of(*h))).unwrap_or_default();
            let titles: Vec<String> = app.hwnds.iter().map(|h| sys::window_text(win::hwnd_of(*h))).collect();
            let text_y = self.icons_top(cfg) + self.icons_height(cfg) + 10.0 * s;
            let app_close = self.icon_hover.filter(|i| *i < self.apps.len()).map(|i| {
                let r = self.icon_rect(cfg, i);
                let side = (18.0 * s).max(r.w * 0.22);
                (R::new(r.x - side * 0.25, r.y - side * 0.25, side, side), self.close_hot == Some((true, i)))
            });
            (size, icons, hl, hover_r, name, first_title, titles, text_y, app_close, self.shown_app() == self.index)
        });
        let win_labels: Vec<String> = if style == Style::Win { self.wins.iter().map(|(h, _)| sys::window_text(win::hwnd_of(*h))).collect() } else { Vec::new() };
        let (index, win_index, win_mode, hover_win) = (self.index, self.win_index, self.win_mode, self.hover_win);
        {
            let mut cv = self.surf.canvas();
            cv.clear();
            if let Some(path) = rounded_rect(full.inset(0.5), radius) {
                cv.fill_path(&path, rgba(30, 30, 36, cfg.switcher_opacity.min(255) as u8));
                if let Some((img, op)) = bg_img {
                    crate::dock::draw_cover(&mut cv, img, full, &path, *op);
                }
                cv.stroke_path(&path, rgba(255, 255, 255, 45), 1.0);
            }
            match mac {
                Some((size, icons, hl, hover_r, name, first_title, titles, text_y, app_close, own)) => {
                    let m = size * 0.08;
                    cv.fill_rrect(R::new(hl.0 - m, hl.1 - m, size + 2.0 * m, size + 2.0 * m), size * 0.16, rgba(255, 255, 255, if win_mode { 30 } else { 48 }));
                    if let Some(hr) = hover_r {
                        cv.fill_rrect(hr.adjusted(-m, -m, m, m), size * 0.16, rgba(255, 255, 255, 20));
                    }
                    for (r, icon) in &icons {
                        if let Some(img) = icon {
                            let f = (img.w.max(img.h) as f32).max(1.0);
                            cv.image(img, R::centered(r.cx(), r.cy(), img.w as f32 * r.w / f, img.h as f32 * r.h / f));
                        }
                    }
                    if let Some((r, hot)) = app_close {
                        close_button(&mut cv, r, hot, s);
                    }
                    if cfg.switcher_titles {
                        let fr = R::new(8.0 * s, text_y, full.w - 16.0 * s, 20.0 * s);
                        text::draw_in(&mut cv, &name, Font::Semibold, 13.3 * s, fr, Align::Center, rgba(245, 245, 248, 255));
                        if cfg.switcher_window_title && !cfg.switcher_previews && !first_title.is_empty() {
                            text::draw_in(&mut cv, &first_title, Font::Regular, 11.0 * s, fr.translated(0.0, 20.0 * s), Align::Center, rgba(255, 255, 255, 140));
                        }
                    }
                    for (i, (_, t)) in tiles.iter().enumerate() {
                        let chosen = own && i == win_index;
                        let box_ = t.adjusted(-6.0 * s, -6.0 * s, 6.0 * s, 6.0 * s);
                        if hover_win == Some(i) && !(chosen && win_mode) {
                            cv.fill_rrect(box_, (THUMB_RADIUS + 5.0) * s, rgba(255, 255, 255, 30));
                        }
                        if chosen && win_mode {
                            cv.fill_rrect(box_, (THUMB_RADIUS + 5.0) * s, rgba(10, 132, 255, 70));
                            cv.stroke_rrect(box_, (THUMB_RADIUS + 5.0) * s, rgba(10, 132, 255, 255), 2.0);
                        }
                        let title = titles.get(i).cloned().unwrap_or_default();
                        let tr = R::new(t.x - 6.0 * s, t.bottom() + 6.0 * s, t.w + 12.0 * s, 18.0 * s);
                        text::draw_in(&mut cv, &title, Font::Regular, 11.0 * s, tr, Align::Center, rgba(255, 255, 255, if chosen { 235 } else { 150 }));
                    }
                }
                None => {
                    for (i, (_, t)) in tiles.iter().enumerate() {
                        let sel = i == index;
                        if sel || hover_win == Some(i) {
                            let b = t.adjusted(-7.0 * s, -7.0 * s, 7.0 * s, (WLABEL - 4.0) * s);
                            cv.fill_rrect(b, (THUMB_RADIUS + 6.0) * s, if sel { rgba(10, 132, 255, 70) } else { rgba(255, 255, 255, 26) });
                            if sel {
                                cv.stroke_rrect(b, (THUMB_RADIUS + 6.0) * s, rgba(10, 132, 255, 255), 2.0);
                            }
                        }
                        let tr = R::new(t.x, t.bottom() + WICON / 2.0 * s + 2.0 * s, t.w, 18.0 * s);
                        text::draw_in(&mut cv, win_labels.get(i).map(|x| x.as_str()).unwrap_or(""), Font::Regular, 12.0 * s, tr, Align::Center, rgba(255, 255, 255, if sel { 240 } else { 160 }));
                    }
                }
            }
            // живые миниатюры — в окне под этим слоем: над ними слой должен быть пустым. В Python-версии
            // DWM рисует миниатюры поверх содержимого окна, и заливка выбранного окна видна только рамкой
            // вокруг миниатюры; здесь без «дырки» она красила саму миниатюру в синий
            for (_, t) in &tiles {
                let (x0, y0) = (t.x.round(), t.y.round());
                cv.clear_rect(R::new(x0, y0, t.right().round() - x0, t.bottom().round() - y0));
            }
        }
        let a = (255.0 * k) as u8;
        self.surf.present(self.hwnd, self.x, self.y, a);
        let glass_kind = crate::app::blur_kind(cfg);
        if let Some(g) = self.glass.as_mut() {
            g.place(self.x, self.y, self.x + self.w, self.y + self.h, radius as i32, self.hwnd, glass_kind, 0x01000000);
        }
        // живые миниатюры и слой скругления поверх
        let rect = RECT { left: self.x, top: self.y, right: self.x + self.w, bottom: self.y + self.h };
        let tr = THUMB_RADIUS * s;
        let icons_win: Vec<(R, Option<Rc<Image>>)> = if style == Style::Win {
            self.win_tiles().iter().zip(&self.wins).map(|((_, t), (_, ai))| (R::new(t.cx() - WICON / 2.0 * s, t.bottom() - WICON / 2.0 * s, WICON * s, WICON * s), self.apps.get(*ai).and_then(|a| a.icon.clone()))).collect()
        } else {
            Vec::new()
        };
        let close = match style {
            Style::Mac => hover_win.and_then(|i| tiles.get(i)).map(|t| (close_rect(t.1, s), self.close_hot == Some((false, hover_win.unwrap())))),
            Style::Win => hover_win.and_then(|i| tiles.get(i)).map(|t| (close_rect(t.1, s), self.close_hot == Some((false, hover_win.unwrap())))),
        };
        if let Some(th) = self.thumbs.as_mut() {
            if tiles.is_empty() {
                th.hide();
            } else {
                th.show(self.hwnd, rect, &tiles, a);
                let rects: Vec<R> = tiles.iter().map(|t| t.1).collect();
                th.mask(rect, &rects, tr, rgba(30, 30, 36, 235), a, |cv| {
                    for (r, icon) in &icons_win {
                        if let Some(img) = icon {
                            cv.image(img, *r);
                        }
                    }
                    if let Some((r, hot)) = close {
                        close_button(cv, r, hot, s);
                    }
                });
            }
        }
    }

    fn hide_all(&mut self) {
        self.active = false;
        self.shown = false;
        if let Some(t) = self.thumbs.as_mut() {
            t.hide();
        }
        if !self.hwnd.is_invalid() {
            win::hide(self.hwnd);
        }
        if let Some(g) = self.glass.as_mut() {
            g.hide();
        }
        self.surf.free();
    }

    /// Кадр: показ после задержки, подсветка едет к выбранной иконке, появление.
    pub fn frame(&mut self, cfg: &Config) -> bool {
        if !self.active {
            return false;
        }
        let now = crate::anim::now();
        if !self.shown {
            if now < self.show_at {
                return true;
            }
            self.place(cfg);
            self.shown = true;
            self.opened = now;
            if self.hwnd.is_invalid() {
                self.hwnd = win::create_layered("MiniDockSwitcher", true, false);
                win::dwm::exclude_from_peek(self.hwnd);
            }
            if self.glass.is_none() {
                self.glass = Some(Glass::new());
            }
            if self.thumbs.is_none() {
                self.thumbs = Some(Thumbs::new());
            }
            self.paint(cfg, &None);
            win::show_noactivate(self.hwnd);
            win::set_topmost(self.hwnd);
        }
        let dt = crate::anim::frame_dt(&mut self.last);
        let mut busy = now - self.opened < 0.2;
        if self.style == Style::Mac && !self.apps.is_empty() {
            let t = self.icon_rect(cfg, self.index);
            let (tx, ty) = (t.x, t.y);
            match self.hl {
                None => self.hl = Some((tx, ty)),
                Some((x, y)) => {
                    if (x - tx).abs() + (y - ty).abs() < 0.5 {
                        self.hl = Some((tx, ty));
                    } else {
                        let k = smooth(0.35, dt);
                        self.hl = Some((lerp(x, tx, k), lerp(y, ty, k)));
                        busy = true;
                    }
                }
            }
        }
        busy
    }
}

fn icon_for_exe(app: &mut App, exe: &str) -> Option<Rc<Image>> {
    for d in &app.docks {
        for s in d.all_apps() {
            if let Some(it) = d.get(s) {
                if it.owns_exe(exe) && it.icon.is_some() {
                    return it.icon.clone();
                }
            }
        }
    }
    let sw = &mut app.switcher;
    if let Some(i) = sw.icons.get(exe) {
        return i.clone();
    }
    let img = crate::win::shell::file_icon(exe, crate::dock::ICON_PX).map(Rc::new);
    if sw.icons.len() > 48 {
        sw.icons.clear();
    }
    sw.icons.insert(exe.to_string(), img.clone());
    img
}

/// Приложения в порядке последнего использования (свёрнутое окно остаётся на своём месте).
fn collect(app: &mut App) -> Vec<Entry> {
    let mru: HashMap<isize, usize> = app.mru.iter().enumerate().map(|(i, h)| (*h, i)).collect();
    let wins = sys::app_windows();
    let mut z: HashMap<isize, usize> = HashMap::new();
    for (_, hs) in &wins {
        for h in hs {
            let k = h.0 as isize;
            let n = z.len();
            z.insert(k, n);
        }
    }
    let rank = |h: isize| mru.get(&h).copied().unwrap_or(mru.len() + z.get(&h).copied().unwrap_or(0));
    let mut out: Vec<(usize, Entry)> = Vec::new();
    let minimized = app.cfg.switcher_minimized;
    for (exe, hs) in wins {
        let mut hs: Vec<isize> = hs.iter().map(|h| h.0 as isize).filter(|h| minimized || !sys::is_iconic(win::hwnd_of(*h))).collect();
        if hs.is_empty() {
            continue;
        }
        hs.sort_by_key(|h| rank(*h));
        hs.truncate(8);
        let icon = icon_for_exe(app, &exe);
        let name = app
            .docks
            .iter()
            .flat_map(|d| d.all_apps().into_iter().filter_map(move |s| d.get(s)))
            .find(|it| it.owns_exe(&exe))
            .map(|it| it.name.clone())
            .unwrap_or_else(|| crate::win::shell::exe_description(&exe).unwrap_or_else(|| sys::file_stem(&exe)));
        out.push((rank(hs[0]), Entry { exe, hwnds: hs, name, icon, row: (Vec::new(), 0.0) }));
    }
    out.sort_by_key(|e| e.0);
    out.into_iter().map(|e| e.1).collect()
}

/// Кадр из главного цикла: показ после задержки, анимация подсветки.
pub fn tick(app: &mut App) -> bool {
    let cfg = app.cfg.clone();
    let busy = app.switcher.frame(&cfg);
    if app.switcher.shown && busy {
        let bg = app.background("switcher");
        app.switcher.paint(&cfg, &bg);
    }
    busy
}

pub fn begin(app: &mut App, dir: i32) {
    let apps = collect(app);
    if apps.is_empty() {
        return;
    }
    app.close_preview();
    let sw = &mut app.switcher;
    sw.style = if app.cfg.switcher_style == "windows" { Style::Win } else { Style::Mac };
    sw.apps = apps;
    sw.wins.clear();
    if sw.style == Style::Win {
        let mru: HashMap<isize, usize> = app.mru.iter().enumerate().map(|(i, h)| (*h, i)).collect();
        let mut wins: Vec<(isize, usize)> = sw.apps.iter().enumerate().flat_map(|(ai, a)| a.hwnds.iter().map(move |h| (*h, ai))).collect();
        let order: HashMap<isize, usize> = wins.iter().enumerate().map(|(i, w)| (w.0, i)).collect();
        wins.sort_by_key(|w| mru.get(&w.0).copied().unwrap_or(mru.len() + order[&w.0]));
        sw.wins = wins;
    }
    let n = if sw.style == Style::Win { sw.wins.len() } else { sw.apps.len() };
    sw.index = if n > 1 { ((if dir > 0 { 1 } else { -1 }) as isize).rem_euclid(n as isize) as usize } else { 0 };
    sw.win_index = 0;
    sw.win_mode = false;
    sw.hover = None;
    sw.hover_win = None;
    sw.icon_hover = None;
    sw.close_hot = None;
    sw.hl = None;
    sw.active = true;
    sw.shown = false;
    // короткое Alt+Tab переключает без вспышки окна
    sw.show_at = crate::anim::now() + app.cfg.switcher_delay as f64 / 1000.0;
    sw.last = crate::anim::now();
    app.request_frame();
}

fn repaint(app: &mut App) {
    if app.switcher.shown {
        let cfg = app.cfg.clone();
        let bg = app.background("switcher");
        app.switcher.paint(&cfg, &bg);
    }
    app.request_frame();
}

pub fn step(app: &mut App, d: i32) {
    let sw = &mut app.switcher;
    if !sw.active {
        return;
    }
    let n = if sw.style == Style::Win { sw.wins.len() } else { sw.apps.len() };
    if n == 0 {
        return;
    }
    sw.index = (sw.index as i32 + d).rem_euclid(n as i32) as usize;
    sw.win_index = 0;
    sw.win_mode = false;
    sw.hover = None;
    sw.hover_win = None;
    repaint(app);
}

pub fn arrow(app: &mut App, vk: u32) {
    let cfg = app.cfg.clone();
    let sw = &mut app.switcher;
    if !sw.active {
        return;
    }
    if sw.style == Style::Win {
        if vk == 0x25 || vk == 0x27 {
            return step(app, if vk == 0x27 { 1 } else { -1 });
        }
        let pos = sw.rows.iter().enumerate().find_map(|(r, (idx, _, _))| idx.iter().position(|i| *i == sw.index).map(|c| (r, c)));
        if let Some((r, c)) = pos {
            let r2 = r as i32 + if vk == 0x28 { 1 } else { -1 };
            if r2 >= 0 && (r2 as usize) < sw.rows.len() {
                let idx = &sw.rows[r2 as usize].0;
                sw.index = idx[c.min(idx.len() - 1)];
            }
        }
        return repaint(app);
    }
    if sw.apps.is_empty() {
        return;
    }
    let n = sw.apps[sw.index].hwnds.len();
    if sw.hover.is_some_and(|h| h != sw.index) {
        sw.hover = None;
        sw.hover_win = None;
    }
    if vk == 0x28 && cfg.switcher_previews && n > 0 {
        sw.win_mode = true;
    } else if vk == 0x26 {
        sw.win_mode = false;
    } else if sw.win_mode {
        sw.win_index = (sw.win_index as i32 + if vk == 0x27 { 1 } else { -1 }).rem_euclid(n.max(1) as i32) as usize;
    } else {
        return step(app, if vk == 0x27 { 1 } else { -1 });
    }
    repaint(app);
}

pub fn next_window(app: &mut App) {
    let sw = &mut app.switcher;
    if !sw.active {
        return;
    }
    if sw.style == Style::Win {
        let n = sw.wins.len();
        let ai = sw.wins.get(sw.index).map(|w| w.1);
        for k in 1..n {
            let j = (sw.index + k) % n;
            if Some(sw.wins[j].1) == ai {
                sw.index = j;
                break;
            }
        }
        return repaint(app);
    }
    if sw.apps.is_empty() {
        return;
    }
    let n = sw.apps[sw.index].hwnds.len();
    if sw.hover.is_some_and(|h| h != sw.index) {
        sw.hover = None;
        sw.hover_win = None;
    }
    if n > 0 {
        sw.win_index = if sw.win_mode { (sw.win_index + 1) % n } else { 1 % n };
        sw.win_mode = true;
    }
    repaint(app);
}

pub fn cancel(app: &mut App) {
    app.switcher.hide_all();
    crate::text::trim_cache();
}

/// Переключиться на выбранное клавиатурой (или на hwnd — клик по миниатюре/иконке).
fn commit_to(app: &mut App, hwnd: Option<isize>) {
    let sw = &mut app.switcher;
    if !sw.active {
        return sw.hide_all();
    }
    let target = match hwnd {
        Some(h) => Some(h),
        None => match sw.style {
            Style::Win => sw.wins.get(sw.index).map(|w| w.0),
            Style::Mac => sw.apps.get(sw.index).and_then(|a| a.hwnds.get(sw.win_index.min(a.hwnds.len().saturating_sub(1))).copied()),
        },
    };
    sw.hide_all();
    let Some(h) = target else { return };
    let w = win::hwnd_of(h);
    if sys::is_iconic(w) && app.cfg.switcher_genie {
        // как при клике по доку: окно «джинном» вытекает из иконки программы в доке
        let di = app.dock_for_window(h);
        app.docks[di].reveal(0.8);
        app.restore_window(h);
    } else {
        sys::force_foreground(w);
    }
}

pub fn commit(app: &mut App) {
    commit_to(app, None);
}

pub fn quit_selected(app: &mut App) {
    let sw = &mut app.switcher;
    if !sw.active {
        return;
    }
    match sw.style {
        Style::Win => {
            if sw.wins.is_empty() {
                return;
            }
            let i = sw.index;
            let (h, _) = sw.wins.remove(i);
            sys::close_window(win::hwnd_of(h));
            if sw.wins.is_empty() {
                return sw.hide_all();
            }
            sw.index %= sw.wins.len();
            let cfg = app.cfg.clone();
            app.switcher.place(&cfg);
        }
        Style::Mac => {
            if sw.apps.is_empty() {
                return;
            }
            let idx = sw.index;
            if sw.win_mode && !sw.apps[idx].hwnds.is_empty() {
                let wi = sw.win_index.min(sw.apps[idx].hwnds.len() - 1);
                let h = sw.apps[idx].hwnds.remove(wi);
                sys::close_window(win::hwnd_of(h));
                if !sw.apps[idx].hwnds.is_empty() {
                    sw.win_index %= sw.apps[idx].hwnds.len();
                    let cfg = app.cfg.clone();
                    app.switcher.place(&cfg);
                    return repaint(app);
                }
            } else {
                for h in &sw.apps[idx].hwnds {
                    sys::close_window(win::hwnd_of(*h));
                }
            }
            sw.apps.remove(idx);
            if sw.apps.is_empty() {
                return sw.hide_all();
            }
            sw.index %= sw.apps.len();
            sw.win_index = 0;
            sw.win_mode = false;
            sw.hl = None;
            let cfg = app.cfg.clone();
            app.switcher.place(&cfg);
        }
    }
    repaint(app);
}

pub fn on_message(app: &mut App, hwnd: HWND, msg: u32, _wp: WPARAM, lp: LPARAM) -> Option<LRESULT> {
    if app.switcher.hwnd != hwnd || hwnd.is_invalid() {
        return None;
    }
    let cfg = app.cfg.clone();
    let (x, y) = win::lparam_xy(lp);
    let (fx, fy) = (x as f32, y as f32);
    match msg {
        WM_MOUSEACTIVATE => return Some(LRESULT(MA_NOACTIVATE as isize)),
        WM_MOUSEMOVE => {
            let sw = &mut app.switcher;
            if !sw.tracking {
                sw.tracking = true;
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
            let s = sw.scale;
            let tiles = sw.tiles(&cfg);
            let label = if sw.style == Style::Win { WLABEL * s } else { 24.0 * s };
            let w = tiles.iter().position(|(_, t)| t.adjusted(-6.0 * s, -6.0 * s, 6.0 * s, label).contains(fx, fy));
            let icon = if w.is_none() && sw.style == Style::Mac {
                let m = sw.cell(&cfg).0 * 0.08;
                (0..sw.apps.len()).find(|&i| sw.icon_rect(&cfg, i).adjusted(-m, -m, m, m).contains(fx, fy))
            } else {
                None
            };
            sw.hover_win = w;
            sw.icon_hover = icon;
            sw.close_hot = match (w, icon) {
                (Some(i), _) if close_rect(tiles[i].1, s).contains(fx, fy) => Some((false, i)),
                (_, Some(i)) => {
                    let r = sw.icon_rect(&cfg, i);
                    let side = (18.0 * s).max(r.w * 0.22);
                    R::new(r.x - side * 0.25, r.y - side * 0.25, side, side).contains(fx, fy).then_some((true, i))
                }
                _ => None,
            };
            // наведение только показывает окна приложения; выбор для отпускания Alt не меняется
            if w.is_none() && cfg.switcher_hover {
                if let Some(i) = icon {
                    if i != sw.shown_app() {
                        sw.hover = Some(i);
                    }
                }
            }
            repaint(app);
        }
        win::WM_MOUSELEAVE => {
            let sw = &mut app.switcher;
            sw.tracking = false;
            sw.hover_win = None;
            sw.icon_hover = None;
            sw.close_hot = None;
            repaint(app);
        }
        WM_LBUTTONUP => {
            let sw = &mut app.switcher;
            let tiles = sw.tiles(&cfg);
            if let Some((is_app, k)) = sw.close_hot {
                if is_app {
                    // закрыть всё приложение
                    if let Some(a) = sw.apps.get(k) {
                        for h in a.hwnds.clone() {
                            sys::close_window(win::hwnd_of(h));
                        }
                    }
                    sw.apps.remove(k);
                } else if let Some((h, _)) = tiles.get(k) {
                    sys::close_window(win::hwnd_of(*h));
                    for a in &mut sw.apps {
                        a.hwnds.retain(|x| x != h);
                    }
                    sw.apps.retain(|a| !a.hwnds.is_empty());
                    sw.wins.retain(|w| w.0 != *h);
                }
                if sw.apps.is_empty() {
                    sw.hide_all();
                    return Some(LRESULT(0));
                }
                sw.index = sw.index.min(if sw.style == Style::Win { sw.wins.len() } else { sw.apps.len() }.saturating_sub(1));
                sw.hover = None;
                sw.hover_win = None;
                sw.close_hot = None;
                app.switcher.place(&cfg);
                repaint(app);
                return Some(LRESULT(0));
            }
            let target = if let Some(i) = sw.hover_win {
                tiles.get(i).map(|t| t.0)
            } else if let Some(i) = sw.icon_hover {
                sw.apps.get(i).and_then(|a| a.hwnds.first().copied())
            } else {
                None
            };
            if let Some(h) = target {
                win::hooks::END_SWITCH.store(true, std::sync::atomic::Ordering::Relaxed);
                commit_to(app, Some(h));
            }
        }
        _ => return None,
    }
    Some(LRESULT(0))
}

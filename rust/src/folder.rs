//! Папки в доке: окно над иконкой папки — название (можно переименовать) и сетка программ.

use std::rc::Rc;

use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::app::{Action, App};
use crate::config::Config;
use crate::gfx::*;
use crate::glass::Glass;
use crate::lineedit::{Edit, LineEdit};
use crate::menu::{MenuItem, Owner};
use crate::text::{self, Align, Font};
use crate::win::{self, shell, sys, Surface};

const CELL_W: f32 = 92.0;
const CELL_H: f32 = 96.0;
const PAD: f32 = 18.0;
const TITLE_H: f32 = 40.0;

pub struct FolderPopup {
    hwnd: HWND,
    surf: Surface,
    glass: Option<Glass>,
    open: Option<(usize, u64)>,
    title: LineEdit,
    hover: Option<usize>,
    x: i32,
    y: i32,
    w: i32,
    h: i32,
    scale: f32,
    opened: f64,
    slide_down: bool,
}

impl FolderPopup {
    pub fn new() -> FolderPopup {
        FolderPopup { hwnd: HWND::default(), surf: Surface::default(), glass: None, open: None, title: LineEdit::default(), hover: None, x: 0, y: 0, w: 0, h: 0, scale: 1.0, opened: 0.0, slide_down: false }
    }

    pub fn is_open_for(&self, di: usize) -> bool {
        self.open.is_some_and(|(d, _)| d == di)
    }

    pub fn is_open_for_item(&self, di: usize, id: u64) -> bool {
        self.open == Some((di, id))
    }

    fn cols(n: usize) -> usize {
        ((n.max(1) as f32).sqrt().ceil() as usize).clamp(1, 5)
    }

    fn cell_rect(&self, n: usize, i: usize) -> R {
        let s = self.scale;
        let c = Self::cols(n);
        R::new((PAD + (i % c) as f32 * CELL_W) * s, (PAD + TITLE_H + (i / c) as f32 * CELL_H) * s, CELL_W * s, CELL_H * s)
    }

    fn title_rect(&self) -> R {
        let s = self.scale;
        R::new(PAD * s, (PAD - 4.0) * s, self.w as f32 - 2.0 * PAD * s, 30.0 * s)
    }

    pub fn frame(&mut self, _cfg: &Config) -> bool {
        self.open.is_some() && ((crate::anim::now() - self.opened) < 0.2 || self.title.focused)
    }
}

pub fn open(app: &mut App, di: usize, id: u64, edit: bool) {
    let cfg = app.cfg.clone();
    let Some(it) = app.docks[di].item(id) else { return };
    let n = it.children.len();
    let name = it.name.clone();
    let anchor = app.docks[di].icon_global(&cfg, id);
    let side = app.docks[di].side;
    let f = &mut app.folder;
    if f.hwnd.is_invalid() {
        f.hwnd = win::create_focusable("MiniDockFolder", true);
        win::dwm::exclude_from_peek(f.hwnd);
    }
    let mon = sys::point_monitor((anchor.left + anchor.right) / 2, (anchor.top + anchor.bottom) / 2);
    f.scale = mon.scale;
    let s = f.scale;
    let c = FolderPopup::cols(n);
    let rows = n.div_ceil(c).max(1);
    let w = (PAD * 2.0 + c as f32 * CELL_W).max(220.0) * s;
    let h = if n > 0 { PAD * 2.0 + TITLE_H + rows as f32 * CELL_H } else { PAD * 2.0 + TITLE_H + 60.0 } * s;
    let (acx, acy) = ((anchor.left + anchor.right) as f32 / 2.0, (anchor.top + anchor.bottom) as f32 / 2.0);
    let (mut x, mut y) = match side {
        crate::dock::Side::Bottom => (acx - w / 2.0, anchor.top as f32 - h - 14.0 * s),
        crate::dock::Side::Left => (anchor.right as f32 + 14.0 * s, acy - h / 2.0),
        crate::dock::Side::Right => (anchor.left as f32 - w - 14.0 * s, acy - h / 2.0),
    };
    let m = mon.rect;
    x = x.clamp(m.left as f32 + 6.0 * s, m.right as f32 - w - 6.0 * s);
    y = y.clamp(m.top as f32 + 6.0 * s, m.bottom as f32 - h - 6.0 * s);
    f.x = x as i32;
    f.y = y as i32;
    f.w = w as i32;
    f.h = h as i32;
    f.open = Some((di, id));
    f.title = LineEdit::new(&name);
    f.title.focused = edit;
    if edit {
        f.title.select_all();
    }
    f.hover = None;
    f.opened = crate::anim::now();
    f.slide_down = side == crate::dock::Side::Bottom;
    if f.glass.is_none() {
        f.glass = Some(Glass::new());
    }
    app.docks[di].menu_open = true; // док не прячется, пока папка открыта
    paint(app);
    let f = &app.folder;
    unsafe {
        let _ = SetWindowPos(f.hwnd, Some(HWND_TOPMOST), f.x, f.y, f.w, f.h, SWP_SHOWWINDOW);
    }
    sys::force_foreground(app.folder.hwnd);
    app.request_frame();
}

pub fn close(app: &mut App) {
    rename(app);
    let f = &mut app.folder;
    if let Some((di, _)) = f.open.take() {
        win::hide(f.hwnd);
        if let Some(g) = f.glass.as_mut() {
            g.hide();
        }
        f.surf.free();
        if let Some(d) = app.docks.get_mut(di) {
            d.menu_open = app.menus.is_open();
            d.wake();
        }
        app.request_frame();
    }
}

fn rename(app: &mut App) {
    let f = &mut app.folder;
    let Some((di, id)) = f.open else { return };
    let name = f.title.text.trim().to_string();
    f.title.focused = false;
    let cur = app.docks.get(di).and_then(|d| d.item(id)).map(|i| i.name.clone());
    let name = if name.is_empty() { "Папка".to_string() } else { name };
    if cur.as_deref() != Some(name.as_str()) {
        app.docks[di].rename_folder(id, &name);
        crate::dock::save_pinned(app, di);
    }
}

fn paint(app: &mut App) {
    let cfg = app.cfg.clone();
    let Some((di, id)) = app.folder.open else { return };
    let kids: Vec<(String, Option<Rc<Image>>, bool)> = {
        let d = &mut app.docks[di];
        let s = app.folder.scale;
        let Some(it) = d.item_mut(id) else { return };
        it.children.iter_mut().map(|c| (c.name.clone(), c.scaled(60.0 * s), !c.hwnds.is_empty())).collect()
    };
    let bg = app.background("folders");
    let f = &mut app.folder;
    if !f.surf.ensure(f.w, f.h) {
        return;
    }
    let s = f.scale;
    let k = ease_out(((crate::anim::now() - f.opened) / 0.17).min(1.0) as f32);
    let full = R::new(0.0, 0.0, f.w as f32, f.h as f32);
    let title_r = f.title_rect();
    let n = kids.len();
    let cells: Vec<R> = (0..n).map(|i| f.cell_rect(n, i)).collect();
    let hover = f.hover;
    let title = f.title.clone();
    let _ = &cfg;
    {
        let mut cv = f.surf.canvas();
        cv.clear();
        if let Some(path) = rounded_rect(full.inset(0.5), 16.0 * s) {
            cv.fill_path(&path, rgba(34, 34, 40, 165));
            if let Some((img, op)) = &bg {
                crate::dock::draw_cover(&mut cv, img, full, &path, *op);
            }
            cv.stroke_path(&path, rgba(255, 255, 255, 45), 1.0);
        }
        if title.focused {
            cv.fill_rrect(title_r, 6.0 * s, rgba(255, 255, 255, 30));
        }
        title.paint(&mut cv, title_r, Font::Semibold, 16.0 * s, rgba(242, 242, 245, 255), "", true);
        if kids.is_empty() {
            let r = R::new(0.0, (PAD + TITLE_H) * s, full.w, 30.0 * s);
            text::draw_in(&mut cv, "Перетащите сюда программы", Font::Regular, 12.0 * s, r, Align::Center, rgba(255, 255, 255, 130));
            text::draw_in(&mut cv, "из дока", Font::Regular, 12.0 * s, r.translated(0.0, 18.0 * s), Align::Center, rgba(255, 255, 255, 130));
        }
        for (i, (name, icon, running)) in kids.iter().enumerate() {
            let r = cells[i];
            if hover == Some(i) {
                cv.fill_rrect(r.adjusted(4.0 * s, 2.0 * s, -4.0 * s, -2.0 * s), 12.0 * s, rgba(255, 255, 255, 34));
            }
            let ir = R::new(r.cx() - 30.0 * s, r.y + 8.0 * s, 60.0 * s, 60.0 * s);
            if let Some(img) = icon {
                cv.image(img, R::centered(ir.cx(), ir.cy(), img.w as f32, img.h as f32));
            }
            if *running {
                cv.fill_circle(r.cx(), ir.bottom() + 4.0 * s, 1.8 * s, rgba(235, 235, 240, 230));
            }
            text::draw_in(&mut cv, name, Font::Regular, 12.0 * s, R::new(r.x + 4.0 * s, ir.bottom() + 8.0 * s, r.w - 8.0 * s, 18.0 * s), Align::Center, rgba(240, 240, 245, 255));
        }
    }
    let dy = if f.slide_down { (-10.0 * s * (1.0 - k)) as i32 } else { 0 };
    f.surf.present(f.hwnd, f.x, f.y + dy, (255.0 * k) as u8);
    if let Some(g) = f.glass.as_mut() {
        g.place(f.x, f.y + dy, f.x + f.w, f.y + dy + f.h, (16.0 * s) as i32, f.hwnd, crate::app::blur_kind(&cfg), 0x01000000);
    }
}

fn act(f: impl Fn(&mut App) + 'static) -> Option<Action> {
    Some(Rc::new(f))
}

pub fn on_message(app: &mut App, hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> Option<LRESULT> {
    if app.folder.hwnd != hwnd || hwnd.is_invalid() {
        return None;
    }
    let Some((di, id)) = app.folder.open else { return Some(LRESULT(0)) };
    let (x, y) = win::lparam_xy(lp);
    let (fx, fy) = (x as f32, y as f32);
    let n = app.docks[di].item(id).map(|i| i.children.len()).unwrap_or(0);
    let index_at = |f: &FolderPopup| (0..n).find(|&i| f.cell_rect(n, i).contains(fx, fy));
    match msg {
        WM_MOUSEMOVE => {
            let h = index_at(&app.folder);
            if h != app.folder.hover {
                app.folder.hover = h;
                paint(app);
            }
        }
        WM_LBUTTONDOWN => {
            let tr = app.folder.title_rect();
            if tr.contains(fx, fy) {
                let s = app.folder.scale;
                app.folder.title.focused = true;
                app.folder.title.click(fx, tr, Font::Semibold, 16.0 * s, true);
            } else if app.folder.title.focused {
                rename(app);
            }
            paint(app);
        }
        WM_LBUTTONUP => {
            if let Some(i) = index_at(&app.folder) {
                let child = app.docks[di].item(id).and_then(|f| f.children.get(i)).map(|c| c.id);
                close(app);
                if let Some(cid) = child {
                    crate::dock::activate(app, di, cid, false);
                }
            }
        }
        WM_RBUTTONUP => {
            if let Some(i) = index_at(&app.folder) {
                let Some(c) = app.docks[di].item(id).and_then(|f| f.children.get(i)) else { return Some(LRESULT(0)) };
                let (cid, path) = (c.id, c.path.clone());
                let items = vec![
                    MenuItem::action("Открыть новое окно", act(move |a| {
                        close(a);
                        crate::dock::activate(a, di, cid, true);
                    })),
                    MenuItem::action("Убрать из папки", act(move |a| {
                        a.docks[di].take_out_of_folder(cid);
                        crate::dock::save_pinned(a, di);
                        a.poll_now();
                        if a.folder.open.is_some() {
                            open(a, di, id, false);
                        }
                    })),
                    MenuItem::action("Показать в проводнике", act(move |_| shell::show_in_explorer(&path))),
                ];
                let (cx, cy) = win::cursor_pos();
                app.show_menu(items, cx, cy, Owner::Folder);
            }
        }
        WM_CHAR => {
            if app.folder.title.focused {
                match app.folder.title.on_char(char::from_u32(wp.0 as u32).unwrap_or('\0')) {
                    Edit::Enter => rename(app),
                    Edit::Escape => {
                        let cur = app.docks[di].item(id).map(|i| i.name.clone()).unwrap_or_default();
                        app.folder.title.set(&cur);
                        app.folder.title.focused = false;
                    }
                    _ => {}
                }
                paint(app);
            } else if wp.0 == 0x1B {
                close(app);
            }
        }
        WM_KEYDOWN => {
            if app.folder.title.focused {
                app.folder.title.on_key(wp.0 as u32);
                paint(app);
            } else if wp.0 == 0x1B {
                close(app);
            }
        }
        WM_ACTIVATE => {
            if wp.0 & 0xFFFF == 0 {
                let other = HWND(lp.0 as *mut _);
                if app.menus.owns(other).is_none() {
                    close(app);
                }
            }
        }
        _ => return None,
    }
    app.request_frame();
    Some(LRESULT(0))
}

/// Каждый кадр, пока открыто: анимация появления и мигание курсора в названии.
pub fn tick(app: &mut App) {
    if app.folder.open.is_some() {
        paint(app);
    }
}

pub fn _anchor(_: RECT) {}

//! Верхняя панель в стиле macOS: меню программы, часы, значки системы, виджеты.

use std::collections::HashMap;
use std::rc::Rc;

use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows::Win32::System::SystemInformation::GetLocalTime;
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::app::{Action, App};
use crate::config::Config;
use crate::flyouts::{Flyout, Kind};
use crate::gfx::*;
use crate::menu::{MenuItem, Owner};
use crate::text::{self, Align, Font};
use crate::win::{self, appbar, shell, sys, Surface};

const DAYS: [&str; 7] = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];
const MONTHS: [&str; 12] = ["янв.", "февр.", "мар.", "апр.", "мая", "июн.", "июл.", "авг.", "сент.", "окт.", "нояб.", "дек."];

#[derive(Clone)]
struct Hit {
    r: R,
    key: String,
}

pub struct MenuBar {
    pub hwnd: HWND,
    surf: Surface,
    pub home: Option<String>,
    pub reserved: bool,
    pub visible: bool,
    mon: sys::Monitor,
    pub target: HWND,
    app_name: String,
    prev_name: Option<String>,
    name_t: f32,
    app_exe: String,
    hits: Vec<Hit>,
    pub active: Option<String>,
    hover: Option<String>,
    glow: HashMap<String, f32>,
    slide: f32,
    last: f64,
    state: (String, String, Option<f32>, &'static str),
    widget_w: HashMap<String, (f32, f64)>,
    blur_on: bool,
    tracking: bool,
    native: Vec<String>,
    dirty: bool,
}

impl MenuBar {
    pub fn new(home: Option<String>) -> MenuBar {
        let hwnd = win::create_layered("MiniDock Menu", true, false);
        win::dwm::exclude_from_peek(hwnd);
        MenuBar {
            hwnd,
            surf: Surface::default(),
            home,
            reserved: false,
            visible: false,
            mon: sys::primary_monitor(),
            target: HWND::default(),
            app_name: "Проводник".into(),
            prev_name: None,
            name_t: 1.0,
            app_exe: String::new(),
            hits: Vec::new(),
            active: None,
            hover: None,
            glow: HashMap::new(),
            slide: 0.0,
            last: crate::anim::now(),
            state: (String::new(), String::new(), None, ""),
            widget_w: HashMap::new(),
            blur_on: false,
            tracking: false,
            native: Vec::new(),
            dirty: true,
        }
    }

    fn monitor(&self) -> sys::Monitor {
        match &self.home {
            None => sys::primary_monitor(),
            Some(n) => sys::monitors().into_iter().find(|m| &m.name == n).unwrap_or_else(sys::primary_monitor),
        }
    }

    fn h(&self, cfg: &Config) -> i32 {
        (cfg.bar_height * self.mon.scale).round() as i32
    }

    pub fn place(&mut self, cfg: &Config) {
        self.mon = self.monitor();
        self.reserve(cfg);
        if cfg.bar_blur != self.blur_on {
            self.blur_on = cfg.bar_blur;
            win::dwm::set_blur(self.hwnd, if cfg.bar_blur { crate::app::blur_kind(cfg) } else { win::dwm::Blur::None }, 0x01000000);
        }
        self.dirty = true;
    }

    pub fn reserve(&mut self, cfg: &Config) {
        let m = self.mon.rect;
        appbar::reserve(self.hwnd, RECT { left: m.left, top: m.top, right: m.right, bottom: m.top + self.h(cfg) }, appbar::EDGE_TOP);
        self.reserved = true;
    }

    pub fn release(&mut self) {
        if self.reserved {
            appbar::release(self.hwnd);
            self.reserved = false;
        }
    }

    pub fn show(&mut self) {
        if !self.visible {
            self.visible = true;
            self.slide = 0.0;
            self.dirty = true;
            win::show_noactivate(self.hwnd);
        }
    }

    pub fn hide(&mut self) {
        if self.visible {
            self.visible = false;
            win::hide(self.hwnd);
        }
    }

    pub fn destroy(&mut self) {
        unsafe {
            let _ = DestroyWindow(self.hwnd);
        }
    }

    pub fn misplaced(&self) -> bool {
        let r = win::window_rect(self.hwnd);
        let m = self.monitor().rect;
        (r.left - m.left).abs() > 3 || (r.top - m.top).abs() > 3 || (r.right - m.right).abs() > 3
    }

    pub fn menu_closed(&mut self) {
        self.active = None;
        self.dirty = true;
    }

    pub fn refresh_volume(&mut self) {
        self.dirty = true;
    }

    pub fn animating(&self) -> bool {
        self.visible && (self.dirty || self.slide < 1.0 || self.name_t < 1.0 || !self.glow.is_empty() || self.hover.is_some() || self.active.is_some())
    }

    pub fn on_foreground(&mut self, hwnd: HWND) {
        if hwnd.is_invalid() || sys::window_pid(hwnd) == sys::my_pid() {
            return;
        }
        let cls = sys::class_name(hwnd);
        let (target, name, exe) = if ["Progman", "WorkerW", "Shell_TrayWnd", ""].contains(&cls.as_str()) {
            (HWND::default(), "Проводник".to_string(), String::new())
        } else {
            let root = sys::root(hwnd);
            let h = if root.is_invalid() { hwnd } else { root };
            let exe = sys::window_exe(h);
            let name = (!exe.is_empty()).then(|| shell::exe_description(&exe)).flatten().unwrap_or_else(|| {
                let t: String = sys::window_text(h).chars().take(40).collect();
                if t.is_empty() {
                    "Окно".into()
                } else {
                    t
                }
            });
            (h, name, exe)
        };
        self.target = target;
        self.app_exe = exe;
        self.native = native_titles(target);
        if name != self.app_name {
            // название программы плавно сменяется
            self.prev_name = Some(std::mem::replace(&mut self.app_name, name));
            self.name_t = 0.0;
        }
        self.dirty = true;
    }

    pub fn on_second(&mut self, cfg: &Config) {
        let state = (clock_text(cfg), sys::keyboard_lang(), if cfg.show_battery { crate::sysinfo::battery().map(|b| b.0) } else { None }, crate::audio::glyph());
        if state != self.state {
            self.state = state;
            self.dirty = true;
        }
    }

    /// Кадр: анимации и отрисовка. true — ещё нужны кадры.
    pub fn frame(&mut self, cfg: &Config, widgets: &[(String, String)], bg: &Option<(Rc<Image>, f32)>) -> bool {
        let now = crate::anim::now();
        let dt = ((now - self.last) as f32).clamp(0.001, 0.05);
        self.last = now;
        let mut busy = false;
        if self.slide < 1.0 {
            self.slide = (self.slide + 0.07 * dt / FRAME_REF).min(1.0);
            busy = true;
        }
        if self.name_t < 1.0 {
            self.name_t = (self.name_t + 0.08 * dt / FRAME_REF).min(1.0);
            busy = true;
        }
        let want: Vec<String> = self.hover.iter().chain(self.active.iter()).cloned().collect();
        let keys: Vec<String> = self.glow.keys().cloned().chain(want.iter().cloned()).collect();
        for k in keys {
            let t = if want.contains(&k) { 1.0 } else { 0.0 };
            let v = self.glow.entry(k).or_insert(0.0);
            if (*v - t).abs() > 0.01 {
                *v += (t - *v) * smooth(0.3, dt);
                busy = true;
            } else {
                *v = t;
            }
        }
        self.glow.retain(|_, v| *v > 0.0);
        if busy || self.dirty {
            self.dirty = false;
            self.paint(cfg, widgets, bg);
        }
        busy
    }

    fn paint(&mut self, cfg: &Config, widgets: &[(String, String)], bg: &Option<(Rc<Image>, f32)>) {
        let s = self.mon.scale;
        let m = self.mon.rect;
        let (w, h) = (m.right - m.left, self.h(cfg));
        if !self.surf.ensure(w, h) {
            return;
        }
        if self.state.0.is_empty() {
            self.state = (clock_text(cfg), sys::keyboard_lang(), if cfg.show_battery { crate::sysinfo::battery().map(|b| b.0) } else { None }, crate::audio::glyph());
        }
        let hf = h as f32;
        let dy = -hf * (1.0 - ease_out(self.slide));
        let mut hits: Vec<Hit> = Vec::new();
        let glow = self.glow.clone();
        let active = self.active.clone();
        let mut cv = self.surf.canvas();
        cv.clear();
        cv.fill_rect(R::new(0.0, dy, w as f32, hf), rgba(24, 24, 28, cfg.bar_opacity.min(255) as u8));
        if let Some((img, op)) = bg {
            if let Some(path) = rounded_rect(R::new(0.0, dy, w as f32, hf), 0.0) {
                crate::dock::draw_cover(&mut cv, img, R::new(0.0, dy, w as f32, hf), &path, *op);
            }
        }
        cv.fill_rect(R::new(0.0, dy + hf - 1.0, w as f32, 1.0), rgba(255, 255, 255, 25));
        let mut add = |cv: &mut Canvas, x: f32, ww: f32, key: &str| -> R {
            let r = R::new(x, dy + 3.0 * s, ww, hf - 6.0 * s);
            let g = glow.get(key).copied().unwrap_or(0.0);
            if g > 0.0 {
                let a = if active.as_deref() == Some(key) { 60.0 } else { 34.0 };
                cv.fill_rrect(r, 5.0 * s, rgba(255, 255, 255, (a * g) as u8));
            }
            hits.push(Hit { r: r.translated(0.0, -dy), key: key.to_string() });
            r
        };
        // логотип Windows
        let r = add(&mut cv, 8.0 * s, 32.0 * s, "logo");
        let (cx, cy, q) = (r.cx(), r.cy(), 5.0 * s);
        for (dx, dyy) in [(-1.0, -1.0), (0.0, -1.0), (-1.0, 0.0), (0.0, 0.0)] {
            cv.fill_rect(R::new((cx + dx * (q + s)).round(), (cy + dyy * (q + s)).round(), q, q), rgba(245, 245, 248, 255));
        }
        let mut x = 44.0 * s;
        let fsz = 13.3 * s;
        let nw = text::width(&self.app_name, Font::Bold, fsz) + 20.0 * s;
        let r = add(&mut cv, x, nw, "app");
        let k = ease_out(self.name_t);
        if let Some(prev) = &self.prev_name {
            if k < 1.0 {
                cv.opacity = 1.0 - k;
                text::draw_in(&mut cv, prev, Font::Bold, fsz, r.translated(0.0, -8.0 * s * k), Align::Center, rgba(250, 250, 252, 255));
            }
        }
        cv.opacity = k;
        text::draw_in(&mut cv, &self.app_name, Font::Bold, fsz, r.translated(0.0, 8.0 * s * (1.0 - k)), Align::Center, rgba(250, 250, 252, 255));
        cv.opacity = 1.0;
        x += nw;
        let mut menus: Vec<String> = self.native.clone();
        menus.push("Окно".into());
        for (i, t) in menus.iter().enumerate() {
            let mw = text::width(t, Font::Semibold, fsz) + 18.0 * s;
            let key = if i + 1 == menus.len() { "window".to_string() } else { format!("m{i}") };
            let r = add(&mut cv, x, mw, &key);
            text::draw_in(&mut cv, t, Font::Semibold, fsz, r, Align::Center, rgba(245, 245, 248, 255));
            x += mw;
        }
        // правая часть: справа налево
        let mut x = w as f32 - 10.0 * s;
        let (clock, lang, bat, vol) = self.state.clone();
        let mut items: Vec<(&str, String, String)> = vec![("clock", clock.clone(), "clock".into())];
        if let Some(b) = bat {
            items.push(("text", format!("{b:.0}%"), "bat".into()));
        }
        items.push(("text", lang, "lang".into()));
        items.push(("icon", vol.to_string(), "vol".into()));
        items.push(("icon", "\u{E701}".into(), "net".into()));
        if cfg.show_bluetooth && crate::bluetooth::has_radio() == Some(true) {
            items.push(("icon", "\u{E702}".into(), "bt".into()));
        }
        items.push(("icon", "\u{E721}".into(), "search".into()));
        items.push(("icon", "\u{E70E}".into(), "tray".into()));
        for (id, t) in widgets.iter().rev() {
            items.push(("widget", t.clone(), format!("w:{id}")));
        }
        let now = crate::anim::now();
        for (kind, t, key) in items {
            if kind == "widget" {
                let glyph = crate::widgets::icon_of(&key[2..]);
                let gw = if glyph.is_empty() { 0.0 } else { text::width(glyph, Font::Icons, 14.0 * s) + 6.0 * s };
                // ширина растёт сразу, а сужается не раньше чем через несколько секунд — надпись не «дёргается»
                let tw = text::width(&t, Font::Semibold, fsz);
                let keep = self.widget_w.get(&key).copied();
                let keep = match keep {
                    Some((kw, at)) if tw <= kw && now - at <= 4.0 => (kw, at),
                    _ => {
                        self.widget_w.insert(key.clone(), (tw, now));
                        (tw, now)
                    }
                };
                let ww = gw + keep.0 + 18.0 * s;
                x -= ww;
                let r = add(&mut cv, x, ww, &key);
                if !glyph.is_empty() {
                    text::draw_in(&mut cv, glyph, Font::Icons, 14.0 * s, R::new(r.x + 9.0 * s, r.y, gw, r.h), Align::Left, rgba(245, 245, 248, 255));
                }
                text::draw_in(&mut cv, &t, Font::Semibold, fsz, R::new(r.x + 9.0 * s + gw, r.y, keep.0, r.h), Align::Center, rgba(245, 245, 248, 255));
                continue;
            }
            let (font, size) = match kind {
                "icon" => (Font::Icons, 14.0 * s),
                _ => (Font::Semibold, fsz),
            };
            let mut ww = text::width(&t, font, size) + 18.0 * s;
            if kind == "clock" {
                // ширина часов не меняется каждую секунду
                let base: String = t.trim_end_matches(|c: char| c.is_ascii_digit()).to_string() + "00";
                ww = text::width(&base, font, size) + 18.0 * s;
            }
            x -= ww;
            let r = add(&mut cv, x, ww, &key);
            text::draw_in(&mut cv, &t, font, size, r, Align::Center, rgba(245, 245, 248, 255));
        }
        drop(cv);
        self.hits = hits;
        self.surf.present(self.hwnd, m.left, m.top, 255);
    }

    fn hit_at(&self, x: f32, y: f32) -> Option<Hit> {
        self.hits.iter().find(|h| h.r.contains(x, y)).cloned()
    }

    fn screen_rect(&self, r: R) -> RECT {
        let m = self.mon.rect;
        RECT { left: m.left + r.x as i32, top: m.top + r.y as i32, right: m.left + r.right() as i32, bottom: m.top + r.bottom() as i32 }
    }
}

fn clock_text(cfg: &Config) -> String {
    let t = unsafe { GetLocalTime() };
    let time = if cfg.clock_seconds { format!("{:02}:{:02}:{:02}", t.wHour, t.wMinute, t.wSecond) } else { format!("{:02}:{:02}", t.wHour, t.wMinute) };
    if cfg.clock_date {
        format!("{} {} {}  {}", DAYS[t.wDayOfWeek as usize % 7], t.wDay, MONTHS[(t.wMonth as usize + 11) % 12], time)
    } else {
        time
    }
}

// ---------------------------------------------------------------- нативные меню классических программ

fn menu_string(hm: HMENU, i: u32) -> String {
    let mut buf = [0u16; 256];
    let n = unsafe { GetMenuStringW(hm, i, Some(&mut buf), MF_BYPOSITION) };
    String::from_utf16_lossy(&buf[..n.max(0) as usize])
}

fn native_titles(target: HWND) -> Vec<String> {
    if target.is_invalid() {
        return Vec::new();
    }
    unsafe {
        let hm = GetMenu(target);
        if hm.is_invalid() {
            return Vec::new();
        }
        let n = GetMenuItemCount(Some(hm)).clamp(0, 12);
        let mut out = Vec::new();
        for i in 0..n {
            let t = menu_string(hm, i as u32).replace('&', "");
            let t = t.split('\t').next().unwrap_or("").trim().to_string();
            if !t.is_empty() && !GetSubMenu(hm, i).is_invalid() {
                out.push(t);
            }
        }
        out.truncate(8);
        out
    }
}

fn fill_native(items: &mut Vec<MenuItem>, hm: HMENU, hwnd: HWND, index: usize) {
    unsafe {
        let mut res = 0usize;
        let _ = SendMessageTimeoutW(hwnd, WM_INITMENUPOPUP, WPARAM(hm.0 as usize), LPARAM(index as isize), SMTO_ABORTIFHUNG, 200, Some(&mut res));
        let n = GetMenuItemCount(Some(hm)).max(0);
        for i in 0..n {
            let state = GetMenuState(hm, i as u32, MF_BYPOSITION);
            if state & MF_SEPARATOR.0 != 0 {
                items.push(MenuItem::sep());
                continue;
            }
            let text = menu_string(hm, i as u32);
            if text.is_empty() {
                continue;
            }
            let (label, shortcut) = match text.split_once('\t') {
                Some((a, b)) => (a.replace('&', ""), b.to_string()),
                None => (text.replace('&', ""), String::new()),
            };
            let sub = GetSubMenu(hm, i);
            if !sub.is_invalid() {
                let mut kids = Vec::new();
                fill_native(&mut kids, sub, hwnd, i as usize);
                items.push(MenuItem::submenu(&label, kids));
                continue;
            }
            let id = GetMenuItemID(hm, i);
            let h = hwnd.0 as isize;
            let act: Action = Rc::new(move |_| {
                let w = win::hwnd_of(h);
                sys::force_foreground(w);
                let _ = PostMessageW(Some(w), WM_COMMAND, WPARAM(id as usize), LPARAM(0));
            });
            let mut it = if state & MF_CHECKED.0 != 0 { MenuItem::check(&label, true, Some(act)) } else { MenuItem::action(&label, Some(act)) };
            it.enabled = state & (MF_GRAYED.0 | MF_DISABLED.0) == 0;
            it.shortcut = shortcut;
            items.push(it);
        }
    }
}

// ---------------------------------------------------------------- действия

fn act(f: impl Fn(&mut App) + 'static) -> Option<Action> {
    Some(Rc::new(f))
}

fn confirm(text: &str, cmd: &'static [&'static str]) {
    let t = text.to_string();
    crate::app::defer(move || unsafe {
        let r = MessageBoxW(None, &windows::core::HSTRING::from(t.as_str()), windows::core::w!("MiniDock"), MB_YESNO | MB_ICONQUESTION | MB_TOPMOST | MB_SETFOREGROUND);
        if r == IDYES {
            shell::spawn(cmd[0], &cmd[1..]);
        }
    });
}

fn logo_menu() -> Vec<MenuItem> {
    vec![
        MenuItem::action("Об этом компьютере", act(|_| {
            shell::launch("ms-settings:about", "", None);
        })),
        MenuItem::sep(),
        MenuItem::action("Launchpad", act(|a| a.toggle_launchpad())),
        MenuItem::action("Параметры Windows…", act(|_| {
            shell::launch("ms-settings:", "", None);
        })),
        MenuItem::action("Настройки MiniDock…", act(|a| a.open_settings())),
        MenuItem::action("Меню «Пуск» Windows", act(|_| sys::press_keys(&[0x5B]))),
        MenuItem::action("Диспетчер задач", act(|_| {
            shell::spawn("taskmgr.exe", &[]);
        })),
        MenuItem::sep(),
        MenuItem::action("Сон", act(|_| unsafe {
            let _ = windows::Win32::System::Power::SetSuspendState(false, false, false);
        })),
        MenuItem::action("Перезагрузить…", act(|_| confirm("Перезагрузить компьютер?", &["shutdown", "/r", "/t", "0"]))),
        MenuItem::action("Выключить…", act(|_| confirm("Выключить компьютер?", &["shutdown", "/s", "/t", "0"]))),
        MenuItem::sep(),
        MenuItem::action("Заблокировать экран", act(|_| unsafe {
            let _ = windows::Win32::System::Shutdown::LockWorkStation();
        })),
        MenuItem::action("Завершить сеанс…", act(|_| confirm("Выйти из системы?", &["shutdown", "/l"]))),
    ]
}

fn app_windows_of(app: &App, exe: &str) -> Vec<isize> {
    app.wins.iter().filter(|(e, _)| e == exe).flat_map(|(_, hs)| hs.iter().map(|h| h.0 as isize)).collect()
}

fn app_menu(app: &App, bi: usize) -> Vec<MenuItem> {
    let b = &app.bars[bi];
    let name = b.app_name.clone();
    if b.app_exe.is_empty() {
        return vec![
            MenuItem::action("Новое окно Проводника", act(|_| {
                shell::spawn("explorer.exe", &[]);
            })),
            MenuItem::action("Свернуть все окна", act(|_| sys::press_keys(&[0x5B, 0x44]))),
        ];
    }
    let exe = b.app_exe.clone();
    let e1 = exe.clone();
    let e2 = exe.clone();
    let e3 = exe.clone();
    vec![
        MenuItem::action(&format!("Скрыть «{name}»"), act(move |a| {
            for h in app_windows_of(a, &e1) {
                a.minimize_window(h);
            }
        })),
        MenuItem::action("Скрыть остальные", act(move |a| {
            let others: Vec<isize> = a.wins.iter().filter(|(e, _)| *e != e2).flat_map(|(_, hs)| hs.iter().map(|h| h.0 as isize)).collect();
            for h in others {
                a.minimize_window(h);
            }
        })),
        MenuItem::sep(),
        MenuItem::action("Показать в проводнике", act(move |_| shell::show_in_explorer(&exe))),
        MenuItem::sep(),
        MenuItem::action(&format!("Завершить «{name}»"), act(move |a| {
            for h in app_windows_of(a, &e3) {
                sys::close_window(win::hwnd_of(h));
            }
        })),
    ]
}

fn window_menu(app: &App, bi: usize) -> Vec<MenuItem> {
    let h = app.bars[bi].target;
    let ok = !h.is_invalid() && sys::is_window(h);
    let zoomed = ok && sys::is_zoomed(h);
    let k = h.0 as isize;
    let mut items = vec![
        MenuItem::action("Свернуть", act(move |a| a.minimize_window(k))),
        MenuItem::action(if zoomed { "Восстановить" } else { "Развернуть" }, act(move |_| sys::syscommand(win::hwnd_of(k), if zoomed { SC_RESTORE } else { SC_MAXIMIZE }))),
        MenuItem::action("Закрыть окно", act(move |_| sys::close_window(win::hwnd_of(k)))),
    ];
    for it in &mut items {
        it.enabled = ok;
    }
    items.push(MenuItem::sep());
    items.push(MenuItem::action("Все окна (Task View)", act(|_| sys::press_keys(&[0x5B, 0x09]))));
    items
}

fn native_menu(app: &App, bi: usize, index: usize) -> Vec<MenuItem> {
    let hwnd = app.bars[bi].target;
    let mut items = Vec::new();
    unsafe {
        let hm = GetMenu(hwnd);
        let subs: Vec<HMENU> = (0..GetMenuItemCount(Some(hm)).max(0)).map(|i| GetSubMenu(hm, i)).filter(|s| !s.is_invalid()).collect();
        if let Some(&sub) = subs.get(index) {
            fill_native(&mut items, sub, hwnd, index);
        }
    }
    if items.is_empty() {
        items.push(MenuItem::disabled("(пусто)"));
    }
    items
}

pub fn close_flyout(app: &mut App) {
    if let Some(mut f) = app.flyout.take() {
        crate::dbg_log(&format!("close_flyout {:?}", f.kind));
        if let Kind::Widget(id) = &f.kind {
            app.widgets.set_detailed(id, false);
        }
        let bi = f.bar;
        f.close();
        app.flyout_closed_at = crate::anim::now();
        app.flyout_closed_key = Some(f.key.clone());
        if let Some(b) = app.bars.get_mut(bi) {
            b.active = None;
            b.dirty = true;
        }
        app.request_frame();
    }
}

fn toggle_flyout(app: &mut App, bi: usize, kind: Kind, key: &str, r: RECT) {
    let same = app.flyout.as_ref().is_some_and(|f| f.key == key && f.bar == bi);
    // клик по значку открытого окна: окно уже закрылось от потери фокуса — не открываем снова
    let just_closed = app.flyout_closed_key.as_deref() == Some(key) && crate::anim::now() - app.flyout_closed_at < 0.3;
    close_flyout(app);
    if same || just_closed {
        return;
    }
    if let Kind::Widget(id) = &kind {
        app.widgets.set_detailed(id, true);
    }
    crate::dbg_log(&format!("open_flyout {kind:?} at {:?}", (r.left, r.top, r.right, r.bottom)));
    app.flyout = Some(Flyout::open(kind, bi, key, r));
    app.bars[bi].active = Some(key.to_string());
    app.request_frame();
}

fn on_click(app: &mut App, bi: usize, key: &str, r: RECT) {
    let menu_at = |app: &mut App, items: Vec<MenuItem>, key: &str| {
        app.bars[bi].active = Some(key.to_string());
        app.show_menu(items, r.left, r.bottom + 3, Owner::Bar(bi));
    };
    match key {
        "logo" => menu_at(app, logo_menu(), key),
        "app" => {
            let m = app_menu(app, bi);
            menu_at(app, m, key);
        }
        "window" => {
            let m = window_menu(app, bi);
            menu_at(app, m, key);
        }
        k if k.starts_with('m') => {
            let i: usize = k[1..].parse().unwrap_or(0);
            let m = native_menu(app, bi, i);
            menu_at(app, m, key);
        }
        "clock" => toggle_flyout(app, bi, Kind::Clock, key, r),
        "bat" => toggle_flyout(app, bi, Kind::Battery, key, r),
        "lang" => toggle_flyout(app, bi, Kind::Lang, key, r),
        "vol" => toggle_flyout(app, bi, Kind::Volume, key, r),
        "net" => toggle_flyout(app, bi, Kind::Wifi, key, r),
        "bt" => toggle_flyout(app, bi, Kind::Bluetooth, key, r),
        "tray" => toggle_flyout(app, bi, Kind::Tray, key, r),
        "search" => app.toggle_spotlight(),
        k if k.starts_with("w:") => toggle_flyout(app, bi, Kind::Widget(k[2..].to_string()), key, r),
        _ => {}
    }
}

pub fn on_message(app: &mut App, hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> Option<LRESULT> {
    if let Some(r) = crate::flyouts::on_message(app, hwnd, msg, wp.0, lp.0) {
        return Some(LRESULT(r));
    }
    if msg == WM_LBUTTONDOWN {
        crate::dbg_log(&format!("bar::on_message down hwnd={:?}", hwnd.0));
    }
    let bi = app.bars.iter().position(|b| b.hwnd == hwnd)?;
    let (x, y) = win::lparam_xy(lp);
    let (fx, fy) = (x as f32, y as f32);
    match msg {
        WM_MOUSEMOVE => {
            let b = &mut app.bars[bi];
            if !b.tracking {
                b.tracking = true;
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
            let key = b.hit_at(fx, fy).map(|h| h.key);
            if key != b.hover {
                b.hover = key;
                app.request_frame();
            }
        }
        win::WM_MOUSELEAVE => {
            let b = &mut app.bars[bi];
            b.tracking = false;
            b.hover = None;
            app.request_frame();
        }
        WM_LBUTTONDOWN => {
            crate::dbg_log(&format!("bar down {fx},{fy} hits={}", app.bars[bi].hits.len()));
            if let Some(h) = app.bars[bi].hit_at(fx, fy) {
                let r = app.bars[bi].screen_rect(h.r);
                on_click(app, bi, &h.key, r);
                app.request_frame();
            }
        }
        WM_RBUTTONUP => {
            let items = vec![
                MenuItem::action("Настройки MiniDock…", act(|a| a.open_settings())),
                MenuItem::action("Показать панель задач Windows", act(|a| a.peek_taskbar())),
                MenuItem::sep(),
                MenuItem::action("Выход", act(|a| a.quit())),
            ];
            let (cx, cy) = win::cursor_pos();
            app.show_menu(items, cx, cy, Owner::Bar(bi));
        }
        win::WM_APP_APPBAR => {}
        _ => return None,
    }
    Some(LRESULT(0))
}

/// Кадры всплывающих окон панели.
pub fn flyouts_frame(app: &mut App, active: &mut bool) {
    if crate::widgets::Host::take_changed() {
        for b in &mut app.bars {
            b.dirty = true;
            *active = true;
        }
    }
    let cfg = app.cfg.clone();
    if let Some(f) = app.flyout.as_mut() {
        *active |= f.frame(&cfg, &app.widgets);
    }
}

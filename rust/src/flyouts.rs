//! Всплывающие окна верхней панели: календарь, звук, Wi‑Fi, Bluetooth, трей, раскладка, аккумулятор, виджеты.

use std::rc::Rc;

use windows::Win32::Foundation::HWND;
use windows::Win32::System::SystemInformation::GetLocalTime;
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::app::App;
use crate::audio::{self, Flow};
use crate::bigclock::{MONTHS_GEN, WEEKDAYS};
use crate::gfx::*;
use crate::glass::Glass;
use crate::panel::{Event, Hit, MediaInfo, Panel, Row};
use crate::win::{self, shell, sys, Surface};

#[derive(Clone, PartialEq, Debug)]
pub enum Kind {
    Clock,
    Battery,
    Lang,
    Volume,
    Wifi,
    Bluetooth,
    Tray,
    Widget(String),
}

impl Kind {
    fn width(&self) -> f32 {
        match self {
            Kind::Volume => 400.0,
            Kind::Clock => 300.0,
            Kind::Wifi | Kind::Bluetooth => 290.0,
            Kind::Tray => 252.0,
            Kind::Widget(_) => 320.0,
            _ => 240.0,
        }
    }
}

pub struct Flyout {
    pub hwnd: HWND,
    surf: Surface,
    glass: Glass,
    pub kind: Kind,
    pub bar: usize,
    pub key: String,
    panel: Panel,
    pub x: i32,
    y: i32,
    w: i32,
    h_cur: f32,
    scale: f32,
    opened: f64,
    last: f64,
    cal: (i32, u32),
    wifi: Option<Option<crate::sysinfo::WifiInfo>>,
    bt: Option<crate::bluetooth::Info>,
    bt_busy: bool,
    tray: Vec<crate::tray::TrayIcon>,
    tray_icons: Vec<Option<Rc<Image>>>,
    layouts: Vec<(isize, String)>,
    apps_open: bool,
    apps: Vec<audio::AppSession>,
    app_icons: Vec<Option<Rc<Image>>>,
    outs: Vec<audio::Device>,
    ins: Vec<audio::Device>,
    media: Vec<MediaInfo>,
    media_keys: Vec<(String, String, String)>,
    pub alive: bool,
}

thread_local! {
    static ICONS: std::cell::RefCell<std::collections::HashMap<String, Option<Rc<Image>>>> = std::cell::RefCell::new(std::collections::HashMap::new());
}

/// Значок программы по exe или AUMID (как у медиа-сессий: «Spotify.exe», «chrome.exe», ID приложения Store).
fn app_icon(app: &str, size: i32) -> Option<Rc<Image>> {
    let key = format!("{app}|{size}");
    if let Some(i) = ICONS.with(|c| c.borrow().get(&key).cloned()) {
        return i;
    }
    let img = if std::path::Path::new(app).is_absolute() {
        shell::file_icon(app, size)
    } else if app.to_lowercase().ends_with(".exe") {
        // найти запущенный процесс с таким именем
        sys::app_windows()
            .into_iter()
            .find(|(exe, _)| sys::file_name(exe).eq_ignore_ascii_case(app))
            .and_then(|(exe, _)| shell::file_icon(&exe, size))
    } else {
        shell::start_app_icon(app, size)
    }
    .map(|i| Rc::new(i.scaled(size as u32, size as u32)));
    ICONS.with(|c| {
        let mut c = c.borrow_mut();
        if c.len() > 40 {
            c.clear();
        }
        c.insert(key, img.clone());
    });
    img
}

/// Обложка без прозрачных полей (Spotify кладёт свой логотип под обложку на прозрачном фоне).
fn trim_cover(img: Image) -> Image {
    let (w, h) = (img.w as usize, img.h as usize);
    let d = img.pm.data();
    let solid = |x: usize, y: usize| d[(y * w + x) * 4 + 3] > 200;
    let row_full: Vec<bool> = (0..h).map(|y| (0..w).filter(|&x| solid(x, y)).count() * 2 > w).collect();
    let Some(top) = row_full.iter().position(|&f| f) else { return img };
    let mut bottom = top;
    while bottom + 1 < h && row_full[bottom + 1] {
        bottom += 1;
    }
    if (bottom - top + 1) * w < w * h / 4 || (top == 0 && bottom == h - 1) {
        return img;
    }
    let cols: Vec<usize> = (0..w).filter(|&x| (top..=bottom).filter(|&y| solid(x, y)).count() * 10 > (bottom - top + 1) * 9).collect();
    let (Some(&l), Some(&r)) = (cols.first(), cols.last()) else { return img };
    let (cw, ch) = (r - l + 1, bottom - top + 1);
    let mut data = vec![0u8; cw * ch * 4];
    for y in 0..ch {
        let so = ((top + y) * w + l) * 4;
        data[y * cw * 4..(y + 1) * cw * 4].copy_from_slice(&d[so..so + cw * 4]);
    }
    Image::from_bgra_premul(cw as u32, ch as u32, data).unwrap_or(img)
}

/// Сильно размытая копия картинки (фон карточки плеера).
fn blurred(img: &Image) -> Image {
    let small = img.scaled((img.w / 6).max(8), (img.h / 6).max(8));
    let mut pm = small.pm.clone();
    let (w, h) = (pm.width() as usize, pm.height() as usize);
    blur_rgb(pm.data_mut(), w, h, 3.0, 1.25);
    Image::from_pixmap(pm)
}

impl Flyout {
    pub fn open(kind: Kind, bar: usize, key: &str, anchor: windows::Win32::Foundation::RECT) -> Flyout {
        let hwnd = win::create_focusable("MiniDockFlyout", true);
        win::dwm::exclude_from_peek(hwnd);
        let mon = sys::point_monitor((anchor.left + anchor.right) / 2, anchor.bottom);
        let s = mon.scale;
        let mut panel = Panel::new(kind.width());
        panel.scale = s;
        let t = unsafe { GetLocalTime() };
        let mut f = Flyout {
            hwnd,
            surf: Surface::default(),
            glass: Glass::new(),
            kind: kind.clone(),
            bar,
            key: key.to_string(),
            panel,
            x: 0,
            y: anchor.bottom + (6.0 * s) as i32,
            w: (kind.width() * s) as i32,
            h_cur: 0.0,
            scale: s,
            opened: crate::anim::now(),
            last: crate::anim::now(),
            cal: (t.wYear as i32, t.wMonth as u32),
            wifi: None,
            bt: None,
            bt_busy: false,
            tray: Vec::new(),
            tray_icons: Vec::new(),
            layouts: Vec::new(),
            apps_open: false,
            apps: Vec::new(),
            app_icons: Vec::new(),
            outs: Vec::new(),
            ins: Vec::new(),
            media: Vec::new(),
            media_keys: Vec::new(),
            alive: true,
        };
        let cx = (anchor.left + anchor.right) / 2;
        f.x = (cx - f.w / 2).clamp(mon.rect.left + (6.0 * s) as i32, mon.rect.right - f.w - (6.0 * s) as i32);
        f.load();
        f
    }

    /// Данные при открытии (медленное — в фоне).
    fn load(&mut self) {
        match &self.kind {
            Kind::Wifi => {
                crate::app::spawn_then(crate::sysinfo::wifi, |a, r| {
                    if let Some(f) = a.flyout.as_mut().filter(|f| f.kind == Kind::Wifi) {
                        f.wifi = Some(r);
                        f.rebuild_soon();
                    }
                });
            }
            Kind::Bluetooth => self.load_bt(),
            Kind::Tray => {
                self.tray = crate::tray::icons();
                self.tray_icons = self.tray.iter_mut().map(|t| t.icon.take().map(|i| Rc::new(i.scaled(22, 22)))).collect();
            }
            Kind::Lang => {
                self.layouts = keyboard_layouts();
            }
            Kind::Volume => {
                self.outs = audio::devices(Flow::Output);
                self.ins = audio::devices(Flow::Input);
                self.refresh_media();
            }
            _ => {}
        }
    }

    fn load_bt(&mut self) {
        self.bt_busy = true;
        crate::app::spawn_then(crate::bluetooth::info, |a, r| {
            if let Some(f) = a.flyout.as_mut().filter(|f| f.kind == Kind::Bluetooth) {
                f.bt = Some(r);
                f.bt_busy = false;
                f.rebuild_soon();
            }
        });
    }

    pub fn refresh_media(&mut self) {
        crate::media::collect(|sessions| {
            crate::app::post_from_thread(Box::new(move |a| {
                if let Some(f) = a.flyout.as_mut().filter(|f| f.kind == Kind::Volume) {
                    f.set_media(sessions);
                    f.rebuild_soon();
                }
            }));
        });
    }

    fn set_media(&mut self, sessions: Vec<crate::media::Session>) {
        let mut out = Vec::new();
        let mut keys = Vec::new();
        for s in sessions {
            let key = (s.app.clone(), s.title.clone(), s.artist.clone());
            // та же песня — обложку не декодируем заново
            if let Some(i) = self.media_keys.iter().position(|k| *k == key) {
                let mut m = self.media[i].clone();
                m.playing = s.playing;
                m.id = s.id;
                out.push(m);
                keys.push(key);
                continue;
            }
            let cover = s.cover.as_ref().and_then(|b| crate::imageio::load_bytes(b, 300)).map(trim_cover);
            let blurred_bg = cover.as_ref().map(|c| Rc::new(blurred(c)));
            out.push(MediaInfo {
                id: s.id,
                app: s.app.clone(),
                title: s.title.clone(),
                artist: s.artist.clone(),
                playing: s.playing,
                cover: cover.map(Rc::new),
                blurred: blurred_bg,
                icon: app_icon(&s.app, (26.0 * self.scale) as i32),
            });
            keys.push(key);
        }
        self.media = out;
        self.media_keys = keys;
    }

    fn rebuild_soon(&mut self) {
        self.last = 0.0; // следующий кадр пересоберёт строки
    }

    fn rows(&mut self, app_cfg: &crate::config::Config, widgets: &crate::widgets::Host) -> Vec<Row> {
        let t = unsafe { GetLocalTime() };
        match &self.kind {
            Kind::Clock => vec![
                Row::Big(format!("{:02}:{:02}:{:02}", t.wHour, t.wMinute, t.wSecond)),
                Row::Text(format!("{}, {} {} {}", WEEKDAYS[t.wDayOfWeek as usize % 7], t.wDay, MONTHS_GEN[(t.wMonth as usize + 11) % 12], t.wYear)),
                Row::Calendar(self.cal.0, self.cal.1, (t.wYear as i32, t.wMonth as u32, t.wDay as u32), 101, 102),
                Row::Buttons(vec![(103, "Уведомления".into()), (104, "Дата и время…".into())]),
            ],
            Kind::Battery => match crate::sysinfo::battery() {
                None => vec![Row::Title("Аккумулятор".into()), Row::Text("Нет данных".into())],
                Some((p, plugged, left)) => {
                    let state = if plugged {
                        "Подключено к сети".to_string()
                    } else if let Some(s) = left {
                        format!("Осталось примерно {} ч {} мин", s / 3600, s % 3600 / 60)
                    } else {
                        "Работа от батареи".to_string()
                    };
                    vec![Row::Title("Аккумулятор".into()), Row::Big(format!("{p:.0}%")), Row::Text(state), Row::Button(201, "Параметры питания…".into(), false)]
                }
            },
            Kind::Lang => {
                let cur = current_layout();
                let mut rows = vec![Row::Title("Раскладка клавиатуры".into())];
                for (i, (hkl, name)) in self.layouts.iter().enumerate() {
                    rows.push(Row::Button(300 + i as u32, name.clone(), *hkl == cur));
                }
                rows.push(Row::Button(399, "Языковые параметры…".into(), false));
                rows
            }
            Kind::Volume => {
                let mut rows = vec![Row::Section("Общая громкость".into())];
                let (lv, mt) = audio::level(Flow::Output).unwrap_or((0.0, false));
                rows.push(Row::Volume(401, lv, mt, audio::GLYPH_SPEAKER, audio::GLYPH_MUTE, Some((402, audio::GLYPH_MIXER, self.apps_open))));
                if self.apps_open {
                    if self.apps.is_empty() {
                        rows.push(Row::Text("Сейчас ни одна программа не воспроизводит звук".into()));
                    }
                    for (i, a) in self.apps.iter().enumerate() {
                        rows.push(Row::AppVolume(450 + i as u32, self.app_icons.get(i).cloned().flatten(), a.name.clone(), a.level()));
                    }
                }
                let (lm, mm) = audio::level(Flow::Input).unwrap_or((0.0, false));
                rows.push(Row::Volume(403, lm, mm, audio::GLYPH_MIC, audio::GLYPH_MIC_OFF, Some((404, audio::GLYPH_SETTINGS, false))));
                rows.push(Row::Sep);
                rows.push(Row::Section("Устройство вывода".into()));
                for (i, d) in self.outs.iter().enumerate() {
                    rows.push(Row::Device(500 + i as u32, d.name.clone(), d.media, d.comm));
                }
                rows.push(Row::Sep);
                rows.push(Row::Section("Устройство ввода".into()));
                for (i, d) in self.ins.iter().enumerate() {
                    rows.push(Row::Device(600 + i as u32, d.name.clone(), d.media, d.comm));
                }
                if !self.media.is_empty() {
                    rows.push(Row::Sep);
                    rows.push(Row::Section("Медиаплееры".into()));
                    let solid = app_cfg.media_bg == "solid";
                    for m in &self.media {
                        let mut m = m.clone();
                        if solid {
                            m.blurred = None;
                        }
                        rows.push(Row::Media(m));
                        rows.push(Row::Space(8.0));
                    }
                }
                rows
            }
            Kind::Wifi => {
                let mut rows = vec![Row::Title("Wi‑Fi".into())];
                match &self.wifi {
                    None => rows.push(Row::Text("Поиск сетей…".into())),
                    Some(None) => rows.push(Row::Text("Wi‑Fi адаптер не найден. Подключение по кабелю или сеть отключена.".into())),
                    Some(Some((cur, nets))) => {
                        rows.push(Row::Text(match cur {
                            Some((n, _)) => format!("Подключено: {n}"),
                            None => "Не подключено".into(),
                        }));
                        for (i, (name, sig, secure)) in nets.iter().take(9).enumerate() {
                            let is_cur = cur.as_ref().is_some_and(|c| &c.0 == name);
                            let n = 1 + (*sig as usize / 25).min(3);
                            let bars: String = "▂▄▆█".chars().take(n).collect::<String>() + &"·".repeat(4 - n);
                            let lock = if *secure && !is_cur { "   🔒" } else { "" };
                            rows.push(Row::Button(700 + i as u32, format!("{bars}   {name}{lock}"), is_cur));
                        }
                    }
                }
                rows.push(Row::Button(799, "Параметры сети…".into(), false));
                rows
            }
            Kind::Bluetooth => {
                let on = self.bt.as_ref().and_then(|b| b.on).unwrap_or(false);
                let mut rows = vec![Row::TitleToggle("Bluetooth".into(), 801, on)];
                match &self.bt {
                    None => rows.push(Row::Text("Ищем устройства…".into())),
                    Some(b) if self.bt_busy => rows.push(Row::Text(if b.on == Some(true) { "Выключаем…".into() } else { "Включаем…".into() })),
                    Some(b) => match b.on {
                        None => rows.push(Row::Text("Bluetooth-адаптер не найден.".into())),
                        Some(false) => rows.push(Row::Text("Bluetooth выключен".into())),
                        Some(true) => {
                            let conn: Vec<&str> = b.devices.iter().filter(|d| d.1).map(|d| d.0.as_str()).collect();
                            rows.push(Row::Text(if !conn.is_empty() {
                                format!("Подключено: {}", conn.join(", "))
                            } else if b.devices.is_empty() {
                                "Нет сопряжённых устройств".into()
                            } else {
                                "Нет подключённых устройств".into()
                            }));
                            for (i, (n, c)) in b.devices.iter().take(9).enumerate() {
                                rows.push(Row::Button(810 + i as u32, if *c { format!("  {n}   ✓") } else { format!("  {n}") }, *c));
                            }
                        }
                    },
                }
                rows.push(Row::Button(899, "Параметры Bluetooth…".into(), false));
                rows
            }
            Kind::Tray => {
                let mut rows = vec![Row::Title("Значки в трее".into())];
                if self.tray.is_empty() {
                    rows.push(Row::Text("Значков нет".into()));
                } else {
                    rows.push(Row::IconGrid(self.tray.iter().enumerate().map(|(i, t)| (900 + i as u32, self.tray_icons.get(i).cloned().flatten(), t.tip.clone())).collect()));
                }
                rows.push(Row::Text("Левый клик — открыть, правый — меню программы".into()));
                rows
            }
            Kind::Widget(id) => widgets.panel_rows(id),
        }
    }

    /// Кадр: пересобрать строки (раз в 200 мс или по требованию), анимации, отрисовка. true — нужны кадры.
    pub fn frame(&mut self, cfg: &crate::config::Config, widgets: &crate::widgets::Host) -> bool {
        let now = crate::anim::now();
        let need_rows = now - self.last > 0.2 || self.panel.rows.is_empty();
        if need_rows {
            self.last = now;
            let rows = self.rows(cfg, widgets);
            self.panel.set_rows(rows);
        }
        let s = self.scale;
        let target_h = self.panel.height() * s;
        let first = self.h_cur == 0.0;
        if first {
            self.h_cur = target_h;
        } else if (self.h_cur - target_h).abs() > 0.5 {
            self.h_cur += (target_h - self.h_cur) * 0.3;
        } else {
            self.h_cur = target_h;
        }
        let moving = self.panel.tick() | ((self.h_cur - target_h).abs() > 0.5);
        let t = ((now - self.opened) / 0.17).min(1.0) as f32;
        let k = ease_out(t);
        self.paint(k);
        // часы, громкость и устройства обновляются на месте; кадры чаще — только пока что-то движется
        moving || t < 1.0 || matches!(self.kind, Kind::Clock | Kind::Volume) && need_rows
    }

    fn paint(&mut self, k: f32) {
        let s = self.scale;
        let (w, h) = (self.w, self.h_cur.ceil() as i32);
        if !self.surf.ensure(w, h.max(1)) {
            return;
        }
        let full = R::new(0.0, 0.0, w as f32, h as f32);
        {
            let mut cv = self.surf.canvas();
            cv.clear();
            cv.fill_rrect(full.inset(0.5), 12.0 * s, rgba(34, 34, 40, 150));
            cv.stroke_rrect(full.inset(0.5), 12.0 * s, rgba(255, 255, 255, 40), 1.0);
            self.panel.paint(&mut cv, 0.0, 0.0);
        }
        let dy = (-10.0 * s * (1.0 - k)) as i32;
        self.surf.present(self.hwnd, self.x, self.y + dy, (255.0 * k) as u8);
        self.glass.place(self.x, self.y + dy, self.x + w, self.y + dy + h, (12.0 * s) as i32, self.hwnd, win::dwm::Blur::Blur, 0x01000000);
        if !win::is_visible(self.hwnd) {
            crate::dbg_log(&format!("flyout show {:?} {}x{} at {},{}", self.kind, w, h, self.x, self.y));
            unsafe {
                let _ = SetWindowPos(self.hwnd, Some(HWND_TOPMOST), self.x, self.y + dy, w, h, SWP_SHOWWINDOW);
            }
            sys::force_foreground(self.hwnd);
        }
    }

    pub fn close(&mut self) {
        self.alive = false;
        win::hide(self.hwnd);
        self.glass.hide();
        self.surf.free();
        unsafe {
            let _ = DestroyWindow(self.hwnd);
        }
    }

    pub fn contains(&self, x: i32, y: i32) -> bool {
        x >= self.x && x < self.x + self.w && y >= self.y && y < self.y + self.h_cur as i32
    }
}

fn keyboard_layouts() -> Vec<(isize, String)> {
    use windows::Win32::UI::Input::KeyboardAndMouse::{GetKeyboardLayoutList, HKL};
    unsafe {
        let n = GetKeyboardLayoutList(None);
        let mut buf = vec![HKL::default(); n.max(1) as usize];
        let n = GetKeyboardLayoutList(Some(&mut buf));
        buf.truncate(n.max(0) as usize);
        buf.iter()
            .map(|h| {
                let lang = (h.0 as usize & 0xFFFF) as u32;
                let mut w = [0u16; 128];
                let len = windows::Win32::Globalization::GetLocaleInfoW(lang, 0x02, Some(&mut w)); // LOCALE_SLANGUAGE
                let name = String::from_utf16_lossy(&w[..(len.max(1) - 1) as usize]);
                (h.0 as isize, if name.is_empty() { format!("{:x}", lang) } else { name })
            })
            .collect()
    }
}

fn current_layout() -> isize {
    use windows::Win32::UI::Input::KeyboardAndMouse::GetKeyboardLayout;
    unsafe {
        let fg = GetForegroundWindow();
        let tid = if fg.is_invalid() { 0 } else { GetWindowThreadProcessId(fg, None) };
        GetKeyboardLayout(tid).0 as isize
    }
}

// ---------------------------------------------------------------- события (через App)

fn open_uri(u: &str) {
    shell::launch(u, "", None);
}

pub fn on_message(app: &mut App, hwnd: HWND, msg: u32, wp: usize, lp: isize) -> Option<isize> {
    let f = app.flyout.as_mut()?;
    if f.hwnd != hwnd {
        return None;
    }
    let (x, y) = win::lparam_xy(windows::Win32::Foundation::LPARAM(lp));
    let (fx, fy) = (x as f32, y as f32);
    match msg {
        WM_MOUSEMOVE => {
            if let Some(Event::Slide(id, v)) = f.panel.mouse_move(fx, fy) {
                slide(app, id, v);
            }
            app.request_frame();
        }
        WM_LBUTTONDOWN => {
            unsafe {
                windows::Win32::UI::Input::KeyboardAndMouse::SetCapture(hwnd);
            }
            if let Some(Event::Slide(id, v)) = f.panel.mouse_down(fx, fy) {
                slide(app, id, v);
            }
            app.request_frame();
        }
        WM_LBUTTONUP | WM_RBUTTONUP => {
            unsafe {
                let _ = windows::Win32::UI::Input::KeyboardAndMouse::ReleaseCapture();
            }
            if msg == WM_LBUTTONUP {
                if let Some(Event::Click(h)) = f.panel.mouse_up(fx, fy) {
                    click(app, h, false);
                }
            } else if let Some(h) = f.panel.hit_at(fx / f.scale, fy / f.scale) {
                click(app, h, true);
            }
            app.request_frame();
        }
        WM_ACTIVATE => {
            if wp & 0xFFFF == 0 {
                let other = HWND(lp as *mut _);
                if app.menus.owns(other).is_none() {
                    crate::bar::close_flyout(app);
                }
            }
        }
        WM_KEYDOWN => {
            if wp == 0x1B {
                crate::bar::close_flyout(app);
            }
        }
        win::WM_MOUSELEAVE => {
            f.panel.leave();
            app.request_frame();
        }
        _ => return None,
    }
    Some(0)
}

fn slide(app: &mut App, id: u32, v: f32) {
    let Some(f) = app.flyout.as_mut() else { return };
    match id {
        401 => audio::set_level(Flow::Output, v),
        403 => audio::set_level(Flow::Input, v),
        450..=499 => {
            if let Some(a) = f.apps.get((id - 450) as usize) {
                a.set_level(v);
            }
        }
        _ => {}
    }
    f.rebuild_soon();
    for b in &mut app.bars {
        b.refresh_volume();
    }
}

fn click(app: &mut App, h: Hit, right: bool) {
    let Some(f) = app.flyout.as_mut() else { return };
    let id = h.id;
    let close = |app: &mut App| crate::bar::close_flyout(app);
    match (&f.kind, id) {
        (Kind::Clock, 101) => {
            let (y, m) = f.cal;
            f.cal = if m == 1 { (y - 1, 12) } else { (y, m - 1) };
            f.rebuild_soon();
        }
        (Kind::Clock, 102) => {
            let (y, m) = f.cal;
            f.cal = if m == 12 { (y + 1, 1) } else { (y, m + 1) };
            f.rebuild_soon();
        }
        (Kind::Clock, 103) => {
            close(app);
            sys::press_keys(&[0x5B, 0x41]); // Win+A
        }
        (Kind::Clock, 104) => {
            close(app);
            open_uri("ms-settings:dateandtime");
        }
        (Kind::Battery, 201) => {
            close(app);
            open_uri("ms-settings:batterysaver");
        }
        (Kind::Lang, 300..=398) => {
            if let Some((hkl, _)) = f.layouts.get((id - 300) as usize).cloned() {
                let target = app.bars.get(f.bar).map(|b| b.target).unwrap_or_default();
                close(app);
                let t = if target.is_invalid() { sys::foreground() } else { target };
                sys::force_foreground(t);
                unsafe {
                    let _ = PostMessageW(Some(t), WM_INPUTLANGCHANGEREQUEST, windows::Win32::Foundation::WPARAM(0), windows::Win32::Foundation::LPARAM(hkl));
                }
            }
        }
        (Kind::Lang, 399) => {
            close(app);
            open_uri("ms-settings:regionlanguage");
        }
        (Kind::Volume, 401) => {
            audio::toggle_mute(Flow::Output);
            f.rebuild_soon();
        }
        (Kind::Volume, 403) => {
            audio::toggle_mute(Flow::Input);
            f.rebuild_soon();
        }
        (Kind::Volume, 402) => {
            f.apps_open = !f.apps_open;
            if f.apps_open {
                f.apps = audio::app_sessions();
                let sz = (20.0 * f.scale) as i32;
                f.app_icons = f.apps.iter().map(|a| app_icon(&a.exe, sz)).collect();
            }
            f.rebuild_soon();
        }
        (Kind::Volume, 404) => {
            close(app);
            open_uri("ms-settings:sound");
        }
        (Kind::Volume, 500..=699) => {
            let list = if id < 600 { &f.outs } else { &f.ins };
            let i = (id % 100) as usize;
            if let Some(d) = list.get(i).cloned() {
                if audio::set_default(&d.id, h.part == 1) {
                    f.outs = audio::devices(Flow::Output);
                    f.ins = audio::devices(Flow::Input);
                    f.rebuild_soon();
                }
            }
        }
        (Kind::Volume, 9000..=9999) => {
            let sid = (id - 9000) as usize;
            let action = match h.part {
                0 => "prev",
                1 => "toggle",
                _ => "next",
            };
            if action == "toggle" {
                if let Some(m) = f.media.iter_mut().find(|m| m.id == sid) {
                    m.playing = !m.playing; // сразу показать новое состояние, пока Windows отвечает
                }
            }
            crate::media::control(sid, action);
            crate::app::defer_ms(300, || {
                crate::app::with(|a| {
                    if let Some(f) = a.flyout.as_mut() {
                        f.refresh_media();
                    }
                });
            });
            f.rebuild_soon();
        }
        (Kind::Wifi, 700..=798) => {
            if let Some(Some((_, nets))) = &f.wifi {
                if let Some(n) = nets.get((id - 700) as usize) {
                    let name = n.0.clone();
                    std::thread::spawn(move || crate::sysinfo::wifi_connect(&name));
                }
            }
            close(app);
        }
        (Kind::Wifi, 799) => {
            close(app);
            open_uri("ms-settings:network-wifi");
        }
        (Kind::Bluetooth, 801) => {
            if f.bt_busy || f.bt.as_ref().is_none_or(|b| b.on.is_none()) {
                return;
            }
            let on = !f.bt.as_ref().and_then(|b| b.on).unwrap_or(false);
            f.bt_busy = true;
            f.rebuild_soon();
            crate::app::spawn_then(
                move || {
                    crate::bluetooth::set_enabled(on);
                    crate::bluetooth::info()
                },
                |a, r| {
                    if let Some(f) = a.flyout.as_mut().filter(|f| f.kind == Kind::Bluetooth) {
                        f.bt = Some(r);
                        f.bt_busy = false;
                        f.rebuild_soon();
                    }
                },
            );
        }
        (Kind::Bluetooth, 810..=899) => {
            close(app);
            open_uri("ms-settings:bluetooth");
        }
        (Kind::Tray, 900..=999) => {
            if let Some(t) = f.tray.get((id - 900) as usize) {
                let (hw, uid, msg) = (t.hwnd, t.uid, t.msg);
                close(app);
                crate::app::defer_ms(160, move || crate::tray::click(hw, uid, msg, right));
            }
        }
        (Kind::Widget(_), 1) => {
            close(app);
            shell::launch("taskmgr.exe", "", None);
        }
        (Kind::Widget(_), 2) => {
            close(app);
            open_uri("ms-settings:network");
        }
        (Kind::Widget(_), 10..=12) => {
            if let Some(s) = app.widgets.current_media() {
                crate::media::control(s.id, ["toggle", "next", "prev"][(id - 10) as usize]);
            }
            close(app);
        }
        _ => {}
    }
}

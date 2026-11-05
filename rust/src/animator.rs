//! Анимации окон: «джинн» и «масштаб» на живых DWM-миниатюрах, закрытие, открытие из иконки.
//!
//! Сворачивание и разворачивание НЕ трогают стиль чужого окна (в Python-версии окно делалось прозрачным
//! на время анимации — у Photoshop и других программ, рисующих видеокартой, это ломало отрисовку:
//! чёрная миниатюра, полупрозрачное окно, «не разворачивается»). Теперь:
//!   • свернуть: поверх окна сразу кладётся его живая миниатюра, окно сворачивается, миниатюра «втекает» в иконку;
//!   • развернуть: миниатюра свёрнутого окна «вытекает» из иконки на место окна, и только в конце окно
//!     разворачивается — ровно под последним кадром; кадр убирается, когда окно уже нарисовалось.
//! Системная анимация сворачивания Windows на время работы MiniDock выключается (и возвращается при выходе),
//! чтобы она не играла поверх нашей.
//! Только для открытия программы из дока окно на долю секунды делается прозрачным — с ожиданием не дольше
//! 80 мс в фоновом потоке, без программ, рисующих видеокартой, и с проверкой раз в секунду, что окно вернулось.

use std::collections::{HashMap, HashSet};
use std::sync::mpsc;
use std::time::Duration;

use windows::Win32::Foundation::{HWND, RECT};
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::config::Config;
use crate::gfx::*;
use crate::win::{self, dwm::Thumb, sys, Surface};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Mode {
    Genie,
    Scale,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Flow {
    Down,
    Left,
    Right,
}

impl Flow {
    fn from(cfg: &Config) -> Flow {
        match cfg.flow() {
            "left" => Flow::Left,
            "right" => Flow::Right,
            _ => Flow::Down,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Done {
    Nothing,
    /// в конце развернуть окно (оно всё время анимации оставалось свёрнутым)
    Restore,
    /// вернуть видимость окну запущенной программы и вывести вперёд
    Unhide,
}

struct Anim {
    hwnd: isize,
    thumbs: Vec<Thumb>,
    src: [f32; 4],
    dst: [f32; 4],
    source: RECT,
    reverse: bool,
    flow: Flow,
    mode: Mode,
    start: f64,
    dur: f64,
    held: bool,
    held_since: f64,
    done: Done,
    finished: bool,
    release_at: Option<f64>,
}

/// (l, t, r, b) → (a0, f0, a1, f1): f растёт в сторону дока, a — поперёк.
fn to_flow(r: [f32; 4], flow: Flow) -> [f32; 4] {
    let [l, t, rr, b] = r;
    match flow {
        Flow::Down => [l, t, rr, b],
        Flow::Right => [t, l, b, rr],
        Flow::Left => [t, -rr, b, -l],
    }
}

fn from_flow(v: [f32; 4], flow: Flow) -> [f32; 4] {
    let [a0, f0, a1, f1] = v;
    match flow {
        Flow::Down => [a0, f0, a1, f1],
        Flow::Right => [f0, a0, f1, a1],
        Flow::Left => [-f1, a0, -f0, a1],
    }
}

fn rf(r: RECT) -> [f32; 4] {
    [r.left as f32, r.top as f32, r.right as f32, r.bottom as f32]
}

struct Launch {
    exe: String,
    icon: RECT,
    deadline: f64,
}

/// Снимок окна для анимации закрытия (только одного — последнего активного: снимки тяжёлые).
struct Snap {
    hwnd: isize,
    img: Image,
    rect: RECT,
    at: f64,
}

struct Closing {
    img: Image,
    rect: RECT,
    start: f64,
    dur: f64,
}

/// Программы, которые рисуют окно видеокартой: прозрачный слой ломает им отрисовку.
const NO_FADE_EXES: [&str; 11] = [
    "photoshop.exe",
    "illustrator.exe",
    "afterfx.exe",
    "adobe premiere pro.exe",
    "indesign.exe",
    "lightroom.exe",
    "adobe media encoder.exe",
    "animate.exe",
    "photoshop elements.exe",
    "blender.exe",
    "unity.exe",
];

pub struct Animator {
    overlay: HWND,
    overlay_shown: bool,
    origin: (i32, i32),
    anims: Vec<Anim>,
    /// окна запущенных программ, спрятанные до анимации появления: hwnd → исходный exstyle
    pub hidden: HashMap<isize, u32>,
    launches: Vec<Launch>,
    pub geo: HashMap<isize, (RECT, [i32; 4])>,
    snap: Option<Snap>,
    closer_hwnd: HWND,
    closer_surf: Surface,
    closing: Vec<Closing>,
    closer_rect: RECT,
    slow_exes: HashSet<String>,
    tries: HashMap<isize, u32>,
    pub stats_unhid: u32,
    sysanim_orig: Option<bool>,
}

impl Animator {
    pub fn new() -> Animator {
        let overlay = unsafe {
            CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
                win::OVERLAY_CLASS,
                windows::core::w!("MiniDockGenie"),
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
            let _ = SetLayeredWindowAttributes(overlay, windows::Win32::Foundation::COLORREF(0), 255, LWA_ALPHA);
        }
        win::dwm::extend_frame(overlay);
        win::dwm::exclude_from_peek(overlay);
        win::dwm::disable_transitions(overlay);
        let closer_hwnd = win::create_layered("MiniDockClose", true, true);
        Animator {
            overlay,
            overlay_shown: false,
            origin: (0, 0),
            anims: Vec::new(),
            hidden: HashMap::new(),
            launches: Vec::new(),
            geo: HashMap::new(),
            snap: None,
            closer_hwnd,
            closer_surf: Surface::default(),
            closing: Vec::new(),
            closer_rect: RECT::default(),
            slow_exes: HashSet::new(),
            tries: HashMap::new(),
            stats_unhid: 0,
            sysanim_orig: None,
        }
    }

    pub fn is_animating(&self) -> bool {
        !self.anims.is_empty() || !self.closing.is_empty()
    }

    pub fn busy(&self, hwnd: isize) -> bool {
        self.anims.iter().any(|a| a.hwnd == hwnd)
    }

    pub fn memory(&self) -> usize {
        self.snap.as_ref().map_or(0, |s| s.img.bytes()) + self.closer_surf.bytes() + self.closing.iter().map(|c| c.img.bytes()).sum::<usize>()
    }

    // -- системная анимация сворачивания Windows
    /// Пока работают наши анимации, системная (окно «улетает» в панель задач) выключена.
    pub fn apply_system_animation(&mut self, cfg: &Config) {
        let cur = system_min_animation();
        if cfg.animations {
            if cur {
                if self.sysanim_orig.is_none() {
                    self.sysanim_orig = Some(true);
                }
                set_system_min_animation(false);
            }
        } else if let Some(orig) = self.sysanim_orig.take() {
            set_system_min_animation(orig);
        }
    }

    pub fn restore_system_animation(&mut self) {
        if let Some(orig) = self.sysanim_orig.take() {
            set_system_min_animation(orig);
        }
    }

    pub fn sysanim_orig(&self) -> Option<bool> {
        self.sysanim_orig
    }

    pub fn set_sysanim_orig(&mut self, v: Option<bool>) {
        if self.sysanim_orig.is_none() {
            self.sysanim_orig = v;
        }
    }

    // -- подложка для миниатюр: одно окно на весь виртуальный экран
    fn ensure_overlay(&mut self) {
        let (l, t) = unsafe { (GetSystemMetrics(SM_XVIRTUALSCREEN), GetSystemMetrics(SM_YVIRTUALSCREEN)) };
        let (w, h) = unsafe { (GetSystemMetrics(SM_CXVIRTUALSCREEN), GetSystemMetrics(SM_CYVIRTUALSCREEN)) };
        if !self.overlay_shown || self.origin != (l, t) {
            self.origin = (l, t);
            unsafe {
                let _ = SetWindowPos(self.overlay, Some(HWND_TOPMOST), l, t, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW);
            }
            self.overlay_shown = true;
        }
    }

    fn hide_overlay(&mut self) {
        if self.overlay_shown {
            self.overlay_shown = false;
            win::hide(self.overlay);
        }
    }

    /// Начать анимацию окна hwnd: src — рамка окна, dst — иконка (физические пиксели).
    fn play(&mut self, hwnd: isize, src: RECT, dst: RECT, insets: [i32; 4], reverse: bool, dur: f32, cfg: &Config, mode: Mode, done: Done, held: bool) -> bool {
        if self.busy(hwnd) {
            return false;
        }
        let flow = Flow::from(cfg);
        let f = to_flow(rf(src), flow);
        let n = if mode == Mode::Scale { 1 } else { ((f[3] - f[1]) / 5.0).clamp(40.0, 180.0) as usize };
        self.ensure_overlay();
        let mut thumbs = Vec::with_capacity(n);
        for _ in 0..n {
            match Thumb::new(self.overlay, win::hwnd_of(hwnd)) {
                Some(t) => thumbs.push(t),
                None => return false,
            }
        }
        let (sw, sh) = thumbs[0].source_size();
        if sw <= 0 || sh <= 0 {
            return false;
        }
        let [il, it, ir, ib] = if sw <= insets[0] + insets[2] + 8 || sh <= insets[1] + insets[3] + 8 { [0; 4] } else { insets };
        let source = RECT { left: il, top: it, right: (sw - ir).max(il + 1), bottom: (sh - ib).max(it + 1) };
        let now = crate::anim::now();
        let a = Anim {
            hwnd,
            thumbs,
            src: rf(src),
            dst: rf(dst),
            source,
            reverse,
            flow,
            mode,
            start: now,
            dur: dur.max(0.05) as f64,
            held,
            held_since: now,
            done,
            finished: false,
            release_at: None,
        };
        self.render(&a, if reverse { 1.0 } else { 0.0 });
        self.anims.push(a);
        true
    }

    fn render(&self, a: &Anim, g: f32) {
        let (ox, oy) = (self.origin.0 as f32, self.origin.1 as f32);
        let s = [a.src[0] - ox, a.src[1] - oy, a.src[2] - ox, a.src[3] - oy];
        let t = [a.dst[0] - ox, a.dst[1] - oy, a.dst[2] - ox, a.dst[3] - oy];
        let src = a.source;
        if a.mode == Mode::Scale {
            let k = ease(g);
            let r = [lerp(s[0], t[0], k), lerp(s[1], t[1], k), lerp(s[2], t[2], k), lerp(s[3], t[3], k)];
            let dst = RECT { left: r[0].round() as i32, top: r[1].round() as i32, right: (r[2].round() as i32).max(r[0].round() as i32 + 1), bottom: (r[3].round() as i32).max(r[1].round() as i32 + 1) };
            let op = if g > 0.65 { (255.0 * clamp01((1.0 - g) / 0.35)) as u8 } else { 255 };
            a.thumbs[0].update(dst, Some(src), op, true, false);
            return;
        }
        let flow = a.flow;
        let [sa0, sf0, sa1, sf1] = to_flow(s, flow);
        let [ta0, tf0, ta1, tf1] = to_flow(t, flow);
        let (sw, sh) = ((src.right - src.left) as f32, (src.bottom - src.top) as f32);
        let n = a.thumbs.len();
        let ph1 = ease(clamp01(g / 0.4)); // фаза 1: край окна у дока сужается к иконке
        let ph2 = ease(clamp01((g - 0.25) / 0.75)); // фаза 2: окно «стекает» в иконку
        let span = if (tf0 - sf0).abs() > 1.0 { tf0 - sf0 } else { 1.0 };
        let opacity = if g > 0.88 { (255.0 * clamp01((1.0 - g) / 0.12)) as u8 } else { 255 };
        let f_at = |v: f32| {
            let bv = (ph2 + 0.7 * v * ph2 * (1.0 - ph2)).min(1.0);
            lerp(sf0 + v * (sf1 - sf0), tf0 + v * (tf1 - tf0), bv)
        };
        for (i, th) in a.thumbs.iter().enumerate() {
            let (v0, v1) = (i as f32 / n as f32, (i + 1) as f32 / n as f32);
            let (y0, y1) = (f_at(v0), f_at(v1));
            let k = ease(clamp01(((y0 + y1) / 2.0 - sf0) / span));
            let left = lerp(sa0, lerp(sa0, ta0, k), ph1);
            let right = lerp(sa1, lerp(sa1, ta1, k), ph1);
            let rs = match flow {
                Flow::Down => RECT { left: src.left, top: src.top + (v0 * sh).round() as i32, right: src.right, bottom: src.top + (v1 * sh).round() as i32 },
                Flow::Right => RECT { left: src.left + (v0 * sw).round() as i32, top: src.top, right: src.left + (v1 * sw).round() as i32, bottom: src.bottom },
                Flow::Left => RECT { left: src.right - (v1 * sw).round() as i32, top: src.top, right: src.right - (v0 * sw).round() as i32, bottom: src.bottom },
            };
            let r = from_flow([left.round(), y0.round(), right.round().max(left.round() + 1.0), y1.round().max(y0.round() + 1.0)], flow);
            let dst = RECT { left: r[0] as i32, top: r[1] as i32, right: r[2] as i32, bottom: r[3] as i32 };
            th.update(dst, Some(rs), opacity, right - left >= 0.5, false);
        }
    }

    /// Кадр. true — ещё есть анимации.
    pub fn tick(&mut self) -> bool {
        let now = crate::anim::now();
        let mut i = 0;
        let mut restore: Vec<isize> = Vec::new();
        let mut unhide: Vec<isize> = Vec::new();
        while i < self.anims.len() {
            let a = &mut self.anims[i];
            if let Some(at) = a.release_at {
                if now >= at {
                    self.anims.remove(i);
                    continue;
                }
                i += 1;
                continue;
            }
            if a.held {
                // стоп-кадр поверх окна, которое вот-вот свернётся; не свернулось за 1,5 с — убрать
                if now - a.held_since > 1.5 {
                    self.anims.remove(i);
                    continue;
                }
                i += 1;
                continue;
            }
            let t = ((now - a.start) / a.dur).clamp(0.0, 1.0) as f32;
            let g = if a.reverse { 1.0 - t } else { t };
            let a = &self.anims[i];
            self.render(a, g);
            let a = &mut self.anims[i];
            if t >= 1.0 && !a.finished {
                a.finished = true;
                match a.done {
                    Done::Restore => {
                        restore.push(a.hwnd);
                        // держим последний кадр, пока окно не развернётся и не нарисуется (событие MINIMIZEEND)
                        a.release_at = Some(now + 1.5);
                    }
                    Done::Unhide => {
                        unhide.push(a.hwnd);
                        a.release_at = Some(now + 0.05);
                    }
                    Done::Nothing => a.release_at = Some(now + if a.reverse { 0.04 } else { 0.0 }),
                }
            }
            i += 1;
        }
        for h in restore {
            let w = win::hwnd_of(h);
            unsafe {
                let _ = ShowWindowAsync(w, SW_RESTORE);
            }
            sys::force_foreground(w);
        }
        for h in unhide {
            self.unhide(h);
            sys::force_foreground(win::hwnd_of(h));
        }
        if self.anims.is_empty() {
            self.hide_overlay();
        }
        let closing = self.tick_closing(now);
        !self.anims.is_empty() || closing
    }

    // -- свернуть
    /// Окно вот-вот свернётся: его живая миниатюра уже лежит поверх, стартуем по событию.
    fn hold(&mut self, cfg: &Config, hwnd: isize, icon: RECT) -> bool {
        let w = win::hwnd_of(hwnd);
        let Some(g) = sys::window_geometry(w) else { return false };
        self.geo.insert(hwnd, g);
        self.play(hwnd, g.0, icon, g.1, false, cfg.anim_dur(0.5), cfg, mode_of(cfg), Done::Nothing, true)
    }

    /// Свернуть окно самим (клик в доке, меню панели).
    pub fn minimize(&mut self, cfg: &Config, hwnd: isize, icon: RECT, fullscreen: bool) {
        if cfg.animations && !fullscreen && !sys::is_iconic(win::hwnd_of(hwnd)) {
            self.take_snapshot(hwnd); // для анимации закрытия, если окно потом закроют из свёрнутого
            self.hold(cfg, hwnd, icon);
        }
        crate::app::dwm_flush_then(move || sys::syscommand(win::hwnd_of(hwnd), SC_MINIMIZE));
    }

    pub fn on_minimize_start(&mut self, cfg: &Config, hwnd: isize, icon: RECT, fullscreen: bool) {
        if let Some(a) = self.anims.iter_mut().find(|a| a.hwnd == hwnd && a.held) {
            a.held = false;
            a.start = crate::anim::now();
            return;
        }
        if !cfg.animations || fullscreen || self.hidden.contains_key(&hwnd) {
            return;
        }
        let w = win::hwnd_of(hwnd);
        if !sys::is_app_window(w, false) {
            return;
        }
        let g = sys::window_geometry(w).or_else(|| self.geo.get(&hwnd).copied()).or_else(|| sys::restored_frame(w));
        if let Some(g) = g {
            self.play(hwnd, g.0, icon, g.1, false, cfg.anim_dur(0.5), cfg, mode_of(cfg), Done::Nothing, false);
        }
    }

    /// Кнопка мыши нажата в окне: если это кнопки в правом верхнем углу — окно сейчас свернётся или закроется.
    pub fn on_press(&mut self, cfg: &Config, hwnd: isize, icon: impl Fn(isize) -> RECT) {
        if !cfg.animations {
            return;
        }
        let root = sys::root(win::hwnd_of(hwnd));
        if root.is_invalid() {
            return;
        }
        let Some((f, _)) = sys::window_geometry(root) else { return };
        let (x, y) = win::cursor_pos();
        if !(f.right - 220 <= x && x <= f.right && f.top <= y && y <= f.top + 60) {
            return;
        }
        let rk = root.0 as isize;
        let hit = unsafe {
            let mut res = 0usize;
            let lp = ((y as u32 & 0xFFFF) << 16 | (x as u32 & 0xFFFF)) as isize;
            if SendMessageTimeoutW(root, WM_NCHITTEST, windows::Win32::Foundation::WPARAM(0), windows::Win32::Foundation::LPARAM(lp), SMTO_ABORTIFHUNG, 60, Some(&mut res)).0 != 0 {
                res as u32
            } else {
                0
            }
        };
        self.take_snapshot(rk); // закроют — анимация закрытия по свежему кадру
        if hit == HTMINBUTTON {
            let ic = icon(rk);
            self.hold(cfg, rk, ic);
        }
    }

    /// Кнопку отпустили, а окно не свернулось (увели мышь с кнопки) — убрать стоп-кадр.
    pub fn drop_held(&mut self, hwnd: isize) {
        self.anims.retain(|a| !(a.hwnd == hwnd && a.held));
    }

    pub fn held_any(&self) -> Option<isize> {
        self.anims.iter().find(|a| a.held).map(|a| a.hwnd)
    }

    // -- развернуть
    /// Развернуть окно с анимацией из icon (физические пиксели). Окно разворачивается в конце анимации.
    pub fn restore(&mut self, cfg: &Config, hwnd: isize, icon: RECT, fullscreen: bool) {
        let w = win::hwnd_of(hwnd);
        if !cfg.animations || fullscreen || !sys::is_iconic(w) || self.busy(hwnd) {
            sys::force_foreground(w);
            return;
        }
        let g = sys::restored_frame(w).map(|g| {
            // известная рамка (окно двигали до сворачивания) точнее, чем «нормальное положение» Windows
            match self.geo.get(&hwnd) {
                Some(k) if !sys::is_zoomed(w) => *k,
                _ => g,
            }
        });
        let Some(g) = g else {
            sys::force_foreground(w);
            return;
        };
        if !self.play(hwnd, g.0, icon, g.1, true, cfg.anim_dur(0.45), cfg, mode_of(cfg), Done::Restore, false) {
            sys::force_foreground(w);
        }
    }

    /// Окно развернулось (наше или чужое разворачивание).
    pub fn on_minimize_end(&mut self, hwnd: isize) {
        let now = crate::anim::now();
        for a in &mut self.anims {
            if a.hwnd == hwnd && a.finished && a.done == Done::Restore {
                // окно уже под последним кадром — пару кадров на отрисовку, и убираем
                a.release_at = Some(now + 0.06);
            }
        }
    }

    // -- запуск программы из дока: окно вылетает из иконки
    pub fn expect_launch(&mut self, cfg: &Config, exe: &str, icon: RECT) {
        if cfg.animations && cfg.open_animation && !exe.is_empty() {
            self.launches.push(Launch { exe: exe.to_lowercase(), icon, deadline: crate::anim::now() + 15.0 });
        }
    }

    pub fn wants_create(&mut self) -> bool {
        let now = crate::anim::now();
        self.launches.retain(|l| l.deadline > now);
        !self.launches.is_empty()
    }

    fn launch_for(&mut self, hwnd: isize) -> Option<usize> {
        let now = crate::anim::now();
        self.launches.retain(|l| l.deadline > now);
        if self.launches.is_empty() {
            return None;
        }
        let exe = sys::window_exe(win::hwnd_of(hwnd));
        let sysroot = sys::system_root();
        self.launches.iter().position(|l| {
            let folder = format!("{}\\", sys::parent_dir(&l.exe));
            exe == l.exe || (folder.len() > 3 && !folder.starts_with(&sysroot) && exe.starts_with(&folder))
        })
    }

    /// Создано окно: если это верхнее окно запускаемой из дока программы — сразу делаем его прозрачным.
    pub fn on_create(&mut self, hwnd: isize) {
        if self.launches.is_empty() || self.hidden.contains_key(&hwnd) {
            return;
        }
        let w = win::hwnd_of(hwnd);
        if sys::root(w) != w || !sys::owner(w).is_invalid() || sys::is_visible(w) {
            return;
        }
        let ex = sys::exstyle(w);
        if (ex & WS_EX_TOOLWINDOW.0 != 0 && ex & WS_EX_APPWINDOW.0 == 0) || sys::style(w) & WS_CHILD.0 != 0 {
            return;
        }
        if self.launch_for(hwnd).is_some() {
            self.hide_for_animation(hwnd);
        }
    }

    /// Окно показалось. Возвращает true, если нужен следующий кадр.
    pub fn on_show(&mut self, cfg: &Config, hwnd: isize) -> bool {
        if self.launches.is_empty() {
            return false;
        }
        let w = win::hwnd_of(hwnd);
        if sys::root(w) != w {
            return false;
        }
        if !sys::is_app_window(w, true) {
            if self.hidden.contains_key(&hwnd) {
                self.unhide(hwnd); // спрятали при создании, а это не окно программы — вернуть как было
            }
            return false;
        }
        let Some(i) = self.launch_for(hwnd) else { return false };
        let l = self.launches.remove(i);
        if !self.hide_for_animation(hwnd) {
            return false;
        }
        let Some(g) = sys::window_geometry(w) else {
            self.unhide(hwnd);
            return false;
        };
        if !self.play(hwnd, g.0, l.icon, g.1, true, cfg.anim_dur(0.38), cfg, Mode::Scale, Done::Unhide, false) {
            self.unhide(hwnd);
            return false;
        }
        true
    }

    fn hide_for_animation(&mut self, hwnd: isize) -> bool {
        if self.hidden.contains_key(&hwnd) {
            return true;
        }
        match self.make_invisible(hwnd) {
            Some(ex) => {
                self.hidden.insert(hwnd, ex);
                true
            }
            None => false,
        }
    }

    pub fn unhide(&mut self, hwnd: isize) {
        if let Some(ex) = self.hidden.remove(&hwnd) {
            make_visible_async(hwnd, ex);
        }
    }

    /// Сделать чужое окно прозрачным (только для анимации запуска). None — нельзя/не успели.
    fn make_invisible(&mut self, hwnd: isize) -> Option<u32> {
        let w = win::hwnd_of(hwnd);
        let ex = sys::exstyle(w);
        if ex & WS_EX_LAYERED.0 != 0 {
            return None;
        }
        let exe = sys::file_name(&sys::window_exe(w)).to_lowercase();
        if NO_FADE_EXES.contains(&exe.as_str()) || self.slow_exes.contains(&exe) || !sys::responds(w, 30) {
            return None;
        }
        // смена стиля — синхронный вызов в поток программы: ждём не дольше 80 мс в фоновом потоке
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let w = win::hwnd_of(hwnd);
            unsafe {
                SetWindowLongPtrW(w, GWL_EXSTYLE, (ex | WS_EX_LAYERED.0) as isize);
                let _ = SetLayeredWindowAttributes(w, windows::Win32::Foundation::COLORREF(0), 0, LWA_ALPHA);
            }
            let _ = tx.send(());
        });
        if rx.recv_timeout(Duration::from_millis(80)).is_err() {
            self.slow_exes.insert(exe.clone());
            crate::log::line(&format!("{exe} не ответил вовремя — его окна больше не прячем для анимаций"));
            // если стиль всё-таки применится позже — ensure_visible вернёт окно (оно в списке)
            self.hidden.insert(hwnd, ex);
            let h = hwnd;
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(1500));
                make_visible_blocking(h, ex);
            });
            self.hidden.remove(&hwnd);
            return None;
        }
        if is_transparent(w) {
            Some(ex)
        } else {
            make_visible_async(hwnd, ex);
            None
        }
    }

    /// Раз в секунду: окна, которые мы прятали, но так и не вернули, — вернуть.
    pub fn ensure_visible(&mut self) -> u32 {
        let busy: HashSet<isize> = self.anims.iter().map(|a| a.hwnd).collect();
        let mut fixed = 0;
        let now = crate::anim::now();
        let _ = now;
        let keys: Vec<isize> = self.hidden.keys().copied().collect();
        for h in keys {
            let w = win::hwnd_of(h);
            if !sys::is_window(w) {
                self.hidden.remove(&h);
                continue;
            }
            if busy.contains(&h) {
                continue;
            }
            // в списке дольше анимации — значит, «потерялось»
            let t = self.tries.entry(h).or_insert(0);
            *t += 1;
            if *t >= 3 {
                if let Some(ex) = self.hidden.remove(&h) {
                    make_visible_async(h, ex);
                    fixed += 1;
                }
                self.tries.remove(&h);
            }
        }
        self.tries.retain(|h, _| self.hidden.contains_key(h));
        self.stats_unhid += fixed;
        fixed
    }

    // -- закрытие
    /// Кадр активного окна для анимации закрытия (один, уменьшенный — снимки тяжёлые).
    pub fn take_snapshot(&mut self, hwnd: isize) {
        let w = win::hwnd_of(hwnd);
        if !sys::is_app_window(w, true) || sys::is_iconic(w) {
            return;
        }
        if let Some(g) = sys::window_geometry(w) {
            self.geo.insert(hwnd, g);
        }
        if let Some((img, rect)) = crate::capture::window_image(w, 960) {
            self.snap = Some(Snap { hwnd, img, rect, at: crate::anim::now() });
        }
    }

    pub fn on_hide(&mut self, cfg: &Config, hwnd: isize) -> bool {
        if !cfg.animations || !cfg.close_animation || self.hidden.contains_key(&hwnd) {
            return false;
        }
        let Some(s) = self.snap.as_ref() else { return false };
        if s.hwnd != hwnd || crate::anim::now() - s.at > 600.0 {
            return false;
        }
        let w = win::hwnd_of(hwnd);
        if sys::is_window(w) && (sys::is_visible(w) || sys::is_iconic(w)) {
            return false;
        }
        let s = self.snap.take().unwrap();
        self.closing.push(Closing { img: s.img, rect: s.rect, start: crate::anim::now(), dur: cfg.anim_dur(0.22) as f64 });
        true
    }

    fn tick_closing(&mut self, now: f64) -> bool {
        self.closing.retain(|c| now - c.start < c.dur);
        if self.closing.is_empty() {
            if self.closer_surf.is_allocated() {
                win::hide(self.closer_hwnd);
                self.closer_surf.free();
            }
            return false;
        }
        // слой закрытия — по размеру окна (с запасом), а не на весь экран
        let mut u = self.closing[0].rect;
        for c in &self.closing[1..] {
            u = RECT { left: u.left.min(c.rect.left), top: u.top.min(c.rect.top), right: u.right.max(c.rect.right), bottom: u.bottom.max(c.rect.bottom) };
        }
        let pad = 20;
        let r = RECT { left: u.left - pad, top: u.top - pad, right: u.right + pad, bottom: u.bottom + pad + 12 };
        let (w, h) = (r.right - r.left, r.bottom - r.top);
        if !self.closer_surf.ensure(w, h) {
            return false;
        }
        let items: Vec<(f32, R)> = self
            .closing
            .iter()
            .map(|c| {
                let k = ease(((now - c.start) / c.dur) as f32);
                let src = R::new((c.rect.left - r.left) as f32, (c.rect.top - r.top) as f32, (c.rect.right - c.rect.left) as f32, (c.rect.bottom - c.rect.top) as f32);
                let s = 1.0 - 0.12 * k;
                let dst = R::centered(src.cx(), src.cy() + 12.0 * k, src.w * s, src.h * s);
                (1.0 - k, dst)
            })
            .collect();
        {
            let mut cv = self.closer_surf.canvas();
            cv.clear();
            for (c, (alpha, dst)) in self.closing.iter().zip(items) {
                cv.image_alpha(&c.img, dst, alpha);
            }
        }
        self.closer_surf.present(self.closer_hwnd, r.left, r.top, 255);
        if self.closer_rect != r || !win::is_visible(self.closer_hwnd) {
            self.closer_rect = r;
            win::show_noactivate(self.closer_hwnd);
            win::set_topmost(self.closer_hwnd);
        }
        true
    }

    pub fn forget(&mut self, alive: &HashSet<isize>) {
        self.geo.retain(|h, _| alive.contains(h) || sys::is_window(win::hwnd_of(*h)));
        if let Some(s) = &self.snap {
            if !sys::is_window(win::hwnd_of(s.hwnd)) && crate::anim::now() - s.at > 5.0 {
                self.snap = None;
            }
        }
    }

    pub fn remember_geometry(&mut self, wins: &[(String, Vec<HWND>)]) {
        for (_, hs) in wins {
            for h in hs {
                let k = h.0 as isize;
                if !self.hidden.contains_key(&k) && !self.busy(k) {
                    if let Some(g) = sys::window_geometry(*h) {
                        self.geo.insert(k, g);
                    }
                }
            }
        }
    }

    /// Выход: всё вернуть как было.
    pub fn cleanup(&mut self) {
        for (h, ex) in std::mem::take(&mut self.hidden) {
            make_visible_blocking(h, ex);
        }
        self.anims.clear();
        self.hide_overlay();
        self.restore_system_animation();
    }
}

fn mode_of(cfg: &Config) -> Mode {
    if cfg.minimize_effect == "scale" {
        Mode::Scale
    } else {
        Mode::Genie
    }
}

fn is_transparent(w: HWND) -> bool {
    unsafe {
        if sys::exstyle(w) & WS_EX_LAYERED.0 == 0 {
            return false;
        }
        let mut key = windows::Win32::Foundation::COLORREF(0);
        let mut alpha = 0u8;
        let mut flags = LAYERED_WINDOW_ATTRIBUTES_FLAGS(0);
        if GetLayeredWindowAttributes(w, Some(&mut key), Some(&mut alpha), Some(&mut flags)).is_err() {
            return false;
        }
        flags.0 & LWA_ALPHA.0 != 0 && alpha == 0
    }
}

fn make_visible_blocking(hwnd: isize, ex: u32) {
    let w = win::hwnd_of(hwnd);
    unsafe {
        let _ = SetLayeredWindowAttributes(w, windows::Win32::Foundation::COLORREF(0), 255, LWA_ALPHA);
        SetWindowLongPtrW(w, GWL_EXSTYLE, ex as isize);
        let _ = windows::Win32::Graphics::Gdi::RedrawWindow(
            Some(w),
            None,
            None,
            windows::Win32::Graphics::Gdi::RDW_INVALIDATE | windows::Win32::Graphics::Gdi::RDW_FRAME | windows::Win32::Graphics::Gdi::RDW_ALLCHILDREN,
        );
    }
}

/// Вернуть видимость в фоне: занятая программа не подвесит MiniDock.
fn make_visible_async(hwnd: isize, ex: u32) {
    std::thread::spawn(move || make_visible_blocking(hwnd, ex));
}

fn system_min_animation() -> bool {
    let mut ai = ANIMATIONINFO { cbSize: std::mem::size_of::<ANIMATIONINFO>() as u32, iMinAnimate: 0 };
    unsafe {
        let _ = SystemParametersInfoW(SPI_GETANIMATION, ai.cbSize, Some(&mut ai as *mut _ as *mut _), SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS(0));
    }
    ai.iMinAnimate != 0
}

fn set_system_min_animation(on: bool) {
    let mut ai = ANIMATIONINFO { cbSize: std::mem::size_of::<ANIMATIONINFO>() as u32, iMinAnimate: on as i32 };
    unsafe {
        let _ = SystemParametersInfoW(SPI_SETANIMATION, ai.cbSize, Some(&mut ai as *mut _ as *mut _), SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS(0));
    }
}

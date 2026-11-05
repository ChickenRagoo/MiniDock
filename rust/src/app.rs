//! Приложение: общее состояние, маршрутизация сообщений окон, кадры анимаций, опрос и события окон.
//!
//! Всё живёт в одном потоке интерфейса; состояние — в thread_local RefCell. Сообщения, пришедшие, пока
//! состояние занято (Windows иногда вызывает оконную процедуру изнутри наших же вызовов), уходят
//! в DefWindowProc; модальные вещи (диалог выбора файла) выполняются отложенными задачами.

use std::cell::RefCell;
use std::collections::{HashMap, HashSet, VecDeque};
use std::rc::Rc;

use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::animator::Animator;
use crate::config::Config;
use crate::dock::{self, Dock};
use crate::gfx::Image;
use crate::menu::{MenuItem, Menus, Owner};
use crate::win::{self, appbar, hooks, sys};

pub type Action = Rc<dyn Fn(&mut App)>;

thread_local! {
    static APP: RefCell<Option<App>> = const { RefCell::new(None) };
    static TASKS: RefCell<VecDeque<Box<dyn FnOnce()>>> = RefCell::new(VecDeque::new());
}

/// Доступ к приложению (None — оно сейчас занято выше по стеку или ещё не создано).
pub fn with<R>(f: impl FnOnce(&mut App) -> R) -> Option<R> {
    APP.with(|a| match a.try_borrow_mut() {
        Ok(mut g) => g.as_mut().map(f),
        Err(_) => None,
    })
}

/// Выполнить после выхода из текущего обработчика (без захваченного состояния).
pub fn defer(f: impl FnOnce() + 'static) {
    TASKS.with(|t| t.borrow_mut().push_back(Box::new(f)));
    let t = hooks::TARGET.load(std::sync::atomic::Ordering::Relaxed);
    win::post(win::hwnd_of(t), win::WM_APP_TASK, 0, 0);
}

/// В фоне: дождаться, пока DWM покажет текущий кадр (2 кадра), затем выполнить f.
pub fn dwm_flush_then(f: impl FnOnce() + Send + 'static) {
    std::thread::spawn(move || {
        win::dwm::flush();
        win::dwm::flush();
        f();
    });
}

fn run_tasks() {
    loop {
        let t = TASKS.with(|t| t.borrow_mut().pop_front());
        match t {
            Some(f) => f(),
            None => break,
        }
    }
    loop {
        let t = REMOTE.lock().unwrap().pop_front();
        match t {
            Some(f) => {
                with(f);
            }
            None => break,
        }
    }
}

type Remote = Box<dyn FnOnce(&mut App) + Send>;
static REMOTE: std::sync::Mutex<VecDeque<Remote>> = std::sync::Mutex::new(VecDeque::new());

/// Из фонового потока: выполнить f в главном потоке с доступом к приложению.
pub fn post_from_thread(f: Remote) {
    REMOTE.lock().unwrap().push_back(f);
    let t = hooks::TARGET.load(std::sync::atomic::Ordering::Relaxed);
    win::post(win::hwnd_of(t), win::WM_APP_TASK, 0, 0);
}

/// Работа в фоновом потоке, результат — в главный поток.
pub fn spawn_then<T: Send + 'static>(work: impl FnOnce() -> T + Send + 'static, done: impl FnOnce(&mut App, T) + Send + 'static) {
    std::thread::spawn(move || {
        let r = work();
        post_from_thread(Box::new(move |a| done(a, r)));
    });
}

/// Размытие под панелями: чистое нативное размытие DWM — как «стекло» Python-версии (Acrylic добавлял
/// шум и серую подложку, панели выглядели темнее и «грязнее»).
pub fn blur_kind(_cfg: &Config) -> win::dwm::Blur {
    win::dwm::Blur::Blur
}

const TIMER_POLL: usize = 1;
const TIMER_IDLE: usize = 2;
const TIMER_WINDOWS: usize = 3;
const TIMER_REPORT: usize = 4;
const TIMER_FAST: usize = 5;
const TIMER_CLOCK: usize = 6;

/// Часы обновляются ровно на границе секунды (а не когда-то в течение неё).
fn arm_clock(msg: HWND) {
    let ms = unsafe { windows::Win32::System::SystemInformation::GetLocalTime() }.wMilliseconds as u32;
    win::set_timer(msg, TIMER_CLOCK, 1000 - ms.min(999) + 5);
}

pub struct App {
    pub cfg: Config,
    pub msg: HWND,
    pub hooks: hooks::WinEventHooks,
    pub docks: Vec<Dock>,
    pub bars: Vec<crate::bar::MenuBar>,
    pub menus: Menus,
    pub preview: crate::preview::Preview,
    pub animator: Animator,
    pub launchpad: crate::launchpad::Launchpad,
    pub spotlight: crate::spotlight::Spotlight,
    pub switcher: crate::switcher::Switcher,
    pub folder: crate::folder::FolderPopup,
    pub clock: crate::bigclock::BigClock,
    pub settings: Option<crate::settings::Settings>,
    pub flyout: Option<crate::flyouts::Flyout>,
    pub flyout_closed_at: f64,
    pub flyout_closed_key: Option<String>,
    pub widgets: crate::widgets::Host,
    pub catalog: crate::catalog::Catalog,
    pub fullscreen: bool,
    pub taskbar_hidden: bool,
    pub mru: Vec<isize>,
    pub wins: Vec<(String, Vec<HWND>)>,
    save_at: Option<f64>,
    ticks: u64,
    fast_until: f64,
    placed_at: f64,
    peek_until: f64,
    pub stats: HashMap<&'static str, u32>,
    backgrounds: HashMap<String, Option<Rc<Image>>>,
    pub started: f64,
    pub quitting: bool,
}

impl App {
    pub fn new(msg: HWND) -> App {
        let mut cfg = Config::load();
        if cfg.pinned.is_none() {
            cfg.pinned = Some(crate::config::default_pinned());
        }
        cfg.save();
        let dock = Dock::new(&cfg, None);
        let mut app = App {
            hooks: hooks::WinEventHooks::install(),
            docks: vec![dock],
            bars: Vec::new(),
            menus: Menus::new(),
            preview: crate::preview::Preview::new(),
            animator: Animator::new(),
            launchpad: crate::launchpad::Launchpad::new(),
            spotlight: crate::spotlight::Spotlight::new(),
            switcher: crate::switcher::Switcher::new(),
            folder: crate::folder::FolderPopup::new(),
            clock: crate::bigclock::BigClock::new(),
            settings: None,
            flyout: None,
            flyout_closed_at: 0.0,
            flyout_closed_key: None,
            widgets: crate::widgets::Host::new(),
            catalog: crate::catalog::Catalog::new(),
            fullscreen: false,
            taskbar_hidden: false,
            mru: Vec::new(),
            wins: Vec::new(),
            save_at: None,
            ticks: 0,
            fast_until: 0.0,
            placed_at: 0.0,
            peek_until: 0.0,
            stats: HashMap::new(),
            backgrounds: HashMap::new(),
            started: crate::anim::now(),
            quitting: false,
            cfg,
            msg,
        };
        // настройки прошлого запуска: системная анимация сворачивания, которую мы выключили
        let orig = app.cfg.extra.get("sysanim_orig").and_then(|v| v.as_bool());
        app.animator.set_sysanim_orig(orig);
        app.bars.push(crate::bar::MenuBar::new(None));
        app.sync_hook_flags();
        app.apply_layout();
        unsafe {
            // тестовые команды (--cmd) и команды от программ с другими правами доходят до окна сообщений
            let _ = ChangeWindowMessageFilterEx(msg, WM_COPYDATA, MSGFLT_ALLOW, None);
        }
        win::set_timer(msg, TIMER_POLL, 1000);
        arm_clock(msg);
        win::set_timer(msg, TIMER_IDLE, 50);
        win::set_timer(msg, TIMER_REPORT, 3600 * 1000);
        app.poll();
        app.refresh_windows();
        let fg = sys::foreground();
        for b in &mut app.bars {
            b.on_foreground(fg);
        }
        app.request_frame();
        // каталог приложений для Launchpad и Spotlight — в фоне, чуть позже старта
        defer_ms(2500, || {
            with(|a| a.catalog.load());
        });
        crate::log::line(&format!("MiniDock {} (Rust) запущен", env!("CARGO_PKG_VERSION")));
        app
    }

    pub fn install(app: App) {
        APP.with(|a| *a.borrow_mut() = Some(app));
    }

    pub fn take() -> Option<App> {
        APP.with(|a| a.borrow_mut().take())
    }

    // -- настройки
    pub fn save_soon(&mut self) {
        self.save_at = Some(crate::anim::now() + 0.4);
    }

    pub fn set_cfg(&mut self, f: impl FnOnce(&mut Config)) {
        let before = self.cfg.clone();
        f(&mut self.cfg);
        if self.cfg != before {
            self.save_soon();
            self.sync_hook_flags();
            self.apply_layout();
        }
    }

    pub fn sync_hook_flags(&self) {
        use std::sync::atomic::Ordering::Relaxed;
        hooks::CUSTOM_SWITCHER.store(self.cfg.custom_switcher, Relaxed);
        hooks::SPOTLIGHT.store(self.cfg.spotlight, Relaxed);
        hooks::WIN_LAUNCHPAD.store(self.cfg.win_key_launchpad, Relaxed);
        hooks::CLOCK_HOTKEY.store(if self.cfg.big_clock { self.cfg.big_clock_hotkey } else { 0 }, Relaxed);
    }

    /// Своя картинка фона части интерфейса (настройка backgrounds).
    pub fn background(&self, part: &str) -> Option<(Rc<Image>, f32)> {
        let (path, op) = self.cfg.background(part)?;
        let img = self.backgrounds.get(&path)?.clone()?;
        Some((img, op))
    }

    fn load_backgrounds(&mut self) {
        let wanted: HashSet<String> = self.cfg.backgrounds.keys().filter_map(|k| self.cfg.background(k).map(|b| b.0)).collect();
        self.backgrounds.retain(|p, _| wanted.contains(p));
        for p in wanted {
            if !self.backgrounds.contains_key(&p) {
                let img = crate::imageio::load(&p, 2560).map(Rc::new);
                self.backgrounds.insert(p, img);
            }
        }
    }

    // -- раскладка
    pub fn bar_height(&self) -> f32 {
        if self.cfg.menubar {
            self.cfg.bar_height
        } else {
            0.0
        }
    }

    pub fn apply_layout(&mut self) {
        self.load_backgrounds();
        self.widgets.set_enabled(&self.cfg.bar_widgets);
        if self.cfg.replace_taskbar {
            let mut orig = self.cfg.taskbar_orig_state;
            if appbar::hide_taskbar(&mut orig) {
                self.cfg.taskbar_orig_state = orig;
                self.cfg.save();
            }
            self.taskbar_hidden = true;
        } else if self.taskbar_hidden || self.cfg.taskbar_orig_state.is_some() {
            let mut orig = self.cfg.taskbar_orig_state;
            appbar::restore_taskbar(&mut orig);
            self.cfg.taskbar_orig_state = orig;
            self.taskbar_hidden = false;
            self.save_soon();
        }
        self.place_bars();
        self.place_docks();
        self.apply_shadows();
        self.animator.apply_system_animation(&self.cfg);
        let orig = self.animator.sysanim_orig();
        let cur = self.cfg.extra.get("sysanim_orig").and_then(|v| v.as_bool());
        if orig != cur {
            match orig {
                Some(v) => self.cfg.extra.insert("sysanim_orig".into(), v.into()),
                None => self.cfg.extra.remove("sysanim_orig"),
            };
            self.save_soon();
        }
        self.forget_old_area();
        self.request_frame();
    }

    fn place_docks(&mut self) {
        let mons = sys::monitors();
        let wanted: Vec<Option<String>> = std::iter::once(None)
            .chain(mons.iter().filter(|m| !m.primary && self.cfg.dock_all_screens).map(|m| Some(m.name.clone())))
            .collect();
        let mut keep = Vec::new();
        for d in std::mem::take(&mut self.docks) {
            if wanted.contains(&d.home) {
                keep.push(d);
            } else {
                let mut d = d;
                d.release();
                d.hide();
                unsafe {
                    let _ = DestroyWindow(d.hwnd);
                }
            }
        }
        for w in &wanted {
            if !keep.iter().any(|d| &d.home == w) {
                keep.push(Dock::new(&self.cfg, w.clone()));
            }
        }
        keep.sort_by_key(|d| d.home.is_some());
        self.docks = keep;
        let bh = self.bar_height();
        let full = self.fullscreen;
        let infull = self.cfg.dock_in_fullscreen;
        for d in &mut self.docks {
            d.place(&self.cfg, bh);
            if !full || infull {
                d.show();
            }
        }
    }

    fn place_bars(&mut self) {
        let mons = sys::monitors();
        let wanted: Vec<Option<String>> = if self.cfg.menubar {
            std::iter::once(None)
                .chain(mons.iter().filter(|m| !m.primary && self.cfg.bar_all_screens).map(|m| Some(m.name.clone())))
                .collect()
        } else {
            Vec::new()
        };
        let mut keep = Vec::new();
        for b in std::mem::take(&mut self.bars) {
            if wanted.contains(&b.home) || (b.home.is_none() && !self.cfg.menubar) {
                keep.push(b);
            } else {
                let mut b = b;
                b.release();
                b.hide();
                b.destroy();
            }
        }
        for w in &wanted {
            if !keep.iter().any(|b| &b.home == w) {
                let mut b = crate::bar::MenuBar::new(w.clone());
                b.on_foreground(sys::foreground());
                keep.push(b);
            }
        }
        keep.sort_by_key(|b| b.home.is_some());
        self.bars = keep;
        let full = self.fullscreen;
        for b in &mut self.bars {
            if self.cfg.menubar && !full {
                b.place(&self.cfg);
                b.show();
            } else {
                b.hide();
                if !self.cfg.menubar {
                    b.release();
                }
            }
        }
    }

    fn apply_shadows(&mut self) {
        if self.cfg.shadows_orig.is_none() {
            self.cfg.shadows_orig = Some(sys::window_shadows());
            self.save_soon();
        }
        let want = self.cfg.window_shadows && self.cfg.shadows_orig == Some(true);
        if sys::window_shadows() != want {
            sys::set_window_shadows(want);
        }
    }

    /// Прежние версии сами задавали рабочую область — если панелей не зарегистрировано, вернуть обычную.
    fn forget_old_area(&self) {
        if !appbar::any_reserved() {
            let area = appbar::default_work_area();
            if sys::get_work_area() != area {
                sys::set_work_area(area);
            }
        }
    }

    fn check_appbars(&mut self) {
        let cfg = self.cfg.clone();
        let mut n = 0;
        for d in &mut self.docks {
            if d.reserved && appbar::lost(d.hwnd) {
                d.reserved = false;
                d.reserve(&cfg);
                n += 1;
            }
        }
        for b in &mut self.bars {
            if b.reserved && appbar::lost(b.hwnd) {
                b.reserved = false;
                b.reserve(&cfg);
                n += 1;
            }
        }
        *self.stats.entry("area").or_insert(0) += n;
    }

    /// Док и панель стоят у своего края и целиком на своём мониторе? (после игр со сменой разрешения)
    fn check_placement(&mut self) {
        let mut wrong = Vec::new();
        for d in &self.docks {
            if !d.visible {
                continue;
            }
            let m = sys::monitors().into_iter().find(|m| match &d.home {
                None => m.primary,
                Some(n) => &m.name == n,
            });
            let Some(m) = m else { continue };
            let r = win::window_rect(d.hwnd);
            let mr = m.rect;
            let tol = 3;
            let inside = r.left >= mr.left - tol && r.top >= mr.top - tol && r.right <= mr.right + tol && r.bottom <= mr.bottom + tol;
            let at_edge = match d.side {
                dock::Side::Bottom => mr.bottom - tol - 200 <= r.bottom && r.bottom <= mr.bottom + tol,
                dock::Side::Left => (r.left - mr.left).abs() <= tol,
                dock::Side::Right => (r.right - mr.right).abs() <= tol,
            };
            if !(inside && at_edge) || m.rect != d.mon.rect {
                wrong.push(format!("Dock {:?} на мониторе {:?}", (r.left, r.top, r.right, r.bottom), (mr.left, mr.top, mr.right, mr.bottom)));
            }
        }
        for b in &self.bars {
            if b.visible && b.misplaced() {
                wrong.push("MenuBar".to_string());
            }
        }
        let now = crate::anim::now();
        if !wrong.is_empty() && now - self.placed_at > 5.0 {
            self.placed_at = now;
            let n = self.stats.entry("placed").or_insert(0);
            *n += 1;
            if *n <= 3 {
                crate::log::line(&format!("не на месте, расставляю заново: {}", wrong.join("; ")));
            }
            self.apply_layout();
        }
    }

    // -- кадры
    pub fn request_frame(&mut self) {
        crate::anim::set_running(true);
    }

    fn frame(&mut self) {
        let t0 = std::time::Instant::now();
        self.frame_inner();
        let us = t0.elapsed().as_micros() as u64;
        thread_local! { static ST: std::cell::Cell<(u64, u64, u64)> = const { std::cell::Cell::new((0, 0, 0)) }; }
        ST.with(|c| {
            let (n, sum, max) = c.get();
            let (n, sum, max) = (n + 1, sum + us, max.max(us));
            if n >= 120 {
                crate::dbg_log(&format!("кадр: в среднем {} мкс, максимум {} мкс", sum / n, max));
                c.set((0, 0, 0));
            } else {
                c.set((n, sum, max));
            }
        });
    }

    fn frame_inner(&mut self) {
        crate::anim::frame_done();
        let mut active = false;
        let cfg = self.cfg.clone();
        for di in 0..self.docks.len() {
            if self.docks[di].visible && self.docks[di].animating {
                let ctx = dock::ctx_for(self, di);
                let d = &mut self.docks[di];
                d.tick(&cfg, &ctx);
                d.paint(&cfg, &ctx);
                active |= d.animating;
            }
        }
        let wtexts: Vec<(String, String)> = self.widgets.enabled().iter().map(|id| (id.clone(), self.widgets.bar_text(id))).collect();
        let bar_bg = self.background("menubar");
        for b in &mut self.bars {
            if b.animating() {
                active |= b.frame(&cfg, &wtexts, &bar_bg);
            }
        }
        active |= self.menus.tick();
        active |= self.animator.tick();
        active |= self.preview.frame(&cfg);
        if self.folder.frame(&cfg) {
            crate::folder::tick(self);
            active = true;
        }
        active |= crate::launchpad::tick(self);
        active |= crate::spotlight::tick(self);
        active |= crate::switcher::tick(self);
        active |= self.clock.frame(&cfg);
        active |= crate::settings::tick(self);
        crate::bar::flyouts_frame(self, &mut active);
        crate::anim::set_running(active);
    }

    // -- опрос раз в секунду
    fn poll(&mut self) {
        self.ticks += 1;
        let now = crate::anim::now();
        if let Some(t) = self.save_at {
            if now >= t {
                self.save_at = None;
                self.cfg.save();
            }
        }
        if self.cfg.replace_taskbar {
            if self.peek_until > 0.0 && now > self.peek_until {
                self.peek_until = 0.0;
            }
            if self.peek_until == 0.0 && appbar::taskbar_visible() {
                let mut orig = self.cfg.taskbar_orig_state;
                appbar::hide_taskbar(&mut orig);
                self.cfg.taskbar_orig_state = orig;
            }
        }
        if self.ticks % 5 == 0 {
            self.check_appbars();
        }
        let fixed = self.animator.ensure_visible();
        if fixed > 0 {
            *self.stats.entry("unhid").or_insert(0) += fixed;
        }
        if self.ticks % 2 == 0 && !self.fullscreen {
            self.check_placement();
        }
        self.check_fullscreen();
        // дополнительные мониторы: прятать док/панель, если игра на весь экран именно там
        if self.docks.len() > 1 || self.bars.len() > 1 {
            let game = sys::fullscreen_monitor();
            let infull = self.cfg.dock_in_fullscreen;
            for d in self.docks.iter_mut().skip(1) {
                let hide = game.as_ref().is_some_and(|g| Some(&g.name) == d.home.as_ref()) && !infull;
                if hide {
                    d.hide();
                } else {
                    d.show();
                }
            }
            for b in self.bars.iter_mut().skip(1) {
                let hide = game.as_ref().is_some_and(|g| Some(&g.name) == b.home.as_ref());
                if hide {
                    b.hide();
                } else if self.cfg.menubar {
                    b.show();
                }
            }
        }

        if self.ticks % 15 == 0 {
            // страховка: события окон могли потеряться
            self.refresh_windows();
        }
        if self.ticks == 60 {
            self.report();
        }
        self.request_frame();
    }

    fn check_fullscreen(&mut self) {
        let full = sys::foreground_is_fullscreen() && !self.launchpad.visible();
        if full || self.fullscreen {
            self.fast_until = crate::anim::now() + 2.0;
            win::set_timer(self.msg, TIMER_FAST, 50);
        }
        if full != self.fullscreen {
            self.fullscreen = full;
            let infull = self.cfg.dock_in_fullscreen;
            for d in &mut self.docks {
                if full && !infull {
                    d.hide();
                } else {
                    d.show();
                    if full {
                        // поверх полноэкранного: док сразу прячется у края и выезжает по наведению
                        d.hide_k = 1.0;
                        d.wake();
                    }
                }
            }
            for b in &mut self.bars {
                if full {
                    b.hide();
                } else if self.cfg.menubar {
                    b.show();
                }
            }
            if !full {
                // игра могла менять разрешение экрана — расставить док и панель заново
                let msg = self.msg;
                let _ = msg;
                defer_ms(300, || {
                    with(|a| a.apply_layout());
                });
                defer_ms(2500, || {
                    with(|a| a.apply_layout());
                });
            }
            self.request_frame();
        }
    }

    // -- окна программ
    pub fn refresh_windows(&mut self) {
        self.wins = sys::app_windows();
        let wins = self.wins.clone();
        for d in &mut self.docks {
            d.poll_windows(&wins);
        }
        self.animator.remember_geometry(&wins);
        let alive: HashSet<isize> = wins.iter().flat_map(|(_, h)| h.iter().map(|w| w.0 as isize)).collect();
        self.animator.forget(&alive);
        self.mru.retain(|h| alive.contains(h) || sys::is_window(win::hwnd_of(*h)));
        self.request_frame();
    }

    pub fn poll_now(&mut self) {
        self.refresh_windows();
    }

    fn windows_changed_soon(&mut self) {
        win::set_timer(self.msg, TIMER_WINDOWS, 60);
    }

    fn on_events(&mut self) {
        let evs = hooks::take_events();
        let cfg = self.cfg.clone();
        let mut need_frame = false;
        for e in evs {
            let h = win::hwnd_of(e.hwnd);
            match e.event {
                hooks::EVENT_FOREGROUND => {
                    self.touch_mru(e.hwnd);
                    for b in &mut self.bars {
                        b.on_foreground(h);
                    }
                    let k = e.hwnd;
                    defer_ms(400, move || {
                        with(|a| {
                            if a.cfg.animations && !a.fullscreen && sys::foreground().0 as isize == k {
                                a.animator.take_snapshot(k);
                            }
                        });
                    });
                    self.windows_changed_soon();
                    need_frame = true;
                }
                hooks::EVENT_CAPTURE => {
                    let docks = &self.docks;
                    let c = &self.cfg;
                    let icon = |hw: isize| icon_for_window(docks, c, hw);
                    self.animator.on_press(&cfg, e.hwnd, icon);
                    need_frame = true;
                }
                hooks::EVENT_MINSTART => {
                    let icon = icon_for_window(&self.docks, &self.cfg, e.hwnd);
                    let di = dock_index_for_window(&self.docks, e.hwnd);
                    self.docks[di].reveal(1.0);
                    self.animator.on_minimize_start(&cfg, e.hwnd, icon, self.fullscreen);
                    need_frame = true;
                }
                hooks::EVENT_MINEND => {
                    self.animator.on_minimize_end(e.hwnd);
                    need_frame = true;
                }
                hooks::EVENT_CREATE => self.animator.on_create(e.hwnd),
                hooks::EVENT_SHOW => {
                    need_frame |= self.animator.on_show(&cfg, e.hwnd);
                    if !self.animator.wants_create() {
                        self.hooks.want_create(false);
                    }
                    self.windows_changed_soon();
                }
                hooks::EVENT_HIDE => {
                    need_frame |= self.animator.on_hide(&cfg, e.hwnd);
                    self.windows_changed_soon();
                }
                hooks::EVENT_DESTROY | hooks::EVENT_CLOAKED | hooks::EVENT_UNCLOAKED => self.windows_changed_soon(),
                hooks::EVENT_NAMECHANGE => {
                    if sys::foreground() == h {
                        for b in &mut self.bars {
                            b.on_foreground(h);
                        }
                    }
                    if self.wins.iter().all(|(_, hs)| !hs.contains(&h)) && sys::is_app_window(h, true) {
                        self.windows_changed_soon();
                    }
                }
                hooks::EVENT_MOVESIZEEND => {
                    if let Some(g) = sys::window_geometry(h) {
                        self.animator.geo.insert(e.hwnd, g);
                    }
                }
                _ => {}
            }
        }
        if need_frame {
            self.request_frame();
        }
    }

    fn touch_mru(&mut self, h: isize) {
        let w = win::hwnd_of(h);
        if sys::root(w) != w {
            return;
        }
        self.mru.retain(|x| *x != h);
        self.mru.insert(0, h);
        if self.mru.len() > 200 {
            self.mru.truncate(150);
        }
    }

    /// Клик по иконке запущенной программы: развернуть / переключить окно / свернуть.
    pub fn focus_windows(&mut self, hwnds: &[isize], icon: RECT) {
        let fg = sys::foreground().0 as isize;
        let next;
        // свёрнутое окно Windows иногда оставляет «активным» — это не «уже впереди»
        if hwnds.contains(&fg) && !sys::is_iconic(win::hwnd_of(fg)) {
            if hwnds.len() == 1 {
                let cfg = self.cfg.clone();
                self.animator.minimize(&cfg, fg, icon, self.fullscreen);
                self.request_frame();
                return;
            }
            let i = hwnds.iter().position(|h| *h == fg).unwrap();
            next = hwnds[(i + 1) % hwnds.len()];
        } else {
            next = hwnds[0];
        }
        if sys::is_iconic(win::hwnd_of(next)) {
            let cfg = self.cfg.clone();
            self.animator.restore(&cfg, next, icon, self.fullscreen);
            self.request_frame();
        } else {
            sys::force_foreground(win::hwnd_of(next));
        }
    }

    pub fn minimize_window(&mut self, hwnd: isize) {
        let icon = icon_for_window(&self.docks, &self.cfg, hwnd);
        let cfg = self.cfg.clone();
        self.animator.minimize(&cfg, hwnd, icon, self.fullscreen);
        self.request_frame();
    }

    pub fn restore_window(&mut self, hwnd: isize) {
        let icon = icon_for_window(&self.docks, &self.cfg, hwnd);
        let cfg = self.cfg.clone();
        self.animator.restore(&cfg, hwnd, icon, self.fullscreen);
        self.request_frame();
    }

    pub fn dock_for_window(&self, hwnd: isize) -> usize {
        dock_index_for_window(&self.docks, hwnd)
    }

    // -- меню, папки, Launchpad, настройки
    pub fn show_menu(&mut self, items: Vec<MenuItem>, x: i32, y: i32, owner: Owner) {
        self.menus.open(items, x, y, owner);
        self.request_frame();
    }

    pub fn menu_closed(&mut self) {
        match self.menus.owner.take() {
            Some(Owner::Dock(di)) => {
                if let Some(d) = self.docks.get_mut(di) {
                    d.menu_open = self.folder.is_open_for(di);
                    d.wake();
                }
            }
            Some(Owner::Bar(bi)) => {
                if let Some(b) = self.bars.get_mut(bi) {
                    b.menu_closed();
                }
            }
            _ => {}
        }
        self.request_frame();
    }

    pub fn toggle_folder(&mut self, di: usize, id: u64) {
        if self.folder.is_open_for_item(di, id) {
            self.close_folder();
        } else {
            self.open_folder(di, id, false);
        }
    }

    pub fn open_folder(&mut self, di: usize, id: u64, edit: bool) {
        crate::folder::open(self, di, id, edit);
    }

    pub fn open_folder_later(&mut self, di: usize, id: u64, edit: bool) {
        defer_ms(250, move || {
            with(|a| a.open_folder(di, id, edit));
        });
    }

    pub fn close_folder(&mut self) {
        crate::folder::close(self);
    }

    pub fn widgets_changed(&mut self) {
        for b in &mut self.bars {
            b.refresh_volume();
        }
        self.request_frame();
    }

    pub fn close_preview(&mut self) {
        self.preview.close();
    }

    pub fn toggle_launchpad(&mut self) {
        crate::launchpad::toggle(self);
    }

    pub fn toggle_spotlight(&mut self) {
        crate::spotlight::toggle(self);
    }

    pub fn open_settings(&mut self) {
        crate::settings::open(self);
    }

    pub fn add_app_dialog(&mut self, di: usize) {
        if let Some(d) = self.docks.get_mut(di) {
            d.menu_open = true;
        }
        let owner = self.docks[di].hwnd.0 as isize;
        defer(move || {
            let desktop = format!("{}\\Desktop", std::env::var("USERPROFILE").unwrap_or_default());
            let _ = desktop;
            let files = crate::win::shell::open_files_dialog(win::hwnd_of(owner), "Добавить в док", "Программы и ярлыки", "*.exe;*.lnk;*.url", true);
            with(|a| {
                if let Some(d) = a.docks.get_mut(di) {
                    d.menu_open = false;
                }
                let mut changed = false;
                for f in files {
                    changed |= a.docks[di].pin_path(&f);
                }
                if changed {
                    dock::save_pinned(a, di);
                    a.poll_now();
                }
            });
        });
    }

    /// Временно показать панель задач Windows (повторный вызов — спрятать).
    pub fn peek_taskbar(&mut self) {
        if !self.cfg.replace_taskbar {
            return;
        }
        let mut orig = self.cfg.taskbar_orig_state;
        if self.peek_until > 0.0 {
            self.peek_until = 0.0;
            appbar::hide_taskbar(&mut orig);
        } else {
            self.peek_until = crate::anim::now() + 15.0;
            for h in appbar::taskbar_windows() {
                unsafe {
                    let _ = ShowWindow(h, SW_SHOW);
                }
            }
        }
    }

    pub fn quit(&mut self) {
        self.quitting = true;
        unsafe {
            PostQuitMessage(0);
        }
    }

    pub fn cleanup(&mut self) {
        self.animator.cleanup();
        hooks::stop_keyboard_hook();
        let mut orig = self.cfg.taskbar_orig_state;
        if self.taskbar_hidden || orig.is_some() {
            appbar::restore_taskbar(&mut orig);
            self.cfg.taskbar_orig_state = orig;
        }
        for d in &mut self.docks {
            d.release();
        }
        for b in &mut self.bars {
            b.release();
        }
        if let Some(orig) = self.cfg.shadows_orig {
            if sys::window_shadows() != orig {
                sys::set_window_shadows(orig);
            }
            self.cfg.shadows_orig = None;
        }
        self.cfg.extra.remove("sysanim_orig");
        self.cfg.save();
    }

    fn report(&mut self) {
        let (private, ws) = sys::process_memory();
        let mons: Vec<String> = sys::monitors().iter().map(|m| format!("{}x{}@{}", m.w(), m.h(), m.scale)).collect();
        let surf: usize = self.docks.iter().map(|d| d.memory()).sum::<usize>() + self.animator.memory();
        crate::log::raw(&format!(
            "[{}] MiniDock {} (Rust): экраны {}; память {} МБ (рабочий набор {} МБ), буферы и иконки {} МБ; панели регистрировали заново {} раз, док и панель расставляли заново {} раз, невидимые окна чинили {} раз\n",
            crate::log::stamp(false),
            env!("CARGO_PKG_VERSION"),
            mons.join(", "),
            private / 1048576,
            ws / 1048576,
            surf / 1048576,
            self.stats.get("area").unwrap_or(&0),
            self.stats.get("placed").unwrap_or(&0),
            self.stats.get("unhid").unwrap_or(&0),
        ));
    }

    // -- сообщения
    fn on_command(&mut self, i: usize) {
        match crate::COMMANDS.get(i).map(|c| c.0) {
            Some("--quit") => self.quit(),
            Some("--launchpad") => self.toggle_launchpad(),
            Some("--settings") => self.open_settings(),
            Some("--spotlight") => self.toggle_spotlight(),
            Some("--show") => {
                self.fullscreen = false;
                self.apply_layout();
                for d in &mut self.docks {
                    d.reveal(3.0);
                }
                crate::signal(crate::SHOW_ACK);
            }
            Some("--reveal") => {
                for d in &mut self.docks {
                    d.reveal(4.0);
                }
            }
            Some("--report") => self.report(),
            _ => {}
        }
        self.request_frame();
    }

    fn on_hook(&mut self, a: usize, arg: isize) {
        use hooks::Action::*;
        let Some(action) = hooks::Action::from(a) else { return };
        match action {
            SwitchBegin => crate::switcher::begin(self, arg as i32),
            SwitchStep => crate::switcher::step(self, arg as i32),
            SwitchCommit => crate::switcher::commit(self),
            SwitchArrow => crate::switcher::arrow(self, arg as u32),
            SwitchNextWin => crate::switcher::next_window(self),
            SwitchCancel => crate::switcher::cancel(self),
            SwitchQuit => crate::switcher::quit_selected(self),
            Launchpad => self.toggle_launchpad(),
            Spotlight => self.toggle_spotlight(),
            LaunchpadKey => {
                let vk = (arg & 0xFFFF) as u32;
                let ch = char::from_u32(((arg >> 16) & 0xFFFF_FFFF) as u32);
                crate::launchpad::feed_key(self, vk, ch.filter(|c| *c != '\0'));
            }
            ClockShow => crate::bigclock::show(self),
            ClockHide => crate::bigclock::hide(self),
            HotkeyRecorded => {
                if let Some(s) = self.settings.as_mut() {
                    s.hotkey_recorded(arg as u32);
                }
                if arg != 0 {
                    let code = arg as u32;
                    self.set_cfg(|c| c.big_clock_hotkey = code);
                }
            }
        }
        self.request_frame();
    }

    pub fn on_message(&mut self, hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> Option<LRESULT> {
        if hwnd == self.msg {
            match msg {
                win::WM_APP_FRAME => self.frame(),
                win::WM_APP_HOOK => self.on_hook(wp.0, lp.0),
                win::WM_APP_COMMAND => self.on_command(wp.0),
                WM_TIMER => match wp.0 {
                    TIMER_POLL => self.poll(),
                    TIMER_IDLE => self.idle(),
                    TIMER_WINDOWS => {
                        win::kill_timer(self.msg, TIMER_WINDOWS);
                        self.refresh_windows();
                    }
                    TIMER_REPORT => self.report(),
                    TIMER_CLOCK => {
                        arm_clock(self.msg);
                        let cfg = self.cfg.clone();
                        for b in &mut self.bars {
                            b.on_second(&cfg);
                        }
                        self.clock.on_second(&cfg);
                        self.request_frame();
                    }
                    TIMER_FAST => {
                        self.check_fullscreen();
                        if crate::anim::now() > self.fast_until {
                            win::kill_timer(self.msg, TIMER_FAST);
                        }
                    }
                    _ => {}
                },
                WM_COPYDATA => {
                    let cds = unsafe { &*(lp.0 as *const windows::Win32::System::DataExchange::COPYDATASTRUCT) };
                    if cds.dwData == 0x4D44 {
                        let text = unsafe { String::from_utf8_lossy(std::slice::from_raw_parts(cds.lpData as *const u8, cds.cbData as usize)).to_string() };
                        defer(move || crate::testcmd::run(&text));
                    }
                    return Some(LRESULT(1));
                }
                WM_DISPLAYCHANGE | WM_SETTINGCHANGE => {
                    defer_ms(500, || {
                        with(|a| a.apply_layout());
                    });
                }
                _ => return None,
            }
            return Some(LRESULT(0));
        }
        if msg == WM_LBUTTONDOWN {
            crate::dbg_log(&format!("on_message down {:?}: bars={:?} docks={:?} menus={:?}", hwnd.0, self.bars.iter().map(|b| b.hwnd.0).collect::<Vec<_>>(), self.docks.iter().map(|d| d.hwnd.0).collect::<Vec<_>>(), self.menus.hwnds().iter().map(|h| h.0).collect::<Vec<_>>()));
        }
        if crate::menu::on_message(self, hwnd, msg, wp.0, lp.0) {
            return Some(LRESULT(0));
        }
        if msg == WM_MOUSEACTIVATE {
            // наши панели не забирают фокус кликом (кроме окон с вводом текста — у них свои обработчики)
            if self.docks.iter().any(|d| d.hwnd == hwnd) || self.bars.iter().any(|b| b.hwnd == hwnd) {
                return Some(LRESULT(MA_NOACTIVATE as isize));
            }
        }
        if let Some(di) = self.docks.iter().position(|d| d.hwnd == hwnd) {
            match msg {
                WM_LBUTTONDOWN | WM_LBUTTONUP | WM_MOUSEMOVE | WM_RBUTTONUP => {
                    let (x, y) = win::lparam_xy(lp);
                    dock::on_mouse(self, di, msg, x, y);
                    return Some(LRESULT(0));
                }
                WM_DROPFILES => {
                    let files = drop_files(wp);
                    let mut changed = false;
                    for f in files {
                        changed |= self.docks[di].pin_path(&f);
                    }
                    if changed {
                        dock::save_pinned(self, di);
                        self.poll_now();
                    }
                    return Some(LRESULT(0));
                }
                win::WM_APP_APPBAR => return Some(LRESULT(0)),
                _ => return None,
            }
        }
        if let Some(r) = crate::bar::on_message(self, hwnd, msg, wp, lp) {
            return Some(r);
        }
        if let Some(r) = crate::preview::on_message(self, hwnd, msg, wp, lp) {
            return Some(r);
        }
        if let Some(r) = crate::folder::on_message(self, hwnd, msg, wp, lp) {
            return Some(r);
        }
        if let Some(r) = crate::launchpad::on_message(self, hwnd, msg, wp, lp) {
            return Some(r);
        }
        if let Some(r) = crate::spotlight::on_message(self, hwnd, msg, wp, lp) {
            return Some(r);
        }
        if let Some(r) = crate::switcher::on_message(self, hwnd, msg, wp, lp) {
            return Some(r);
        }
        if let Some(r) = crate::settings::on_message(self, hwnd, msg, wp, lp) {
            return Some(r);
        }
        None
    }

    fn idle(&mut self) {
        let cfg = self.cfg.clone();
        let mut wake = false;
        for di in 0..self.docks.len() {
            let ctx = dock::ctx_for(self, di);
            let d = &mut self.docks[di];
            let was = d.animating;
            d.idle_check(&cfg, &ctx);
            wake |= d.animating && !was;
        }
        // стоп-кадр под кнопкой «свернуть»: кнопку отпустили, а окно не свернулось
        if let Some(h) = self.animator.held_any() {
            if !sys::key_down(0x01) {
                defer_ms(250, move || {
                    with(|a| a.animator.drop_held(h));
                });
            }
        }
        wake |= crate::preview::idle(self);
        if wake {
            self.request_frame();
        }
    }
}

fn drop_files(wp: WPARAM) -> Vec<String> {
    use windows::Win32::UI::Shell::{DragFinish, DragQueryFileW, HDROP};
    let h = HDROP(wp.0 as *mut _);
    let mut out = Vec::new();
    unsafe {
        let n = DragQueryFileW(h, u32::MAX, None);
        for i in 0..n {
            let mut buf = [0u16; 1024];
            let len = DragQueryFileW(h, i, Some(&mut buf));
            out.push(String::from_utf16_lossy(&buf[..len as usize]));
        }
        DragFinish(h);
    }
    out
}

pub fn dock_index_for_window(docks: &[Dock], hwnd: isize) -> usize {
    let mon = sys::window_monitor(win::hwnd_of(hwnd));
    docks.iter().position(|d| d.mon.rect == mon.rect).unwrap_or(0)
}

/// Иконка окна в доке (там, куда сворачивать), физические пиксели.
pub fn icon_for_window(docks: &[Dock], cfg: &Config, hwnd: isize) -> RECT {
    let di = dock_index_for_window(docks, hwnd);
    let d = &docks[di];
    let exe = sys::window_exe(win::hwnd_of(hwnd));
    d.icon_screen_rect(cfg, d.item_for_hwnd(hwnd, &exe))
}

thread_local! {
    static TIMERS: RefCell<Vec<(usize, Box<dyn FnOnce()>)>> = RefCell::new(Vec::new());
    static NEXT_TIMER: RefCell<usize> = const { RefCell::new(1000) };
}

/// Выполнить f через ms миллисекунд (одноразовый таймер главного потока).
pub fn defer_ms(ms: u32, f: impl FnOnce() + 'static) {
    let id = NEXT_TIMER.with(|n| {
        let mut n = n.borrow_mut();
        *n += 1;
        if *n > 1_000_000 {
            *n = 1000;
        }
        *n
    });
    TIMERS.with(|t| t.borrow_mut().push((id, Box::new(f))));
    unsafe {
        let t = hooks::TARGET.load(std::sync::atomic::Ordering::Relaxed);
        SetTimer(Some(win::hwnd_of(t)), id, ms, None);
    }
}

fn fire_timer(id: usize) -> bool {
    let f = TIMERS.with(|t| {
        let mut t = t.borrow_mut();
        t.iter().position(|(i, _)| *i == id).map(|p| t.remove(p).1)
    });
    if let Some(f) = f {
        unsafe {
            let t = hooks::TARGET.load(std::sync::atomic::Ordering::Relaxed);
            let _ = KillTimer(Some(win::hwnd_of(t)), id);
        }
        f();
        true
    } else {
        false
    }
}

pub unsafe extern "system" fn wndproc(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    if msg == WM_LBUTTONDOWN || msg == WM_LBUTTONUP {
        let busy = APP.with(|a| a.try_borrow_mut().is_err());
        crate::dbg_log(&format!("wndproc {msg:#x} hwnd={:?} busy={busy}", hwnd.0));
    }
    if msg == win::WM_APP_TASK {
        run_tasks();
        with(|a| a.on_events());
        return LRESULT(0);
    }
    if msg == WM_TIMER && wp.0 >= 1000 && fire_timer(wp.0) {
        return LRESULT(0);
    }
    if let Some(Some(r)) = with(|a| a.on_message(hwnd, msg, wp, lp)) {
        return r;
    }
    DefWindowProcW(hwnd, msg, wp, lp)
}

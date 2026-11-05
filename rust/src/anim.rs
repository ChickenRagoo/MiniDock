//! Часы кадров: отдельный поток ждёт следующего кадра DWM (в такт монитору — 60/120/144/240 Гц)
//! и будит главный поток. Пока анимаций нет, поток спит на событии и не тратит процессор.

use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering::*};
use std::sync::{Condvar, Mutex};
use std::time::Instant;

use windows::Win32::Foundation::{HWND, LPARAM, WPARAM};
use windows::Win32::UI::WindowsAndMessaging::PostMessageW;

static RUNNING: Mutex<bool> = Mutex::new(false);
static WAKE: Condvar = Condvar::new();
static PENDING: AtomicBool = AtomicBool::new(false);
// Сторож часов: ожидание развёртки (D3DKMTWaitForVerticalBlankEvent) не имеет тайм-аута, и если драйвер
// перестаёт присылать развёртку, поток часов висит, а все анимации замирают на полпути (недоигранное
// сворачивание так и висело поверх экрана картинкой). Главный поток замечает, что кадров нет,
// и запускает новые часы на обычном таймере; зависший поток, если когда-нибудь проснётся, выходит.
static GENERATION: AtomicU32 = AtomicU32::new(0);
static FALLBACK: AtomicBool = AtomicBool::new(false);
static LAST_TICK_US: AtomicU64 = AtomicU64::new(0); // поток часов дождался кадра
static LAST_FRAME_US: AtomicU64 = AtomicU64::new(0); // главный поток нарисовал кадр
static RUNNING_SINCE_US: AtomicU64 = AtomicU64::new(0);
static STALL: AtomicBool = AtomicBool::new(false);

/// Для проверки (--cmd stallclock): текущий поток часов «зависает» на следующем кадре.
pub fn simulate_stall() {
    STALL.store(true, Release);
}

fn now_us() -> u64 {
    (now() * 1e6) as u64
}

static START: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();

/// Секунды с запуска (монотонно).
pub fn now() -> f64 {
    START.get_or_init(Instant::now).elapsed().as_secs_f64()
}

#[repr(C)]
struct OpenAdapter {
    hdc: *mut core::ffi::c_void,
    adapter: u32,
    luid_low: u32,
    luid_high: i32,
    source: u32,
}

#[repr(C)]
struct WaitVBlank {
    adapter: u32,
    device: u32,
    source: u32,
}

type OpenFn = unsafe extern "system" fn(*mut OpenAdapter) -> i32;
type WaitFn = unsafe extern "system" fn(*const WaitVBlank) -> i32;

/// Ожидание кадровой развёртки основного монитора (D3DKMTWaitForVerticalBlankEvent) — точно в такт
/// монитору (60/120/144/180/240 Гц), не завися от того, сколько DWM сейчас компонует.
fn vblank_waiter() -> Option<(WaitFn, WaitVBlank)> {
    use windows::core::{s, w};
    use windows::Win32::Graphics::Gdi::{GetDC, ReleaseDC};
    use windows::Win32::System::LibraryLoader::{GetProcAddress, LoadLibraryW};
    unsafe {
        let lib = LoadLibraryW(w!("gdi32.dll")).ok()?;
        let open: OpenFn = std::mem::transmute(GetProcAddress(lib, s!("D3DKMTOpenAdapterFromHdc"))?);
        let wait: WaitFn = std::mem::transmute(GetProcAddress(lib, s!("D3DKMTWaitForVerticalBlankEvent"))?);
        let dc = GetDC(None);
        let mut o = OpenAdapter { hdc: dc.0, adapter: 0, luid_low: 0, luid_high: 0, source: 0 };
        let r = open(&mut o);
        ReleaseDC(None, dc);
        if r != 0 {
            return None;
        }
        let wv = WaitVBlank { adapter: o.adapter, device: 0, source: o.source };
        (wait(&wv) == 0).then_some((wait, wv))
    }
}

/// Частота основного монитора (для часов на обычном таймере).
fn refresh_rate() -> u32 {
    use windows::Win32::Graphics::Gdi::{EnumDisplaySettingsW, DEVMODEW, ENUM_CURRENT_SETTINGS};
    let mut dm = DEVMODEW { dmSize: std::mem::size_of::<DEVMODEW>() as u16, ..Default::default() };
    let f = unsafe { if EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, &mut dm).as_bool() { dm.dmDisplayFrequency } else { 60 } };
    if (30..=500).contains(&f) { f } else { 60 }
}

/// Ждать примерно один кадр по таймеру высокого разрешения (запасные часы).
fn wait_timer(timer: windows::Win32::Foundation::HANDLE, us: i64) {
    use windows::Win32::System::Threading::{SetWaitableTimer, WaitForSingleObject};
    unsafe {
        let due = -(us * 10); // отрицательное — относительно сейчас, в сотнях наносекунд
        if !timer.is_invalid() && SetWaitableTimer(timer, &due, 0, None, None, false).is_ok() {
            WaitForSingleObject(timer, 100);
        } else {
            std::thread::sleep(std::time::Duration::from_micros(us as u64));
        }
    }
}

pub fn start_clock() {
    let gen = GENERATION.load(Acquire);
    std::thread::Builder::new()
        .name("MiniDockFrames".into())
        .stack_size(128 * 1024)
        .spawn(move || {
            let fallback = FALLBACK.load(Acquire);
            let vb = if fallback { None } else { vblank_waiter() };
            let period_us = 1_000_000 / refresh_rate() as i64;
            let timer = if vb.is_none() {
                use windows::Win32::System::Threading::{CreateWaitableTimerExW, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS};
                unsafe { CreateWaitableTimerExW(None, None, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS.0).unwrap_or_default() }
            } else {
                windows::Win32::Foundation::HANDLE::default()
            };
            loop {
            {
                let mut on = RUNNING.lock().unwrap();
                while !*on {
                    on = WAKE.wait(on).unwrap();
                }
            }
            if GENERATION.load(Acquire) != gen {
                return; // сторож уже запустил новые часы
            }
            let t = Instant::now();
            if STALL.swap(false, AcqRel) {
                std::thread::sleep(std::time::Duration::from_secs(5));
            }
            match &vb {
                Some((wait, wv)) => unsafe {
                    wait(wv);
                },
                None => wait_timer(timer, period_us),
            }
            if GENERATION.load(Acquire) != gen {
                return;
            }
            LAST_TICK_US.store(now_us(), Release);
            if t.elapsed().as_micros() < 1000 {
                // DWM не ждал (композиция выключена или монитор спит) — не крутимся вхолостую
                std::thread::sleep(std::time::Duration::from_micros(4000));
            }
            // следующий кадр не ставим в очередь, пока главный поток не нарисовал этот
            if !PENDING.swap(true, AcqRel) {
                let t = crate::win::hooks::TARGET.load(Relaxed);
                unsafe {
                    let _ = PostMessageW(Some(HWND(t as *mut _)), crate::win::WM_APP_FRAME, WPARAM(0), LPARAM(0));
                }
            }
            }
        })
        .ok();
}

/// Длительность кадра для плавных анимаций. Анимация продолжилась после простоя — отсчёт заново
/// (иначе первый шаг считался как за 50 мс и всё дёргалось рывком).
pub fn frame_dt(last: &mut f64) -> f32 {
    let now = now();
    let gap = now - *last;
    *last = now;
    if gap > 0.1 {
        crate::gfx::FRAME_REF
    } else {
        (gap as f32).clamp(0.001, 0.05)
    }
}

/// Включить/выключить поток кадров (главный поток решает по итогам кадра).
pub fn set_running(on: bool) {
    let mut r = RUNNING.lock().unwrap();
    if *r != on {
        *r = on;
        if on {
            RUNNING_SINCE_US.store(now_us(), Release);
            WAKE.notify_all();
        }
    }
}

/// Главный поток обработал кадр.
pub fn frame_done() {
    PENDING.store(false, Release);
    LAST_FRAME_US.store(now_us(), Release);
}

/// Кадр пришёл, когда главный поток был занят (открыт системный диалог): следующий всё равно нужен.
pub fn frame_skipped() {
    PENDING.store(false, Release);
}

/// Сторож (главный поток, 20 раз в секунду). true — кадров давно не было: нарисовать кадр сейчас.
pub fn watchdog() -> bool {
    if !*RUNNING.lock().unwrap() {
        return false;
    }
    let now = now_us();
    let since = RUNNING_SINCE_US.load(Acquire);
    let stale = |t: u64| now.saturating_sub(t.max(since)) > 300_000;
    if stale(LAST_TICK_US.load(Acquire)) {
        // поток часов не возвращается из ожидания развёртки — новые часы на таймере
        let was = FALLBACK.swap(true, AcqRel);
        crate::log::line(if was { "часы кадров не отвечали — запустил заново" } else { "развёртка монитора перестала приходить — часы кадров переведены на таймер" });
        GENERATION.fetch_add(1, AcqRel);
        PENDING.store(false, Release);
        LAST_TICK_US.store(now, Release);
        start_clock();
        return true;
    }
    if PENDING.load(Acquire) && stale(LAST_FRAME_US.load(Acquire)) {
        // сообщение о кадре потерялось — без этого следующий кадр не пришёл бы никогда
        PENDING.store(false, Release);
        return true;
    }
    false
}

/// Анимация «туда-обратно» 0→1 с длительностью и кривой.
#[derive(Clone, Copy, Debug)]
pub struct Tween {
    pub start: f64,
    pub dur: f64,
    pub from: f32,
    pub to: f32,
}

impl Tween {
    pub fn new(from: f32, to: f32, dur: f32) -> Tween {
        Tween { start: now(), dur: dur.max(0.001) as f64, from, to }
    }
    pub fn t(&self) -> f32 {
        ((now() - self.start) / self.dur).clamp(0.0, 1.0) as f32
    }
    pub fn done(&self) -> bool {
        self.t() >= 1.0
    }
    pub fn value(&self, curve: fn(f32) -> f32) -> f32 {
        crate::gfx::lerp(self.from, self.to, curve(self.t()))
    }
}

/// Значение, плавно догоняющее цель.
#[derive(Clone, Copy, Debug, Default)]
pub struct Smooth {
    pub v: f32,
    pub target: f32,
}

impl Smooth {
    pub fn new(v: f32) -> Smooth {
        Smooth { v, target: v }
    }
    /// Шаг на кадр dt; true — ещё движется.
    pub fn step(&mut self, k: f32, dt: f32, eps: f32) -> bool {
        if self.v == self.target {
            return false;
        }
        self.v += (self.target - self.v) * crate::gfx::smooth(k, dt);
        if (self.v - self.target).abs() < eps {
            self.v = self.target;
        }
        true
    }
}

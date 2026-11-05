//! Часы кадров: отдельный поток ждёт следующего кадра DWM (в такт монитору — 60/120/144/240 Гц)
//! и будит главный поток. Пока анимаций нет, поток спит на событии и не тратит процессор.

use std::sync::atomic::{AtomicBool, Ordering::*};
use std::sync::{Condvar, Mutex};
use std::time::Instant;

use windows::Win32::Foundation::{HWND, LPARAM, WPARAM};
use windows::Win32::UI::WindowsAndMessaging::PostMessageW;

static RUNNING: Mutex<bool> = Mutex::new(false);
static WAKE: Condvar = Condvar::new();
static PENDING: AtomicBool = AtomicBool::new(false);

static START: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();

/// Секунды с запуска (монотонно).
pub fn now() -> f64 {
    START.get_or_init(Instant::now).elapsed().as_secs_f64()
}

pub fn start_clock() {
    std::thread::Builder::new()
        .name("MiniDockFrames".into())
        .stack_size(128 * 1024)
        .spawn(|| loop {
            {
                let mut on = RUNNING.lock().unwrap();
                while !*on {
                    on = WAKE.wait(on).unwrap();
                }
            }
            let t = Instant::now();
            crate::win::dwm::flush();
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
        })
        .ok();
}

/// Включить/выключить поток кадров (главный поток решает по итогам кадра).
pub fn set_running(on: bool) {
    let mut r = RUNNING.lock().unwrap();
    if *r != on {
        *r = on;
        if on {
            WAKE.notify_one();
        }
    }
}

/// Главный поток обработал кадр.
pub fn frame_done() {
    PENDING.store(false, Release);
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

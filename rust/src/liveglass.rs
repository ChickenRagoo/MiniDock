//! Живое «стекло», как в Python-версии: участок экрана ПОД окнами MiniDock, размытый и нарисованный
//! самим окном (док) — в той же форме со сглаженными краями и в том же кадре, что и всё остальное.
//!
//! Обычный снимок экрана захватывает и сам док, поэтому снимает лупа Windows (Magnification API): она
//! рисует экран, пропуская заданные окна. Лупа ненадёжна и течёт памятью, поэтому работает в отдельном
//! маленьком процессе (minidock.exe --glass-helper): упадёт он — док остаётся, а «стекло» берётся
//! из размытия Windows, как раньше. Задания (какой участок снимать) и готовые размытые кадры ходят
//! сообщениями WM_COPYDATA. Пока дока на экране нет — заданий нет, и помощник спит.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use windows::core::w;
use windows::core::BOOL;
use windows::Win32::Foundation::{HANDLE, HWND, LPARAM, LRESULT, RECT, WAIT_OBJECT_0, WPARAM};
use windows::Win32::Graphics::Gdi::{InvalidateRect, UpdateWindow, HRGN};
use windows::Win32::System::DataExchange::COPYDATASTRUCT;
use windows::Win32::System::Threading::{OpenProcess, PROCESS_SYNCHRONIZE};
use windows::Win32::UI::Magnification::*;
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::gfx::Image;

pub const DOWNSCALE: i32 = 4; // размываем копию в 4 раза меньше — быстро, а после размытия разницы не видно
pub const MARGIN: i32 = 28; // снимаем чуть больше, чтобы у краёв размытие не темнело
const BUCKET: i32 = 128; // окна лупы — размером, округлённым вверх: один размер служит многим участкам
const FPS: f64 = 30.0; // пока под окном что-то меняется
const IDLE_FPS: f64 = 8.0; // картинка стоит — снимаем реже и ничего не отдаём

const CD_HELLO: usize = 0x474C_4849; // помощник → MiniDock: «я готов», wParam — его окно
const CD_JOBS: usize = 0x474C_4A42; // MiniDock → помощник: список заданий
const CD_FRAME: usize = 0x474C_4652; // помощник → MiniDock: размытый кадр

#[derive(Clone, Copy, PartialEq, Debug)]
pub struct Job {
    pub slot: u32,
    pub rect: RECT,
    pub radius: f32,
}

fn jobs_to_bytes(jobs: &[Job]) -> Vec<u8> {
    let mut v = Vec::with_capacity(jobs.len() * 24);
    for j in jobs {
        v.extend_from_slice(&j.slot.to_le_bytes());
        for x in [j.rect.left, j.rect.top, j.rect.right, j.rect.bottom] {
            v.extend_from_slice(&x.to_le_bytes());
        }
        v.extend_from_slice(&j.radius.to_le_bytes());
    }
    v
}

fn jobs_from_bytes(b: &[u8]) -> Vec<Job> {
    b.chunks_exact(24)
        .map(|c| {
            let u = |i: usize| u32::from_le_bytes(c[i..i + 4].try_into().unwrap());
            let i = |k: usize| u(k) as i32;
            Job { slot: u(0), rect: RECT { left: i(4), top: i(8), right: i(12), bottom: i(16) }, radius: f32::from_bits(u(20)) }
        })
        .collect()
}

unsafe fn send_copydata(to: HWND, from: HWND, kind: usize, data: &[u8], timeout_ms: u32) -> bool {
    let cds = COPYDATASTRUCT { dwData: kind, cbData: data.len() as u32, lpData: data.as_ptr() as *mut _ };
    let mut res = 0usize;
    SendMessageTimeoutW(to, WM_COPYDATA, WPARAM(from.0 as usize), LPARAM(&cds as *const _ as isize), SMTO_ABORTIFHUNG, timeout_ms, Some(&mut res)).0 != 0
}

// ================================================================== MiniDock: заказчик кадров

thread_local! {
    /// Кадры и «привет» от помощника: WM_COPYDATA может прийти, пока состояние программы занято
    /// (главный поток сам ждёт SendMessage), поэтому они складываются сюда и разбираются потом.
    static INBOX: RefCell<Inbox> = RefCell::new(Inbox::default());
}

#[derive(Default)]
struct Inbox {
    helper: Option<HWND>,
    frames: Vec<(u32, Image, RECT)>,
}

/// WM_COPYDATA от помощника (вызывается из оконной процедуры до разбора остальных сообщений).
/// true — сообщение наше.
pub fn on_copydata(wp: WPARAM, lp: LPARAM) -> bool {
    let cds = unsafe { &*(lp.0 as *const COPYDATASTRUCT) };
    match cds.dwData {
        CD_HELLO => {
            INBOX.with(|b| b.borrow_mut().helper = Some(HWND(wp.0 as *mut _)));
        }
        CD_FRAME => {
            let data = unsafe { std::slice::from_raw_parts(cds.lpData as *const u8, cds.cbData as usize) };
            if data.len() < 28 {
                return true;
            }
            let u = |i: usize| u32::from_le_bytes(data[i..i + 4].try_into().unwrap());
            let (slot, w, h) = (u(0), u(4), u(8));
            let rect = RECT { left: u(12) as i32, top: u(16) as i32, right: u(20) as i32, bottom: u(24) as i32 };
            let px = &data[28..];
            if w == 0 || h == 0 || px.len() != (w * h * 4) as usize {
                return true;
            }
            if let Some(img) = Image::from_bgra_premul(w, h, px.to_vec()) {
                INBOX.with(|b| {
                    let mut b = b.borrow_mut();
                    b.frames.retain(|f| f.0 != slot);
                    b.frames.push((slot, img, rect));
                });
            }
        }
        _ => return false,
    }
    crate::app::defer(|| {
        crate::app::with(|a| a.on_live_glass());
    });
    true
}

pub struct LiveGlass {
    child: Option<std::process::Child>,
    helper: Option<HWND>,
    jobs: Vec<Job>,
    sent: Option<Vec<Job>>,
    frames: HashMap<u32, (Rc<Image>, RECT)>,
    fails: u32,
    started: f64,
    pub disabled: bool,
}

impl LiveGlass {
    pub fn new() -> LiveGlass {
        LiveGlass { child: None, helper: None, jobs: Vec::new(), sent: None, frames: HashMap::new(), fails: 0, started: 0.0, disabled: false }
    }

    /// Снимать участок rect (физические пиксели) для «стекла» slot, размытие radius (в уменьшенной копии).
    pub fn set_job(&mut self, slot: u32, rect: RECT, radius: f32) {
        if self.disabled {
            return;
        }
        let job = Job { slot, rect, radius };
        match self.jobs.iter_mut().find(|j| j.slot == slot) {
            Some(j) if *j == job => {}
            Some(j) => *j = job,
            None => self.jobs.push(job),
        }
        self.ensure_helper();
        self.send();
    }

    pub fn remove(&mut self, slot: u32) {
        let had = self.jobs.len();
        self.jobs.retain(|j| j.slot != slot);
        self.frames.remove(&slot);
        if self.jobs.len() != had {
            self.send();
        }
    }

    /// Последний кадр «стекла» slot: картинка и участок экрана, который она покрывает.
    pub fn frame(&self, slot: u32) -> Option<(Rc<Image>, RECT)> {
        self.frames.get(&slot).cloned()
    }

    /// Разобрать пришедшее от помощника. Возвращает «стёкла», у которых новый кадр.
    pub fn take_inbox(&mut self) -> Vec<u32> {
        let (helper, frames) = INBOX.with(|b| {
            let mut b = b.borrow_mut();
            (b.helper.take(), std::mem::take(&mut b.frames))
        });
        if let Some(h) = helper {
            self.helper = Some(h);
            self.sent = None;
            self.fails = 0;
            self.send();
        }
        let mut out = Vec::new();
        for (slot, img, rect) in frames {
            if self.jobs.iter().any(|j| j.slot == slot) {
                self.frames.insert(slot, (Rc::new(img), rect));
                out.push(slot);
            }
        }
        out
    }

    fn send(&mut self) {
        let Some(h) = self.helper else { return };
        if self.sent.as_ref() == Some(&self.jobs) {
            return;
        }
        let me = crate::win::hwnd_of(crate::win::hooks::TARGET.load(std::sync::atomic::Ordering::Relaxed));
        if unsafe { send_copydata(h, me, CD_JOBS, &jobs_to_bytes(&self.jobs), 150) } {
            self.sent = Some(self.jobs.clone());
        } else if unsafe { !IsWindow(Some(h)).as_bool() } {
            self.helper = None; // помощник закрылся — следующее задание запустит новый
        }
    }

    fn ensure_helper(&mut self) {
        if let Some(c) = self.child.as_mut() {
            match c.try_wait() {
                Ok(None) => return, // работает (или ещё запускается)
                _ => {
                    self.child = None;
                    self.helper = None;
                    self.sent = None;
                    // упал вскоре после запуска — считаем неудачей; после трёх подряд — размытие Windows
                    if crate::anim::now() - self.started < 30.0 {
                        self.fails += 1;
                    }
                    if self.fails >= 3 {
                        crate::log::line("живое размытие не работает (помощник падает) — включаю размытие Windows");
                        self.disabled = true;
                        self.frames.clear();
                        return;
                    }
                }
            }
        }
        let Ok(exe) = std::env::current_exe() else {
            self.disabled = true;
            return;
        };
        let me = crate::win::hooks::TARGET.load(std::sync::atomic::Ordering::Relaxed);
        match std::process::Command::new(exe).arg("--glass-helper").arg(format!("{me:x}")).spawn() {
            Ok(c) => {
                self.child = Some(c);
                self.started = crate::anim::now();
            }
            Err(_) => self.disabled = true,
        }
    }

    /// Выход из программы: помощник выходит сам, когда закрывается MiniDock, но не ждём этого.
    pub fn shutdown(&mut self) {
        if let Some(mut c) = self.child.take() {
            let _ = c.kill();
        }
    }

    pub fn memory(&self) -> usize {
        self.frames.values().map(|f| f.0.bytes()).sum()
    }
}

// ================================================================== процесс-помощник: лупа Windows

thread_local! {
    static GOT: RefCell<Option<(Vec<u8>, u32, u32, u32)>> = const { RefCell::new(None) };
    static NEW_JOBS: RefCell<Option<Vec<Job>>> = const { RefCell::new(None) };
}

unsafe extern "system" fn on_image(
    _hwnd: HWND,
    src: *mut core::ffi::c_void,
    sh: MAGIMAGEHEADER,
    _dst: *mut core::ffi::c_void,
    _dh: MAGIMAGEHEADER,
    _unclipped: RECT,
    _clipped: RECT,
    _dirty: HRGN,
) -> BOOL {
    let n = (sh.stride * sh.height) as usize;
    if !src.is_null() && n > 0 {
        let data = std::slice::from_raw_parts(src as *const u8, n).to_vec();
        GOT.with(|g| *g.borrow_mut() = Some((data, sh.width, sh.height, sh.stride)));
    }
    BOOL(1)
}

unsafe extern "system" fn helper_proc(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    if msg == WM_COPYDATA {
        let cds = &*(lp.0 as *const COPYDATASTRUCT);
        if cds.dwData == CD_JOBS {
            let data = std::slice::from_raw_parts(cds.lpData as *const u8, cds.cbData as usize);
            let jobs = jobs_from_bytes(data);
            NEW_JOBS.with(|j| *j.borrow_mut() = Some(jobs));
        }
        return LRESULT(1);
    }
    DefWindowProcW(hwnd, msg, wp, lp)
}

struct Mag {
    host: HWND,
    pool: Vec<((i32, i32), HWND)>,
    excluded: Vec<HWND>,
}

impl Mag {
    /// Окно лупы не меньше w×h (из пула; окна лупы не уничтожаются — уничтоженное оставляет утечку).
    fn window(&mut self, w: i32, h: i32) -> Option<(HWND, i32, i32)> {
        let key = ((w + BUCKET - 1) / BUCKET * BUCKET, (h + BUCKET - 1) / BUCKET * BUCKET);
        if let Some((_, h)) = self.pool.iter().find(|(k, _)| *k == key) {
            return Some((*h, key.0, key.1));
        }
        let hwnd = unsafe { CreateWindowExW(WINDOW_EX_STYLE(0), WC_MAGNIFIERW, w!("m"), WS_CHILD | WS_VISIBLE, 0, 0, key.0, key.1, Some(self.host), None, Some(crate::win::hinstance()), None).ok()? };
        if unsafe { !MagSetImageScalingCallback(hwnd, Some(on_image)).as_bool() } {
            return None;
        }
        self.pool.push((key, hwnd));
        self.apply_filter(hwnd);
        Some((hwnd, key.0, key.1))
    }

    fn apply_filter(&self, hwnd: HWND) {
        let mut list = self.excluded.clone();
        if list.is_empty() {
            list.push(HWND::default());
        }
        unsafe {
            let _ = MagSetWindowFilterList(hwnd, MW_FILTERMODE_EXCLUDE, self.excluded.len() as i32, list.as_mut_ptr());
        }
    }

    /// Окна MiniDock (док, панель, всплывающие) на снимке быть не должны.
    fn exclude(&mut self, pid: u32) {
        let mut mine: Vec<HWND> = Vec::new();
        unsafe extern "system" fn cb(h: HWND, lp: LPARAM) -> windows::core::BOOL {
            let st = &mut *(lp.0 as *mut (u32, Vec<HWND>));
            let mut p = 0u32;
            GetWindowThreadProcessId(h, Some(&mut p));
            if p == st.0 && IsWindowVisible(h).as_bool() {
                st.1.push(h);
            }
            windows::core::BOOL(1)
        }
        let mut st = (pid, std::mem::take(&mut mine));
        unsafe {
            let _ = EnumWindows(Some(cb), LPARAM(&mut st as *mut _ as isize));
        }
        st.1.truncate(64);
        if st.1 != self.excluded {
            self.excluded = st.1;
            for (_, h) in &self.pool {
                self.apply_filter(*h);
            }
        }
    }

    /// Участок экрана без окон MiniDock, уменьшенный в DOWNSCALE раз: BGR float, ширина, высота.
    fn grab(&mut self, r: RECT) -> Option<(Vec<f32>, usize, usize)> {
        let (w, h) = (r.right - r.left, r.bottom - r.top);
        if w < DOWNSCALE || h < DOWNSCALE {
            return None;
        }
        let (hwnd, bw, bh) = self.window(w, h)?;
        GOT.with(|g| *g.borrow_mut() = None);
        unsafe {
            // снимаем участок размером с окно лупы и обрезаем лишнее справа и снизу
            let _ = MagSetWindowSource(hwnd, RECT { left: r.left, top: r.top, right: r.left + bw, bottom: r.top + bh });
            let _ = InvalidateRect(Some(hwnd), None, true);
            let _ = UpdateWindow(hwnd);
        }
        let (data, gw, gh, stride) = GOT.with(|g| g.borrow_mut().take())?;
        let (gw, gh) = ((gw as i32).min(w) as usize, (gh as i32).min(h) as usize);
        let d = DOWNSCALE as usize;
        let (ww, hh) = (gw / d, gh / d);
        if ww == 0 || hh == 0 {
            return None;
        }
        let stride = stride as usize;
        let mut out = vec![0f32; ww * hh * 3];
        for y in 0..hh {
            for x in 0..ww {
                let (mut b, mut g, mut rr) = (0u32, 0u32, 0u32);
                for dy in 0..d {
                    let row = (y * d + dy) * stride;
                    for dx in 0..d {
                        let i = row + (x * d + dx) * 4;
                        if i + 2 < data.len() {
                            b += data[i] as u32;
                            g += data[i + 1] as u32;
                            rr += data[i + 2] as u32;
                        }
                    }
                }
                let o = (y * ww + x) * 3;
                let n = (d * d) as f32;
                out[o] = b as f32 / n;
                out[o + 1] = g as f32 / n;
                out[o + 2] = rr as f32 / n;
            }
        }
        Some((out, ww, hh))
    }
}

/// Размытие по одной оси (окно 2r+1 с центром в пикселе, края — повтор крайних пикселей).
fn box_blur(a: &[f32], w: usize, h: usize, r: usize, horizontal: bool) -> Vec<f32> {
    let mut out = vec![0f32; a.len()];
    let (n, lines) = if horizontal { (w, h) } else { (h, w) };
    let idx = |line: usize, k: usize| if horizontal { (line * w + k) * 3 } else { (k * w + line) * 3 };
    let div = (2 * r + 1) as f32;
    for line in 0..lines {
        for c in 0..3 {
            let at = |k: isize| a[idx(line, k.clamp(0, n as isize - 1) as usize) + c];
            let mut sum: f32 = (-(r as isize)..=(r as isize)).map(at).sum();
            for k in 0..n {
                out[idx(line, k) + c] = sum / div;
                sum += at(k as isize + r as isize + 1) - at(k as isize - r as isize);
            }
        }
    }
    out
}

/// Размытие уменьшенной копии: 3 прохода box ≈ гаусс, плюс немного насыщенности (как в Python-версии).
fn blur_bgra(a: Vec<f32>, w: usize, h: usize, radius: f32, saturation: f32) -> Vec<u8> {
    let r = ((radius / 1.7) as usize).max(1);
    let mut a = a;
    for _ in 0..3 {
        a = box_blur(&a, w, h, r, true);
        a = box_blur(&a, w, h, r, false);
    }
    let mut out = vec![255u8; w * h * 4];
    for i in 0..w * h {
        let (b, g, rr) = (a[i * 3], a[i * 3 + 1], a[i * 3 + 2]);
        let lum = b * 0.114 + g * 0.587 + rr * 0.299;
        for (k, v) in [b, g, rr].into_iter().enumerate() {
            out[i * 4 + k] = (lum + (v - lum) * saturation).clamp(0.0, 255.0) as u8;
        }
    }
    out
}

/// minidock.exe --glass-helper <окно MiniDock>
pub fn helper_main(parent_hex: &str) {
    let Ok(p) = isize::from_str_radix(parent_hex.trim(), 16) else { return };
    let parent = HWND(p as *mut _);
    unsafe {
        let _ = windows::Win32::UI::HiDpi::SetProcessDpiAwarenessContext(windows::Win32::UI::HiDpi::DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        let mut pid = 0u32;
        GetWindowThreadProcessId(parent, Some(&mut pid));
        let Ok(parent_proc) = OpenProcess(PROCESS_SYNCHRONIZE, false, pid) else { return };
        if !MagInitialize().as_bool() {
            return;
        }
        let hinst = crate::win::hinstance();
        let wc = WNDCLASSW { lpfnWndProc: Some(helper_proc), hInstance: hinst, lpszClassName: w!("MiniDockGlassHelper"), ..Default::default() };
        RegisterClassW(&wc);
        // невидимое окно-хозяин (прозрачное, без панели задач и фокуса); лупы — его дочерние окна
        let host = CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
            w!("MiniDockGlassHelper"),
            w!("MiniDockLiveGlass"),
            WS_POPUP,
            0,
            0,
            64,
            64,
            None,
            None,
            Some(hinst),
            None,
        )
        .unwrap_or_default();
        if host.is_invalid() {
            return;
        }
        let _ = SetLayeredWindowAttributes(host, windows::Win32::Foundation::COLORREF(0), 0, LWA_ALPHA);
        let _ = ShowWindow(host, SW_SHOWNOACTIVATE);
        let _ = ChangeWindowMessageFilterEx(host, WM_COPYDATA, MSGFLT_ALLOW, None);
        let mut mag = Mag { host, pool: Vec::new(), excluded: Vec::new() };
        if mag.window(16, 16).is_none() {
            return;
        }
        if !send_copydata(parent, host, CD_HELLO, &[], 2000) {
            return;
        }
        let mut jobs: Vec<Job> = Vec::new();
        let mut last: HashMap<u32, (Job, Vec<f32>)> = HashMap::new();
        let mut still = 0u32;
        let mut excluded_at = 0.0;
        let t0 = std::time::Instant::now();
        let now = || t0.elapsed().as_secs_f64();
        loop {
            let start = now();
            let mut msg = MSG::default();
            while PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() {
                if msg.message == WM_QUIT {
                    return;
                }
                DispatchMessageW(&msg);
            }
            if WaitForSingleObjectSafe(parent_proc, 0) || !IsWindow(Some(parent)).as_bool() {
                return; // MiniDock закрылся
            }
            let mut changed_jobs = false;
            if let Some(j) = NEW_JOBS.with(|j| j.borrow_mut().take()) {
                changed_jobs = j != jobs;
                jobs = j;
                last.retain(|slot, _| jobs.iter().any(|j| j.slot == *slot));
            }
            if jobs.is_empty() {
                // спим, пока не придёт задание или не закроется MiniDock
                let _ = MsgWaitForMultipleObjects(Some(&[parent_proc]), false, 2000, QS_ALLINPUT);
                continue;
            }
            if changed_jobs || start - excluded_at > 0.5 {
                mag.exclude(pid);
                excluded_at = start;
            }
            let mut sent_any = false;
            for job in &jobs {
                let Some((arr, ww, hh)) = mag.grab(job.rect) else { continue };
                if let Some((pj, prev)) = last.get(&job.slot) {
                    if pj == job && prev.len() == arr.len() {
                        let diff: f32 = prev.iter().zip(&arr).map(|(a, b)| (a - b).abs()).sum::<f32>() / arr.len() as f32;
                        if diff < 0.4 {
                            continue; // под окном всё как было
                        }
                    }
                }
                last.insert(job.slot, (*job, arr.clone()));
                let px = blur_bgra(arr, ww, hh, job.radius, 1.3);
                let covered = RECT { left: job.rect.left, top: job.rect.top, right: job.rect.left + ww as i32 * DOWNSCALE, bottom: job.rect.top + hh as i32 * DOWNSCALE };
                let mut data = Vec::with_capacity(28 + px.len());
                for v in [job.slot, ww as u32, hh as u32, covered.left as u32, covered.top as u32, covered.right as u32, covered.bottom as u32] {
                    data.extend_from_slice(&v.to_le_bytes());
                }
                data.extend_from_slice(&px);
                if !send_copydata(parent, host, CD_FRAME, &data, 300) {
                    last.remove(&job.slot); // не дошло — пошлём снова
                }
                sent_any = true;
            }
            still = if sent_any { 0 } else { still + 1 };
            let fps = if still < 6 { FPS } else { IDLE_FPS };
            let wait = (1.0 / fps - (now() - start)).max(0.0);
            let _ = MsgWaitForMultipleObjects(Some(&[parent_proc]), false, (wait * 1000.0) as u32, QS_ALLINPUT);
        }
    }
}

#[allow(non_snake_case)]
unsafe fn WaitForSingleObjectSafe(h: HANDLE, ms: u32) -> bool {
    windows::Win32::System::Threading::WaitForSingleObject(h, ms) == WAIT_OBJECT_0
}

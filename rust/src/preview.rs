//! Миниатюры окон над доком (заглушка).
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use crate::app::App;
use crate::config::Config;

pub struct Preview { pub visible: bool, pub item: Option<(usize, u64)> }
impl Preview {
    pub fn new() -> Preview { Preview { visible: false, item: None } }
    pub fn mouse_inside(&self) -> bool { false }
    pub fn close(&mut self) {}
    pub fn frame(&mut self, _cfg: &Config) -> bool { false }
}
pub fn on_message(_app: &mut App, _h: HWND, _m: u32, _w: WPARAM, _l: LPARAM) -> Option<LRESULT> { None }
pub fn idle(_app: &mut App) -> bool { false }

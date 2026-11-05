//! Alt+Tab (заглушка).
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use crate::app::App;
use crate::config::Config;

pub struct Switcher {}
impl Switcher {
    pub fn new() -> Switcher { Switcher {} }
    pub fn frame(&mut self, _cfg: &Config) -> bool { false }
}
pub fn begin(_app: &mut App, _d: i32) {}
pub fn step(_app: &mut App, _d: i32) {}
pub fn commit(_app: &mut App) {}
pub fn arrow(_app: &mut App, _vk: u32) {}
pub fn next_window(_app: &mut App) {}
pub fn cancel(_app: &mut App) {}
pub fn quit_selected(_app: &mut App) {}
pub fn on_message(_app: &mut App, _h: HWND, _m: u32, _w: WPARAM, _l: LPARAM) -> Option<LRESULT> { None }

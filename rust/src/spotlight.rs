//! Spotlight (заглушка).
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use crate::app::App;
use crate::config::Config;

pub struct Spotlight {}
impl Spotlight {
    pub fn new() -> Spotlight { Spotlight {} }
    pub fn frame(&mut self, _cfg: &Config) -> bool { false }
}
pub fn toggle(_app: &mut App) {}
pub fn on_message(_app: &mut App, _h: HWND, _m: u32, _w: WPARAM, _l: LPARAM) -> Option<LRESULT> { None }

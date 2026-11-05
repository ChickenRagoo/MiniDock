//! Launchpad (заглушка).
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use crate::app::App;
use crate::config::Config;

pub struct Launchpad {}
impl Launchpad {
    pub fn new() -> Launchpad { Launchpad {} }
    pub fn blocks_dock(&self) -> bool { false }
    pub fn visible(&self) -> bool { false }
    pub fn frame(&mut self, _cfg: &Config) -> bool { false }
}
pub fn toggle(_app: &mut App) {}
pub fn feed_key(_app: &mut App, _vk: u32, _ch: Option<char>) {}
pub fn on_message(_app: &mut App, _h: HWND, _m: u32, _w: WPARAM, _l: LPARAM) -> Option<LRESULT> { None }

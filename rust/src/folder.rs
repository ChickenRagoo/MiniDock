//! Папки дока (заглушка).
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use crate::app::App;
use crate::config::Config;

pub struct FolderPopup {}
impl FolderPopup {
    pub fn new() -> FolderPopup { FolderPopup {} }
    pub fn is_open_for(&self, _di: usize) -> bool { false }
    pub fn is_open_for_item(&self, _di: usize, _id: u64) -> bool { false }
    pub fn frame(&mut self, _cfg: &Config) -> bool { false }
}
pub fn open(_app: &mut App, _di: usize, _id: u64, _edit: bool) {}
pub fn close(_app: &mut App) {}
pub fn on_message(_app: &mut App, _h: HWND, _m: u32, _w: WPARAM, _l: LPARAM) -> Option<LRESULT> { None }

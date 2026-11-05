//! Однострочное поле ввода: курсор, выделение, буфер обмена, Ctrl+стрелки по словам, мигающий курсор.

use crate::gfx::*;
use crate::text::{self, Font};

#[derive(Default, Clone)]
pub struct LineEdit {
    pub text: String,
    /// позиция курсора (в символах)
    pub cursor: usize,
    /// начало выделения (если есть)
    pub anchor: Option<usize>,
    pub focused: bool,
    blink_from: f64,
}

pub enum Edit {
    None,
    Changed,
    Enter,
    Escape,
    Up,
    Down,
    Tab,
}

impl LineEdit {
    pub fn new(text: &str) -> LineEdit {
        let n = text.chars().count();
        LineEdit { text: text.to_string(), cursor: n, anchor: None, focused: false, blink_from: crate::anim::now() }
    }

    pub fn set(&mut self, text: &str) {
        self.text = text.to_string();
        self.cursor = text.chars().count();
        self.anchor = None;
    }

    pub fn select_all(&mut self) {
        self.anchor = Some(0);
        self.cursor = self.len();
    }

    fn len(&self) -> usize {
        self.text.chars().count()
    }

    fn byte(&self, ci: usize) -> usize {
        self.text.char_indices().nth(ci).map(|(b, _)| b).unwrap_or(self.text.len())
    }

    fn selection(&self) -> Option<(usize, usize)> {
        let a = self.anchor?;
        (a != self.cursor).then(|| (a.min(self.cursor), a.max(self.cursor)))
    }

    fn delete_selection(&mut self) -> bool {
        if let Some((a, b)) = self.selection() {
            let (ba, bb) = (self.byte(a), self.byte(b));
            self.text.replace_range(ba..bb, "");
            self.cursor = a;
            self.anchor = None;
            return true;
        }
        self.anchor = None;
        false
    }

    pub fn insert(&mut self, s: &str) {
        self.delete_selection();
        let b = self.byte(self.cursor);
        let clean: String = s.chars().filter(|c| !c.is_control()).collect();
        self.text.insert_str(b, &clean);
        self.cursor += clean.chars().count();
        self.blink_from = crate::anim::now();
    }

    fn word_left(&self) -> usize {
        let chars: Vec<char> = self.text.chars().collect();
        let mut i = self.cursor;
        while i > 0 && chars[i - 1].is_whitespace() {
            i -= 1;
        }
        while i > 0 && !chars[i - 1].is_whitespace() {
            i -= 1;
        }
        i
    }

    fn word_right(&self) -> usize {
        let chars: Vec<char> = self.text.chars().collect();
        let mut i = self.cursor;
        while i < chars.len() && !chars[i].is_whitespace() {
            i += 1;
        }
        while i < chars.len() && chars[i].is_whitespace() {
            i += 1;
        }
        i
    }

    fn move_to(&mut self, pos: usize, shift: bool) {
        if shift {
            if self.anchor.is_none() {
                self.anchor = Some(self.cursor);
            }
        } else {
            self.anchor = None;
        }
        self.cursor = pos.min(self.len());
        self.blink_from = crate::anim::now();
    }

    /// WM_CHAR.
    pub fn on_char(&mut self, c: char) -> Edit {
        match c {
            '\r' | '\n' => Edit::Enter,
            '\u{1b}' => Edit::Escape,
            '\t' => Edit::Tab,
            '\u{8}' => {
                if !self.delete_selection() && self.cursor > 0 {
                    let (a, b) = (self.byte(self.cursor - 1), self.byte(self.cursor));
                    self.text.replace_range(a..b, "");
                    self.cursor -= 1;
                }
                self.blink_from = crate::anim::now();
                Edit::Changed
            }
            '\u{7f}' => {
                // Ctrl+Backspace — слово
                if !self.delete_selection() {
                    let to = self.word_left();
                    let (a, b) = (self.byte(to), self.byte(self.cursor));
                    self.text.replace_range(a..b, "");
                    self.cursor = to;
                }
                Edit::Changed
            }
            c if c.is_control() => Edit::None,
            c => {
                let mut buf = [0u8; 4];
                self.insert(c.encode_utf8(&mut buf));
                Edit::Changed
            }
        }
    }

    /// WM_KEYDOWN (стрелки, Home/End, Delete, Ctrl+A/C/V/X).
    pub fn on_key(&mut self, vk: u32) -> Edit {
        let shift = crate::win::sys::key_down(0x10);
        let ctrl = crate::win::sys::key_down(0x11);
        match vk {
            0x25 => {
                let p = if ctrl { self.word_left() } else if !shift && self.selection().is_some() { self.selection().unwrap().0 } else { self.cursor.saturating_sub(1) };
                self.move_to(p, shift);
            }
            0x27 => {
                let p = if ctrl { self.word_right() } else if !shift && self.selection().is_some() { self.selection().unwrap().1 } else { self.cursor + 1 };
                self.move_to(p, shift);
            }
            0x24 => self.move_to(0, shift),
            0x23 => self.move_to(self.len(), shift),
            0x26 => return Edit::Up,
            0x28 => return Edit::Down,
            0x2E => {
                if !self.delete_selection() && self.cursor < self.len() {
                    let (a, b) = (self.byte(self.cursor), self.byte(self.cursor + 1));
                    self.text.replace_range(a..b, "");
                }
                return Edit::Changed;
            }
            0x41 if ctrl => self.select_all(),
            0x43 if ctrl => {
                if let Some((a, b)) = self.selection() {
                    set_clipboard(&self.text[self.byte(a)..self.byte(b)]);
                }
            }
            0x58 if ctrl => {
                if let Some((a, b)) = self.selection() {
                    set_clipboard(&self.text[self.byte(a)..self.byte(b)]);
                    self.delete_selection();
                    return Edit::Changed;
                }
            }
            0x56 if ctrl => {
                if let Some(t) = get_clipboard() {
                    let line = t.lines().next().unwrap_or("").to_string();
                    self.insert(&line);
                    return Edit::Changed;
                }
            }
            _ => {}
        }
        Edit::None
    }

    /// Курсор в позицию по координате x (клик мышью); r — где нарисовано поле.
    pub fn click(&mut self, x: f32, r: R, font: Font, size: f32, center: bool) {
        let start = if center { r.cx() - text::width(&self.text, font, size) / 2.0 } else { r.x };
        let mut best = 0;
        let mut bd = f32::MAX;
        let chars: Vec<char> = self.text.chars().collect();
        for i in 0..=chars.len() {
            let s: String = chars[..i].iter().collect();
            let d = (start + text::width(&s, font, size) - x).abs();
            if d < bd {
                bd = d;
                best = i;
            }
        }
        self.move_to(best, false);
    }

    /// Нарисовать: текст (или подсказку), выделение и мигающий курсор.
    pub fn paint(&self, cv: &mut Canvas, r: R, font: Font, size: f32, color: Color, placeholder: &str, center: bool) {
        let (line, asc) = text::metrics(font, size);
        let base = r.y + (r.h - line) / 2.0 + asc;
        let tw = text::width(&self.text, font, size);
        let x0 = if center { r.cx() - tw / 2.0 } else { r.x };
        let prefix = |i: usize| -> f32 { text::width(&self.text.chars().take(i).collect::<String>(), font, size) };
        if let Some((a, b)) = self.selection() {
            let (xa, xb) = (x0 + prefix(a), x0 + prefix(b));
            cv.fill_rect(R::new(xa, r.y + (r.h - line) / 2.0, xb - xa, line), rgba(10, 132, 255, 140));
        }
        if self.text.is_empty() {
            if !placeholder.is_empty() {
                let pw = text::width(placeholder, font, size);
                let px = if center { r.cx() - pw / 2.0 } else { r.x };
                text::draw_at(cv, placeholder, font, size, px.round(), base.round(), with_alpha(color, 0.45));
            }
        } else {
            text::draw_at(cv, &self.text, font, size, x0.round(), base.round(), color);
        }
        if self.focused && self.caret_on() {
            let cx = (x0 + prefix(self.cursor)).round();
            cv.fill_rect(R::new(cx, r.y + (r.h - line) / 2.0 + 1.0, (size / 14.0).max(1.0).round(), line - 2.0), color);
        }
    }

    pub fn caret_on(&self) -> bool {
        ((crate::anim::now() - self.blink_from) % 1.06) < 0.53
    }
}

pub fn set_clipboard(s: &str) {
    use windows::Win32::Foundation::{HANDLE, HGLOBAL};
    use windows::Win32::System::DataExchange::{CloseClipboard, EmptyClipboard, OpenClipboard, SetClipboardData};
    use windows::Win32::System::Memory::{GlobalAlloc, GlobalLock, GlobalUnlock, GMEM_MOVEABLE};
    let w: Vec<u16> = s.encode_utf16().chain(std::iter::once(0)).collect();
    unsafe {
        if OpenClipboard(None).is_err() {
            return;
        }
        let _ = EmptyClipboard();
        if let Ok(h) = GlobalAlloc(GMEM_MOVEABLE, w.len() * 2) {
            let p = GlobalLock(h) as *mut u16;
            if !p.is_null() {
                std::ptr::copy_nonoverlapping(w.as_ptr(), p, w.len());
                let _ = GlobalUnlock(h);
                let _ = SetClipboardData(13, Some(HANDLE(h.0))); // CF_UNICODETEXT
            } else {
                let _ = windows::Win32::Foundation::GlobalFree(Some(HGLOBAL(h.0)));
            }
        }
        let _ = CloseClipboard();
    }
}

pub fn get_clipboard() -> Option<String> {
    use windows::Win32::Foundation::HGLOBAL;
    use windows::Win32::System::DataExchange::{CloseClipboard, GetClipboardData, OpenClipboard};
    use windows::Win32::System::Memory::{GlobalLock, GlobalUnlock};
    unsafe {
        OpenClipboard(None).ok()?;
        let out = GetClipboardData(13).ok().and_then(|h| {
            let g = HGLOBAL(h.0);
            let p = GlobalLock(g) as *const u16;
            if p.is_null() {
                return None;
            }
            let mut n = 0;
            while *p.add(n) != 0 {
                n += 1;
            }
            let s = String::from_utf16_lossy(std::slice::from_raw_parts(p, n));
            let _ = GlobalUnlock(g);
            Some(s)
        });
        let _ = CloseClipboard();
        out
    }
}

//! Текст: шрифты Windows (Segoe UI и др.), растеризация ab_glyph с кэшем глифов.
//!
//! Файлы шрифтов отображаются в память (MapViewOfFile), а не читаются: страницы общие с системой
//! и не считаются собственной памятью процесса.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use ab_glyph::{Font as _, FontRef, GlyphId, PxScale, ScaleFont};

use crate::gfx::{Canvas, Color, R};

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum Font {
    Regular,
    Semibold,
    Bold,
    Light,
    /// Segoe MDL2 Assets / Segoe Fluent Icons — значки.
    Icons,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Align {
    Left,
    Center,
    Right,
}

struct Fonts {
    faces: Vec<(Font, FontRef<'static>)>,
    /// запасные шрифты для символов, которых нет в основном (стрелки, «⌘», эмодзи-контуры)
    fallback: Vec<FontRef<'static>>,
}

fn map_file(path: &str) -> Option<&'static [u8]> {
    use windows::core::HSTRING;
    use windows::Win32::Foundation::{CloseHandle, GENERIC_READ};
    use windows::Win32::Storage::FileSystem::{CreateFileW, GetFileSize, FILE_ATTRIBUTE_NORMAL, FILE_SHARE_READ, OPEN_EXISTING};
    use windows::Win32::System::Memory::{CreateFileMappingW, MapViewOfFile, FILE_MAP_READ, PAGE_READONLY};
    unsafe {
        let h = CreateFileW(&HSTRING::from(path), GENERIC_READ.0, FILE_SHARE_READ, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None).ok()?;
        let size = GetFileSize(h, None);
        if size == 0 || size == u32::MAX {
            let _ = CloseHandle(h);
            return None;
        }
        let m = CreateFileMappingW(h, None, PAGE_READONLY, 0, 0, None).ok();
        let _ = CloseHandle(h);
        let m = m?;
        let view = MapViewOfFile(m, FILE_MAP_READ, 0, 0, 0);
        let _ = CloseHandle(m);
        if view.Value.is_null() {
            return None;
        }
        Some(std::slice::from_raw_parts(view.Value as *const u8, size as usize))
    }
}

fn fonts_dir() -> String {
    let root = std::env::var("SystemRoot").unwrap_or_else(|_| "C:\\Windows".into());
    format!("{root}\\Fonts\\")
}

fn load(file: &str) -> Option<FontRef<'static>> {
    let data = map_file(&(fonts_dir() + file))?;
    FontRef::try_from_slice(data).ok()
}

impl Fonts {
    fn new() -> Fonts {
        let mut faces = Vec::new();
        let regular = load("segoeui.ttf");
        for (f, files) in [
            (Font::Regular, &["segoeui.ttf"][..]),
            (Font::Semibold, &["seguisb.ttf", "segoeuib.ttf", "segoeui.ttf"][..]),
            (Font::Bold, &["segoeuib.ttf", "seguisb.ttf", "segoeui.ttf"][..]),
            (Font::Light, &["segoeuil.ttf", "segoeuisl.ttf", "segoeui.ttf"][..]),
            (Font::Icons, &["SegoeIcons.ttf", "segmdl2.ttf"][..]),
        ] {
            if let Some(face) = files.iter().find_map(|f| load(f)) {
                faces.push((f, face));
            } else if let Some(r) = regular.clone() {
                faces.push((f, r));
            }
        }
        let fallback = ["seguisym.ttf", "segmdl2.ttf", "seguiemj.ttf", "msyh.ttc", "arial.ttf"]
            .iter()
            .filter_map(|f| load(f))
            .collect();
        Fonts { faces, fallback }
    }

    fn face(&self, f: Font) -> Option<&FontRef<'static>> {
        self.faces.iter().find(|(k, _)| *k == f).map(|(_, v)| v).or_else(|| self.faces.first().map(|(_, v)| v))
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
struct GlyphKey {
    font: u8, // 0..=4 — Font, 10+ — запасной
    id: u16,
    size: u32, // px * 16
    sub: u8,   // доля пикселя по x (четверти)
}

struct GlyphBitmap {
    left: i32,
    top: i32,
    w: u32,
    h: u32,
    cov: Vec<u8>,
}

struct TextEngine {
    fonts: Fonts,
    glyphs: HashMap<GlyphKey, Rc<GlyphBitmap>>,
    gamma: [u8; 256],
}

thread_local! {
    static ENGINE: RefCell<Option<TextEngine>> = const { RefCell::new(None) };
}

fn with_engine<T>(f: impl FnOnce(&mut TextEngine) -> T) -> T {
    ENGINE.with(|e| {
        let mut e = e.borrow_mut();
        if e.is_none() {
            let mut gamma = [0u8; 256];
            for (i, g) in gamma.iter_mut().enumerate() {
                // чуть «плотнее», как ClearType-градации Windows: тонкие штрихи не выглядят серыми
                *g = ((i as f32 / 255.0).powf(0.78) * 255.0).round() as u8;
            }
            *e = Some(TextEngine { fonts: Fonts::new(), glyphs: HashMap::new(), gamma });
        }
        f(e.as_mut().unwrap())
    })
}

/// Сбросить кэш глифов (закрыли большое окно — Launchpad, настройки).
pub fn trim_cache() {
    ENGINE.with(|e| {
        if let Some(e) = e.borrow_mut().as_mut() {
            e.glyphs.clear();
            e.glyphs.shrink_to_fit();
        }
    });
}

fn font_index(f: Font) -> u8 {
    match f {
        Font::Regular => 0,
        Font::Semibold => 1,
        Font::Bold => 2,
        Font::Light => 3,
        Font::Icons => 4,
    }
}

impl TextEngine {
    /// Шрифт и глиф для символа: основной шрифт, а если в нём символа нет — запасные.
    fn resolve(&self, font: Font, ch: char) -> (u8, &FontRef<'static>, GlyphId) {
        let main = self.fonts.face(font).expect("нет шрифтов");
        let id = main.glyph_id(ch);
        if id.0 != 0 || ch.is_whitespace() {
            return (font_index(font), main, id);
        }
        for (i, fb) in self.fonts.fallback.iter().enumerate() {
            let id = fb.glyph_id(ch);
            if id.0 != 0 {
                return (10 + i as u8, fb, id);
            }
        }
        (font_index(font), main, id)
    }

    fn scale_for(face: &FontRef<'static>, size_px: f32) -> PxScale {
        // size_px — кегль (em), как font-size в Qt/CSS; ab_glyph считает масштаб по высоте ascent−descent
        let upem = face.units_per_em().unwrap_or(2048.0);
        PxScale::from(size_px * face.height_unscaled() / upem)
    }

    fn layout(&self, text: &str, font: Font, size: f32) -> (Vec<(u8, GlyphId, f32)>, f32) {
        let mut out = Vec::with_capacity(text.len());
        let mut x = 0.0f32;
        let mut prev: Option<(u8, GlyphId)> = None;
        for ch in text.chars() {
            let (fi, face, id) = self.resolve(font, ch);
            let sf = face.as_scaled(Self::scale_for(face, size));
            if let Some((pf, pid)) = prev {
                if pf == fi {
                    x += sf.kern(pid, id);
                }
            }
            out.push((fi, id, x));
            x += sf.h_advance(id);
            prev = Some((fi, id));
        }
        (out, x)
    }

    fn face_by_index(&self, fi: u8) -> &FontRef<'static> {
        if fi >= 10 {
            &self.fonts.fallback[(fi - 10) as usize]
        } else {
            let f = [Font::Regular, Font::Semibold, Font::Bold, Font::Light, Font::Icons][fi as usize];
            self.fonts.face(f).unwrap()
        }
    }

    fn glyph(&mut self, fi: u8, id: GlyphId, size: f32, sub: f32) -> Option<Rc<GlyphBitmap>> {
        let key = GlyphKey { font: fi, id: id.0, size: (size * 16.0) as u32, sub: (sub * 4.0) as u8 & 3 };
        if let Some(g) = self.glyphs.get(&key) {
            return Some(g.clone());
        }
        let face = self.face_by_index(fi).clone();
        let scale = Self::scale_for(&face, size);
        let glyph = id.with_scale_and_position(scale, ab_glyph::point(key.sub as f32 / 4.0, 0.0));
        let outlined = face.outline_glyph(glyph)?;
        let b = outlined.px_bounds();
        let (w, h) = (b.width().ceil() as u32, b.height().ceil() as u32);
        let mut cov = vec![0u8; (w * h) as usize];
        outlined.draw(|x, y, c| {
            if x < w && y < h {
                cov[(y * w + x) as usize] = self.gamma[(c.clamp(0.0, 1.0) * 255.0) as usize];
            }
        });
        let g = Rc::new(GlyphBitmap { left: b.min.x.floor() as i32, top: b.min.y.floor() as i32, w, h, cov });
        if self.glyphs.len() > 3000 {
            self.glyphs.clear();
        }
        self.glyphs.insert(key, g.clone());
        Some(g)
    }
}

/// Высота строки и положение базовой линии для кегля size.
pub fn metrics(font: Font, size: f32) -> (f32, f32) {
    with_engine(|e| {
        let face = e.fonts.face(font).unwrap();
        let sf = face.as_scaled(TextEngine::scale_for(face, size));
        (sf.ascent() - sf.descent() + sf.line_gap(), sf.ascent())
    })
}

pub fn width(text: &str, font: Font, size: f32) -> f32 {
    with_engine(|e| e.layout(text, font, size).1)
}

/// Обрезать текст с «…», чтобы он влез в max_w.
pub fn elide(text: &str, font: Font, size: f32, max_w: f32) -> String {
    if width(text, font, size) <= max_w {
        return text.to_string();
    }
    let chars: Vec<char> = text.chars().collect();
    let (mut lo, mut hi) = (0usize, chars.len());
    while lo < hi {
        let mid = (lo + hi).div_ceil(2);
        let s: String = chars[..mid].iter().collect::<String>() + "…";
        if width(&s, font, size) <= max_w {
            lo = mid;
        } else {
            hi = mid - 1;
        }
    }
    chars[..lo].iter().collect::<String>().trim_end().to_string() + "…"
}

/// Нарисовать строку: (x, baseline) — начало базовой линии.
pub fn draw_at(cv: &mut Canvas, text: &str, font: Font, size: f32, x: f32, baseline: f32, color: Color) {
    let alpha = color.alpha() * cv.opacity;
    if alpha <= 0.0 || text.is_empty() {
        return;
    }
    // цвет уже «в перевёрнутых каналах» (gfx::rgba) — кладём как есть
    let (cr, cg, cb) = (color.red(), color.green(), color.blue());
    with_engine(|e| {
        let (glyphs, _) = e.layout(text, font, size);
        let pw = cv.pm.width() as i32;
        let ph = cv.pm.height() as i32;
        let data = cv.pm.data_mut();
        for (fi, id, gx) in glyphs {
            let px = x + gx;
            let ix = px.floor();
            let Some(g) = e.glyph(fi, id, size, px - ix) else { continue };
            let ox = ix as i32 + g.left;
            let oy = baseline.round() as i32 + g.top;
            for row in 0..g.h as i32 {
                let yy = oy + row;
                if yy < 0 || yy >= ph {
                    continue;
                }
                for col in 0..g.w as i32 {
                    let xx = ox + col;
                    if xx < 0 || xx >= pw {
                        continue;
                    }
                    let c = g.cov[(row as u32 * g.w + col as u32) as usize];
                    if c == 0 {
                        continue;
                    }
                    let a = alpha * c as f32 / 255.0;
                    let i = ((yy * pw + xx) * 4) as usize;
                    let inv = 1.0 - a;
                    data[i] = (cr * a * 255.0 + data[i] as f32 * inv).min(255.0) as u8;
                    data[i + 1] = (cg * a * 255.0 + data[i + 1] as f32 * inv).min(255.0) as u8;
                    data[i + 2] = (cb * a * 255.0 + data[i + 2] as f32 * inv).min(255.0) as u8;
                    data[i + 3] = (a * 255.0 + data[i + 3] as f32 * inv).min(255.0) as u8;
                }
            }
        }
    });
}

/// Строка в прямоугольнике: по вертикали — по центру, по горизонтали — align. Не влезает — «…».
pub fn draw_in(cv: &mut Canvas, text: &str, font: Font, size: f32, r: R, align: Align, color: Color) {
    let s = elide(text, font, size, r.w.max(1.0));
    let w = width(&s, font, size);
    let (line, ascent) = metrics(font, size);
    let x = match align {
        Align::Left => r.x,
        Align::Center => r.x + (r.w - w) / 2.0,
        Align::Right => r.right() - w,
    };
    let baseline = r.y + (r.h - line) / 2.0 + ascent;
    draw_at(cv, &s, font, size, x.round(), baseline, color);
}

/// Перенос по словам: строки, которые влезают в max_w (не больше max_lines; последняя — с «…»).
pub fn wrap(text: &str, font: Font, size: f32, max_w: f32, max_lines: usize) -> Vec<String> {
    let mut lines = Vec::new();
    let mut cur = String::new();
    for word in text.split_whitespace() {
        let cand = if cur.is_empty() { word.to_string() } else { format!("{cur} {word}") };
        if width(&cand, font, size) <= max_w || cur.is_empty() {
            cur = cand;
        } else {
            lines.push(std::mem::take(&mut cur));
            cur = word.to_string();
        }
    }
    if !cur.is_empty() {
        lines.push(cur);
    }
    if lines.len() > max_lines {
        let rest = lines[max_lines - 1..].join(" ");
        lines.truncate(max_lines - 1);
        lines.push(elide(&rest, font, size, max_w));
    }
    lines.into_iter().map(|l| elide(&l, font, size, max_w)).collect()
}

//! Отрисовка: tiny-skia поверх буфера окна.
//!
//! Буфер слоистого окна Windows — BGRA с предумноженной альфой, а tiny-skia считает свои пиксели RGBA.
//! Чтобы не переставлять каналы на каждом кадре, всё рисуется «в перевёрнутых каналах»: цвета из кода
//! переводятся функцией [`rgba`] (меняет местами R и B), а картинки от Windows (иконки, снимки окон)
//! и так лежат как BGRA — их можно класть в буфер как есть.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

pub use tiny_skia::{
    BlendMode, Color, FillRule, FilterQuality, GradientStop, LinearGradient, Mask, Paint, PathBuilder, Pixmap,
    PixmapMut, PixmapPaint, Point, RadialGradient, Rect, Shader, SpreadMode, Stroke, Transform,
};

/// Цвет из кода → цвет для буфера (R и B меняются местами, см. описание модуля).
pub fn rgba(r: u8, g: u8, b: u8, a: u8) -> Color {
    Color::from_rgba8(b, g, r, a)
}

pub fn rgbf(r: f32, g: f32, b: f32, a: f32) -> Color {
    Color::from_rgba(b.clamp(0.0, 1.0), g.clamp(0.0, 1.0), r.clamp(0.0, 1.0), a.clamp(0.0, 1.0)).unwrap_or(Color::BLACK)
}

pub fn with_alpha(c: Color, a: f32) -> Color {
    let mut c = c;
    c.set_alpha((c.alpha() * a).clamp(0.0, 1.0));
    c
}

/// «#rrggbb» / «#aarrggbb» → цвет.
pub fn parse_hex(s: &str) -> Option<Color> {
    let h = s.trim().trim_start_matches('#');
    let v = u32::from_str_radix(h, 16).ok()?;
    match h.len() {
        6 => Some(rgba((v >> 16) as u8, (v >> 8) as u8, v as u8, 255)),
        8 => Some(rgba((v >> 16) as u8, (v >> 8) as u8, v as u8, (v >> 24) as u8)),
        3 => {
            let r = ((v >> 8) & 0xF) as u8 * 17;
            let g = ((v >> 4) & 0xF) as u8 * 17;
            let b = (v & 0xF) as u8 * 17;
            Some(rgba(r, g, b, 255))
        }
        _ => None,
    }
}

pub fn to_hex(c: Color) -> String {
    // в буфере каналы переставлены — обратно: red() — это синий
    let u = c.to_color_u8();
    format!("#{:02x}{:02x}{:02x}", u.blue(), u.green(), u.red())
}

pub fn lerp_color(a: Color, b: Color, t: f32) -> Color {
    let t = t.clamp(0.0, 1.0);
    Color::from_rgba(
        a.red() + (b.red() - a.red()) * t,
        a.green() + (b.green() - a.green()) * t,
        a.blue() + (b.blue() - a.blue()) * t,
        a.alpha() + (b.alpha() - a.alpha()) * t,
    )
    .unwrap_or(a)
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct R {
    pub x: f32,
    pub y: f32,
    pub w: f32,
    pub h: f32,
}

impl R {
    pub const fn new(x: f32, y: f32, w: f32, h: f32) -> R {
        R { x, y, w, h }
    }
    pub fn from_ltrb(l: f32, t: f32, r: f32, b: f32) -> R {
        R { x: l, y: t, w: r - l, h: b - t }
    }
    pub fn right(&self) -> f32 {
        self.x + self.w
    }
    pub fn bottom(&self) -> f32 {
        self.y + self.h
    }
    pub fn cx(&self) -> f32 {
        self.x + self.w / 2.0
    }
    pub fn cy(&self) -> f32 {
        self.y + self.h / 2.0
    }
    pub fn contains(&self, x: f32, y: f32) -> bool {
        x >= self.x && x < self.x + self.w && y >= self.y && y < self.y + self.h
    }
    pub fn inset(&self, d: f32) -> R {
        R::new(self.x + d, self.y + d, self.w - 2.0 * d, self.h - 2.0 * d)
    }
    pub fn adjusted(&self, l: f32, t: f32, r: f32, b: f32) -> R {
        R::from_ltrb(self.x + l, self.y + t, self.right() + r, self.bottom() + b)
    }
    pub fn translated(&self, dx: f32, dy: f32) -> R {
        R::new(self.x + dx, self.y + dy, self.w, self.h)
    }
    pub fn centered(cx: f32, cy: f32, w: f32, h: f32) -> R {
        R::new(cx - w / 2.0, cy - h / 2.0, w, h)
    }
    pub fn scaled(&self, k: f32) -> R {
        R::centered(self.cx(), self.cy(), self.w * k, self.h * k)
    }
    pub fn lerp(&self, o: &R, k: f32) -> R {
        R::new(lerp(self.x, o.x, k), lerp(self.y, o.y, k), lerp(self.w, o.w, k), lerp(self.h, o.h, k))
    }
    pub fn near(&self, o: &R, eps: f32) -> bool {
        (self.x - o.x).abs() < eps && (self.y - o.y).abs() < eps && (self.w - o.w).abs() < eps && (self.h - o.h).abs() < eps
    }
    pub fn intersect(&self, o: &R) -> Option<R> {
        let l = self.x.max(o.x);
        let t = self.y.max(o.y);
        let r = self.right().min(o.right());
        let b = self.bottom().min(o.bottom());
        (r > l && b > t).then(|| R::from_ltrb(l, t, r, b))
    }
    pub fn union(&self, o: &R) -> R {
        R::from_ltrb(self.x.min(o.x), self.y.min(o.y), self.right().max(o.right()), self.bottom().max(o.bottom()))
    }
    pub fn sk(&self) -> Option<Rect> {
        Rect::from_xywh(self.x, self.y, self.w.max(0.01), self.h.max(0.01))
    }
}

pub fn lerp(a: f32, b: f32, t: f32) -> f32 {
    a + (b - a) * t
}

pub fn clamp01(x: f32) -> f32 {
    x.clamp(0.0, 1.0)
}

/// Плавная «ступенька» (как ease в Python-версии).
pub fn ease(t: f32) -> f32 {
    let t = clamp01(t);
    t * t * (3.0 - 2.0 * t)
}

pub fn ease_out(t: f32) -> f32 {
    let t = clamp01(t);
    1.0 - (1.0 - t).powi(3)
}

pub fn ease_in_out_cubic(t: f32) -> f32 {
    let t = clamp01(t);
    if t < 0.5 {
        4.0 * t * t * t
    } else {
        1.0 - (-2.0 * t + 2.0).powi(3) / 2.0
    }
}

/// «Перелёт» в конце (OutBack) — для пружинистых появлений.
pub fn ease_out_back(t: f32) -> f32 {
    let t = clamp01(t);
    let c1 = 1.70158;
    let c3 = c1 + 1.0;
    1.0 + c3 * (t - 1.0).powi(3) + c1 * (t - 1.0).powi(2)
}

pub const FRAME_REF: f32 = 0.014;

/// Коэффициент «догоняния» k (подобран для кадра 14 мс) для кадра длиной dt.
pub fn smooth(k: f32, dt: f32) -> f32 {
    1.0 - (1.0 - k).powf(dt / FRAME_REF)
}

pub fn rounded_rect(r: R, radius: f32) -> Option<tiny_skia::Path> {
    let rad = radius.min(r.w / 2.0).min(r.h / 2.0).max(0.0);
    let mut pb = PathBuilder::new();
    if rad <= 0.01 {
        pb.push_rect(r.sk()?);
        return pb.finish();
    }
    // скругление «как в Qt»: дуги Безье (k = 0.5523)
    let k = 0.552_284_8 * rad;
    let (l, t, rr, b) = (r.x, r.y, r.right(), r.bottom());
    pb.move_to(l + rad, t);
    pb.line_to(rr - rad, t);
    pb.cubic_to(rr - rad + k, t, rr, t + rad - k, rr, t + rad);
    pb.line_to(rr, b - rad);
    pb.cubic_to(rr, b - rad + k, rr - rad + k, b, rr - rad, b);
    pb.line_to(l + rad, b);
    pb.cubic_to(l + rad - k, b, l, b - rad + k, l, b - rad);
    pb.line_to(l, t + rad);
    pb.cubic_to(l, t + rad - k, l + rad - k, t, l + rad, t);
    pb.close();
    pb.finish()
}

/// «Сквиркл» как у иконок macOS — суперэллипс (n≈5).
pub fn squircle(r: R) -> Option<tiny_skia::Path> {
    let mut pb = PathBuilder::new();
    let n = 64;
    let (cx, cy, a, b) = (r.cx(), r.cy(), r.w / 2.0, r.h / 2.0);
    for i in 0..=n {
        let th = i as f32 / n as f32 * std::f32::consts::TAU;
        let (c, s) = (th.cos(), th.sin());
        let x = cx + a * c.signum() * c.abs().powf(2.0 / 5.0);
        let y = cy + b * s.signum() * s.abs().powf(2.0 / 5.0);
        if i == 0 {
            pb.move_to(x, y);
        } else {
            pb.line_to(x, y);
        }
    }
    pb.close();
    pb.finish()
}

pub fn circle(cx: f32, cy: f32, r: f32) -> Option<tiny_skia::Path> {
    PathBuilder::from_circle(cx, cy, r.max(0.01))
}

pub fn solid(c: Color) -> Paint<'static> {
    let mut p = Paint::default();
    p.set_color(c);
    p.anti_alias = true;
    p
}

/// Холст поверх буфера окна (или любой Pixmap).
pub struct Canvas<'a> {
    pub pm: PixmapMut<'a>,
    pub opacity: f32,
    pub clip: Option<Mask>,
}

impl<'a> Canvas<'a> {
    pub fn new(pm: PixmapMut<'a>) -> Self {
        Canvas { pm, opacity: 1.0, clip: None }
    }
    pub fn width(&self) -> f32 {
        self.pm.width() as f32
    }
    pub fn height(&self) -> f32 {
        self.pm.height() as f32
    }
    pub fn clear(&mut self) {
        self.pm.fill(Color::TRANSPARENT);
    }
    pub fn fill_all(&mut self, c: Color) {
        self.pm.fill(c);
    }

    fn paint(&self, c: Color) -> Paint<'static> {
        solid(with_alpha(c, self.opacity))
    }

    pub fn fill_path(&mut self, path: &tiny_skia::Path, c: Color) {
        let p = self.paint(c);
        self.pm.fill_path(path, &p, FillRule::Winding, Transform::identity(), self.clip.as_ref());
    }

    pub fn fill_path_shader(&mut self, path: &tiny_skia::Path, shader: Shader) {
        let mut p = Paint::default();
        p.shader = shader;
        p.anti_alias = true;
        if self.opacity < 1.0 {
            // у шейдера нет общей прозрачности — нарисуем в отдельный слой
            self.layered(|cv| cv.fill_path_shader(path, p.shader.clone()));
            return;
        }
        self.pm.fill_path(path, &p, FillRule::Winding, Transform::identity(), self.clip.as_ref());
    }

    pub fn stroke_path(&mut self, path: &tiny_skia::Path, c: Color, width: f32) {
        let p = self.paint(c);
        let st = Stroke { width, ..Stroke::default() };
        self.pm.stroke_path(path, &p, &st, Transform::identity(), self.clip.as_ref());
    }

    pub fn stroke_path_shader(&mut self, path: &tiny_skia::Path, shader: Shader, width: f32) {
        let mut p = Paint::default();
        p.shader = shader;
        p.anti_alias = true;
        let st = Stroke { width, ..Stroke::default() };
        self.pm.stroke_path(path, &p, &st, Transform::identity(), self.clip.as_ref());
    }

    pub fn fill_rect(&mut self, r: R, c: Color) {
        if let Some(rect) = r.sk() {
            let p = self.paint(c);
            self.pm.fill_rect(rect, &p, Transform::identity(), self.clip.as_ref());
        }
    }

    pub fn fill_rrect(&mut self, r: R, radius: f32, c: Color) {
        if let Some(path) = rounded_rect(r, radius) {
            self.fill_path(&path, c);
        }
    }

    pub fn stroke_rrect(&mut self, r: R, radius: f32, c: Color, width: f32) {
        if let Some(path) = rounded_rect(r, radius) {
            self.stroke_path(&path, c, width);
        }
    }

    pub fn fill_circle(&mut self, cx: f32, cy: f32, r: f32, c: Color) {
        if let Some(path) = circle(cx, cy, r) {
            self.fill_path(&path, c);
        }
    }

    pub fn stroke_circle(&mut self, cx: f32, cy: f32, r: f32, c: Color, w: f32) {
        if let Some(path) = circle(cx, cy, r) {
            self.stroke_path(&path, c, w);
        }
    }

    pub fn line(&mut self, x0: f32, y0: f32, x1: f32, y1: f32, c: Color, w: f32) {
        let mut pb = PathBuilder::new();
        pb.move_to(x0, y0);
        pb.line_to(x1, y1);
        if let Some(path) = pb.finish() {
            let p = self.paint(c);
            let st = Stroke { width: w, line_cap: tiny_skia::LineCap::Round, ..Stroke::default() };
            self.pm.stroke_path(&path, &p, &st, Transform::identity(), self.clip.as_ref());
        }
    }

    pub fn polyline(&mut self, pts: &[(f32, f32)], c: Color, w: f32) {
        let mut pb = PathBuilder::new();
        for (i, &(x, y)) in pts.iter().enumerate() {
            if i == 0 {
                pb.move_to(x, y)
            } else {
                pb.line_to(x, y)
            }
        }
        if let Some(path) = pb.finish() {
            let p = self.paint(c);
            let st = Stroke {
                width: w,
                line_cap: tiny_skia::LineCap::Round,
                line_join: tiny_skia::LineJoin::Round,
                ..Stroke::default()
            };
            self.pm.stroke_path(&path, &p, &st, Transform::identity(), self.clip.as_ref());
        }
    }

    /// Картинка в прямоугольник dst (масштаб — качественный).
    pub fn image(&mut self, img: &Image, dst: R) {
        self.image_alpha(img, dst, 1.0)
    }

    pub fn image_alpha(&mut self, img: &Image, dst: R, alpha: f32) {
        if dst.w < 0.5 || dst.h < 0.5 {
            return;
        }
        let sx = dst.w / img.w as f32;
        let sy = dst.h / img.h as f32;
        let q = if (sx - 1.0).abs() < 0.001 && (sy - 1.0).abs() < 0.001 && dst.x.fract() == 0.0 && dst.y.fract() == 0.0 {
            FilterQuality::Nearest
        } else {
            FilterQuality::Bilinear
        };
        let paint = PixmapPaint { opacity: (alpha * self.opacity).clamp(0.0, 1.0), blend_mode: BlendMode::SourceOver, quality: q };
        let t = Transform::from_row(sx, 0.0, 0.0, sy, dst.x, dst.y);
        self.pm.draw_pixmap(0, 0, img.pm.as_ref(), &paint, t, self.clip.as_ref());
    }

    /// Картинка, обрезанная по форме path (заливка шейдером-картинкой — края сглажены).
    pub fn image_in_path(&mut self, img: &Image, dst: R, path: &tiny_skia::Path, alpha: f32) {
        let sx = dst.w / img.w as f32;
        let sy = dst.h / img.h as f32;
        let shader = tiny_skia::Pattern::new(
            img.pm.as_ref(),
            SpreadMode::Pad,
            FilterQuality::Bilinear,
            (alpha * self.opacity).clamp(0.0, 1.0),
            Transform::from_row(sx, 0.0, 0.0, sy, dst.x, dst.y),
        );
        let mut p = Paint::default();
        p.shader = shader;
        p.anti_alias = true;
        self.pm.fill_path(path, &p, FillRule::Winding, Transform::identity(), self.clip.as_ref());
    }

    /// Нарисовать содержимое в отдельный слой и положить его с общей прозрачностью.
    pub fn layered(&mut self, f: impl FnOnce(&mut Canvas)) {
        let (w, h) = (self.pm.width(), self.pm.height());
        if let Some(mut layer) = Pixmap::new(w, h) {
            {
                let mut cv = Canvas::new(layer.as_mut());
                f(&mut cv);
            }
            let paint = PixmapPaint { opacity: self.opacity, blend_mode: BlendMode::SourceOver, quality: FilterQuality::Nearest };
            self.pm.draw_pixmap(0, 0, layer.as_ref(), &paint, Transform::identity(), self.clip.as_ref());
        }
    }

    /// Мягкая тень под скруглённым прямоугольником (размытие маски).
    pub fn shadow(&mut self, r: R, radius: f32, blur: f32, dy: f32, alpha: f32) {
        let pad = (blur * 2.0).ceil() as i32 + 2;
        let w = (r.w.ceil() as i32 + pad * 2).max(1) as u32;
        let h = (r.h.ceil() as i32 + pad * 2).max(1) as u32;
        let key = (w, h, (radius * 4.0) as u32, (blur * 4.0) as u32);
        let img = SHADOWS.with(|cache| {
            let mut cache = cache.borrow_mut();
            if let Some(i) = cache.get(&key) {
                return i.clone();
            }
            let mut pm = Pixmap::new(w, h).unwrap();
            if let Some(path) = rounded_rect(R::new(pad as f32, pad as f32, r.w, r.h), radius) {
                let p = solid(Color::BLACK);
                pm.fill_path(&path, &p, FillRule::Winding, Transform::identity(), None);
            }
            blur_alpha(&mut pm, blur);
            let img = Rc::new(Image::from_pixmap(pm));
            if cache.len() > 24 {
                cache.clear();
            }
            cache.insert(key, img.clone());
            img
        });
        self.image_alpha(&img, R::new(r.x - pad as f32, r.y - pad as f32 + dy, w as f32, h as f32), alpha);
    }
}

thread_local! {
    static SHADOWS: RefCell<HashMap<(u32, u32, u32, u32), Rc<Image>>> = RefCell::new(HashMap::new());
}

/// Картинка в памяти (BGRA, предумноженная альфа — как буфер окна).
pub struct Image {
    pub pm: Pixmap,
    pub w: u32,
    pub h: u32,
}

impl Image {
    pub fn from_pixmap(pm: Pixmap) -> Image {
        let (w, h) = (pm.width(), pm.height());
        Image { pm, w, h }
    }

    /// Из буфера BGRA с предумноженной альфой (как отдаёт Windows).
    pub fn from_bgra_premul(w: u32, h: u32, data: Vec<u8>) -> Option<Image> {
        let size = tiny_skia::IntSize::from_wh(w, h)?;
        let pm = Pixmap::from_vec(data, size)?;
        Some(Image { pm, w, h })
    }

    pub fn new(w: u32, h: u32) -> Option<Image> {
        Pixmap::new(w.max(1), h.max(1)).map(Image::from_pixmap)
    }

    pub fn bytes(&self) -> usize {
        (self.w * self.h * 4) as usize
    }

    /// Качественное уменьшение: сначала пополам (усреднение 2×2), пока можно, потом билинейно.
    pub fn scaled(&self, w: u32, h: u32) -> Image {
        let (w, h) = (w.max(1), h.max(1));
        if w == self.w && h == self.h {
            return Image::from_pixmap(self.pm.clone());
        }
        let mut cur: Option<Pixmap> = None;
        let mut cw = self.w;
        let mut ch = self.h;
        while cw / 2 >= w && ch / 2 >= h && cw >= 2 && ch >= 2 {
            let src = cur.as_ref().unwrap_or(&self.pm);
            let half = halve(src);
            cw = half.width();
            ch = half.height();
            cur = Some(half);
        }
        let src = cur.as_ref().unwrap_or(&self.pm);
        if cw == w && ch == h {
            return Image::from_pixmap(src.clone());
        }
        let mut out = Pixmap::new(w, h).unwrap();
        let paint = PixmapPaint { opacity: 1.0, blend_mode: BlendMode::Source, quality: FilterQuality::Bicubic };
        let t = Transform::from_scale(w as f32 / cw as f32, h as f32 / ch as f32);
        out.draw_pixmap(0, 0, src.as_ref(), &paint, t, None);
        Image::from_pixmap(out)
    }

    pub fn alpha_at(&self, x: u32, y: u32) -> u8 {
        if x >= self.w || y >= self.h {
            return 0;
        }
        self.pm.data()[((y * self.w + x) * 4 + 3) as usize]
    }
}

fn halve(src: &Pixmap) -> Pixmap {
    let (w, h) = ((src.width() / 2).max(1), (src.height() / 2).max(1));
    let mut out = Pixmap::new(w, h).unwrap();
    let sw = src.width() as usize;
    let s = src.data();
    let d = out.data_mut();
    for y in 0..h as usize {
        for x in 0..w as usize {
            let i0 = ((y * 2) * sw + x * 2) * 4;
            let i1 = i0 + 4;
            let i2 = i0 + sw * 4;
            let i3 = i2 + 4;
            let o = (y * w as usize + x) * 4;
            for c in 0..4 {
                let v = s[i0 + c] as u32 + s[i1 + c] as u32 + s[i2 + c] as u32 + s[i3 + c] as u32;
                d[o + c] = ((v + 2) / 4) as u8;
            }
        }
    }
    out
}

/// Размытие альфы (для теней): три прохода box ≈ гаусс.
pub fn blur_alpha(pm: &mut Pixmap, radius: f32) {
    let (w, h) = (pm.width() as usize, pm.height() as usize);
    let r = ((radius / 1.7) as usize).max(1);
    let mut a: Vec<f32> = pm.data().chunks(4).map(|p| p[3] as f32).collect();
    let mut tmp = vec![0f32; a.len()];
    for _ in 0..3 {
        box_pass(&a, &mut tmp, w, h, r, true);
        box_pass(&tmp, &mut a, w, h, r, false);
    }
    for (px, v) in pm.data_mut().chunks_mut(4).zip(a) {
        let v = v.clamp(0.0, 255.0) as u8;
        px[0] = 0;
        px[1] = 0;
        px[2] = 0;
        px[3] = v;
    }
}

fn box_pass(src: &[f32], dst: &mut [f32], w: usize, h: usize, r: usize, horiz: bool) {
    let (n, lines) = if horiz { (w, h) } else { (h, w) };
    let norm = 1.0 / (2 * r + 1) as f32;
    for line in 0..lines {
        let idx = |i: usize| if horiz { line * w + i } else { i * w + line };
        let at = |i: isize| src[idx(i.clamp(0, n as isize - 1) as usize)];
        let mut acc = 0.0;
        for i in -(r as isize)..=(r as isize) {
            acc += at(i);
        }
        for i in 0..n {
            dst[idx(i)] = acc * norm;
            acc += at(i as isize + r as isize + 1) - at(i as isize - r as isize);
        }
    }
}

/// Размытие цветной картинки (фон «стекла»): три прохода box + немного насыщенности.
pub fn blur_rgb(data: &mut [u8], w: usize, h: usize, radius: f32, saturation: f32) {
    let r = ((radius / 1.7) as usize).max(1);
    let mut ch: [Vec<f32>; 3] = [Vec::new(), Vec::new(), Vec::new()];
    for c in 0..3 {
        ch[c] = data.chunks(4).map(|p| p[c] as f32).collect();
    }
    let mut tmp = vec![0f32; w * h];
    for c in &mut ch {
        for _ in 0..3 {
            box_pass(c, &mut tmp, w, h, r, true);
            box_pass(&tmp, c, w, h, r, false);
        }
    }
    for (i, px) in data.chunks_mut(4).enumerate() {
        let (b, g, rr) = (ch[0][i], ch[1][i], ch[2][i]);
        let lum = 0.114 * b + 0.587 * g + 0.299 * rr;
        px[0] = (lum + (b - lum) * saturation).clamp(0.0, 255.0) as u8;
        px[1] = (lum + (g - lum) * saturation).clamp(0.0, 255.0) as u8;
        px[2] = (lum + (rr - lum) * saturation).clamp(0.0, 255.0) as u8;
        px[3] = 255;
    }
}

/// Кэш уменьшенных копий картинки (как pix_for в Python): шаг 4 px, не больше 24 размеров.
#[derive(Default)]
pub struct ScaledCache {
    items: Vec<(u32, Rc<Image>)>,
}

impl ScaledCache {
    pub fn get(&mut self, src: &Image, size: f32) -> Rc<Image> {
        let s = (((size / 4.0).round() as u32) * 4).max(4);
        if let Some((_, img)) = self.items.iter().find(|(k, _)| *k == s) {
            return img.clone();
        }
        if self.items.len() >= 24 {
            self.items.remove(0);
        }
        let (w, h) = if src.w >= src.h {
            (s, (s as f32 * src.h as f32 / src.w as f32).round().max(1.0) as u32)
        } else {
            ((s as f32 * src.w as f32 / src.h as f32).round().max(1.0) as u32, s)
        };
        let img = Rc::new(src.scaled(w, h));
        self.items.push((s, img.clone()));
        img
    }
    pub fn clear(&mut self) {
        self.items.clear();
    }
    pub fn bytes(&self) -> usize {
        self.items.iter().map(|(_, i)| i.bytes()).sum()
    }
}

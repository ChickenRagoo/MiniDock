//! Содержимое всплывающих окон: строки (заголовок, текст, «подпись … значение», полоса, кнопка, ползунок,
//! переключатель, календарь, карточка плеера…), нарисованные вручную. Новые данные меняются на месте,
//! полосы плавно доезжают; наведение подсвечивает кнопки, ползунки тянутся мышью.

use std::collections::HashMap;
use std::rc::Rc;

use crate::gfx::*;
use crate::text::{self, Align, Font};

pub const ACCENT: (u8, u8, u8) = (10, 132, 255);
pub const MIX: (u8, u8, u8) = (92, 150, 166);

pub fn accent(a: u8) -> Color {
    rgba(ACCENT.0, ACCENT.1, ACCENT.2, a)
}

#[derive(Clone)]
pub struct MediaInfo {
    pub id: usize,
    pub app: String,
    pub title: String,
    pub artist: String,
    pub playing: bool,
    pub cover: Option<Rc<Image>>,
    pub blurred: Option<Rc<Image>>,
    pub icon: Option<Rc<Image>>,
}

#[derive(Clone)]
pub enum Row {
    Title(String),
    /// заголовок с переключателем справа
    TitleToggle(String, u32, bool),
    Text(String),
    Section(String),
    Big(String),
    Value(String, String),
    Bar(String, f32, String),
    Sep,
    Button(u32, String, bool),
    /// две кнопки рядом
    Buttons(Vec<(u32, String)>),
    /// громкость: (id, доля, выключен, значок, значок «выключен», кнопка справа (id, значок, нажата))
    Volume(u32, f32, bool, &'static str, &'static str, Option<(u32, &'static str, bool)>),
    /// громкость программы: (id, значок, имя, доля)
    AppVolume(u32, Option<Rc<Image>>, String, f32),
    /// устройство: (id, имя, для музыки, для звонков)
    Device(u32, String, bool, bool),
    /// сетка значков трея: [(id, значок, подсказка)]
    IconGrid(Vec<(u32, Option<Rc<Image>>, String)>),
    /// календарь месяца: (год, месяц 1..12, сегодня (г, м, д), id кнопок «назад»/«вперёд»)
    Calendar(i32, u32, (i32, u32, u32), u32, u32),
    Media(MediaInfo),
    /// кольца: [(подпись, доля, текст в центре, мелкий текст)]
    Rings(Vec<(String, Option<f32>, String, String)>),
    /// плитки «число + подпись»
    Stats(Vec<(String, String)>),
    /// пустое место
    Space(f32),
}

impl Row {
    fn height(&self, w: f32) -> f32 {
        match self {
            Row::Title(_) | Row::TitleToggle(..) => 26.0,
            Row::Text(t) => 16.0 * text::wrap(t, Font::Regular, 11.0, w, 4).len().max(1) as f32 + 4.0,
            Row::Section(_) => 20.0,
            Row::Big(_) => 48.0,
            Row::Value(..) => 24.0,
            Row::Bar(..) => 40.0,
            Row::Sep => 13.0,
            Row::Button(..) | Row::Buttons(..) => 34.0,
            Row::Volume(..) => 34.0,
            Row::AppVolume(..) => 30.0,
            Row::Device(..) => 30.0,
            Row::IconGrid(v) => 44.0 * v.len().div_ceil(5).max(1) as f32,
            Row::Calendar(..) => 232.0,
            Row::Media(_) => 130.0,
            Row::Rings(_) => 156.0,
            Row::Stats(_) => 62.0,
            Row::Space(h) => *h,
        }
    }
}

/// Что под курсором: (id, часть) — часть: 0 основная, 1 вторая половина (устройства «для звонков»), 2 кнопка справа.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub struct Hit {
    pub id: u32,
    pub part: u8,
}

pub enum Event {
    Click(Hit),
    Slide(u32, f32),
}

pub struct Panel {
    pub rows: Vec<Row>,
    pub width: f32,
    pub pad: f32,
    pub scale: f32,
    hits: Vec<(R, Hit)>,
    sliders: Vec<(R, u32)>,
    pub hover: Option<Hit>,
    pub pressed: Option<Hit>,
    drag: Option<u32>,
    shown: HashMap<usize, f32>,
    targets: HashMap<usize, f32>,
    glow: HashMap<Hit, f32>,
    last: f64,
}

impl Panel {
    pub fn new(width: f32) -> Panel {
        Panel {
            rows: Vec::new(),
            width,
            pad: 14.0,
            scale: 1.0,
            hits: Vec::new(),
            sliders: Vec::new(),
            hover: None,
            pressed: None,
            drag: None,
            shown: HashMap::new(),
            targets: HashMap::new(),
            glow: HashMap::new(),
            last: crate::anim::now(),
        }
    }

    pub fn set_rows(&mut self, rows: Vec<Row>) {
        self.targets.clear();
        for (i, r) in rows.iter().enumerate() {
            match r {
                Row::Bar(_, f, _) => {
                    self.targets.insert(i, f.clamp(0.0, 1.0));
                }
                Row::Rings(g) => {
                    for (k, x) in g.iter().enumerate() {
                        self.targets.insert(1000 + i * 10 + k, x.1.unwrap_or(0.0).clamp(0.0, 1.0));
                    }
                }
                _ => {}
            }
        }
        self.rows = rows;
    }

    /// Высота содержимого (логические пиксели).
    pub fn height(&self) -> f32 {
        let w = self.width - 2.0 * self.pad;
        12.0 + self.rows.iter().map(|r| r.height(w)).sum::<f32>() + self.pad
    }

    /// Шаг анимаций (полосы, подсветка). true — ещё движется.
    pub fn tick(&mut self) -> bool {
        let now = crate::anim::now();
        let dt = ((now - self.last) as f32).clamp(0.001, 0.05);
        self.last = now;
        let mut busy = false;
        let k = smooth(0.12, dt);
        for (i, t) in self.targets.clone() {
            let v = self.shown.entry(i).or_insert(0.0);
            if (*v - t).abs() > 0.002 {
                *v += (t - *v) * k;
                busy = true;
            } else {
                *v = t;
            }
        }
        let want: Vec<Hit> = self.hover.into_iter().chain(self.pressed).collect();
        let keys: Vec<Hit> = self.glow.keys().copied().chain(want.iter().copied()).collect();
        for h in keys {
            let t = if want.contains(&h) { 1.0 } else { 0.0 };
            let v = self.glow.entry(h).or_insert(0.0);
            if (*v - t).abs() > 0.01 {
                *v += (t - *v) * smooth(0.35, dt);
                busy = true;
            } else {
                *v = t;
            }
        }
        self.glow.retain(|_, v| *v > 0.0);
        busy
    }

    pub fn hit_at(&self, x: f32, y: f32) -> Option<Hit> {
        self.hits.iter().find(|(r, _)| r.contains(x, y)).map(|(_, h)| *h)
    }

    fn slider_at(&self, x: f32, y: f32) -> Option<(R, u32)> {
        self.sliders.iter().find(|(r, _)| r.adjusted(-4.0, -8.0, 4.0, 8.0).contains(x, y)).copied()
    }

    fn slider_value(r: R, x: f32) -> f32 {
        ((x - r.x) / r.w.max(1.0)).clamp(0.0, 1.0)
    }

    /// Мышь (координаты окна, логические). Возвращает событие.
    pub fn mouse_move(&mut self, x: f32, y: f32) -> Option<Event> {
        let (x, y) = (x / self.scale, y / self.scale);
        if let Some(id) = self.drag {
            if let Some((r, _)) = self.sliders.iter().find(|(_, i)| *i == id).copied() {
                return Some(Event::Slide(id, Self::slider_value(r, x)));
            }
        }
        self.hover = self.hit_at(x, y);
        None
    }

    pub fn mouse_down(&mut self, x: f32, y: f32) -> Option<Event> {
        let (x, y) = (x / self.scale, y / self.scale);
        if let Some((r, id)) = self.slider_at(x, y) {
            self.drag = Some(id);
            return Some(Event::Slide(id, Self::slider_value(r, x)));
        }
        self.pressed = self.hit_at(x, y);
        None
    }

    pub fn mouse_up(&mut self, x: f32, y: f32) -> Option<Event> {
        let (x, y) = (x / self.scale, y / self.scale);
        if self.drag.take().is_some() {
            return None;
        }
        let p = self.pressed.take();
        let h = self.hit_at(x, y);
        if p.is_some() && p == h {
            return h.map(Event::Click);
        }
        None
    }

    pub fn dragging(&self) -> bool {
        self.drag.is_some()
    }

    pub fn leave(&mut self) {
        self.hover = None;
    }

    fn glow(&self, h: Hit) -> f32 {
        self.glow.get(&h).copied().unwrap_or(0.0)
    }

    /// Нарисовать строки в cv, начиная с (x0, y0) в физических пикселях; clip_h — сколько видно по высоте.
    pub fn paint(&mut self, cv: &mut Canvas, x0: f32, y0: f32) {
        let s = self.scale;
        let w = self.width - 2.0 * self.pad;
        let mut y = 12.0;
        self.hits.clear();
        self.sliders.clear();
        let rows = self.rows.clone();
        let px = |v: f32| v * s;
        let x0l = x0 / s;
        let y0l = y0 / s;
        let lx = x0l + self.pad;
        for (i, row) in rows.iter().enumerate() {
            let h = row.height(w);
            let r = R::new(px(lx), px(y0l + y), px(w), px(h));
            match row {
                Row::Title(t) => text::draw_in(cv, t, Font::Bold, 13.0 * s, r, Align::Left, rgba(245, 245, 248, 255)),
                Row::TitleToggle(t, id, on) => {
                    text::draw_in(cv, t, Font::Bold, 13.0 * s, r, Align::Left, rgba(245, 245, 248, 255));
                    let tr = R::new(r.right() - px(40.0), r.cy() - px(10.0), px(38.0), px(20.0));
                    toggle(cv, tr, if *on { 1.0 } else { 0.0 }, s);
                    self.hits.push((R::new(lx + w - 40.0, y0l + y, 40.0, h), Hit { id: *id, part: 0 }));
                }
                Row::Text(t) => {
                    let lines = text::wrap(t, Font::Regular, 11.0 * s, r.w, 4);
                    for (k, l) in lines.iter().enumerate() {
                        let lr = R::new(r.x, r.y + px(2.0 + 16.0 * k as f32), r.w, px(16.0));
                        text::draw_in(cv, l, Font::Regular, 11.0 * s, lr, Align::Left, rgba(255, 255, 255, 140));
                    }
                }
                Row::Section(t) => text::draw_in(cv, t, Font::Semibold, 12.0 * s, r, Align::Left, rgba(255, 255, 255, 150)),
                Row::Big(t) => text::draw_in(cv, t, Font::Light, 40.0 * s, r, Align::Left, rgba(245, 245, 248, 255)),
                Row::Value(a, b) => {
                    let vw = (text::width(b, Font::Regular, 12.0 * s) + 2.0).min(r.w * 0.6);
                    text::draw_in(cv, a, Font::Regular, 12.0 * s, R::new(r.x, r.y, r.w - vw - px(8.0), r.h), Align::Left, rgba(255, 255, 255, 170));
                    text::draw_in(cv, b, Font::Regular, 12.0 * s, R::new(r.right() - vw, r.y, vw, r.h), Align::Right, rgba(245, 245, 248, 255));
                }
                Row::Bar(label, target, value) => {
                    text::draw_in(cv, label, Font::Regular, 12.0 * s, R::new(r.x, r.y + px(2.0), r.w * 0.5, px(18.0)), Align::Left, rgba(255, 255, 255, 190));
                    text::draw_in(cv, value, Font::Regular, 12.0 * s, R::new(r.x + r.w * 0.35, r.y + px(2.0), r.w * 0.65, px(18.0)), Align::Right, rgba(245, 245, 248, 255));
                    let track = R::new(r.x, r.y + px(24.0), r.w, px(8.0));
                    cv.fill_rrect(track, px(4.0), rgba(255, 255, 255, 30));
                    let f = self.shown.get(&i).copied().unwrap_or(0.0);
                    if f > 0.005 {
                        let c = level_color(*target, accent(255));
                        cv.fill_rrect(R::new(r.x, track.y, (r.w * f).max(px(8.0)), px(8.0)), px(4.0), c);
                    }
                }
                Row::Sep => cv.fill_rect(R::new(r.x, (r.cy()).round(), r.w, 1.0), rgba(255, 255, 255, 30)),
                Row::Button(id, t, current) => {
                    let hit = Hit { id: *id, part: 0 };
                    let br = R::new(r.x, r.y + px(4.0), r.w, r.h - px(6.0));
                    button(cv, br, t, *current, self.glow(hit), s);
                    self.hits.push((R::new(lx, y0l + y + 4.0, w, h - 6.0), hit));
                }
                Row::Buttons(list) => {
                    let n = list.len().max(1) as f32;
                    let gap = 8.0;
                    let bw = (w - gap * (n - 1.0)) / n;
                    for (k, (id, t)) in list.iter().enumerate() {
                        let hit = Hit { id: *id, part: 0 };
                        let lxk = lx + k as f32 * (bw + gap);
                        button(cv, R::new(px(lxk), r.y + px(4.0), px(bw), r.h - px(6.0)), t, false, self.glow(hit), s);
                        self.hits.push((R::new(lxk, y0l + y + 4.0, bw, h - 6.0), hit));
                    }
                }
                Row::Volume(id, v, muted, glyph, glyph_off, extra) => {
                    let mh = Hit { id: *id, part: 0 };
                    let ib = R::new(r.x, r.cy() - px(15.0), px(32.0), px(30.0));
                    let g = self.glow(mh);
                    if g > 0.0 {
                        cv.fill_rrect(ib, px(8.0), rgba(255, 255, 255, (30.0 * g) as u8));
                    }
                    text::draw_in(cv, if *muted { glyph_off } else { glyph }, Font::Icons, 16.0 * s, ib, Align::Center, rgba(245, 245, 248, if *muted { 110 } else { 255 }));
                    self.hits.push((R::new(lx, y0l + y + 2.0, 32.0, 30.0), mh));
                    let right = if extra.is_some() { 40.0 } else { 0.0 };
                    let sr = R::new(lx + 40.0, y0l + y + h / 2.0 - 3.0, w - 40.0 - right - 6.0, 6.0);
                    slider(cv, R::new(px(sr.x), px(sr.y), px(sr.w), px(sr.h)), *v, rgba(MIX.0 + 28, MIX.1 + 22, MIX.2 + 20, 255), s, true);
                    self.sliders.push((sr, *id));
                    if let Some((eid, eg, on)) = extra {
                        let eh = Hit { id: *eid, part: 0 };
                        let er = R::new(r.right() - px(32.0), r.cy() - px(15.0), px(32.0), px(30.0));
                        let g = self.glow(eh);
                        if *on {
                            cv.fill_rrect(er, px(8.0), rgba(MIX.0, MIX.1, MIX.2, 200));
                        } else if g > 0.0 {
                            cv.fill_rrect(er, px(8.0), rgba(255, 255, 255, (30.0 * g) as u8));
                        }
                        text::draw_in(cv, eg, Font::Icons, 16.0 * s, er, Align::Center, rgba(245, 245, 248, 255));
                        self.hits.push((R::new(lx + w - 32.0, y0l + y + 2.0, 32.0, 30.0), eh));
                    }
                }
                Row::AppVolume(id, icon, name, v) => {
                    if let Some(ic) = icon {
                        cv.image(ic, R::new(r.x, r.cy() - px(10.0), px(20.0), px(20.0)));
                    }
                    text::draw_in(cv, name, Font::Regular, 12.0 * s, R::new(r.x + px(28.0), r.y, px(104.0), r.h), Align::Left, rgba(242, 242, 245, 255));
                    let sr = R::new(lx + 140.0, y0l + y + h / 2.0 - 2.0, w - 146.0, 4.0);
                    slider(cv, R::new(px(sr.x), px(sr.y), px(sr.w), px(sr.h)), *v, rgba(245, 245, 248, 220), s, false);
                    self.sliders.push((sr, *id));
                }
                Row::Device(id, name, media, comm) => {
                    let seg = R::new(r.x, r.cy() - px(12.0), px(54.0), px(24.0));
                    for (k, (on, glyph)) in [(*media, "\u{E8D6}"), (*comm, "\u{E717}")].iter().enumerate() {
                        let half = R::new(seg.x + seg.w / 2.0 * k as f32, seg.y, seg.w / 2.0, seg.h);
                        let hit = Hit { id: *id, part: k as u8 };
                        let g = self.glow(hit);
                        let base = if *on { rgba(MIX.0, MIX.1, MIX.2, 235) } else { rgba(255, 255, 255, (14.0 + 26.0 * g) as u8) };
                        let mut clip = Mask::new(cv.pm.width(), cv.pm.height()).unwrap();
                        if let Some(p) = rounded_rect(half, 0.0) {
                            clip.fill_path(&p, FillRule::Winding, false, Transform::identity());
                        }
                        cv.clip = Some(clip);
                        cv.fill_rrect(seg, px(7.0), base);
                        cv.clip = None;
                        text::draw_in(cv, glyph, Font::Icons, 12.0 * s, half, Align::Center, rgba(255, 255, 255, if *on { 255 } else { 170 }));
                        self.hits.push((R::new(lx + 27.0 * k as f32, y0l + y + 3.0, 27.0, 24.0), hit));
                    }
                    cv.stroke_rrect(seg.inset(0.5), px(7.0), rgba(255, 255, 255, 50), 1.0);
                    cv.line(seg.cx(), seg.y + px(3.0), seg.cx(), seg.bottom() - px(3.0), rgba(255, 255, 255, 50), 1.0);
                    text::draw_in(cv, name, Font::Regular, 12.0 * s, R::new(seg.right() + px(10.0), r.y, r.w - px(64.0), r.h), Align::Left, rgba(242, 242, 245, 255));
                }
                Row::IconGrid(items) => {
                    for (k, (id, icon, _tip)) in items.iter().enumerate() {
                        let (cx, cy) = ((k % 5) as f32 * 44.0, (k / 5) as f32 * 44.0);
                        let cr = R::new(px(lx + cx), px(y0l + y + cy), px(40.0), px(40.0));
                        let hit = Hit { id: *id, part: 0 };
                        let g = self.glow(hit);
                        if g > 0.0 {
                            cv.fill_rrect(cr, px(7.0), rgba(255, 255, 255, (40.0 * g) as u8));
                        }
                        if let Some(ic) = icon {
                            cv.image(ic, R::centered(cr.cx(), cr.cy(), px(22.0), px(22.0)));
                        }
                        self.hits.push((R::new(lx + cx, y0l + y + cy, 40.0, 40.0), hit));
                    }
                }
                Row::Calendar(year, month, today, prev, next) => {
                    self.paint_calendar(cv, r, *year, *month, *today, *prev, *next, lx, y0l + y);
                }
                Row::Media(m) => {
                    self.paint_media(cv, R::new(r.x, r.y, r.w, px(122.0)), m, lx, y0l + y);
                }
                Row::Rings(g) => {
                    let n = g.len().max(1) as f32;
                    let cell = r.w / n;
                    let d = px(96.0);
                    for (k, (label, frac, center, caption)) in g.iter().enumerate() {
                        let cx = r.x + cell * k as f32 + cell / 2.0;
                        let cy = r.y + px(8.0) + d / 2.0;
                        cv.stroke_circle(cx, cy, d / 2.0, rgba(255, 255, 255, 28), px(9.0));
                        let shown = self.shown.get(&(1000 + i * 10 + k)).copied().unwrap_or(0.0);
                        if frac.is_some() && shown > 0.004 {
                            arc(cv, cx, cy, d / 2.0, shown, level_color(frac.unwrap(), rgba(217, 119, 87, 255)), px(9.0));
                        }
                        text::draw_in(cv, center, Font::Semibold, 22.0 * s, R::centered(cx, cy, d, d), Align::Center, rgba(250, 250, 252, 255));
                        text::draw_in(cv, label, Font::Regular, 12.0 * s, R::new(r.x + cell * k as f32, r.y + px(112.0), cell, px(18.0)), Align::Center, rgba(255, 255, 255, 215));
                        text::draw_in(cv, caption, Font::Regular, 11.0 * s, R::new(r.x + cell * k as f32, r.y + px(130.0), cell, px(16.0)), Align::Center, rgba(255, 255, 255, 130));
                    }
                }
                Row::Stats(st) => {
                    let n = st.len().max(1) as f32;
                    let gap = px(8.0);
                    let tw = (r.w - gap * (n - 1.0)) / n;
                    for (k, (v, l)) in st.iter().enumerate() {
                        let tr = R::new(r.x + k as f32 * (tw + gap), r.y + px(6.0), tw, px(50.0));
                        cv.fill_rrect(tr, px(9.0), rgba(255, 255, 255, 14));
                        text::draw_in(cv, v, Font::Semibold, 14.0 * s, R::new(tr.x, tr.y + px(6.0), tr.w, px(22.0)), Align::Center, rgba(245, 245, 248, 255));
                        text::draw_in(cv, l, Font::Regular, 11.0 * s, R::new(tr.x, tr.y + px(28.0), tr.w, px(16.0)), Align::Center, rgba(255, 255, 255, 130));
                    }
                }
                Row::Space(_) => {}
            }
            y += h;
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn paint_calendar(&mut self, cv: &mut Canvas, r: R, year: i32, month: u32, today: (i32, u32, u32), prev: u32, next: u32, lx: f32, ly: f32) {
        let s = self.scale;
        let px = |v: f32| v * s;
        const MONTHS: [&str; 12] = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
        let head = R::new(r.x, r.y, r.w, px(30.0));
        text::draw_in(cv, &format!("{} {}", MONTHS[(month as usize + 11) % 12], year), Font::Bold, 13.0 * s, head, Align::Center, rgba(242, 242, 245, 255));
        for (k, (id, glyph)) in [(prev, "‹"), (next, "›")].iter().enumerate() {
            let br = if k == 0 { R::new(head.x, head.y, px(30.0), head.h) } else { R::new(head.right() - px(30.0), head.y, px(30.0), head.h) };
            let hit = Hit { id: *id, part: 0 };
            let g = self.glow(hit);
            if g > 0.0 {
                cv.fill_rrect(br.inset(px(2.0)), px(7.0), rgba(255, 255, 255, (40.0 * g) as u8));
            }
            text::draw_in(cv, glyph, Font::Regular, 18.0 * s, br, Align::Center, rgba(242, 242, 245, 255));
            let lr = if k == 0 { R::new(lx, ly, 30.0, 30.0) } else { R::new(lx + r.w / s - 30.0, ly, 30.0, 30.0) };
            self.hits.push((lr, hit));
        }
        let names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
        let cw = r.w / 7.0;
        for (k, n) in names.iter().enumerate() {
            text::draw_in(cv, n, Font::Regular, 11.0 * s, R::new(r.x + cw * k as f32, r.y + px(34.0), cw, px(20.0)), Align::Center, rgba(255, 255, 255, 130));
        }
        let first = weekday(year, month, 1); // 0 = понедельник
        let days = days_in_month(year, month);
        let rows = (first + days).div_ceil(7);
        let ch = px(28.0);
        for d in 1..=days {
            let idx = first + d - 1;
            let (col, row) = (idx % 7, idx / 7);
            let cr = R::new(r.x + cw * col as f32, r.y + px(58.0) + ch * row as f32, cw, ch);
            let is_today = today == (year, month, d);
            if is_today {
                cv.fill_circle(cr.cx(), cr.cy(), px(12.5), accent(225));
            }
            let weekend = col >= 5;
            let c = if is_today { rgba(255, 255, 255, 255) } else if weekend { rgba(255, 125, 115, 255) } else { rgba(242, 242, 245, 255) };
            text::draw_in(cv, &d.to_string(), Font::Regular, 12.0 * s, cr, Align::Center, c);
        }
        let _ = rows;
    }

    fn paint_media(&mut self, cv: &mut Canvas, r: R, m: &MediaInfo, lx: f32, ly: f32) {
        let s = self.scale;
        let px = |v: f32| v * s;
        if let Some(card) = rounded_rect(r, px(12.0)) {
            match &m.blurred {
                Some(bg) => {
                    crate::dock::draw_cover(cv, bg, r, &card, 1.0);
                    cv.fill_path(&card, rgba(10, 10, 16, 120));
                }
                None => cv.fill_path(&card, rgba(255, 255, 255, 22)),
            }
        }
        let art = R::new(r.x + px(10.0), r.y + px(10.0), r.h - px(20.0), r.h - px(20.0));
        match &m.cover {
            Some(c) => {
                if let Some(ap) = rounded_rect(art, px(9.0)) {
                    let side = c.w.min(c.h) as f32;
                    let k = art.w / side;
                    let dst = R::new(art.x - (c.w as f32 - side) / 2.0 * k, art.y - (c.h as f32 - side) / 2.0 * k, c.w as f32 * k, c.h as f32 * k);
                    cv.image_in_path(c, dst, &ap, 1.0);
                }
            }
            None => {
                cv.fill_rrect(art, px(9.0), rgba(255, 255, 255, 30));
                text::draw_in(cv, "\u{E8D6}", Font::Icons, 30.0 * s, art, Align::Center, rgba(255, 255, 255, 160));
            }
        }
        if let Some(ic) = &m.icon {
            cv.image(ic, R::new(art.right() - px(30.0), art.bottom() - px(30.0), px(26.0), px(26.0)));
        }
        let tx = art.right() + px(10.0);
        let tw = r.right() - tx - px(10.0);
        let title = if m.title.is_empty() { "Без названия" } else { &m.title };
        text::draw_in(cv, title, Font::Semibold, 17.0 * s, R::new(tx, r.y + px(14.0), tw, px(30.0)), Align::Center, rgba(255, 255, 255, 255));
        text::draw_in(cv, &m.artist, Font::Regular, 12.0 * s, R::new(tx, r.y + px(44.0), tw, px(20.0)), Align::Center, rgba(255, 255, 255, 200));
        let cx0 = tx + tw / 2.0;
        for (k, (glyph, part)) in [("\u{E892}", 0u8), (if m.playing { "\u{E769}" } else { "\u{E768}" }, 1), ("\u{E893}", 2)].iter().enumerate() {
            let bc = cx0 + (k as f32 - 1.0) * px(54.0);
            let br = R::centered(bc, r.bottom() - px(30.0), px(32.0), px(32.0));
            let hit = Hit { id: 9000 + m.id as u32, part: *part };
            let g = self.glow(hit);
            if g > 0.0 {
                cv.fill_circle(br.cx(), br.cy(), px(16.0), rgba(255, 255, 255, (40.0 * g) as u8));
            }
            text::draw_in(cv, glyph, Font::Icons, 17.0 * s, br, Align::Center, rgba(255, 255, 255, 255));
            let lr = R::new(lx + (bc - r.x) / s - 16.0, ly + 122.0 - 46.0, 32.0, 32.0);
            self.hits.push((lr, hit));
        }
        cv.stroke_rrect(r.inset(0.5), px(12.0), rgba(255, 255, 255, 40), 1.0);
    }
}

pub fn level_color(frac: f32, base: Color) -> Color {
    if frac >= 0.9 {
        rgba(255, 95, 87, 255)
    } else if frac >= 0.75 {
        rgba(255, 189, 46, 255)
    } else {
        base
    }
}

pub fn button(cv: &mut Canvas, r: R, t: &str, current: bool, glow: f32, s: f32) {
    let bg = if current { accent(200) } else { rgba(255, 255, 255, (16.0 + 24.0 * glow) as u8) };
    cv.fill_rrect(r, 7.0 * s, bg);
    text::draw_in(cv, t, Font::Regular, 12.0 * s, R::new(r.x + 10.0 * s, r.y, r.w - 20.0 * s, r.h), Align::Left, rgba(245, 245, 248, 255));
}

/// Переключатель в стиле macOS (k: 0 — выкл, 1 — вкл).
pub fn toggle(cv: &mut Canvas, r: R, k: f32, s: f32) {
    let off = rgba(255, 255, 255, 45);
    let on = accent(255);
    cv.fill_rrect(r, r.h / 2.0, lerp_color(off, on, k));
    let knob = r.h / 2.0 - 2.0 * s;
    cv.fill_circle(lerp(r.x + r.h / 2.0, r.right() - r.h / 2.0, k), r.cy(), knob, rgba(255, 255, 255, 255));
}

pub fn slider(cv: &mut Canvas, r: R, v: f32, fill: Color, s: f32, ring_handle: bool) {
    let rad = r.h / 2.0;
    cv.fill_rrect(r, rad, rgba(255, 255, 255, 36));
    let fw = (r.w * v.clamp(0.0, 1.0)).max(r.h);
    cv.fill_rrect(R::new(r.x, r.y, fw, r.h), rad, fill);
    let hx = r.x + r.w * v.clamp(0.0, 1.0);
    if ring_handle {
        cv.fill_circle(hx, r.cy(), 8.0 * s, fill);
        cv.fill_circle(hx, r.cy(), 5.5 * s, rgba(244, 244, 246, 255));
    } else {
        cv.fill_circle(hx, r.cy(), 6.0 * s, rgba(255, 255, 255, 255));
    }
}

pub fn arc(cv: &mut Canvas, cx: f32, cy: f32, rad: f32, frac: f32, c: Color, w: f32) {
    let n = (64.0 * frac).max(2.0) as usize;
    let pts: Vec<(f32, f32)> = (0..=n)
        .map(|i| {
            let a = -std::f32::consts::FRAC_PI_2 + std::f32::consts::TAU * frac * i as f32 / n as f32;
            (cx + rad * a.cos(), cy + rad * a.sin())
        })
        .collect();
    cv.polyline(&pts, c, w);
}

/// День недели (0 — понедельник).
pub fn weekday(y: i32, m: u32, d: u32) -> u32 {
    // алгоритм Сакамото: 0 — воскресенье
    let t = [0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4];
    let y = if m < 3 { y - 1 } else { y };
    let w = (y + y / 4 - y / 100 + y / 400 + t[(m - 1) as usize] + d as i32).rem_euclid(7);
    ((w + 6) % 7) as u32
}

pub fn days_in_month(y: i32, m: u32) -> u32 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        _ => {
            if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 {
                29
            } else {
                28
            }
        }
    }
}

pub fn size_str(n: f64) -> String {
    let units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
    let mut v = n;
    for (i, u) in units.iter().enumerate() {
        if v.abs() < 1024.0 || i == units.len() - 1 {
            return if i < 2 { format!("{v:.0} {u}") } else { format!("{v:.1} {u}").replace('.', ",") };
        }
        v /= 1024.0;
    }
    String::new()
}

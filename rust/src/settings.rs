//! Окно настроек MiniDock в стиле «Системных настроек» macOS. Все изменения применяются сразу.

use std::collections::HashMap;

use serde_json::Value;
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::app::App;
use crate::config::Config;
use crate::gfx::*;
use crate::menu::{MenuItem, Owner};
use crate::panel::{accent, toggle};
use crate::text::{self, Align, Font};
use crate::win::{self, hooks, shell, sys, Surface};

const W: f32 = 880.0;
const H: f32 = 620.0;
const SIDE: f32 = 210.0;

#[derive(Clone)]
enum Ctl {
    Toggle(&'static str),
    Slider(&'static str, f32, f32, &'static str),
    Combo(&'static str, Vec<(&'static str, &'static str)>),
    Color(&'static str),
    Button(&'static str, u32),
    Hotkey,
    Autostart,
    Widget(&'static str),
    Background(&'static str),
    Text,
}

#[derive(Clone)]
struct Row {
    title: String,
    desc: &'static str,
    ctl: Ctl,
}

fn row(title: &str, ctl: Ctl) -> Row {
    Row { title: title.into(), desc: "", ctl }
}

fn rowd(title: &str, desc: &'static str, ctl: Ctl) -> Row {
    Row { title: title.into(), desc, ctl }
}

const PAGES: [(&str, &str); 10] = [
    ("\u{E713}", "Основные"),
    ("\u{E8A9}", "Док"),
    ("\u{E790}", "Внешний вид"),
    ("\u{E91B}", "Фоны"),
    ("\u{E945}", "Анимации"),
    ("\u{E7C4}", "Верхняя панель"),
    ("\u{E8AB}", "Alt+Tab"),
    ("\u{E80F}", "Launchpad"),
    ("\u{E895}", "Обновления"),
    ("\u{E946}", "О программе"),
];

const BG_PARTS: [(&str, &str); 9] = [
    ("dock", "Док"),
    ("menubar", "Верхняя панель"),
    ("bar", "Окна верхней панели"),
    ("switcher", "Alt+Tab"),
    ("previews", "Миниатюры окон над доком"),
    ("spotlight", "Spotlight"),
    ("folders", "Папки"),
    ("menus", "Контекстные меню"),
    ("launchpad", "Launchpad"),
];

fn page(i: usize) -> Vec<Vec<Row>> {
    use Ctl::*;
    match i {
        0 => vec![
            vec![
                rowd("Заменить панель задач Windows", "Док встаёт на место панели задач, она прячется. При выходе всё возвращается.", Toggle("replace_taskbar")),
                row("Запускать вместе с Windows", Autostart),
                row("Клавиша Win открывает Launchpad", Toggle("win_key_launchpad")),
                row("Spotlight по Alt+Space", Toggle("spotlight")),
            ],
            vec![
                rowd("Большие часы", "Пока зажато сочетание клавиш — посреди экрана большие часы с датой. Отпустили — исчезают.", Toggle("big_clock")),
                rowd("Сочетание клавиш для часов", "Нажмите кнопку и затем нужное сочетание (Esc — отмена).", Hotkey),
            ],
            vec![
                row("Папка настроек", Button("Открыть", 1)),
                rowd("Сбросить все настройки", "Закреплённые приложения сохранятся.", Button("Сбросить", 2)),
            ],
        ],
        1 => vec![
            vec![
                row("Положение на экране", Combo("position", vec![("Снизу", "bottom"), ("Слева", "left"), ("Справа", "right")])),
                row("Размер иконок", Slider("icon_size", 24.0, 96.0, "{:.0} px")),
                row("Увеличение при наведении", Slider("magnify", 1.0, 2.5, "{:.2}×")),
                row("Расстояние между иконками", Slider("spacing", 0.0, 0.5, "{:.2}")),
                row("Отступ от края экрана", Slider("margin", 0.0, 40.0, "{:.0} px")),
                rowd("Док на всех мониторах", "Свой док на каждом мониторе: окна сворачиваются в док того экрана, где они открыты.", Toggle("dock_all_screens")),
            ],
            vec![
                rowd("Автоматически скрывать док", "Док появляется, когда курсор касается края экрана.", Toggle("autohide")),
                row("Задержка скрытия", Slider("autohide_delay", 0.0, 2.0, "{:.2} с")),
                rowd(
                    "Док поверх полноэкранных программ",
                    "В играх, видео на весь экран и в F11 док прячется у края и выезжает, если подвести курсор к краю экрана.",
                    Toggle("dock_in_fullscreen"),
                ),
            ],
            vec![
                row("Значок Launchpad", Toggle("show_launchpad")),
                row("Показывать запущенные приложения", Toggle("show_running")),
                row("Корзина", Toggle("show_trash")),
                row("Подписи при наведении", Toggle("show_labels")),
                rowd("Миниатюры окон при наведении", "Наведите на запущенное приложение: клик по миниатюре — открыть окно, крестик — закрыть.", Toggle("dock_previews")),
                row("Размер миниатюр", Slider("dock_preview", 140.0, 360.0, "{:.0} px")),
                row("Толщина рамки", Slider("dock_preview_pad", 2.0, 24.0, "{:.0} px")),
                row("Задержка показа", Slider("dock_preview_delay", 0.0, 1.5, "{:.2} с")),
                rowd("Показывать окно целиком при наведении на миниатюру", "Как на панели задач Windows: окно встаёт на своё место, остальные становятся прозрачными.", Toggle("dock_peek")),
                row("Подпрыгивание при запуске", Toggle("bounce")),
                row("Индикатор запущенных", Combo("indicator", vec![("Точка", "dot"), ("Полоска", "line"), ("Нет", "none")])),
            ],
        ],
        2 => vec![vec![
            row("Цвет фона", Color("dock_color")),
            row("Непрозрачность фона", Slider("dock_opacity", 0.0, 255.0, "{:.0}")),
            rowd("Размытие под доком", "Нативное размытие Windows (Acrylic): живое, не тратит память MiniDock.", Toggle("dock_blur")),
            row("Скругление углов", Slider("dock_radius", 0.0, 0.5, "{:.2}")),
            row("Светлая обводка", Toggle("dock_border")),
            rowd("Блик от курсора", "Обводка светлее там, где курсор, — как от фонарика.", Toggle("dock_border_light")),
            row("Яркость блика", Slider("dock_light_strength", 0.05, 1.0, "{:.0%}")),
        ]],
        3 => vec![BG_PARTS.iter().map(|(k, t)| rowd(t, "", Background(k))).collect()],
        4 => vec![
            vec![
                rowd("Анимации окон", "Свёртывание в док и разворачивание из него.", Toggle("animations")),
                row("Эффект свёртывания", Combo("minimize_effect", vec![("Джинн", "genie"), ("Масштаб", "scale")])),
                row("Скорость анимаций", Slider("anim_speed", 0.5, 2.0, "{:.2}×")),
                rowd("Открытие программ из иконки", "Окно программы, запущенной из дока или Launchpad, вылетает из её иконки.", Toggle("open_animation")),
                row("Анимация закрытия окон", Toggle("close_animation")),
                rowd("Тени под окнами", "Выключено — у окон программ нет теней. При выходе из MiniDock системная настройка возвращается.", Toggle("window_shadows")),
            ],
            vec![rowd(
                "Системная анимация Windows",
                "Пока включены анимации MiniDock, системная анимация свёртывания выключается сама (и возвращается при выходе) — они не накладываются.",
                Text,
            )],
        ],
        5 => vec![
            vec![
                row("Показывать верхнюю панель", Toggle("menubar")),
                row("На всех мониторах", Toggle("bar_all_screens")),
                row("Высота", Slider("bar_height", 20.0, 36.0, "{:.0} px")),
                row("Непрозрачность", Slider("bar_opacity", 0.0, 255.0, "{:.0}")),
                row("Размытие", Toggle("bar_blur")),
            ],
            vec![
                row("Секунды в часах", Toggle("clock_seconds")),
                row("Дата рядом с часами", Toggle("clock_date")),
                rowd("Заряд аккумулятора", "Только на ноутбуках.", Toggle("show_battery")),
                rowd("Bluetooth", "Значок появляется, если на компьютере есть Bluetooth-адаптер.", Toggle("show_bluetooth")),
            ],
            crate::widgets::BUILTIN.iter().map(|(id, name, _)| row(name, Widget(id))).chain(std::iter::once(rowd("Свои виджеты", "Свои виджеты на Python (файлы .py) работают только в Python-версии MiniDock.", Text))).collect(),
            vec![
                row("Фон карточек плееров в окне звука", Combo("media_bg", vec![("Размытая обложка", "blur"), ("Сплошной цвет", "solid")])),
                row("Цвет фона карточек", Color("media_color")),
            ],
        ],
        6 => vec![
            vec![
                rowd("Свой переключатель вместо системного", "Alt+Tab / Alt+Shift+Tab, стрелки, мышь. Q — закрыть приложение, Esc — отмена.", Toggle("custom_switcher")),
                rowd("Стиль", "Во втором стиле у каждой миниатюры внизу — значок программы.", Combo("switcher_style", vec![("Значки программ и их окна (как в macOS)", "mac"), ("Миниатюры всех окон (как в Windows)", "windows")])),
                row("Посмотреть, как выглядит", Button("Показать", 3)),
            ],
            vec![
                rowd("Миниатюры окон", "Живые миниатюры окон выбранного приложения. ↓ или ` — выбрать окно, ← → — какое, ↑ — назад.", Toggle("switcher_previews")),
                row("Размер миниатюр", Slider("switcher_preview", 140.0, 400.0, "{:.0} px")),
            ],
            vec![
                row("Размер иконок", Slider("switcher_icon", 48.0, 160.0, "{:.0} px")),
                rowd("Иконок и окон в одной строке", "Остальные — на следующей строке. 0 — все в один ряд.", Slider("switcher_per_row", 0.0, 20.0, "{:.0}")),
                row("Непрозрачность фона", Slider("switcher_opacity", 0.0, 255.0, "{:.0}")),
                row("Скругление углов", Slider("switcher_radius", 0.0, 40.0, "{:.0} px")),
                row("Положение", Combo("switcher_position", vec![("По центру экрана", "center"), ("Выше центра", "upper")])),
            ],
            vec![
                row("Название приложения", Toggle("switcher_titles")),
                row("Заголовок окна под названием", Toggle("switcher_window_title")),
                rowd("Задержка появления", "Быстрое нажатие Alt+Tab переключает без показа окна.", Slider("switcher_delay", 0.0, 400.0, "{:.0} мс")),
                row("Показывать свёрнутые приложения", Toggle("switcher_minimized")),
                rowd("Предпросмотр при наведении мыши", "Наведение показывает окна приложения, а переключает только клик.", Toggle("switcher_hover")),
                rowd("Разворачивать «джинном» из дока", "Если выключить — свёрнутое окно появляется сразу.", Toggle("switcher_genie")),
            ],
        ],
        7 => vec![
            vec![rowd("Показывать", "Переключать можно и прямо в Launchpad (вверху или клавишей Tab).", Combo("lp_mode", vec![("Все приложения по алфавиту", "all"), ("Закреплённые плитки из «Пуска»", "start")]))],
            vec![row("Столбцов", Slider("lp_columns", 4.0, 10.0, "{:.0}")), row("Строк", Slider("lp_rows", 3.0, 7.0, "{:.0}")), row("Размер иконок", Slider("lp_icon", 48.0, 128.0, "{:.0} px"))],
            vec![rowd("Список приложений", "Берётся из меню «Пуск», включая приложения Microsoft Store.", Button("Обновить", 4))],
        ],
        8 => vec![vec![
            rowd(&format!("MiniDock {} (нативная версия)", env!("CARGO_PKG_VERSION")), "Обновления нативной версии пока ставятся вручную: новые сборки — на странице релизов.", Text),
            row("Все версии и что в них нового", Button("Открыть на GitHub", 5)),
        ]],
        _ => vec![vec![rowd(
            &format!("MiniDock {} — док, верхняя панель, Launchpad и переключатель окон в стиле macOS", env!("CARGO_PKG_VERSION")),
            "Win — Launchpad · Alt+Space — Spotlight (Enter — открыть, Ctrl+Enter — показать в папке) · Alt+Tab — переключение (Q — закрыть, Esc — отмена) · Shift+клик по иконке — новое окно · зажатое сочетание — большие часы. Выход: правый клик по доку → «Выход».",
            Text,
        )]],
    }
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Hit {
    Close,
    Minimize,
    Nav(usize),
    Ctl(usize, usize, u8),
    Drag,
}

pub struct Settings {
    hwnd: HWND,
    surf: Surface,
    page: usize,
    scroll: f32,
    scroll_to: f32,
    content_h: f32,
    hits: Vec<(R, Hit)>,
    sliders: Vec<(R, usize, usize)>,
    hover: Option<Hit>,
    drag_slider: Option<(usize, usize)>,
    drag_win: Option<(i32, i32)>,
    recording: bool,
    opened: f64,
    x: i32,
    y: i32,
    w: i32,
    h: i32,
    scale: f32,
    knobs: HashMap<&'static str, f32>,
    last: f64,
}

impl Settings {
    pub fn frame(&mut self, cfg: &Config) -> bool {
        let now = crate::anim::now();
        let dt = crate::anim::frame_dt(&mut self.last);
        let mut busy = now - self.opened < 0.2;
        if (self.scroll - self.scroll_to).abs() > 0.5 {
            self.scroll += (self.scroll_to - self.scroll) * smooth(0.25, dt);
            busy = true;
        } else {
            self.scroll = self.scroll_to;
        }
        // переключатели плавно едут к своему положению
        let v = serde_json::to_value(cfg).unwrap_or(Value::Null);
        for (k, kv) in self.knobs.iter_mut() {
            let t = if v.get(*k).and_then(|x| x.as_bool()).unwrap_or(false) { 1.0 } else { 0.0 };
            if (*kv - t).abs() > 0.01 {
                *kv += (t - *kv) * smooth(0.3, dt);
                busy = true;
            } else {
                *kv = t;
            }
        }
        if self.recording {
            busy = true;
        }
        busy
    }

    pub fn hotkey_recorded(&mut self, _code: u32) {
        self.recording = false;
    }
}

fn get(cfg: &Config, key: &str) -> Value {
    serde_json::to_value(cfg).ok().and_then(|v| v.get(key).cloned()).unwrap_or(Value::Null)
}

fn set(app: &mut App, key: &str, val: Value) {
    let Ok(Value::Object(mut m)) = serde_json::to_value(&app.cfg) else { return };
    m.insert(key.to_string(), val);
    if let Ok(c) = serde_json::from_value::<Config>(Value::Object(m)) {
        app.set_cfg(|cfg| *cfg = c);
    }
}

fn fmt_value(f: &str, v: f32) -> String {
    if f.contains('%') {
        return format!("{:.0}%", v * 100.0);
    }
    let decimals = if f.contains(":.2") { 2 } else { 0 };
    let unit = f.split('}').nth(1).unwrap_or("");
    format!("{v:.*}{unit}", decimals)
}

fn is_int(key: &str) -> bool {
    matches!(key, "dock_opacity" | "bar_opacity" | "switcher_icon" | "switcher_opacity" | "switcher_radius" | "switcher_delay" | "switcher_preview" | "switcher_per_row" | "dock_preview" | "dock_preview_pad" | "lp_columns" | "lp_rows")
}

pub fn open(app: &mut App) {
    if app.settings.is_none() {
        let hwnd = win::create_app_window("Настройки MiniDock");
        let m = sys::primary_monitor();
        let s = m.scale;
        let (w, h) = ((W * s) as i32, (H * s) as i32);
        app.settings = Some(Settings {
            hwnd,
            surf: Surface::default(),
            page: 0,
            scroll: 0.0,
            scroll_to: 0.0,
            content_h: 0.0,
            hits: Vec::new(),
            sliders: Vec::new(),
            hover: None,
            drag_slider: None,
            drag_win: None,
            recording: false,
            opened: crate::anim::now(),
            x: (m.rect.left + m.rect.right) / 2 - w / 2,
            y: (m.rect.top + m.rect.bottom) / 2 - h / 2,
            w,
            h,
            scale: s,
            knobs: HashMap::new(),
            last: crate::anim::now(),
        });
    }
    let st = app.settings.as_mut().unwrap();
    st.opened = crate::anim::now();
    paint(app);
    let st = app.settings.as_ref().unwrap();
    unsafe {
        let _ = SetWindowPos(st.hwnd, Some(HWND_TOP), st.x, st.y, st.w, st.h, SWP_SHOWWINDOW);
    }
    // Windows не даёт фоновому процессу выводить окна вперёд — иначе окно открывалось бы позади программ
    sys::force_foreground(st.hwnd);
    app.request_frame();
}

fn close(app: &mut App) {
    if let Some(st) = app.settings.take() {
        hooks::RECORDING.store(false, std::sync::atomic::Ordering::Relaxed);
        unsafe {
            let _ = DestroyWindow(st.hwnd);
        }
        crate::text::trim_cache();
        sys::trim_working_set();
    }
}

fn paint(app: &mut App) {
    let cfg = app.cfg.clone();
    let autostart = sys::autostart_enabled();
    let enabled_widgets = app.widgets.enabled();
    let Some(st) = app.settings.as_mut() else { return };
    if !st.surf.ensure(st.w, st.h) {
        return;
    }
    let s = st.scale;
    let px = |v: f32| v * s;
    let now = crate::anim::now();
    let k = ease_out(((now - st.opened) / 0.17).min(1.0) as f32);
    let full = R::new(0.0, 0.0, st.w as f32, st.h as f32);
    let groups = page(st.page);
    let mut hits: Vec<(R, Hit)> = Vec::new();
    let mut sliders = Vec::new();
    let hover = st.hover;
    let recording = st.recording;
    let mut knobs_needed: Vec<&'static str> = Vec::new();
    for g in &groups {
        for r in g {
            if let Ctl::Toggle(key) = r.ctl {
                knobs_needed.push(key);
            }
        }
    }
    for key in knobs_needed {
        let on = get(&cfg, key).as_bool().unwrap_or(false);
        st.knobs.entry(key).or_insert(if on { 1.0 } else { 0.0 });
    }
    let knobs = st.knobs.clone();
    let scroll = st.scroll;
    let cur_page = st.page;
    let mut content_h = 0.0;
    {
        let mut cv = st.surf.canvas();
        cv.clear();
        if let Some(path) = rounded_rect(full.inset(0.5), px(12.0)) {
            cv.fill_path(&path, rgba(30, 30, 36, 248));
            cv.stroke_path(&path, rgba(255, 255, 255, 40), 1.0);
        }
        cv.fill_rect(R::new(1.0, 1.0, px(SIDE) - 1.0, full.h - 2.0), rgba(255, 255, 255, 10));
        cv.fill_rect(R::new(px(SIDE), 0.0, 1.0, full.h), rgba(255, 255, 255, 40));
        // светофор
        for (i, c) in ["#ff5f57", "#febc2e", "#3a3a40"].iter().enumerate() {
            let cx = px(12.0 + 7.0 + i as f32 * 22.0);
            let cy = px(21.0);
            cv.fill_circle(cx, cy, px(6.0), parse_hex(c).unwrap());
            if i < 2 {
                hits.push((R::centered(cx, cy, px(16.0), px(16.0)), if i == 0 { Hit::Close } else { Hit::Minimize }));
            }
        }
        // меню страниц
        for (i, (glyph, name)) in PAGES.iter().enumerate() {
            let r = R::new(px(12.0), px(50.0 + i as f32 * 36.0), px(SIDE - 24.0), px(32.0));
            if i == cur_page {
                cv.fill_rrect(r, px(7.0), rgba(10, 132, 255, 210));
            } else if hover == Some(Hit::Nav(i)) {
                cv.fill_rrect(r, px(7.0), rgba(255, 255, 255, 22));
            }
            text::draw_in(&mut cv, glyph, Font::Icons, 14.0 * s, R::new(r.x + px(8.0), r.y, px(22.0), r.h), Align::Center, rgba(242, 242, 245, 255));
            text::draw_in(&mut cv, name, Font::Regular, 12.0 * s, R::new(r.x + px(38.0), r.y, r.w - px(42.0), r.h), Align::Left, rgba(242, 242, 245, 255));
            hits.push((r, Hit::Nav(i)));
        }
        // страница (с прокруткой)
        let cx0 = px(SIDE + 26.0);
        let cw = full.w - cx0 - px(26.0);
        let mut clip = Mask::new(cv.pm.width(), cv.pm.height()).unwrap();
        if let Some(p) = rounded_rect(R::new(px(SIDE) + 1.0, 1.0, full.w - px(SIDE) - 2.0, full.h - 2.0), 0.0) {
            clip.fill_path(&p, FillRule::Winding, false, Transform::identity());
        }
        cv.clip = Some(clip);
        let mut y = px(22.0) - scroll;
        text::draw_in(&mut cv, PAGES[cur_page].1, Font::Semibold, 20.0 * s, R::new(cx0, y, cw, px(30.0)), Align::Left, rgba(242, 242, 245, 255));
        y += px(42.0);
        for (gi, g) in groups.iter().enumerate() {
            // высота группы
            let heights: Vec<f32> = g
                .iter()
                .map(|r| {
                    let dl = if r.desc.is_empty() { 0 } else { text::wrap(r.desc, Font::Regular, 11.0 * s, cw - px(28.0) - px(280.0), 6).len() };
                    px(18.0 + 20.0) + dl as f32 * px(15.0)
                })
                .collect();
            let gh: f32 = heights.iter().sum::<f32>() + px(12.0);
            let gr = R::new(cx0, y, cw, gh);
            cv.fill_rrect(gr, px(10.0), rgba(255, 255, 255, 14));
            cv.stroke_rrect(gr.inset(0.5), px(10.0), rgba(255, 255, 255, 22), 1.0);
            let mut ry = y + px(6.0);
            for (ri, (r, rh)) in g.iter().zip(&heights).enumerate() {
                if ri > 0 {
                    cv.fill_rect(R::new(cx0 + px(14.0), ry, cw - px(28.0), 1.0), rgba(255, 255, 255, 20));
                }
                let tx = cx0 + px(14.0);
                let ctl_w = px(280.0);
                let text_w = cw - px(28.0) - if matches!(r.ctl, Ctl::Text) { 0.0 } else { ctl_w };
                text::draw_in(&mut cv, &r.title, Font::Regular, 12.0 * s, R::new(tx, ry + px(9.0), text_w, px(20.0)), Align::Left, rgba(242, 242, 245, 255));
                if !r.desc.is_empty() {
                    for (li, l) in text::wrap(r.desc, Font::Regular, 11.0 * s, text_w, 6).iter().enumerate() {
                        text::draw_in(&mut cv, l, Font::Regular, 11.0 * s, R::new(tx, ry + px(29.0 + li as f32 * 15.0), text_w, px(15.0)), Align::Left, rgba(255, 255, 255, 130));
                    }
                }
                let cy = ry + px(19.0);
                let right = cx0 + cw - px(14.0);
                let hit = Hit::Ctl(gi, ri, 0);
                let glow = if hover == Some(hit) { 1.0 } else { 0.0 };
                match &r.ctl {
                    Ctl::Toggle(key) => {
                        let tr = R::new(right - px(40.0), cy - px(12.0), px(40.0), px(24.0));
                        toggle(&mut cv, R::new(tr.x + px(1.0), tr.y + px(2.0), px(38.0), px(20.0)), knobs.get(key).copied().unwrap_or(0.0), s);
                        hits.push((tr, hit));
                    }
                    Ctl::Autostart => {
                        let tr = R::new(right - px(40.0), cy - px(12.0), px(40.0), px(24.0));
                        toggle(&mut cv, R::new(tr.x + px(1.0), tr.y + px(2.0), px(38.0), px(20.0)), if autostart { 1.0 } else { 0.0 }, s);
                        hits.push((tr, hit));
                    }
                    Ctl::Widget(id) => {
                        let on = enabled_widgets.iter().any(|w| w == id) || cfg.bar_widgets.iter().any(|w| w == id);
                        let tr = R::new(right - px(40.0), cy - px(12.0), px(40.0), px(24.0));
                        toggle(&mut cv, R::new(tr.x + px(1.0), tr.y + px(2.0), px(38.0), px(20.0)), if on { 1.0 } else { 0.0 }, s);
                        hits.push((tr, hit));
                    }
                    Ctl::Slider(key, lo, hi, f) => {
                        let v = get(&cfg, key).as_f64().unwrap_or(0.0) as f32;
                        let label = if *key == "switcher_per_row" && v == 0.0 { "Все".to_string() } else { fmt_value(f, v) };
                        let vw = px(56.0);
                        let sr = R::new(right - vw - px(228.0), cy - px(2.0), px(220.0), px(4.0));
                        let frac = ((v - lo) / (hi - lo)).clamp(0.0, 1.0);
                        cv.fill_rrect(sr, px(2.0), rgba(255, 255, 255, 40));
                        cv.fill_rrect(R::new(sr.x, sr.y, sr.w * frac, sr.h), px(2.0), accent(255));
                        cv.fill_circle(sr.x + sr.w * frac, cy, px(8.0), rgba(255, 255, 255, 255));
                        text::draw_in(&mut cv, &label, Font::Regular, 12.0 * s, R::new(right - vw, ry + px(9.0), vw, px(20.0)), Align::Right, rgba(255, 255, 255, 170));
                        sliders.push((sr, gi, ri));
                    }
                    Ctl::Combo(key, opts) => {
                        let v = get(&cfg, key);
                        let label = opts.iter().find(|o| Some(o.1) == v.as_str()).map(|o| o.0).unwrap_or("—");
                        let bw = (text::width(label, Font::Regular, 12.0 * s) + px(40.0)).max(px(130.0)).min(ctl_w);
                        let br = R::new(right - bw, cy - px(13.0), bw, px(26.0));
                        cv.fill_rrect(br, px(7.0), rgba(255, 255, 255, (26.0 + 18.0 * glow) as u8));
                        cv.stroke_rrect(br.inset(0.5), px(7.0), rgba(255, 255, 255, 30), 1.0);
                        text::draw_in(&mut cv, label, Font::Regular, 12.0 * s, R::new(br.x + px(10.0), br.y, br.w - px(30.0), br.h), Align::Left, rgba(242, 242, 245, 255));
                        let (ax, ay) = (br.right() - px(14.0), br.cy());
                        cv.polyline(&[(ax - px(4.0), ay - px(2.0)), (ax, ay + px(2.0)), (ax + px(4.0), ay - px(2.0))], rgba(242, 242, 245, 200), px(1.4));
                        hits.push((br, hit));
                    }
                    Ctl::Color(key) => {
                        let c = parse_hex(get(&cfg, key).as_str().unwrap_or("#202026")).unwrap_or(rgba(32, 32, 38, 255));
                        let br = R::new(right - px(46.0), cy - px(12.0), px(46.0), px(24.0));
                        cv.fill_rrect(br, px(6.0), c);
                        cv.stroke_rrect(br.inset(0.5), px(6.0), rgba(255, 255, 255, 80), 1.0);
                        hits.push((br, hit));
                    }
                    Ctl::Button(label, _) => {
                        let bw = text::width(label, Font::Regular, 12.0 * s) + px(28.0);
                        let br = R::new(right - bw, cy - px(13.0), bw, px(26.0));
                        cv.fill_rrect(br, px(7.0), rgba(255, 255, 255, (26.0 + 18.0 * glow) as u8));
                        cv.stroke_rrect(br.inset(0.5), px(7.0), rgba(255, 255, 255, 30), 1.0);
                        text::draw_in(&mut cv, label, Font::Regular, 12.0 * s, br, Align::Center, rgba(242, 242, 245, 255));
                        hits.push((br, hit));
                    }
                    Ctl::Hotkey => {
                        let label = if recording { "Нажмите сочетание…".to_string() } else { hooks::hotkey_name(cfg.big_clock_hotkey) };
                        let bw = (text::width(&label, Font::Semibold, 12.0 * s) + px(28.0)).max(px(110.0));
                        let br = R::new(right - bw, cy - px(13.0), bw, px(26.0));
                        let bg = if recording { accent(200) } else { rgba(255, 255, 255, (26.0 + 18.0 * glow) as u8) };
                        cv.fill_rrect(br, px(7.0), bg);
                        cv.stroke_rrect(br.inset(0.5), px(7.0), rgba(255, 255, 255, 30), 1.0);
                        text::draw_in(&mut cv, &label, Font::Semibold, 12.0 * s, br, Align::Center, rgba(242, 242, 245, 255));
                        hits.push((br, hit));
                    }
                    Ctl::Background(part) => {
                        let cur = cfg.background(part);
                        let name = cur.as_ref().map(|c| sys::file_name(&c.0)).unwrap_or_else(|| "нет".into());
                        text::draw_in(&mut cv, &name, Font::Regular, 11.0 * s, R::new(tx + px(200.0), ry + px(9.0), px(150.0), px(20.0)), Align::Left, rgba(255, 255, 255, 130));
                        let b1 = R::new(right - px(176.0), cy - px(13.0), px(92.0), px(26.0));
                        let b2 = R::new(right - px(78.0), cy - px(13.0), px(78.0), px(26.0));
                        for (bi, (br, label)) in [(b1, "Выбрать…"), (b2, "Убрать")].iter().enumerate() {
                            let h = Hit::Ctl(gi, ri, bi as u8);
                            let g = if hover == Some(h) { 1.0 } else { 0.0 };
                            cv.fill_rrect(*br, px(7.0), rgba(255, 255, 255, (26.0 + 18.0 * g) as u8));
                            cv.stroke_rrect(br.inset(0.5), px(7.0), rgba(255, 255, 255, 30), 1.0);
                            text::draw_in(&mut cv, label, Font::Regular, 12.0 * s, *br, Align::Center, rgba(242, 242, 245, 255));
                            hits.push((*br, h));
                        }
                    }
                    Ctl::Text => {}
                }
                ry += rh;
            }
            y += gh + px(12.0);
        }
        content_h = y + scroll + px(22.0);
        cv.clip = None;
    }
    st.hits = hits;
    st.sliders = sliders;
    st.content_h = content_h;
    st.surf.present(st.hwnd, st.x, st.y, (255.0 * k) as u8);
}

fn slider_set(app: &mut App, gi: usize, ri: usize, x: f32) {
    let Some(st) = app.settings.as_ref() else { return };
    let g = page(st.page);
    let Some((sr, _, _)) = st.sliders.iter().find(|(_, a, b)| *a == gi && *b == ri).copied() else { return };
    if let Ctl::Slider(key, lo, hi, f) = g[gi][ri].ctl {
        let frac = ((x - sr.x) / sr.w).clamp(0.0, 1.0);
        let mut v = lo + (hi - lo) * frac;
        let step = if f.contains(":.2") || f.contains('%') { 0.01 } else { 1.0 };
        v = (v / step).round() * step;
        let val = if is_int(key) { Value::from(v.round() as i64) } else { Value::from(v as f64) };
        set(app, key, val);
    }
}

fn click(app: &mut App, h: Hit) {
    let Some(st) = app.settings.as_mut() else { return };
    match h {
        Hit::Close => close(app),
        Hit::Minimize => unsafe {
            let _ = ShowWindow(st.hwnd, SW_MINIMIZE);
        },
        Hit::Nav(i) => {
            st.page = i;
            st.scroll = 0.0;
            st.scroll_to = 0.0;
            st.recording = false;
            hooks::RECORDING.store(false, std::sync::atomic::Ordering::Relaxed);
        }
        Hit::Ctl(gi, ri, part) => {
            let r = page(st.page)[gi][ri].clone();
            match r.ctl {
                Ctl::Toggle(key) => {
                    let on = get(&app.cfg, key).as_bool().unwrap_or(false);
                    set(app, key, Value::Bool(!on));
                }
                Ctl::Autostart => sys::set_autostart(!sys::autostart_enabled()),
                Ctl::Widget(id) => {
                    let mut list = app.cfg.bar_widgets.clone();
                    if list.iter().any(|w| w == id) {
                        list.retain(|w| w != id);
                    } else {
                        list.push(id.to_string());
                    }
                    app.set_cfg(|c| c.bar_widgets = list);
                }
                Ctl::Combo(key, opts) => {
                    let cur = get(&app.cfg, key);
                    let items: Vec<MenuItem> = opts
                        .iter()
                        .map(|(label, value)| {
                            let v = value.to_string();
                            MenuItem::radio(label, cur.as_str() == Some(value), Some(std::rc::Rc::new(move |a: &mut App| set(a, key, Value::String(v.clone())))))
                        })
                        .collect();
                    let (cx, cy) = win::cursor_pos();
                    app.show_menu(items, cx, cy, Owner::Other);
                }
                Ctl::Color(key) => {
                    let cur = get(&app.cfg, key).as_str().unwrap_or("#202026").to_string();
                    let owner = st.hwnd.0 as isize;
                    crate::app::defer(move || {
                        if let Some(c) = choose_color(win::hwnd_of(owner), &cur) {
                            crate::app::with(|a| set(a, key, Value::String(c)));
                        }
                    });
                }
                Ctl::Hotkey => {
                    st.recording = !st.recording;
                    hooks::RECORDING.store(st.recording, std::sync::atomic::Ordering::Relaxed);
                }
                Ctl::Button(_, id) => match id {
                    1 => {
                        shell::spawn("explorer.exe", &[&crate::config::cfg_dir()]);
                    }
                    2 => {
                        let keep_pins = app.cfg.pinned.clone();
                        let keep_tb = app.cfg.taskbar_orig_state;
                        let keep_sh = app.cfg.shadows_orig;
                        app.set_cfg(|c| {
                            *c = Config::default();
                            c.pinned = keep_pins;
                            c.taskbar_orig_state = keep_tb;
                            c.shadows_orig = keep_sh;
                        });
                        app.settings.as_mut().unwrap().knobs.clear();
                    }
                    3 => crate::switcher::begin(app, 1),
                    4 => app.catalog.load(),
                    5 => {
                        shell::launch("https://github.com/ChickenRagoo/MiniDock/releases", "", None);
                    }
                    _ => {}
                },
                Ctl::Background(part) => {
                    if part_click(app, part, gi, ri, part_button(h)) {
                        return;
                    }
                }
                Ctl::Text => {}
                Ctl::Slider(..) => {}
            }
            let _ = part;
        }
        Hit::Drag => {}
    }
    paint(app);
}

fn part_button(h: Hit) -> u8 {
    match h {
        Hit::Ctl(_, _, p) => p,
        _ => 0,
    }
}

fn part_click(app: &mut App, part: &'static str, _gi: usize, _ri: usize, button: u8) -> bool {
    if button == 1 {
        app.set_cfg(|c| {
            c.backgrounds.remove(part);
        });
        return false;
    }
    let owner = app.settings.as_ref().map(|s| s.hwnd.0 as isize).unwrap_or(0);
    crate::app::defer(move || {
        let files = shell::open_files_dialog(win::hwnd_of(owner), "Картинка для фона", "Картинки", "*.png;*.jpg;*.jpeg;*.bmp;*.webp;*.gif", false);
        if let Some(f) = files.into_iter().next() {
            crate::app::with(|a| {
                let mut o = serde_json::Map::new();
                o.insert("path".into(), Value::String(f));
                o.insert("opacity".into(), Value::from(1.0));
                a.set_cfg(|c| {
                    c.backgrounds.insert(part.to_string(), Value::Object(o));
                });
                paint(a);
            });
        }
    });
    true
}

fn choose_color(owner: HWND, cur: &str) -> Option<String> {
    use windows::Win32::Foundation::COLORREF;
    use windows::Win32::UI::Controls::Dialogs::{ChooseColorW, CC_ANYCOLOR, CC_FULLOPEN, CC_RGBINIT, CHOOSECOLORW};
    let c = parse_hex(cur).unwrap_or(rgba(32, 32, 38, 255)).to_color_u8();
    // в буфере каналы переставлены: red() — синий
    let rgb = (c.blue() as u32) | (c.green() as u32) << 8 | (c.red() as u32) << 16;
    let mut custom = [COLORREF(0); 16];
    let mut cc = CHOOSECOLORW {
        lStructSize: std::mem::size_of::<CHOOSECOLORW>() as u32,
        hwndOwner: owner,
        rgbResult: COLORREF(rgb),
        lpCustColors: custom.as_mut_ptr(),
        Flags: CC_RGBINIT | CC_FULLOPEN | CC_ANYCOLOR,
        ..Default::default()
    };
    unsafe {
        if ChooseColorW(&mut cc).as_bool() {
            let v = cc.rgbResult.0;
            return Some(format!("#{:02x}{:02x}{:02x}", v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF));
        }
    }
    None
}

pub fn on_message(app: &mut App, hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> Option<LRESULT> {
    let st = app.settings.as_mut()?;
    if st.hwnd != hwnd {
        return None;
    }
    let (x, y) = win::lparam_xy(lp);
    let (fx, fy) = (x as f32, y as f32);
    let s = st.scale;
    let hit_at = |st: &Settings| st.hits.iter().find(|(r, _)| r.contains(fx, fy)).map(|h| h.1);
    match msg {
        WM_MOUSEMOVE => {
            if let Some((ox, oy)) = st.drag_win {
                let (cx, cy) = win::cursor_pos();
                st.x = cx - ox;
                st.y = cy - oy;
                let (nx, ny) = (st.x, st.y);
                unsafe {
                    let _ = SetWindowPos(st.hwnd, None, nx, ny, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
                }
                return Some(LRESULT(0));
            }
            if let Some((gi, ri)) = st.drag_slider {
                slider_set(app, gi, ri, fx);
                paint(app);
                return Some(LRESULT(0));
            }
            let h = hit_at(st);
            if h != st.hover {
                st.hover = h;
                paint(app);
            }
        }
        WM_LBUTTONDOWN => {
            unsafe {
                windows::Win32::UI::Input::KeyboardAndMouse::SetCapture(hwnd);
            }
            if let Some((_, gi, ri)) = st.sliders.iter().find(|(r, _, _)| r.adjusted(-6.0 * s, -10.0 * s, 6.0 * s, 10.0 * s).contains(fx, fy)).copied() {
                st.drag_slider = Some((gi, ri));
                slider_set(app, gi, ri, fx);
                paint(app);
            } else if hit_at(st).is_none() && fy < 48.0 * s {
                let r = win::window_rect(hwnd);
                let (cx, cy) = win::cursor_pos();
                st.drag_win = Some((cx - r.left, cy - r.top));
            }
        }
        WM_LBUTTONUP => {
            unsafe {
                let _ = windows::Win32::UI::Input::KeyboardAndMouse::ReleaseCapture();
            }
            let was = st.drag_slider.take().is_some() | st.drag_win.take().is_some();
            if !was {
                if let Some(h) = hit_at(st) {
                    click(app, h);
                }
            }
        }
        WM_MOUSEWHEEL => {
            let d = ((wp.0 >> 16) as i16) as f32 / 120.0 * 90.0 * s;
            let max = (st.content_h - st.h as f32).max(0.0);
            st.scroll_to = (st.scroll_to - d).clamp(0.0, max);
        }
        WM_KEYDOWN => {
            if wp.0 == 0x1B && !st.recording {
                close(app);
                return Some(LRESULT(0));
            }
        }
        WM_ACTIVATE => {}
        WM_CLOSE => {
            close(app);
            return Some(LRESULT(0));
        }
        _ => return None,
    }
    app.request_frame();
    Some(LRESULT(0))
}

/// Кадр, пока открыто: прокрутка, переключатели, запись сочетания.
pub fn tick(app: &mut App) -> bool {
    let cfg = app.cfg.clone();
    let busy = match app.settings.as_mut() {
        Some(st) => st.frame(&cfg),
        None => return false,
    };
    if busy {
        paint(app);
    }
    busy
}

//! Spotlight: поиск приложений (на любом языке и в любой раскладке + нечёткий nucleo), файлов (индекс Windows,
//! в фоне), системных команд, калькулятор и поиск в интернете.

use std::collections::{HashMap, HashSet};
use std::rc::Rc;

use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::app::App;
use crate::catalog::Entry;
use crate::config::Config;
use crate::gfx::*;
use crate::glass::Glass;
use crate::lineedit::{Edit, LineEdit};
use crate::text::{self, Align, Font};
use crate::win::{self, shell, sys, Surface};

const ROW_H: f32 = 40.0;
const HEAD_H: f32 = 24.0;
const TOP_H: f32 = 62.0;
const WIDTH: f32 = 720.0;
const MAX_ROWS: usize = 11;
const SKIP_PARTS: [&str; 5] = ["\\appdata\\", "\\.", "\\node_modules\\", "\\$recycle.bin\\", "\\__pycache__\\"];

// ---------------------------------------------------------------- поиск на любом языке

const EN: &str = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./";
const RU: &str = "ёйцукенгшщзхъфывапролджэячсмитьбю.";

/// Текст, набранный не в той раскладке: «ntktuhfv» → «телеграм», «еудупкфь» → «telegram».
pub fn swap_layout(t: &str) -> String {
    let en: Vec<char> = EN.chars().collect();
    let ru: Vec<char> = RU.chars().collect();
    let cyr = t.chars().filter(|c| ('а'..='я').contains(c) || *c == 'ё').count();
    let to_en = cyr * 2 >= t.chars().count();
    t.chars()
        .map(|c| {
            if to_en {
                ru.iter().position(|r| *r == c).map(|i| en[i]).unwrap_or(c)
            } else {
                en.iter().position(|e| *e == c).map(|i| ru[i]).unwrap_or(c)
            }
        })
        .collect()
}

fn translit(c: char) -> &'static str {
    const T: [(&str, &str); 33] = [
        ("а", "a"), ("б", "b"), ("в", "v"), ("г", "g"), ("д", "d"), ("е", "e"), ("ё", "e"), ("ж", "zh"), ("з", "z"), ("и", "i"), ("й", "y"),
        ("к", "k"), ("л", "l"), ("м", "m"), ("н", "n"), ("о", "o"), ("п", "p"), ("р", "r"), ("с", "s"), ("т", "t"), ("у", "u"), ("ф", "f"),
        ("х", "kh"), ("ц", "ts"), ("ч", "ch"), ("ш", "sh"), ("щ", "sch"), ("ъ", ""), ("ы", "y"), ("ь", ""), ("э", "e"), ("ю", "yu"), ("я", "ya"),
    ];
    let mut b = [0u8; 4];
    let s = c.encode_utf8(&mut b);
    T.iter().find(|(k, _)| *k == s).map(|(_, v)| *v).unwrap_or("")
}

/// Звуковой «скелет»: одинаково для «телеграм» и telegram, «хром» и chrome, «стим» и steam.
pub fn skeleton(t: &str) -> String {
    let mut s = String::new();
    for c in t.to_lowercase().chars() {
        let tr = translit(c);
        if tr.is_empty() && !('а'..='я').contains(&c) && c != 'ё' {
            s.push(c);
        } else {
            s.push_str(tr);
        }
    }
    for (a, b) in [("sch", "sh"), ("ph", "f"), ("ck", "k"), ("qu", "kv"), ("c", "k"), ("q", "k"), ("w", "v"), ("x", "ks"), ("you", "iu"), ("ou", "u"), ("ee", "i"), ("ea", "i"), ("y", "i"), ("ai", "i"), ("ei", "i")] {
        s = s.replace(a, b);
    }
    let mut out = String::new();
    let mut last: Option<char> = None;
    for c in s.chars() {
        if last != Some(c) || !c.is_alphabetic() {
            out.push(c);
        }
        last = Some(c);
    }
    out
}

fn base_score(name: &str, q: &str) -> i32 {
    let n = name.to_lowercase();
    if n == q {
        return 120;
    }
    if n.starts_with(q) {
        return 100;
    }
    let words: Vec<&str> = n.split(|c: char| c.is_whitespace() || c == '-' || c == '_').filter(|w| !w.is_empty()).collect();
    if words.iter().any(|w| w.starts_with(q)) {
        return 80;
    }
    if n.contains(q) {
        return 55;
    }
    if q.chars().count() > 1 && words.iter().filter_map(|w| w.chars().next()).collect::<String>().starts_with(q) {
        return 45; // «vsc» → Visual Studio Code
    }
    0
}

thread_local! {
    static MATCHER: std::cell::RefCell<nucleo_matcher::Matcher> = std::cell::RefCell::new(nucleo_matcher::Matcher::new(nucleo_matcher::Config::DEFAULT));
}

fn fuzzy(name: &str, q: &str) -> Option<u32> {
    use nucleo_matcher::pattern::{CaseMatching, Normalization, Pattern};
    let pat = Pattern::parse(q, CaseMatching::Ignore, Normalization::Smart);
    let mut buf = Vec::new();
    MATCHER.with(|m| pat.score(nucleo_matcher::Utf32Str::new(name, &mut buf), &mut m.borrow_mut()))
}

/// Насколько название подходит к запросу (q — в нижнем регистре). Находит и набранное не в той раскладке,
/// и транслитом, и с опечатками/пропусками букв (nucleo) — такие совпадения ниже точных.
pub fn score(name: &str, q: &str) -> i32 {
    let mut best = base_score(name, q);
    let qn = q.chars().count();
    if qn < 2 || best >= 100 {
        return best;
    }
    let swapped = swap_layout(q);
    let extra = if best > 0 { 30 } else { 0 };
    let s = base_score(name, &swapped);
    if s > 0 {
        best = best.max(s - 6 - extra);
    }
    if qn >= 3 && q.chars().any(|c| c.is_alphabetic()) {
        let skn = skeleton(name);
        for (v, pen) in [(q.to_string(), 10), (swapped.clone(), 14)] {
            let sk = skeleton(&v);
            if sk.chars().count() >= 2 {
                let s = base_score(&skn, &sk);
                if s > 0 {
                    best = best.max(s - pen - extra);
                }
            }
        }
        if best == 0 {
            // нечёткий поиск: буквы по порядку с пропусками («tlgrm», «vscd»)
            if let Some(f) = fuzzy(name, q).or_else(|| fuzzy(name, &swapped)) {
                if f >= 16 * qn as u32 {
                    best = 25 + (f / 12).min(15) as i32;
                }
            }
        }
    }
    best
}

// ---------------------------------------------------------------- калькулятор

/// Только арифметика и функции math, никакого произвольного кода.
pub fn calc(text: &str) -> Option<String> {
    let expr: String = text.trim().replace('^', "**").replace('×', "*").replace('÷', "/").replace(',', ".");
    if !expr.chars().any(|c| c.is_ascii_digit()) || !expr.chars().any(|c| "+-*/%(".contains(c)) {
        return None;
    }
    let toks: Vec<char> = expr.chars().filter(|c| !c.is_whitespace()).collect();
    let mut p = Parser { t: toks, i: 0 };
    let v = p.expr()?;
    if p.i != p.t.len() || !v.is_finite() {
        return None;
    }
    if v.fract() == 0.0 && v.abs() < 1e15 {
        return Some(format!("{}", v as i64));
    }
    let s = format!("{:.10}", v);
    let s = s.trim_end_matches('0').trim_end_matches('.').to_string();
    Some(s)
}

struct Parser {
    t: Vec<char>,
    i: usize,
}

impl Parser {
    fn peek(&self) -> Option<char> {
        self.t.get(self.i).copied()
    }
    fn eat(&mut self, s: &str) -> bool {
        let cs: Vec<char> = s.chars().collect();
        if self.t.len() >= self.i + cs.len() && self.t[self.i..self.i + cs.len()] == cs[..] {
            self.i += cs.len();
            return true;
        }
        false
    }
    fn expr(&mut self) -> Option<f64> {
        let mut v = self.term()?;
        loop {
            if self.eat("+") {
                v += self.term()?;
            } else if self.eat("-") {
                v -= self.term()?;
            } else {
                return Some(v);
            }
        }
    }
    fn term(&mut self) -> Option<f64> {
        let mut v = self.power()?;
        loop {
            if self.peek() == Some('*') && self.t.get(self.i + 1) == Some(&'*') {
                return Some(v);
            }
            if self.eat("//") {
                v = (v / self.power()?).floor();
            } else if self.eat("*") {
                v *= self.power()?;
            } else if self.eat("/") {
                v /= self.power()?;
            } else if self.eat("%") {
                v %= self.power()?;
            } else {
                return Some(v);
            }
        }
    }
    fn power(&mut self) -> Option<f64> {
        let b = self.unary()?;
        if self.eat("**") {
            let e = self.power()?;
            if e.abs() > 1000.0 {
                return None;
            }
            return Some(b.powf(e));
        }
        Some(b)
    }
    fn unary(&mut self) -> Option<f64> {
        if self.eat("-") {
            return Some(-self.unary()?);
        }
        if self.eat("+") {
            return self.unary();
        }
        self.atom()
    }
    fn atom(&mut self) -> Option<f64> {
        if self.eat("(") {
            let v = self.expr()?;
            return self.eat(")").then_some(v);
        }
        let start = self.i;
        while self.peek().is_some_and(|c| c.is_ascii_digit() || c == '.') {
            self.i += 1;
        }
        if self.i > start {
            return self.t[start..self.i].iter().collect::<String>().parse().ok();
        }
        while self.peek().is_some_and(|c| c.is_alphabetic()) {
            self.i += 1;
        }
        let name: String = self.t[start..self.i].iter().collect::<String>().to_lowercase();
        match name.as_str() {
            "pi" | "пи" => return Some(std::f64::consts::PI),
            "e" => return Some(std::f64::consts::E),
            _ => {}
        }
        if !self.eat("(") {
            return None;
        }
        let a = self.expr()?;
        if !self.eat(")") {
            return None;
        }
        Some(match name.as_str() {
            "sqrt" => a.sqrt(),
            "sin" => a.to_radians().sin(),
            "cos" => a.to_radians().cos(),
            "tan" => a.to_radians().tan(),
            "log" => a.log10(),
            "ln" => a.ln(),
            "abs" => a.abs(),
            "round" => a.round(),
            "exp" => a.exp(),
            _ => return None,
        })
    }
}

// ---------------------------------------------------------------- результаты

#[derive(Clone)]
enum Icon {
    Glyph(&'static str),
    App(Entry),
    File(String),
}

#[derive(Clone)]
enum Act {
    Run(Entry),
    System(usize),
    Open(String),
    Reveal(String),
    Copy(String),
    Web(String),
}

#[derive(Clone)]
struct Res {
    section: &'static str,
    title: String,
    sub: String,
    icon: Icon,
    act: Act,
    alt: Option<Act>,
    calc: bool,
}

const SYSTEM: [(&str, &str, &str); 20] = [
    ("Launchpad", "launchpad пуск start", "\u{E80F}"),
    ("Настройки MiniDock", "minidock dock док настройки", "\u{E713}"),
    ("Параметры Windows", "settings параметры настройки", "\u{E713}"),
    ("Wi‑Fi и сеть", "wifi сеть интернет network", "\u{E701}"),
    ("Bluetooth", "bluetooth блютуз", "\u{E702}"),
    ("Звук", "sound звук громкость audio", "\u{E767}"),
    ("Дисплей", "display экран монитор разрешение", "\u{E7F4}"),
    ("Персонализация и обои", "обои wallpaper персонализация тема", "\u{E771}"),
    ("Центр обновления Windows", "update обновление", "\u{E895}"),
    ("Приложения и возможности", "удалить программы apps uninstall", "\u{E71D}"),
    ("Диспетчер задач", "task manager диспетчер процессы", "\u{E9D9}"),
    ("Панель управления", "control panel панель управления", "\u{E713}"),
    ("Корзина", "trash корзина recycle", "\u{E74D}"),
    ("Загрузки", "downloads загрузки", "\u{E896}"),
    ("Документы", "documents документы", "\u{E8A5}"),
    ("Рабочий стол", "desktop рабочий стол", "\u{E7F8}"),
    ("Заблокировать экран", "lock блокировка заблокировать", "\u{E72E}"),
    ("Сон", "sleep сон спящий", "\u{E708}"),
    ("Перезагрузить", "restart reboot перезагрузка", "\u{E72C}"),
    ("Выключить", "shutdown выключение выключить", "\u{E7E8}"),
];

fn run_system(app: &mut App, i: usize) {
    let open = |u: &str| {
        shell::launch(u, "", None);
    };
    match i {
        0 => crate::launchpad::open(app),
        1 => app.open_settings(),
        2 => open("ms-settings:"),
        3 => open("ms-settings:network-wifi"),
        4 => open("ms-settings:bluetooth"),
        5 => open("ms-settings:sound"),
        6 => open("ms-settings:display"),
        7 => open("ms-settings:personalization"),
        8 => open("ms-settings:windowsupdate"),
        9 => open("ms-settings:appsfeatures"),
        10 => {
            shell::spawn("taskmgr.exe", &[]);
        }
        11 => {
            shell::spawn("control", &[]);
        }
        12 => shell::open_recycle_bin(),
        13 => {
            shell::spawn("explorer.exe", &["shell:Downloads"]);
        }
        14 => {
            shell::spawn("explorer.exe", &["shell:Personal"]);
        }
        15 => {
            shell::spawn("explorer.exe", &["shell:Desktop"]);
        }
        16 => unsafe {
            let _ = windows::Win32::System::Shutdown::LockWorkStation();
        },
        17 => unsafe {
            let _ = windows::Win32::System::Power::SetSuspendState(false, false, false);
        },
        18 | 19 => {
            let (text, args): (&str, &'static [&'static str]) = if i == 18 { ("Перезагрузить компьютер?", &["/r", "/t", "0"]) } else { ("Выключить компьютер?", &["/s", "/t", "0"]) };
            let t = text.to_string();
            crate::app::defer(move || unsafe {
                let r = MessageBoxW(None, &windows::core::HSTRING::from(t.as_str()), windows::core::w!("MiniDock"), MB_YESNO | MB_ICONQUESTION | MB_TOPMOST | MB_SETFOREGROUND);
                if r == IDYES {
                    shell::spawn("shutdown", args);
                }
            });
        }
        _ => {}
    }
}

pub struct Spotlight {
    hwnd: HWND,
    surf: Surface,
    glass: Option<Glass>,
    pub visible: bool,
    edit: LineEdit,
    results: Vec<Res>,
    files: Vec<(String, String)>,
    selected: usize,
    picked: HashSet<usize>,
    scroll: usize,
    seq: u64,
    file_due: Option<f64>,
    opened: f64,
    closing: Option<f64>,
    was_active: bool,
    x: i32,
    y: i32,
    w: i32,
    h: i32,
    scale: f32,
    icons: HashMap<String, Option<Rc<Image>>>,
}

impl Spotlight {
    pub fn new() -> Spotlight {
        Spotlight {
            hwnd: HWND::default(),
            surf: Surface::default(),
            glass: None,
            visible: false,
            edit: LineEdit::default(),
            results: Vec::new(),
            files: Vec::new(),
            selected: 0,
            picked: HashSet::new(),
            scroll: 0,
            seq: 0,
            file_due: None,
            opened: 0.0,
            closing: None,
            was_active: false,
            x: 0,
            y: 0,
            w: 0,
            h: 0,
            scale: 1.0,
            icons: HashMap::new(),
        }
    }

    pub fn frame(&mut self, _cfg: &Config) -> bool {
        self.visible
    }

    fn rows_layout(&self) -> (Vec<(f32, bool, usize)>, f32) {
        let mut out = Vec::new();
        let mut y = TOP_H;
        let mut last = "";
        for (i, r) in self.results.iter().enumerate().skip(self.scroll).take(MAX_ROWS) {
            if r.section != last {
                out.push((y, true, i));
                y += HEAD_H;
                last = r.section;
            }
            out.push((y, false, i));
            y += ROW_H;
        }
        (out, y)
    }

    fn completion(&self) -> Option<(String, String)> {
        let text = &self.edit.text;
        if text.trim().is_empty() || self.results.is_empty() || self.edit.anchor.is_some_and(|a| a != self.edit.cursor) || self.edit.cursor != text.chars().count() {
            return None;
        }
        let r = &self.results[self.selected];
        if r.calc || r.section == "Интернет" {
            return None;
        }
        let tl = r.title.to_lowercase();
        let ql = text.to_lowercase();
        if tl == ql {
            return None;
        }
        if tl.starts_with(&ql) {
            let rest: String = r.title.chars().skip(text.chars().count()).collect();
            return Some((rest, r.title.clone()));
        }
        Some((format!(" — {}", r.title), r.title.clone()))
    }
}

fn rebuild(app: &mut App, keep_selection: bool) {
    let text = app.spotlight.edit.text.trim().to_string();
    let q = text.to_lowercase();
    let mut res: Vec<Res> = Vec::new();
    if !q.is_empty() {
        let mut apps: Vec<(i32, &Entry)> = app.catalog.entries.iter().filter_map(|e| {
            let s = score(&e.name, &q);
            (s > 0).then_some((s, e))
        }).collect();
        apps.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.name.to_lowercase().cmp(&b.1.name.to_lowercase())));
        let mut system: Vec<(i32, usize)> = SYSTEM
            .iter()
            .enumerate()
            .filter_map(|(i, (n, kw, _))| {
                let s = score(n, &q).max(if kw.contains(q.as_str()) { 40 } else { 0 });
                (s > 0).then_some((s, i))
            })
            .collect();
        system.sort_by(|a, b| b.0.cmp(&a.0));
        let app_row = |e: &Entry, section: &'static str| Res { section, title: e.name.clone(), sub: "Приложение".into(), icon: Icon::App(e.clone()), act: Act::Run(e.clone()), alt: None, calc: false };
        let sys_row = |i: usize, section: &'static str| Res { section, title: SYSTEM[i].0.into(), sub: "Система".into(), icon: Icon::Glyph(SYSTEM[i].2), act: Act::System(i), alt: None, calc: false };
        let mut apps_iter = apps.into_iter().peekable();
        let mut sys_iter = system.into_iter().peekable();
        if let Some(v) = calc(&text) {
            res.push(Res { section: "Калькулятор", title: format!("= {v}"), sub: format!("{text}   ·   Enter — скопировать"), icon: Icon::Glyph("\u{E8EF}"), act: Act::Copy(v), alt: None, calc: true });
        } else if apps_iter.peek().is_some() && sys_iter.peek().is_none_or(|s| apps_iter.peek().unwrap().0 >= s.0) {
            let e = apps_iter.next().unwrap().1;
            res.push(app_row(e, "Лучшее совпадение"));
        } else if let Some((_, i)) = sys_iter.next() {
            res.push(sys_row(i, "Лучшее совпадение"));
        }
        for (_, e) in apps_iter.take(6) {
            res.push(app_row(e, "Приложения"));
        }
        for (_, i) in sys_iter.take(4) {
            res.push(sys_row(i, "Система"));
        }
        for (path, name) in &app.spotlight.files {
            res.push(Res {
                section: "Документы и папки",
                title: name.clone(),
                sub: sys::parent_dir(path),
                icon: Icon::File(path.clone()),
                act: Act::Open(path.clone()),
                alt: Some(Act::Reveal(path.clone())),
                calc: false,
            });
        }
        res.push(Res { section: "Интернет", title: format!("Искать «{text}» в интернете"), sub: "Google".into(), icon: Icon::Glyph("\u{E774}"), act: Act::Web(text.clone()), alt: None, calc: false });
    }
    let sp = &mut app.spotlight;
    sp.results = res;
    if !keep_selection {
        sp.picked.clear();
    }
    let n = sp.results.len();
    sp.picked.retain(|i| *i < n);
    if !keep_selection || sp.selected >= n {
        sp.selected = 0;
        sp.scroll = 0;
    }
    relayout(app);
}

fn relayout(app: &mut App) {
    let sp = &mut app.spotlight;
    let (_, bottom) = sp.rows_layout();
    let h = if sp.results.is_empty() { TOP_H } else { bottom + 10.0 };
    sp.h = (h * sp.scale).ceil() as i32;
    paint(app);
}

fn file_search(app: &mut App) {
    let seq = app.spotlight.seq;
    let text = app.spotlight.edit.text.trim().to_string();
    if text.chars().count() < 2 {
        return;
    }
    crate::app::spawn_then(
        move || {
            crate::win::shell::com_init();
            search_files(&text)
        },
        move |a, mut files| {
            if a.spotlight.seq != seq || !a.spotlight.visible {
                return; // ответ на старый запрос
            }
            let q = a.spotlight.edit.text.trim().to_lowercase();
            files.sort_by_key(|(p, n)| (-score(&sys::file_stem(n), &q), !std::path::Path::new(p).is_dir()));
            files.truncate(8);
            a.spotlight.files = files;
            rebuild(a, true);
        },
    );
}

/// Файлы и папки в домашней папке по индексу Windows (поиск оболочки search-ms).
fn search_files(text: &str) -> Vec<(String, String)> {
    use windows::core::HSTRING;
    use windows::Win32::UI::Shell::*;
    let enc = |s: &str| -> String {
        s.bytes()
            .map(|b| if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) { (b as char).to_string() } else { format!("%{b:02X}") })
            .collect()
    };
    let home = std::env::var("USERPROFILE").unwrap_or_default();
    let q = format!("search-ms:query={}&crumb=location:{}", enc(text), enc(&home));
    let mut out = Vec::new();
    let ql = text.to_lowercase();
    unsafe {
        let Ok(item) = SHCreateItemFromParsingName::<_, _, IShellItem>(&HSTRING::from(q), None) else { return out };
        let Ok(en) = item.BindToHandler::<_, IEnumShellItems>(None, &BHID_EnumItems) else { return out };
        let mut n = 0;
        loop {
            let mut items = [None];
            let mut got = 0u32;
            if en.Next(&mut items, Some(&mut got)).is_err() || got == 0 || n > 200 {
                break;
            }
            n += 1;
            let Some(it) = items[0].take() else { break };
            let Ok(p) = it.GetDisplayName(SIGDN_FILESYSPATH) else { continue };
            let path = shell::take_pwstr(p);
            let low = path.to_lowercase();
            if path.is_empty() || SKIP_PARTS.iter().any(|s| low.contains(s)) {
                continue;
            }
            let name = sys::file_name(&path);
            if !name.to_lowercase().contains(&ql) && score(&sys::file_stem(&name), &ql) == 0 {
                continue; // совпало только содержимое — не показываем (как в Python-версии: по имени)
            }
            out.push((path, name));
            if out.len() >= 60 {
                break;
            }
        }
    }
    out
}

fn icon_for(sp: &mut Spotlight, catalog: &mut crate::catalog::Catalog, r: &Res, size: u32) -> Option<Rc<Image>> {
    let key = match &r.icon {
        Icon::App(e) => format!("a|{}", e.parse),
        Icon::File(p) => format!("f|{p}"),
        Icon::Glyph(_) => return None,
    };
    if let Some(i) = sp.icons.get(&key) {
        return i.clone();
    }
    let img = match &r.icon {
        Icon::App(e) => catalog.icon(e).map(|i| Rc::new(i.scaled(size, size))),
        Icon::File(p) => shell::file_icon(p, size as i32).map(|i| Rc::new(i.scaled(size, size))),
        Icon::Glyph(_) => None,
    };
    if sp.icons.len() > 300 {
        sp.icons.clear();
    }
    sp.icons.insert(key, img.clone());
    img
}

fn paint(app: &mut App) {
    let cfg = app.cfg.clone();
    if !app.spotlight.visible {
        return;
    }
    let s = app.spotlight.scale;
    let isz = (28.0 * s) as u32;
    let (rows, _) = app.spotlight.rows_layout();
    let icons: Vec<Option<Rc<Image>>> = rows
        .iter()
        .map(|(_, head, i)| {
            if *head {
                return None;
            }
            let r = app.spotlight.results[*i].clone();
            let (sp, cat) = (&mut app.spotlight, &mut app.catalog);
            icon_for(sp, cat, &r, isz)
        })
        .collect();
    let bg = app.background("spotlight");
    let sp = &mut app.spotlight;
    if !sp.surf.ensure(sp.w, sp.h) {
        return;
    }
    let now = crate::anim::now();
    let k = match sp.closing {
        Some(t) => 1.0 - ((now - t) / 0.12).min(1.0) as f32,
        None => ease_out(((now - sp.opened) / 0.17).min(1.0) as f32),
    };
    let full = R::new(0.0, 0.0, sp.w as f32, sp.h as f32);
    let comp = sp.completion();
    let edit_r = R::new(54.0 * s, 12.0 * s, (WIDTH - 70.0) * s, 38.0 * s);
    let fsz = 22.7 * s;
    let results = sp.results.clone();
    let (selected, picked) = (sp.selected, sp.picked.clone());
    let edit = sp.edit.clone();
    {
        let mut cv = sp.surf.canvas();
        cv.clear();
        if let Some(path) = rounded_rect(full.inset(0.5), 14.0 * s) {
            cv.fill_path(&path, rgba(32, 32, 38, 175));
            if let Some((img, op)) = &bg {
                crate::dock::draw_cover(&mut cv, img, full, &path, *op);
            }
            cv.stroke_path(&path, rgba(255, 255, 255, 45), 1.0);
        }
        text::draw_in(&mut cv, "\u{E721}", Font::Icons, 20.0 * s, R::new(14.0 * s, 12.0 * s, 36.0 * s, 38.0 * s), Align::Center, rgba(255, 255, 255, 170));
        edit.paint(&mut cv, edit_r, Font::Light, fsz, rgba(244, 244, 247, 255), "Поиск Spotlight", false);
        if let Some((ghost, _)) = comp {
            // серая подсказка автозаполнения прямо после курсора (Tab — принять)
            let x = edit_r.x + text::width(&edit.text, Font::Light, fsz) + 1.0;
            let room = edit_r.right() - x;
            text::draw_in(&mut cv, &ghost, Font::Light, fsz, R::new(x, edit_r.y, room, edit_r.h), Align::Left, rgba(255, 255, 255, 95));
        }
        if !results.is_empty() {
            cv.fill_rect(R::new(0.0, (TOP_H - 4.0) * s, full.w, 1.0), rgba(255, 255, 255, 30));
            for ((y, head, i), icon) in rows.iter().zip(&icons) {
                let r = &results[*i];
                let y = y * s;
                if *head {
                    text::draw_in(&mut cv, r.section, Font::Semibold, 11.0 * s, R::new(18.0 * s, y + 6.0 * s, full.w - 36.0 * s, HEAD_H * s - 6.0 * s), Align::Left, rgba(255, 255, 255, 120));
                    continue;
                }
                let bx = R::new(8.0 * s, y + 2.0 * s, full.w - 16.0 * s, (ROW_H - 4.0) * s);
                if picked.contains(i) && *i != selected {
                    cv.fill_rrect(bx, 8.0 * s, rgba(10, 132, 255, 120));
                }
                if *i == selected {
                    cv.fill_rrect(bx, 8.0 * s, rgba(10, 132, 255, 215));
                }
                let ir = R::new(20.0 * s, y + 6.0 * s, 28.0 * s, 28.0 * s);
                match (&r.icon, icon) {
                    (Icon::Glyph(g), _) => text::draw_in(&mut cv, g, Font::Icons, 17.0 * s, ir, Align::Center, rgba(245, 245, 248, 255)),
                    (_, Some(img)) => cv.image(img, R::centered(ir.cx(), ir.cy(), img.w as f32, img.h as f32)),
                    _ => {}
                }
                let tw = full.w - 90.0 * s - if r.sub.is_empty() { 0.0 } else { 200.0 * s };
                text::draw_in(&mut cv, &r.title, Font::Regular, 13.3 * s, R::new(60.0 * s, y, tw, ROW_H * s), Align::Left, rgba(250, 250, 252, 255));
                if !r.sub.is_empty() {
                    text::draw_in(&mut cv, &r.sub, Font::Regular, 11.0 * s, R::new(full.w - 230.0 * s, y, 205.0 * s, ROW_H * s), Align::Right, rgba(255, 255, 255, if *i == selected { 200 } else { 120 }));
                }
            }
        }
    }
    let dy = (-10.0 * s * (1.0 - k)) as i32;
    sp.surf.present(sp.hwnd, sp.x, sp.y + dy, (255.0 * k) as u8);
    if let Some(g) = sp.glass.as_mut() {
        g.place(sp.x, sp.y + dy, sp.x + sp.w, sp.y + dy + sp.h, (14.0 * s) as i32, sp.hwnd, crate::app::blur_kind(&cfg), 0x01000000);
    }
}

pub fn toggle(app: &mut App) {
    if app.spotlight.visible && app.spotlight.closing.is_none() {
        close(app);
    } else {
        open(app);
    }
}

pub fn open(app: &mut App) {
    if !app.catalog.loaded {
        app.catalog.load();
    }
    let sp = &mut app.spotlight;
    if sp.hwnd.is_invalid() {
        sp.hwnd = win::create_focusable("MiniDockSpotlight", true);
        win::dwm::exclude_from_peek(sp.hwnd);
    }
    if sp.glass.is_none() {
        sp.glass = Some(Glass::new());
    }
    let m = sys::primary_monitor();
    sp.scale = m.scale;
    sp.w = (WIDTH * sp.scale) as i32;
    sp.h = (TOP_H * sp.scale) as i32;
    sp.x = (m.rect.left + m.rect.right) / 2 - sp.w / 2;
    sp.y = m.rect.top + (m.h() as f32 * 0.2) as i32;
    sp.opened = crate::anim::now();
    sp.closing = None;
    sp.was_active = false;
    sp.visible = true;
    sp.edit.focused = true;
    sp.edit.select_all();
    rebuild(app, false);
    let sp = &app.spotlight;
    unsafe {
        let _ = SetWindowPos(sp.hwnd, Some(HWND_TOPMOST), sp.x, sp.y, sp.w, sp.h, SWP_SHOWWINDOW);
    }
    sys::force_foreground(sp.hwnd);
    app.request_frame();
}

pub fn close(app: &mut App) {
    if app.spotlight.closing.is_none() && app.spotlight.visible {
        app.spotlight.closing = Some(crate::anim::now());
        app.request_frame();
    }
}

fn finish_close(app: &mut App) {
    let sp = &mut app.spotlight;
    sp.visible = false;
    sp.closing = None;
    win::hide(sp.hwnd);
    if let Some(g) = sp.glass.as_mut() {
        g.hide();
    }
    sp.glass = None;
    sp.surf.free();
    sp.icons.clear();
    sp.results.clear();
    sp.files.clear();
    app.catalog.trim();
    crate::text::trim_cache();
}

/// Кадр: появление/исчезновение, мигание курсора, отложенный поиск файлов.
pub fn tick(app: &mut App) -> bool {
    if !app.spotlight.visible {
        return false;
    }
    let now = crate::anim::now();
    if let Some(t) = app.spotlight.closing {
        if now - t >= 0.12 {
            finish_close(app);
            return false;
        }
    }
    if let Some(due) = app.spotlight.file_due {
        if now >= due {
            app.spotlight.file_due = None;
            file_search(app);
        }
    }
    paint(app);
    true
}

fn activate(app: &mut App, alt: bool) {
    let sp = &app.spotlight;
    let Some(r) = sp.results.get(sp.selected).cloned() else { return };
    let act = if alt { r.alt.clone().unwrap_or(r.act.clone()) } else { r.act.clone() };
    close(app);
    run_act(app, act);
}

fn run_act(app: &mut App, act: Act) {
    match act {
        Act::Copy(v) => crate::lineedit::set_clipboard(&v),
        Act::Run(e) => crate::app::defer_ms(60, move || crate::catalog::run_entry(&e)),
        Act::System(i) => crate::app::defer_ms(60, move || {
            crate::app::with(|a| run_system(a, i));
        }),
        Act::Open(p) => crate::app::defer_ms(60, move || {
            shell::launch(&p, "", None);
        }),
        Act::Reveal(p) => shell::show_in_explorer(&p),
        Act::Web(t) => {
            let enc: String = t.bytes().map(|b| if b.is_ascii_alphanumeric() { (b as char).to_string() } else { format!("%{b:02X}") }).collect();
            shell::launch(&format!("https://www.google.com/search?q={enc}"), "", None);
        }
    }
    let _ = app;
}

fn move_selection(app: &mut App, d: i32) {
    let sp = &mut app.spotlight;
    if sp.results.is_empty() {
        return;
    }
    sp.selected = (sp.selected as i32 + d).rem_euclid(sp.results.len() as i32) as usize;
    if sp.selected < sp.scroll {
        sp.scroll = sp.selected;
    } else if sp.selected >= sp.scroll + MAX_ROWS {
        sp.scroll = sp.selected + 1 - MAX_ROWS;
    }
    relayout(app);
}

fn accept_completion(app: &mut App) -> bool {
    let Some((_, full)) = app.spotlight.completion() else { return false };
    let keep = app.spotlight.results[app.spotlight.selected].title.clone();
    app.spotlight.edit.set(&full);
    text_changed(app);
    if let Some(i) = app.spotlight.results.iter().position(|r| r.title == keep) {
        app.spotlight.selected = i;
    }
    paint(app);
    true
}

fn text_changed(app: &mut App) {
    let sp = &mut app.spotlight;
    sp.seq += 1;
    sp.files.clear();
    sp.file_due = (sp.edit.text.trim().chars().count() >= 2).then(|| crate::anim::now() + 0.14);
    rebuild(app, false);
}

pub fn on_message(app: &mut App, hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> Option<LRESULT> {
    if app.spotlight.hwnd != hwnd || hwnd.is_invalid() {
        return None;
    }
    let s = app.spotlight.scale;
    let (x, y) = win::lparam_xy(lp);
    let row_at = |sp: &Spotlight, y: i32| -> Option<usize> {
        let (rows, _) = sp.rows_layout();
        rows.iter().find(|(ry, head, _)| !head && (*ry * s) <= y as f32 && (y as f32) < (*ry + ROW_H) * s).map(|r| r.2)
    };
    match msg {
        WM_CHAR => {
            let c = char::from_u32(wp.0 as u32).unwrap_or('\0');
            match app.spotlight.edit.on_char(c) {
                Edit::Changed => text_changed(app),
                Edit::Enter => {
                    let alt = sys::key_down(0x11) || sys::key_down(0x12);
                    if app.spotlight.picked.len() > 1 {
                        let acts: Vec<Act> = app.spotlight.picked.iter().filter_map(|i| app.spotlight.results.get(*i)).filter(|r| !r.calc && r.section != "Интернет").map(|r| r.act.clone()).collect();
                        app.spotlight.picked.clear();
                        close(app);
                        for (k, a) in acts.into_iter().enumerate() {
                            crate::app::defer_ms(60 + 120 * k as u32, move || {
                                crate::app::with(|app| run_act(app, a));
                            });
                        }
                    } else {
                        activate(app, alt);
                    }
                }
                Edit::Escape => {
                    if !app.spotlight.picked.is_empty() {
                        app.spotlight.picked.clear();
                        paint(app);
                    } else if !app.spotlight.edit.text.is_empty() {
                        app.spotlight.edit.set("");
                        text_changed(app);
                    } else {
                        close(app);
                    }
                }
                Edit::Tab => {
                    accept_completion(app);
                }
                _ => paint(app),
            }
        }
        WM_KEYDOWN => match wp.0 as u32 {
            0x26 => move_selection(app, -1),
            0x28 => move_selection(app, 1),
            0x27 if app.spotlight.edit.cursor == app.spotlight.edit.text.chars().count() && accept_completion(app) => {}
            vk => {
                if let Edit::Changed = app.spotlight.edit.on_key(vk) {
                    text_changed(app);
                } else {
                    paint(app);
                }
            }
        },
        WM_MOUSEMOVE => {
            if let Some(i) = row_at(&app.spotlight, y) {
                if i != app.spotlight.selected {
                    app.spotlight.selected = i;
                    paint(app);
                }
            }
        }
        WM_LBUTTONDOWN => {
            if (y as f32) < TOP_H * s {
                let r = R::new(54.0 * s, 12.0 * s, (WIDTH - 70.0) * s, 38.0 * s);
                app.spotlight.edit.click(x as f32, r, Font::Light, 22.7 * s, false);
                paint(app);
            }
        }
        WM_LBUTTONUP | WM_RBUTTONUP => {
            if let Some(i) = row_at(&app.spotlight, y) {
                if msg == WM_LBUTTONUP && sys::key_down(0x11) {
                    // Ctrl+клик — добавить/убрать строку (Enter откроет все)
                    let sp = &mut app.spotlight;
                    if !sp.picked.remove(&i) {
                        sp.picked.insert(i);
                        sp.picked.insert(sp.selected);
                    }
                    paint(app);
                } else {
                    app.spotlight.selected = i;
                    app.spotlight.picked.clear();
                    activate(app, msg == WM_RBUTTONUP);
                }
            }
        }
        WM_MOUSEWHEEL => {
            let d = if ((wp.0 >> 16) as i16) > 0 { -1 } else { 1 };
            let sp = &mut app.spotlight;
            let max = sp.results.len().saturating_sub(MAX_ROWS);
            sp.scroll = (sp.scroll as i32 + d).clamp(0, max as i32) as usize;
            relayout(app);
        }
        WM_ACTIVATE => {
            if wp.0 & 0xFFFF != 0 {
                app.spotlight.was_active = true;
            } else if app.spotlight.was_active && crate::anim::now() - app.spotlight.opened > 0.4 {
                close(app);
            }
        }
        _ => return None,
    }
    app.request_frame();
    Some(LRESULT(0))
}

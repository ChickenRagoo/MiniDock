//! Каталог приложений «Пуска» для Launchpad и Spotlight: индексируется в фоновом потоке,
//! иконки кэшируются на диск (%APPDATA%\MiniDock\iconcache-rs) и в памяти держатся, только пока нужны.

use std::collections::HashMap;
use std::rc::Rc;
use std::sync::Arc;

use crate::gfx::Image;

pub const ICON_PX: u32 = 128;

#[derive(Clone, Debug)]
pub struct Entry {
    pub name: String,
    /// имя разбора shell:AppsFolder (AUMID или путь с известной папкой) — ключ для настроек lp_hidden/lp_folders
    pub parse: String,
    /// для плиток «Пуска»: (колонка, строка, ширина, высота)
    pub tile: Option<(u32, u32, u32, u32)>,
}

pub struct Catalog {
    pub entries: Vec<Entry>,
    pub groups: Vec<Vec<Entry>>,
    pub loading: bool,
    pub loaded: bool,
    icons: HashMap<String, Option<Rc<Image>>>,
}

fn junk(name: &str) -> bool {
    let n = name.to_lowercase();
    const BAD: [&str; 22] = [
        "uninstall", "деинстал", "удален", "удалить", "readme", "read me", "release notes", "help", "справк", "documentation", "документац", "website",
        "web site", "веб-сайт", "сайт", "license", "лиценз", "support", "поддержк", "manual", "руководств", "check for updates",
    ];
    BAD.iter().any(|b| n.contains(b)) || n.starts_with("about ") || n.contains("configur") || n.contains("tap virtual") || n.ends_with(".url")
}

pub fn cache_dir() -> String {
    format!("{}\\iconcache-rs", crate::config::cfg_dir())
}

fn hash(s: &str) -> u64 {
    // FNV-1a — для имён файлов кэша
    let mut h: u64 = 0xcbf29ce484222325;
    for b in s.to_lowercase().bytes() {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}

fn cache_path(key: &str) -> String {
    format!("{}\\{:016x}.bgra", cache_dir(), hash(key))
}

fn read_cached(key: &str) -> Option<Image> {
    let data = std::fs::read(cache_path(key)).ok()?;
    if data.len() < 8 {
        return None;
    }
    let w = u32::from_le_bytes(data[0..4].try_into().ok()?);
    let h = u32::from_le_bytes(data[4..8].try_into().ok()?);
    if (w * h * 4) as usize != data.len() - 8 || w == 0 || h == 0 || w > 512 || h > 512 {
        return None;
    }
    Image::from_bgra_premul(w, h, data[8..].to_vec())
}

fn write_cached(key: &str, img: &Image) {
    let _ = std::fs::create_dir_all(cache_dir());
    let mut data = Vec::with_capacity(8 + img.bytes());
    data.extend_from_slice(&img.w.to_le_bytes());
    data.extend_from_slice(&img.h.to_le_bytes());
    data.extend_from_slice(img.pm.data());
    let _ = std::fs::write(cache_path(key), data);
}

/// Иконка записи из кэша на диске или извлечённая из системы (и сохранённая в кэш).
pub fn load_icon(e: &Entry) -> Option<Image> {
    if let Some(i) = read_cached(&e.parse) {
        return Some(i);
    }
    let img = if std::path::Path::new(&e.parse).is_file() {
        crate::win::shell::file_icon(&e.parse, ICON_PX as i32)
    } else {
        crate::win::shell::start_app_icon(&e.parse, ICON_PX as i32)
    }?;
    let img = if img.w > ICON_PX || img.h > ICON_PX { img.scaled(ICON_PX, ICON_PX) } else { img };
    write_cached(&e.parse, &img);
    Some(img)
}

impl Catalog {
    pub fn new() -> Catalog {
        Catalog { entries: Vec::new(), groups: Vec::new(), loading: false, loaded: false, icons: HashMap::new() }
    }

    /// Перечитать список в фоне (результат придёт в главный поток).
    pub fn load(&mut self) {
        if self.loading {
            return;
        }
        self.loading = true;
        crate::app::spawn_then(
            || {
                crate::win::shell::com_init();
                let mut seen = std::collections::HashSet::new();
                let mut out = Vec::new();
                for a in crate::win::shell::enum_start_apps() {
                    let key = a.name.to_lowercase();
                    if junk(&a.name) || !seen.insert(key) {
                        continue;
                    }
                    out.push(Entry { name: a.name, parse: a.parse, tile: None });
                }
                out.sort_by_key(|e| e.name.to_lowercase());
                out
            },
            |a, entries: Vec<Entry>| {
                a.catalog.entries = entries.clone();
                a.catalog.loaded = true;
                a.catalog.icons.clear();
                crate::launchpad::on_loaded(a);
                // плитки «Пуска» (медленно: PowerShell) и иконки в кэш на диске — вторым этапом
                crate::app::spawn_then(
                    move || {
                        crate::win::shell::com_init();
                        let groups = start_layout(&entries);
                        for e in entries.iter().chain(groups.iter().flatten()) {
                            if read_cached(&e.parse).is_none() {
                                let _ = load_icon(e);
                            }
                        }
                        groups
                    },
                    |a, groups| {
                        a.catalog.groups = groups;
                        a.catalog.loading = false;
                        crate::launchpad::on_loaded(a);
                    },
                );
            },
        );
    }

    /// Иконка записи в памяти (из кэша на диске).
    pub fn icon(&mut self, e: &Entry) -> Option<Rc<Image>> {
        if let Some(i) = self.icons.get(&e.parse) {
            return i.clone();
        }
        let img = read_cached(&e.parse).or_else(|| load_icon(e)).map(Rc::new);
        self.icons.insert(e.parse.clone(), img.clone());
        img
    }

    /// Окна каталога закрыты — иконки из памяти выгружаем.
    pub fn trim(&mut self) {
        self.icons.clear();
        self.icons.shrink_to_fit();
    }

    pub fn memory(&self) -> usize {
        self.icons.values().flatten().map(|i| i.bytes()).sum()
    }
}

/// Плитки «Пуска» Windows (Export-StartLayout): группы с расположением, как в «Пуске».
fn start_layout(known: &[Entry]) -> Vec<Vec<Entry>> {
    use std::os::windows::process::CommandExt;
    let out_file = format!("{}\\startlayout.xml", crate::config::cfg_dir());
    let _ = std::process::Command::new("powershell")
        .args(["-NoProfile", "-NonInteractive", "-Command", &format!("Export-StartLayout -Path '{out_file}'")])
        .creation_flags(0x08000000)
        .output();
    let Ok(xml) = std::fs::read_to_string(&out_file) else { return Vec::new() };
    let mut groups = Vec::new();
    for g in xml.split("<start:Group").skip(1) {
        let g = g.split("</start:Group>").next().unwrap_or("");
        let mut tiles = Vec::new();
        for t in g.split('<').skip(1) {
            let tag = t.split_whitespace().next().unwrap_or("");
            if !(tag.ends_with(":Tile") || tag.ends_with(":DesktopApplicationTile")) {
                continue;
            }
            let attr = |name: &str| -> Option<String> {
                let i = t.find(&format!("{name}=\""))? + name.len() + 2;
                let j = t[i..].find('"')? + i;
                Some(t[i..j].replace("&amp;", "&"))
            };
            let (w, h) = attr("Size").and_then(|s| s.split_once('x').map(|(a, b)| (a.parse().unwrap_or(1), b.parse().unwrap_or(1)))).unwrap_or((1, 1));
            let col: u32 = attr("Column").and_then(|v| v.parse().ok()).unwrap_or(0);
            let row: u32 = attr("Row").and_then(|v| v.parse().ok()).unwrap_or(0);
            let entry = if let Some(lnk) = attr("DesktopApplicationLinkPath") {
                let path = crate::win::shell::expand_env(&lnk);
                if !std::path::Path::new(&path).exists() {
                    continue;
                }
                let mut name = crate::win::sys::file_stem(&path);
                for suf in [" — ярлык", " - ярлык", " - Shortcut", " — Shortcut", ".exe"] {
                    if let Some(n) = name.strip_suffix(suf) {
                        name = n.to_string();
                    }
                }
                Entry { name, parse: path, tile: Some((col, row, w, h)) }
            } else if let Some(id) = attr("AppUserModelID").or_else(|| attr("DesktopApplicationID")) {
                let name = known.iter().find(|k| k.parse.eq_ignore_ascii_case(&id)).map(|k| k.name.clone()).unwrap_or_else(|| id.rsplit('!').next().unwrap_or(&id).to_string());
                Entry { name, parse: id, tile: Some((col, row, w, h)) }
            } else {
                continue;
            };
            tiles.push(entry);
        }
        if !tiles.is_empty() {
            let min_col = tiles.iter().filter_map(|t| t.tile.map(|x| x.0)).min().unwrap_or(0);
            for t in &mut tiles {
                if let Some(x) = t.tile.as_mut() {
                    x.0 -= min_col;
                }
            }
            groups.push(tiles);
        }
    }
    groups
}

/// Путь к exe записи (для анимации запуска из иконки), если его можно узнать.
pub fn entry_exe(e: &Entry) -> Option<String> {
    let known = [
        ("{6D809377-6AF0-444B-8957-A3773F02200E}", std::env::var("ProgramW6432").unwrap_or_else(|_| "C:\\Program Files".into())),
        ("{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}", std::env::var("ProgramFiles(x86)").unwrap_or_else(|_| "C:\\Program Files (x86)".into())),
        ("{F38BF404-1D43-42F2-9305-67DE0B28FC23}", std::env::var("SystemRoot").unwrap_or_else(|_| "C:\\Windows".into())),
        ("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}", format!("{}\\System32", std::env::var("SystemRoot").unwrap_or_else(|_| "C:\\Windows".into()))),
    ];
    let mut p = e.parse.clone();
    for (g, dir) in &known {
        if p.to_uppercase().starts_with(&format!("{g}\\")) {
            p = format!("{dir}{}", &p[g.len()..]);
        }
    }
    let l = p.to_lowercase();
    if l.ends_with(".exe") && std::path::Path::new(&p).is_file() {
        return Some(l);
    }
    if l.ends_with(".lnk") && std::path::Path::new(&p).is_file() {
        return crate::win::shell::resolve_lnk(&p).map(|t| t.0.to_lowercase()).filter(|t| !t.is_empty());
    }
    None
}

/// Запустить запись: файл — напрямую, иначе — как «Пуск»: по ID приложения.
pub fn run_entry(e: &Entry) {
    if std::path::Path::new(&e.parse).is_file() {
        let dir = crate::win::sys::parent_dir(&e.parse);
        if crate::win::shell::launch(&e.parse, "", Some(&dir)) {
            return;
        }
    }
    crate::win::shell::launch_start_app(&e.parse);
}

pub type Shared = Arc<Vec<Entry>>;

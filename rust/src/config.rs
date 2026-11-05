//! Настройки: тот же %APPDATA%\MiniDock\config.json, что у Python-версии (ключи и значения совместимы).
//! Незнакомые ключи сохраняются как есть.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

pub const CFG_VERSION: u32 = 7;

pub fn cfg_dir() -> String {
    format!("{}\\MiniDock", std::env::var("APPDATA").unwrap_or_else(|_| ".".into()))
}

pub fn cfg_file() -> String {
    format!("{}\\config.json", cfg_dir())
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Config {
    pub cfg_version: u32,
    // основное
    pub replace_taskbar: bool,
    pub win_key_launchpad: bool,
    pub custom_switcher: bool,
    pub switcher_titles: bool,
    pub switcher_window_title: bool,
    pub switcher_icon: u32,
    pub switcher_opacity: u32,
    pub switcher_radius: u32,
    pub switcher_delay: u32,
    pub switcher_position: String,
    pub switcher_minimized: bool,
    pub switcher_genie: bool,
    pub switcher_style: String,
    pub switcher_hover: bool,
    pub switcher_previews: bool,
    pub switcher_preview: u32,
    pub bar_widgets: Vec<String>,
    pub media_bg: String,
    pub media_color: String,
    pub switcher_per_row: u32,
    pub dock_previews: bool,
    pub dock_preview: u32,
    pub dock_preview_pad: u32,
    pub dock_preview_delay: f32,
    pub spotlight: bool,
    // док
    pub position: String,
    pub icon_size: f32,
    pub magnify: f32,
    pub spacing: f32,
    pub margin: f32,
    pub autohide: bool,
    pub autohide_delay: f32,
    pub show_running: bool,
    pub show_trash: bool,
    pub show_launchpad: bool,
    pub show_labels: bool,
    pub indicator: String,
    pub bounce: bool,
    /// док выезжает и поверх полноэкранных программ (игр, видео, F11)
    pub dock_in_fullscreen: bool,
    // внешний вид дока
    pub dock_color: String,
    pub dock_opacity: u32,
    pub dock_blur: bool,
    pub glass_live_parts: Vec<String>,
    pub dock_peek: bool,
    pub backgrounds: Map<String, Value>,
    pub glass_live: bool,
    pub glass_radius: f32,
    pub dock_radius: f32,
    pub dock_border: bool,
    pub dock_border_light: bool,
    pub dock_light_strength: f32,
    // анимации окон
    pub animations: bool,
    pub minimize_effect: String,
    pub anim_speed: f32,
    pub open_animation: bool,
    pub close_animation: bool,
    pub auto_update: bool,
    pub window_shadows: bool,
    pub shadows_orig: Option<bool>,
    // верхняя панель
    pub menubar: bool,
    pub bar_all_screens: bool,
    pub dock_all_screens: bool,
    pub bar_height: f32,
    pub bar_opacity: u32,
    pub bar_blur: bool,
    pub clock_seconds: bool,
    pub clock_date: bool,
    pub show_bluetooth: bool,
    pub show_battery: bool,
    /// большие часы посреди экрана, пока зажато сочетание
    pub big_clock: bool,
    /// сочетание: код клавиши | модификаторы << 16 (1 Ctrl, 2 Alt, 4 Shift, 8 Win)
    pub big_clock_hotkey: u32,
    // Launchpad
    pub lp_columns: u32,
    pub lp_rows: u32,
    pub lp_icon: f32,
    pub lp_mode: String,
    pub lp_hidden: Vec<String>,
    pub lp_folders: Vec<Value>,
    // служебное
    pub taskbar_orig_state: Option<u32>,
    pub pinned: Option<Vec<Value>>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

impl Default for Config {
    fn default() -> Self {
        let s = |v: &str| v.to_string();
        Config {
            cfg_version: CFG_VERSION,
            replace_taskbar: true,
            win_key_launchpad: true,
            custom_switcher: true,
            switcher_titles: true,
            switcher_window_title: true,
            switcher_icon: 96,
            switcher_opacity: 150,
            switcher_radius: 22,
            switcher_delay: 110,
            switcher_position: s("center"),
            switcher_minimized: true,
            switcher_genie: true,
            switcher_style: s("mac"),
            switcher_hover: true,
            switcher_previews: true,
            switcher_preview: 240,
            bar_widgets: vec![s("system")],
            media_bg: s("blur"),
            media_color: s("#26262e"),
            switcher_per_row: 0,
            dock_previews: true,
            dock_preview: 220,
            dock_preview_pad: 8,
            dock_preview_delay: 0.45,
            spotlight: true,
            position: s("bottom"),
            icon_size: 40.0,
            magnify: 1.6,
            spacing: 0.12,
            margin: 12.0,
            autohide: true,
            autohide_delay: 0.45,
            show_running: true,
            show_trash: true,
            show_launchpad: false,
            show_labels: true,
            indicator: s("dot"),
            bounce: true,
            dock_in_fullscreen: false,
            dock_color: s("#202026"),
            dock_opacity: 95,
            dock_blur: true,
            glass_live_parts: ["dock", "bar", "switcher", "previews", "spotlight", "folders", "menus"].iter().map(|x| s(x)).collect(),
            dock_peek: true,
            backgrounds: Map::new(),
            glass_live: true,
            glass_radius: 18.0,
            dock_radius: 0.3,
            dock_border: true,
            dock_border_light: true,
            dock_light_strength: 0.4,
            animations: true,
            minimize_effect: s("genie"),
            anim_speed: 1.0,
            open_animation: true,
            close_animation: true,
            auto_update: true,
            window_shadows: false,
            shadows_orig: None,
            menubar: true,
            bar_all_screens: false,
            dock_all_screens: false,
            bar_height: 30.0,
            bar_opacity: 255,
            bar_blur: false,
            clock_seconds: true,
            clock_date: true,
            show_bluetooth: true,
            show_battery: true,
            big_clock: true,
            big_clock_hotkey: 0xC0 | (crate::win::hooks::MOD_ALT << 16), // Alt+`
            lp_columns: 7,
            lp_rows: 5,
            lp_icon: 72.0,
            lp_mode: s("all"),
            lp_hidden: Vec::new(),
            lp_folders: Vec::new(),
            taskbar_orig_state: None,
            pinned: None,
            extra: Map::new(),
        }
    }
}

/// Элемент дока в настройках: путь программы или папка {"folder": имя, "items": [пути]}.
#[derive(Clone, Debug)]
pub enum PinEntry {
    App(String),
    Folder(String, Vec<String>),
}

impl PinEntry {
    pub fn from_value(v: &Value) -> Option<PinEntry> {
        match v {
            Value::String(s) => Some(PinEntry::App(s.clone())),
            Value::Object(o) if o.contains_key("folder") || o.contains_key("items") => {
                let name = o.get("folder").and_then(|v| v.as_str()).unwrap_or("Папка").to_string();
                let items = o
                    .get("items")
                    .and_then(|v| v.as_array())
                    .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
                    .unwrap_or_default();
                Some(PinEntry::Folder(name, items))
            }
            _ => None,
        }
    }

    pub fn to_value(&self) -> Value {
        match self {
            PinEntry::App(p) => Value::String(p.clone()),
            PinEntry::Folder(n, items) => {
                let mut o = Map::new();
                o.insert("folder".into(), Value::String(n.clone()));
                o.insert("items".into(), Value::Array(items.iter().map(|s| Value::String(s.clone())).collect()));
                Value::Object(o)
            }
        }
    }
}

impl Config {
    pub fn load() -> Config {
        let text = std::fs::read_to_string(cfg_file()).unwrap_or_default();
        let mut saved: Map<String, Value> = serde_json::from_str(&text).unwrap_or_default();
        let ver = saved.get("cfg_version").and_then(|v| v.as_u64()).unwrap_or(1) as u32;
        if !saved.is_empty() {
            if ver < 2 {
                saved.remove("hide_taskbar");
                let s = saved.get("icon_size").and_then(|v| v.as_f64()).unwrap_or(40.0).min(40.0);
                saved.insert("icon_size".into(), s.into());
            }
            if ver < 5 {
                let h = saved.get("bar_height").and_then(|v| v.as_f64()).unwrap_or(30.0).max(30.0);
                saved.insert("bar_height".into(), h.into());
                saved.insert("bar_opacity".into(), 255.into());
                saved.insert("bar_blur".into(), false.into());
            }
            if ver < 6 {
                saved.insert("show_launchpad".into(), false.into());
            }
            if ver < 7 {
                saved.insert("switcher_hover".into(), true.into());
            }
        }
        saved.insert("cfg_version".into(), CFG_VERSION.into());
        // по ключу: неправильный тип одного значения не сбрасывает все настройки
        let mut cfg = Config::default();
        let defaults = serde_json::to_value(&cfg).unwrap_or(Value::Null);
        let mut merged = defaults.as_object().cloned().unwrap_or_default();
        for (k, v) in saved {
            let ok = match merged.get(&k) {
                Some(def) => {
                    let mut probe = merged.clone();
                    probe.insert(k.clone(), v.clone());
                    serde_json::from_value::<Config>(Value::Object(probe)).is_ok() || def.is_null()
                }
                None => true,
            };
            if ok {
                merged.insert(k, v);
            }
        }
        if let Ok(c) = serde_json::from_value::<Config>(Value::Object(merged)) {
            cfg = c;
        }
        // виджеты в доке были в 1.7.0–1.7.1 и убраны
        if let Some(p) = cfg.pinned.as_mut() {
            p.retain(|e| !(e.is_object() && e.get("widget").is_some()));
        }
        cfg
    }

    pub fn save(&self) {
        let _ = std::fs::create_dir_all(cfg_dir());
        let tmp = format!("{}.tmp", cfg_file());
        if let Ok(text) = serde_json::to_string_pretty(self) {
            if std::fs::write(&tmp, text).is_ok() {
                let _ = std::fs::rename(&tmp, cfg_file());
            }
        }
    }

    pub fn pins(&self) -> Vec<PinEntry> {
        self.pinned.as_ref().map(|v| v.iter().filter_map(PinEntry::from_value).collect()).unwrap_or_default()
    }

    pub fn set_pins(&mut self, pins: &[PinEntry]) {
        self.pinned = Some(pins.iter().map(|p| p.to_value()).collect());
    }

    /// Своя картинка фона части интерфейса: (путь, непрозрачность).
    pub fn background(&self, part: &str) -> Option<(String, f32)> {
        let spec = self.backgrounds.get(part)?.as_object()?;
        let path = spec.get("path")?.as_str()?.to_string();
        if path.is_empty() {
            return None;
        }
        let op = spec.get("opacity").and_then(|v| v.as_f64()).unwrap_or(1.0) as f32;
        Some((path, op))
    }

    pub fn live_for(&self, part: &str) -> bool {
        self.glass_live && self.glass_live_parts.iter().any(|p| p == part)
    }

    pub fn anim_dur(&self, base: f32) -> f32 {
        base / self.anim_speed.max(0.1)
    }

    pub fn flow(&self) -> &'static str {
        match self.position.as_str() {
            "left" => "left",
            "right" => "right",
            _ => "down",
        }
    }
}

/// Закреплённые по умолчанию (первый запуск): Проводник и популярные программы из «Пуска».
pub fn default_pinned() -> Vec<Value> {
    let mut out = vec![Value::String(format!("{}\\explorer.exe", std::env::var("SystemRoot").unwrap_or_else(|_| "C:\\Windows".into())))];
    let wanted = ["Google Chrome", "Microsoft Edge", "Firefox", "Telegram", "Discord", "Steam", "Visual Studio Code", "Spotify"];
    let roots = [
        format!("{}\\Microsoft\\Windows\\Start Menu\\Programs", std::env::var("APPDATA").unwrap_or_default()),
        format!("{}\\Microsoft\\Windows\\Start Menu\\Programs", std::env::var("ProgramData").unwrap_or_else(|_| "C:\\ProgramData".into())),
    ];
    let mut found: Vec<(String, String)> = Vec::new();
    for root in &roots {
        walk(std::path::Path::new(root), &mut |p| {
            if p.extension().is_some_and(|e| e.eq_ignore_ascii_case("lnk")) {
                let stem = p.file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
                if wanted.contains(&stem.as_str()) && !found.iter().any(|(s, _)| *s == stem) {
                    found.push((stem, p.to_string_lossy().to_string()));
                }
            }
        });
    }
    for w in wanted {
        if let Some((_, p)) = found.iter().find(|(s, _)| s == w) {
            out.push(Value::String(p.clone()));
        }
    }
    out
}

pub fn walk(dir: &std::path::Path, f: &mut dyn FnMut(&std::path::Path)) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let p = e.path();
        if p.is_dir() {
            walk(&p, f);
        } else {
            f(&p);
        }
    }
}

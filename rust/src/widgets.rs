//! Виджеты верхней панели: короткая надпись на панели, по клику — окно с подробностями.
//! Один фоновый поток опрашивает включённые виджеты; данные приходят в главный поток.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::{Arc, Condvar, Mutex};

use crate::panel::{size_str, Row};

pub const BUILTIN: [(&str, &str, &str); 3] = [
    ("system", "Система — процессор и память", "\u{E950}"),
    ("network", "Сеть — скорость загрузки и отдачи", "\u{E896}"),
    ("media", "Плеер — что сейчас играет", "\u{E8D6}"),
];

pub fn name_of(id: &str) -> String {
    BUILTIN.iter().find(|w| w.0 == id).map(|w| w.1.to_string()).unwrap_or_else(|| id.to_string())
}

pub fn icon_of(id: &str) -> &'static str {
    BUILTIN.iter().find(|w| w.0 == id).map(|w| w.2).unwrap_or("")
}

#[derive(Clone, Debug)]
pub enum Data {
    System { cpu: f32, mem: f32, used: u64, total: u64, disks: Vec<(String, f32, u64, u64)>, procs: Vec<(f32, String, u64)>, uptime: String },
    Network { down: f64, up: f64, recv: u64, sent: u64, addrs: Vec<(String, String)> },
    Media { sessions: Vec<crate::media::Session> },
}

struct Shared {
    enabled: Vec<String>,
    detailed: HashSet<String>,
    data: HashMap<String, Data>,
    wake: bool,
}

pub struct Host {
    shared: Arc<(Mutex<Shared>, Condvar)>,
    started: bool,
}

static CHANGED: AtomicBool = AtomicBool::new(false);

impl Host {
    pub fn new() -> Host {
        Host { shared: Arc::new((Mutex::new(Shared { enabled: Vec::new(), detailed: HashSet::new(), data: HashMap::new(), wake: false }), Condvar::new())), started: false }
    }

    pub fn set_enabled(&mut self, ids: &[String]) {
        let ids: Vec<String> = ids.iter().filter(|i| BUILTIN.iter().any(|b| b.0 == i.as_str())).cloned().collect();
        {
            let (m, c) = &*self.shared;
            let mut s = m.lock().unwrap();
            if s.enabled == ids {
                return;
            }
            s.enabled = ids.clone();
            s.data.retain(|k, _| ids.contains(k));
            s.wake = true;
            c.notify_one();
        }
        if !ids.is_empty() && !self.started {
            self.started = true;
            let sh = self.shared.clone();
            std::thread::Builder::new().name("MiniDockWidgets".into()).stack_size(256 * 1024).spawn(move || run(sh)).ok();
        }
    }

    pub fn enabled(&self) -> Vec<String> {
        self.shared.0.lock().unwrap().enabled.clone()
    }

    pub fn set_detailed(&self, id: &str, on: bool) {
        let (m, c) = &*self.shared;
        let mut s = m.lock().unwrap();
        if on {
            s.detailed.insert(id.to_string());
            s.wake = true;
            c.notify_one();
        } else {
            s.detailed.remove(id);
        }
    }

    pub fn data(&self, id: &str) -> Option<Data> {
        self.shared.0.lock().unwrap().data.get(id).cloned()
    }

    /// Пришли новые данные с прошлого вызова?
    pub fn take_changed() -> bool {
        CHANGED.swap(false, Relaxed)
    }

    pub fn bar_text(&self, id: &str) -> String {
        match self.data(id) {
            None => "…".into(),
            Some(Data::System { cpu, mem, .. }) => format!("ЦП {cpu:.0}%   ОЗУ {mem:.0}%"),
            Some(Data::Network { down, .. }) => format!("{}/с", size_str(down)),
            Some(Data::Media { sessions }) => match current(&sessions) {
                None => "Ничего не играет".into(),
                Some(s) => {
                    let line = if s.artist.is_empty() { s.title.clone() } else { format!("{} — {}", s.title, s.artist) };
                    if line.chars().count() <= 42 {
                        line
                    } else {
                        line.chars().take(41).collect::<String>() + "…"
                    }
                }
            },
        }
    }

    /// Строки окна виджета. Кнопки: 1 — диспетчер задач, 2 — параметры сети, 10/11/12 — плеер.
    pub fn panel_rows(&self, id: &str) -> Vec<Row> {
        let Some(d) = self.data(id) else { return vec![Row::Title(name_of(id)), Row::Text("Загружаем…".into())] };
        match d {
            Data::System { cpu, mem, used, total, disks, procs, uptime } => {
                let mut rows = vec![
                    Row::Title("Система".into()),
                    Row::Bar("Процессор".into(), cpu / 100.0, format!("{cpu:.0}%")),
                    Row::Bar("Память".into(), mem / 100.0, format!("{} из {}", size_str(used as f64), size_str(total as f64))),
                ];
                for (n, f, u, t) in disks {
                    rows.push(Row::Bar(format!("Диск {n}"), f, format!("{} из {}", size_str(u as f64), size_str(t as f64))));
                }
                if !procs.is_empty() {
                    rows.push(Row::Sep);
                    rows.push(Row::Text("Больше всего нагружают процессор".into()));
                    for (c, n, m) in procs {
                        rows.push(Row::Value(n, format!("{c:.0}%  ·  {}", size_str(m as f64))));
                    }
                }
                if !uptime.is_empty() {
                    rows.push(Row::Sep);
                    rows.push(Row::Value("Без перезагрузки".into(), uptime));
                }
                rows.push(Row::Button(1, "Диспетчер задач…".into(), false));
                rows
            }
            Data::Network { down, up, recv, sent, addrs } => {
                let mut rows = vec![
                    Row::Title("Сеть".into()),
                    Row::Value("Загрузка".into(), format!("{}/с", size_str(down))),
                    Row::Value("Отдача".into(), format!("{}/с", size_str(up))),
                    Row::Sep,
                    Row::Value("Получено с включения".into(), size_str(recv as f64)),
                    Row::Value("Отправлено".into(), size_str(sent as f64)),
                ];
                if !addrs.is_empty() {
                    rows.push(Row::Sep);
                    rows.push(Row::Text("Адреса".into()));
                    for (n, a) in addrs {
                        rows.push(Row::Value(n, a));
                    }
                }
                rows.push(Row::Button(2, "Параметры сети…".into(), false));
                rows
            }
            Data::Media { sessions } => match current(&sessions) {
                None => vec![Row::Title("Плеер".into()), Row::Text("Сейчас ни одна программа не воспроизводит звук".into())],
                Some(s) => vec![
                    Row::Title(if s.title.is_empty() { "Без названия".into() } else { s.title.clone() }),
                    Row::Text(s.artist.clone()),
                    Row::Sep,
                    Row::Button(10, if s.playing { "Пауза".into() } else { "Играть".into() }, false),
                    Row::Button(11, "Следующий трек".into(), false),
                    Row::Button(12, "Предыдущий трек".into(), false),
                ],
            },
        }
    }

    pub fn current_media(&self) -> Option<crate::media::Session> {
        match self.data("media") {
            Some(Data::Media { sessions }) => current(&sessions).cloned(),
            _ => None,
        }
    }
}

pub fn current(ss: &[crate::media::Session]) -> Option<&crate::media::Session> {
    ss.iter().find(|s| s.playing).or_else(|| ss.first())
}

fn run(sh: Arc<(Mutex<Shared>, Condvar)>) {
    let mut cpu = crate::sysinfo::Cpu::default();
    cpu.percent();
    let mut net_last: Option<(std::time::Instant, (u64, u64))> = None;
    let mut due: HashMap<String, std::time::Instant> = HashMap::new();
    loop {
        let (enabled, detailed) = {
            let s = sh.0.lock().unwrap();
            (s.enabled.clone(), s.detailed.clone())
        };
        let now = std::time::Instant::now();
        let mut nearest = std::time::Duration::from_secs(5);
        for id in &enabled {
            let next = due.get(id).copied().unwrap_or(now);
            if next <= now {
                let det = detailed.contains(id);
                let (data, every) = match id.as_str() {
                    "system" => {
                        let (mem, used, total) = crate::sysinfo::memory();
                        let c = cpu.percent();
                        let (disks, procs, uptime) = if det {
                            (crate::sysinfo::disks(), crate::sysinfo::top_processes(500, 5), crate::sysinfo::uptime())
                        } else {
                            (Vec::new(), Vec::new(), String::new())
                        };
                        (Some(Data::System { cpu: c, mem, used, total, disks, procs, uptime }), 2.0)
                    }
                    "network" => {
                        let io = crate::sysinfo::net_bytes();
                        let t = std::time::Instant::now();
                        let (down, up) = match net_last {
                            Some((pt, (pr, ps))) => {
                                let dt = t.duration_since(pt).as_secs_f64().max(0.1);
                                (io.0.saturating_sub(pr) as f64 / dt, io.1.saturating_sub(ps) as f64 / dt)
                            }
                            None => (0.0, 0.0),
                        };
                        net_last = Some((t, io));
                        let addrs = if det { crate::sysinfo::addresses() } else { Vec::new() };
                        (Some(Data::Network { down, up, recv: io.0, sent: io.1, addrs }), 1.0)
                    }
                    "media" => {
                        let (tx, rx) = std::sync::mpsc::channel();
                        crate::media::collect(move |s| {
                            let _ = tx.send(s);
                        });
                        let s = rx.recv_timeout(std::time::Duration::from_secs(3)).unwrap_or_default();
                        (Some(Data::Media { sessions: s }), 1.0)
                    }
                    _ => (None, 5.0),
                };
                if let Some(d) = data {
                    sh.0.lock().unwrap().data.insert(id.clone(), d);
                    CHANGED.store(true, Relaxed);
                    crate::app::post_from_thread(Box::new(|a| a.widgets_changed()));
                }
                due.insert(id.clone(), std::time::Instant::now() + std::time::Duration::from_secs_f32(every));
            }
            let left = due[id].saturating_duration_since(std::time::Instant::now());
            nearest = nearest.min(left);
        }
        let (m, c) = &*sh;
        let mut s = m.lock().unwrap();
        if !s.wake {
            let (g, _) = c.wait_timeout(s, nearest.max(std::time::Duration::from_millis(50))).unwrap();
            s = g;
        }
        if s.wake {
            s.wake = false;
            due.clear();
        }
        if s.enabled.is_empty() {
            // всё выключили — спим, пока не включат
            while s.enabled.is_empty() {
                s = c.wait(s).unwrap();
            }
        }
    }
}

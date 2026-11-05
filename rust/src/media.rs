//! Что сейчас играет: медиа-сессии Windows (Spotify, браузеры, YouTube Music…) — название, исполнитель,
//! обложка и кнопки «назад / пауза / вперёд». Системное API WinRT в отдельном потоке — интерфейс не ждёт.

use std::collections::HashMap;
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Mutex, OnceLock};

use windows::Media::Control::{GlobalSystemMediaTransportControlsSessionManager as Manager, GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status};
use windows::Storage::Streams::{DataReader, InputStreamOptions};

#[derive(Clone, Debug)]
pub struct Session {
    pub id: usize,
    pub app: String,
    pub title: String,
    pub artist: String,
    pub playing: bool,
    pub cover: Option<std::sync::Arc<Vec<u8>>>,
}

pub enum Req {
    Collect(Box<dyn FnOnce(Vec<Session>) + Send>),
    Control(usize, &'static str),
}

static TX: OnceLock<Mutex<Sender<Req>>> = OnceLock::new();

fn sender() -> &'static Mutex<Sender<Req>> {
    TX.get_or_init(|| {
        let (tx, rx) = channel::<Req>();
        std::thread::Builder::new().name("MiniDockMedia".into()).stack_size(512 * 1024).spawn(move || run(rx)).ok();
        Mutex::new(tx)
    })
}

/// Собрать сессии в фоне; done вызовется в фоновом потоке.
pub fn collect(done: impl FnOnce(Vec<Session>) + Send + 'static) {
    let _ = sender().lock().unwrap().send(Req::Collect(Box::new(done)));
}

/// action: toggle | next | prev.
pub fn control(id: usize, action: &'static str) {
    let _ = sender().lock().unwrap().send(Req::Control(id, action));
}

fn run(rx: Receiver<Req>) {
    unsafe {
        let _ = windows::Win32::System::Com::CoInitializeEx(None, windows::Win32::System::Com::COINIT_MULTITHREADED);
    }
    let mut mgr: Option<Manager> = None;
    let mut covers: HashMap<(String, String, String), Option<std::sync::Arc<Vec<u8>>>> = HashMap::new();
    while let Ok(req) = rx.recv() {
        if mgr.is_none() {
            mgr = Manager::RequestAsync().and_then(|op| op.join()).ok();
        }
        match req {
            Req::Collect(done) => {
                let out = mgr.as_ref().map(|m| sessions(m, &mut covers)).unwrap_or_default();
                done(out);
            }
            Req::Control(id, action) => {
                if let Some(m) = &mgr {
                    if let Ok(list) = m.GetSessions() {
                        if let Ok(s) = list.GetAt(id as u32) {
                            let _ = match action {
                                "toggle" => s.TryTogglePlayPauseAsync().and_then(|o| o.join()),
                                "next" => s.TrySkipNextAsync().and_then(|o| o.join()),
                                _ => s.TrySkipPreviousAsync().and_then(|o| o.join()),
                            };
                        }
                    }
                }
            }
        }
    }
}

fn sessions(m: &Manager, covers: &mut HashMap<(String, String, String), Option<std::sync::Arc<Vec<u8>>>>) -> Vec<Session> {
    let mut out = Vec::new();
    let Ok(list) = m.GetSessions() else { return out };
    let n = list.Size().unwrap_or(0);
    for i in 0..n {
        let Ok(s) = list.GetAt(i) else { continue };
        let Ok(props) = s.TryGetMediaPropertiesAsync().and_then(|o| o.join()) else { continue };
        let app = s.SourceAppUserModelId().map(|h| h.to_string()).unwrap_or_default();
        let title = props.Title().map(|h| h.to_string()).unwrap_or_default();
        let artist = props.Artist().map(|h| h.to_string()).unwrap_or_default();
        let playing = s.GetPlaybackInfo().and_then(|p| p.PlaybackStatus()).map(|st| st == Status::Playing).unwrap_or(false);
        let key = (app.clone(), title.clone(), artist.clone());
        let cover = match covers.get(&key) {
            Some(c) => c.clone(),
            None => {
                let c = read_cover(&props).map(std::sync::Arc::new);
                if covers.len() > 32 {
                    covers.clear();
                }
                covers.insert(key, c.clone());
                c
            }
        };
        out.push(Session { id: i as usize, app, title, artist, playing, cover });
    }
    out
}

fn read_cover(props: &windows::Media::Control::GlobalSystemMediaTransportControlsSessionMediaProperties) -> Option<Vec<u8>> {
    let thumb = props.Thumbnail().ok()?;
    let stream = thumb.OpenReadAsync().ok()?.join().ok()?;
    let size = stream.Size().ok()? as u32;
    if size == 0 || size > 8 * 1024 * 1024 {
        return None;
    }
    let reader = DataReader::CreateDataReader(&stream).ok()?;
    reader.SetInputStreamOptions(InputStreamOptions::ReadAhead).ok()?;
    let got = reader.LoadAsync(size).ok()?.join().ok()?;
    let mut buf = vec![0u8; got as usize];
    reader.ReadBytes(&mut buf).ok()?;
    Some(buf)
}

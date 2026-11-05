//! Звук (Core Audio): громкость и микрофон, устройства вывода/ввода и устройства по умолчанию
//! («для музыки» и «для звонков»), громкость программ.

use windows::core::{Interface, GUID, HRESULT, PCWSTR};
use windows::Win32::Devices::FunctionDiscovery::PKEY_Device_FriendlyName;
use windows::Win32::Media::Audio::Endpoints::IAudioEndpointVolume;
use windows::Win32::Media::Audio::*;
use windows::Win32::System::Com::*;

pub const GLYPH_SPEAKER: &str = "\u{E767}";
pub const GLYPH_MUTE: &str = "\u{E74F}";
pub const GLYPH_MIC: &str = "\u{E720}";
pub const GLYPH_MIC_OFF: &str = "\u{F781}";
pub const GLYPH_MIXER: &str = "\u{E9E9}";
pub const GLYPH_SETTINGS: &str = "\u{E713}";

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Flow {
    Output,
    Input,
}

impl Flow {
    fn raw(self) -> EDataFlow {
        match self {
            Flow::Output => eRender,
            Flow::Input => eCapture,
        }
    }
}

fn enumerator() -> Option<IMMDeviceEnumerator> {
    unsafe { CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL).ok() }
}

pub fn endpoint(flow: Flow) -> Option<IAudioEndpointVolume> {
    unsafe {
        let dev = enumerator()?.GetDefaultAudioEndpoint(flow.raw(), eMultimedia).ok()?;
        dev.Activate::<IAudioEndpointVolume>(CLSCTX_ALL, None).ok()
    }
}

/// (громкость 0..1, выключен) основного устройства.
pub fn level(flow: Flow) -> Option<(f32, bool)> {
    let v = endpoint(flow)?;
    unsafe { Some((v.GetMasterVolumeLevelScalar().ok()?, v.GetMute().ok()?.as_bool())) }
}

pub fn set_level(flow: Flow, value: f32) {
    if let Some(v) = endpoint(flow) {
        unsafe {
            let _ = v.SetMasterVolumeLevelScalar(value.clamp(0.0, 1.0), std::ptr::null());
            if value > 0.0 && v.GetMute().map(|m| m.as_bool()).unwrap_or(false) {
                let _ = v.SetMute(false, std::ptr::null());
            }
        }
    }
}

pub fn toggle_mute(flow: Flow) {
    if let Some(v) = endpoint(flow) {
        unsafe {
            let m = v.GetMute().map(|m| m.as_bool()).unwrap_or(false);
            let _ = v.SetMute(!m, std::ptr::null());
        }
    }
}

/// Значок громкости для верхней панели.
pub fn glyph() -> &'static str {
    match level(Flow::Output) {
        None => GLYPH_SPEAKER,
        Some((l, m)) if m || l <= 0.001 => GLYPH_MUTE,
        Some((l, _)) if l < 0.34 => "\u{E993}",
        Some((l, _)) if l < 0.67 => "\u{E994}",
        _ => "\u{E995}",
    }
}

#[derive(Clone, Debug)]
pub struct Device {
    pub id: String,
    pub name: String,
    pub media: bool,
    pub comm: bool,
}

fn device_id(d: &IMMDevice) -> Option<String> {
    unsafe { d.GetId().ok().map(crate::win::shell::take_pwstr) }
}

fn device_name(d: &IMMDevice) -> Option<String> {
    unsafe {
        let store = d.OpenPropertyStore(STGM_READ).ok()?;
        let v = store.GetValue(&PKEY_Device_FriendlyName).ok()?;
        let s = v.to_string();
        (!s.is_empty()).then_some(s)
    }
}

pub fn devices(flow: Flow) -> Vec<Device> {
    let mut out = Vec::new();
    let Some(e) = enumerator() else { return out };
    unsafe {
        let def = |role: ERole| e.GetDefaultAudioEndpoint(flow.raw(), role).ok().and_then(|d| device_id(&d));
        let (media, comm) = (def(eMultimedia), def(eCommunications));
        let Ok(coll) = e.EnumAudioEndpoints(flow.raw(), DEVICE_STATE_ACTIVE) else { return out };
        for i in 0..coll.GetCount().unwrap_or(0) {
            let Ok(d) = coll.Item(i) else { continue };
            let Some(id) = device_id(&d) else { continue };
            let name = device_name(&d).unwrap_or_else(|| id.clone());
            out.push(Device { media: media.as_deref() == Some(&id), comm: comm.as_deref() == Some(&id), id, name });
        }
    }
    out
}

/// Сделать устройство основным: для музыки/игр (обычный и мультимедийный режим) или для связи.
/// Через IPolicyConfig — им пользуется сама Windows (интерфейс не документирован, но стабилен с Vista).
pub fn set_default(id: &str, comm: bool) -> bool {
    const CLSID_POLICY: GUID = GUID::from_u128(0x870af99c_171d_4f9e_af0d_e63df40c2bc9);
    const IID_POLICY: GUID = GUID::from_u128(0xf8679f50_850a_41cf_9c72_430f290290c8);
    unsafe {
        let Ok(unk) = CoCreateInstance::<_, windows::core::IUnknown>(&CLSID_POLICY, None, CLSCTX_ALL) else { return false };
        let mut pc: *mut core::ffi::c_void = std::ptr::null_mut();
        if unk.query(&IID_POLICY, &mut pc).is_err() || pc.is_null() {
            return false;
        }
        // vtable: 3 метода IUnknown + 10 до SetDefaultEndpoint
        type SetDefault = unsafe extern "system" fn(*mut core::ffi::c_void, PCWSTR, ERole) -> HRESULT;
        type Release = unsafe extern "system" fn(*mut core::ffi::c_void) -> u32;
        let vtbl = *(pc as *const *const usize);
        let set: SetDefault = std::mem::transmute(*vtbl.add(13));
        let release: Release = std::mem::transmute(*vtbl.add(2));
        let w = crate::win::sys::wstr(id);
        let roles: &[ERole] = if comm { &[eCommunications] } else { &[eConsole, eMultimedia] };
        let mut ok = true;
        for &r in roles {
            ok &= set(pc, PCWSTR(w.as_ptr()), r).is_ok();
        }
        release(pc);
        ok
    }
}

pub struct AppSession {
    pub name: String,
    pub exe: String,
    pub volumes: Vec<ISimpleAudioVolume>,
}

impl AppSession {
    pub fn level(&self) -> f32 {
        self.volumes.first().and_then(|v| unsafe { v.GetMasterVolume().ok() }).unwrap_or(0.0)
    }
    pub fn set_level(&self, v: f32) {
        for s in &self.volumes {
            unsafe {
                let _ = s.SetMasterVolume(v.clamp(0.0, 1.0), std::ptr::null());
            }
        }
    }
}

/// Громкость программ: у одной программы бывает несколько сессий (Discord, браузер) — двигаем вместе.
pub fn app_sessions() -> Vec<AppSession> {
    let mut apps: Vec<AppSession> = Vec::new();
    unsafe {
        let Some(e) = enumerator() else { return apps };
        let Ok(dev) = e.GetDefaultAudioEndpoint(eRender, eMultimedia) else { return apps };
        let Ok(mgr) = dev.Activate::<IAudioSessionManager2>(CLSCTX_ALL, None) else { return apps };
        let Ok(en) = mgr.GetSessionEnumerator() else { return apps };
        for i in 0..en.GetCount().unwrap_or(0) {
            let Ok(ctl) = en.GetSession(i) else { continue };
            let Ok(ctl2) = ctl.cast::<IAudioSessionControl2>() else { continue };
            let pid = ctl2.GetProcessId().unwrap_or(0);
            if pid == 0 {
                continue;
            }
            let Some(exe) = crate::win::sys::pid_exe(pid) else { continue };
            let Ok(vol) = ctl.cast::<ISimpleAudioVolume>() else { continue };
            let name = crate::win::shell::exe_description(&exe).unwrap_or_else(|| crate::win::sys::file_stem(&exe));
            if let Some(a) = apps.iter_mut().find(|a| a.exe == exe) {
                a.volumes.push(vol);
            } else {
                apps.push(AppSession { name, exe, volumes: vec![vol] });
            }
        }
    }
    apps.sort_by_key(|a| a.name.to_lowercase());
    apps
}

//! Bluetooth: включён ли адаптер, включить/выключить, сопряжённые устройства и подключены ли они.
//! WinRT (Radios, Bluetooth). Вызовы медленные — только в фоновом потоке.

use std::sync::atomic::{AtomicU8, Ordering::Relaxed};

use windows::Devices::Bluetooth::{BluetoothConnectionStatus, BluetoothDevice, BluetoothLEDevice};
use windows::Devices::Enumeration::DeviceInformation;
use windows::Devices::Radios::{Radio, RadioAccessStatus, RadioKind, RadioState};

/// 0 — не проверяли, 1 — адаптера нет, 2 — есть.
static HAS_RADIO: AtomicU8 = AtomicU8::new(0);

fn mta() {
    unsafe {
        let _ = windows::Win32::System::Com::CoInitializeEx(None, windows::Win32::System::Com::COINIT_MULTITHREADED);
    }
}

fn radio() -> Option<Radio> {
    let list = Radio::GetRadiosAsync().ok()?.join().ok()?;
    (0..list.Size().ok()?).filter_map(|i| list.GetAt(i).ok()).find(|r| r.Kind().ok() == Some(RadioKind::Bluetooth))
}

/// Есть ли адаптер (проверяется в фоне один раз). None — ещё не знаем.
pub fn has_radio() -> Option<bool> {
    match HAS_RADIO.load(Relaxed) {
        0 => {
            HAS_RADIO.store(3, Relaxed); // проверка идёт
            std::thread::spawn(|| {
                mta();
                HAS_RADIO.store(if radio().is_some() { 2 } else { 1 }, Relaxed);
            });
            None
        }
        1 => Some(false),
        2 => Some(true),
        _ => None,
    }
}

pub struct Info {
    /// None — адаптера нет
    pub on: Option<bool>,
    pub devices: Vec<(String, bool)>,
}

pub fn info() -> Info {
    mta();
    let Some(r) = radio() else { return Info { on: None, devices: Vec::new() } };
    let on = r.State().ok() == Some(RadioState::On);
    let mut devices: Vec<(String, bool)> = Vec::new();
    if on {
        let selectors = [BluetoothDevice::GetDeviceSelectorFromPairingState(true), BluetoothLEDevice::GetDeviceSelectorFromPairingState(true)];
        for (k, sel) in selectors.into_iter().enumerate() {
            let Ok(sel) = sel else { continue };
            let Ok(found) = DeviceInformation::FindAllAsyncAqsFilter(&sel).and_then(|o| o.join()) else { continue };
            for i in 0..found.Size().unwrap_or(0) {
                let Ok(d) = found.GetAt(i) else { continue };
                let name = d.Name().map(|h| h.to_string().trim().to_string()).unwrap_or_default();
                if name.is_empty() || devices.iter().any(|(n, _)| *n == name) {
                    continue;
                }
                let id = d.Id().unwrap_or_default();
                let connected = if k == 0 {
                    BluetoothDevice::FromIdAsync(&id).and_then(|o| o.join()).and_then(|b| b.ConnectionStatus()).map(|s| s == BluetoothConnectionStatus::Connected).unwrap_or(false)
                } else {
                    BluetoothLEDevice::FromIdAsync(&id).and_then(|o| o.join()).and_then(|b| b.ConnectionStatus()).map(|s| s == BluetoothConnectionStatus::Connected).unwrap_or(false)
                };
                devices.push((name, connected));
            }
        }
        devices.sort_by(|a, b| (!a.1, a.0.to_lowercase()).cmp(&(!b.1, b.0.to_lowercase())));
    }
    Info { on: Some(on), devices }
}

pub fn set_enabled(on: bool) -> bool {
    mta();
    if Radio::RequestAccessAsync().and_then(|o| o.join()).ok() != Some(RadioAccessStatus::Allowed) {
        return false;
    }
    let Some(r) = radio() else { return false };
    r.SetStateAsync(if on { RadioState::On } else { RadioState::Off }).and_then(|o| o.join()).ok() == Some(RadioAccessStatus::Allowed)
}

"""Bluetooth: включён ли адаптер, включить/выключить, сопряжённые устройства и подключены ли они.
Через системное API Windows (WinRT Radios и Bluetooth). Функции синхронные и медленные — звать в фоне."""
import asyncio

try:
    from winrt.windows.devices.bluetooth import BluetoothConnectionStatus, BluetoothDevice, BluetoothLEDevice
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.devices.radios import Radio, RadioAccessStatus, RadioKind, RadioState
except Exception:  # пакетов winrt нет — значка Bluetooth не будет
    Radio = None

_has_radio = None


def available():
    return Radio is not None


async def _radio():
    for r in await Radio.get_radios_async():
        if r.kind == RadioKind.BLUETOOTH:
            return r
    return None


async def _devices():
    out, seen = [], set()
    for cls in (BluetoothDevice, BluetoothLEDevice):
        try:
            found = await DeviceInformation.find_all_async_aqs_filter(cls.get_device_selector_from_pairing_state(True))
        except Exception:
            continue
        for d in found:
            name = (d.name or "").strip()
            if not name or name in seen:
                continue
            connected = False
            try:
                dev = await cls.from_id_async(d.id)
                connected = dev is not None and dev.connection_status == BluetoothConnectionStatus.CONNECTED
            except Exception:
                pass
            seen.add(name)
            out.append((name, connected))
    out.sort(key=lambda x: (not x[1], x[0].casefold()))  # подключённые сверху
    return out


def has_radio():
    """Есть ли на компьютере Bluetooth-адаптер (один раз за запуск)."""
    global _has_radio
    if _has_radio is None:
        try:
            _has_radio = available() and asyncio.run(_radio()) is not None
        except Exception:
            _has_radio = False
    return _has_radio


def info():
    """dict(on: True/False/None — None, если адаптера нет, devices: [(имя, подключено)])."""
    if not available():
        return dict(on=None, devices=[])

    async def run():
        r = await _radio()
        if r is None:
            return dict(on=None, devices=[])
        return dict(on=r.state == RadioState.ON, devices=await _devices())
    try:
        return asyncio.run(run())
    except Exception:
        return dict(on=None, devices=[])


def set_enabled(on):
    """Включить или выключить Bluetooth. True — получилось."""
    if not available():
        return False

    async def run():
        if await Radio.request_access_async() != RadioAccessStatus.ALLOWED:
            return False
        r = await _radio()
        if r is None:
            return False
        return await r.set_state_async(RadioState.ON if on else RadioState.OFF) == RadioAccessStatus.ALLOWED
    try:
        return asyncio.run(run())
    except Exception:
        return False

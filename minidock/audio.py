"""Звук: устройства вывода и ввода, устройства по умолчанию («для музыки» и «для связи»), громкость программ."""
from comtypes import CLSCTX_ALL

try:
    from pycaw.constants import DEVICE_STATE, EDataFlow, ERole
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
except Exception:
    AudioUtilities = None

OUTPUT, INPUT = 0, 1


def available():
    return AudioUtilities is not None


def devices(flow):
    """[dict(id, name, media, comm)] — активные устройства; media/comm — выбраны ли по умолчанию
    для музыки/игр и для звонков."""
    if not available():
        return []
    e = AudioUtilities.GetDeviceEnumerator()

    def default(role):
        try:
            return e.GetDefaultAudioEndpoint(flow, role).GetId()
        except Exception:
            return None
    media, comm = default(ERole.eMultimedia.value), default(ERole.eCommunications.value)
    out = []
    coll = e.EnumAudioEndpoints(flow, DEVICE_STATE.ACTIVE.value)
    for i in range(coll.GetCount()):
        dev = coll.Item(i)
        did = dev.GetId()
        try:
            name = AudioUtilities.CreateDevice(dev).FriendlyName or did
        except Exception:
            name = did
        out.append(dict(id=did, name=name, media=did == media, comm=did == comm))
    return out


def set_default(device_id, comm=False):
    """Сделать устройство основным: для музыки/игр (обычный и мультимедийный режим) или для связи."""
    roles = [ERole.eCommunications] if comm else [ERole.eConsole, ERole.eMultimedia]
    try:
        AudioUtilities.SetDefaultDevice(device_id, roles)
        return True
    except Exception:
        return False


def default_volume(flow):
    """IAudioEndpointVolume основного устройства вывода/ввода или None."""
    if not available():
        return None
    try:
        dev = AudioUtilities.GetDeviceEnumerator().GetDefaultAudioEndpoint(flow, ERole.eMultimedia.value)
        return dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)
    except Exception:
        return None


def app_sessions():
    """Громкость программ: [dict(name, exe, volumes)] — у одной программы бывает несколько сессий
    (Discord, браузер), их двигаем вместе."""
    if not available():
        return []
    apps = {}
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.Process is None:
                continue
            try:
                exe = s.Process.exe()
                name = s.Process.name()
            except Exception:
                continue
            a = apps.setdefault(exe.lower(), dict(name=name, exe=exe, volumes=[]))
            a["volumes"].append(s.SimpleAudioVolume)
    except Exception:
        return []
    return sorted(apps.values(), key=lambda a: a["name"].casefold())

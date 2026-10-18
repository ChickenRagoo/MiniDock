"""Настройки MiniDock: значения по умолчанию, загрузка и сохранение."""
import json
import os
from pathlib import Path

APP_NAME = "MiniDock"
CFG_VERSION = 7
CFG_DIR = Path(os.environ["APPDATA"]) / APP_NAME
CFG_FILE = CFG_DIR / "config.json"
ICON_CACHE = CFG_DIR / "iconcache"

DEFAULT_CFG = {
    "cfg_version": CFG_VERSION,
    # основное
    "replace_taskbar": True,      # спрятать панель задач Windows, док встаёт на её место
    "win_key_launchpad": True,    # клавиша Win открывает Launchpad вместо «Пуска»
    "custom_switcher": True,      # свой Alt+Tab
    "switcher_titles": True,      # название приложения под иконками в переключателе
    "switcher_window_title": True,  # и заголовок окна под ним
    "switcher_icon": 96,          # размер иконок, px
    "switcher_opacity": 150,      # непрозрачность фона 0..255
    "switcher_radius": 22,        # скругление углов, px
    "switcher_delay": 110,        # через сколько мс показывать окно (быстрое Alt+Tab — без вспышки)
    "switcher_position": "center",  # center | upper
    "switcher_minimized": True,   # показывать приложения, у которых все окна свёрнуты
    "switcher_genie": True,       # свёрнутое окно разворачивается «джинном» из дока
    "switcher_style": "mac",      # mac — значки программ и окна выбранной; windows — миниатюры всех окон
    "switcher_hover": True,       # наведение мыши показывает окна приложения (переключает только клик)
    "switcher_previews": True,    # живые миниатюры окон выбранного приложения
    "switcher_preview": 240,      # ширина миниатюры, px
    "bar_widgets": ["system"],    # виджеты верхней панели (id) в порядке слева направо
    "media_bg": "blur",           # фон карточек плееров в окне звука: blur — размытая обложка, solid — цвет
    "media_color": "#26262e",     # цвет фона карточек при media_bg = solid
    "switcher_per_row": 0,        # сколько иконок/окон в одной строке, дальше — следующая; 0 — все в один ряд
    "dock_previews": True,        # миниатюры окон при наведении на иконку в доке
    "dock_preview": 220,          # ширина миниатюры в доке, px
    "dock_preview_pad": 8,        # толщина рамки вокруг миниатюр предпросмотра в доке
    "dock_preview_delay": 0.45,   # через сколько секунд наведения показывать
    "spotlight": True,            # поиск Spotlight по Alt+Space
    # док
    "position": "bottom",         # bottom | left | right
    "icon_size": 40,
    "magnify": 1.6,
    "spacing": 0.12,              # расстояние между иконками, доля размера иконки
    "margin": 12,                 # отступ дока от края экрана, px
    "autohide": True,
    "autohide_delay": 0.45,       # через сколько секунд прятать док
    "show_running": True,
    "show_trash": True,
    "show_launchpad": False,
    "show_labels": True,
    "indicator": "dot",           # dot | line | none
    "bounce": True,
    # внешний вид дока
    "dock_color": "#202026",
    "dock_opacity": 95,           # 0..255 поверх размытия
    "dock_blur": True,
    "glass_live_parts": ["dock", "bar", "switcher", "previews", "spotlight", "folders", "menus"],  # где живое
    "dock_peek": True,            # наведение на миниатюру окна над доком показывает окно целиком (как в Windows)
    "backgrounds": {},            # свои картинки фона: часть интерфейса -> {"path": файл, "opacity": 0..1}
    "glass_live": True,           # живой фон «стекла» (лупа Windows), иначе — снимок в момент появления
    "glass_radius": 18,           # сила размытия
    "dock_radius": 0.3,           # скругление, доля размера иконки
    "dock_border": True,
    # анимации окон
    "animations": True,
    "minimize_effect": "genie",   # genie | scale
    "anim_speed": 1.0,            # множитель скорости
    "open_animation": True,       # окна программ, запущенных из дока/Launchpad, вылетают из иконки
    "close_animation": True,
    "auto_update": True,          # ставить обновления с GitHub самому (когда компьютер простаивает)
    "window_shadows": False,      # тени под окнами Windows (пока работает MiniDock)
    "shadows_orig": None,         # как было в системе до MiniDock — вернуть при выходе
    # верхняя панель
    "menubar": True,
    "bar_all_screens": False,     # верхняя панель и на дополнительных мониторах
    "dock_all_screens": False,    # док и на дополнительных мониторах
    "bar_height": 30,
    "bar_opacity": 255,
    "bar_blur": False,
    "clock_seconds": True,
    "clock_date": True,
    "show_bluetooth": True,       # значок Bluetooth на верхней панели (если есть адаптер)
    "show_battery": True,
    # Launchpad
    "lp_columns": 7,
    "lp_rows": 5,
    "lp_icon": 72,
    "lp_mode": "all",             # all — все по алфавиту, start — плитки из «Пуска» Windows
    "lp_hidden": [],              # приложения, убранные из Launchpad (ключи parse)
    "lp_folders": [],             # папки Launchpad: [{"name": имя, "items": [приложения]}]
    # служебное
    "taskbar_orig_state": None,
    "pinned": None,               # None = заполнить по умолчанию при первом запуске
}


def load_cfg(default_pinned):
    cfg = dict(DEFAULT_CFG)
    try:
        saved = json.loads(CFG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if saved and saved.get("cfg_version", 1) < 2:
        saved.pop("hide_taskbar", None)
        saved["icon_size"] = min(saved.get("icon_size", 40), 40)
    if saved and saved.get("cfg_version", 1) < 5:
        # верхняя панель: крупнее и непрозрачная
        saved["bar_height"] = max(int(saved.get("bar_height", 30)), 30)
        saved["bar_opacity"] = 255
        saved["bar_blur"] = False
    if saved and saved.get("cfg_version", 1) < 6:
        saved["show_launchpad"] = False  # значок Launchpad убран из дока (включается в настройках)
    if saved and saved.get("cfg_version", 1) < 7:
        saved["switcher_hover"] = True  # наведение больше не переключает, а только показывает окна
    saved["cfg_version"] = CFG_VERSION
    cfg.update({k: v for k, v in saved.items() if k in DEFAULT_CFG})
    if cfg["pinned"]:  # виджеты в доке были в 1.7.0–1.7.1 и убраны
        cfg["pinned"] = [e for e in cfg["pinned"] if not (isinstance(e, dict) and "widget" in e)]
    if cfg["pinned"] is None:
        cfg["pinned"] = default_pinned()
    return cfg


def save_cfg(cfg):
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CFG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CFG_FILE)

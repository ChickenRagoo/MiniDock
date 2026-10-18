# -*- mode: python ; coding: utf-8 -*-
# Сборка MiniDock.exe: pyinstaller build\MiniDock.spec (лучше через build\build.ps1)
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(ROOT, "MiniDock.pyw")],
    pathex=[ROOT],
    hiddenimports=[
        "win32com.shell.shell", "win32com.shell.shellcon", "pythoncom", "pywintypes",
        "pycaw.pycaw", "comtypes", "comtypes.stream", "numpy",
        # карточки плееров в окне звука (системное API медиа Windows)
        "winrt.runtime", "winrt.windows.foundation", "winrt.windows.foundation.collections",
        "winrt.windows.media.control", "winrt.windows.storage.streams",
        "winrt.windows.devices.radios", "winrt.windows.devices.bluetooth", "winrt.windows.devices.enumeration",
        "winrt.windows.devices.bluetooth.genericattributeprofile",
    ],
    excludes=[
        # не используются — меньше размер установщика
        "tkinter", "unittest", "pydoc", "PIL", "PyInstaller",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
        "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtMultimedia", "PySide6.QtCharts",
        "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtSql", "PySide6.QtTest",
        "PySide6.QtBluetooth", "PySide6.QtPositioning", "PySide6.QtWebSockets", "PySide6.QtDesigner",
        "PySide6.QtQuick3D", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtTextToSpeech",
    ],
    noarchive=False,
)
# Qt Quick тянет за собой всё семейство QML-модулей (включая встроенный Chromium на ~200 МБ, 3D, PDF).
# MiniDock использует из Qt Quick только окно и сцену без QML — остальное вырезаем.
DROP = (
    "webengine", "qt6pdf", "quick3d", "quickcontrols2", "quickdialogs2", "quicktemplates2", "quicklayouts",
    "labs", "qt6shadertools", "opengl32sw", "qt6multimedia", "qt6spatialaudio", "qt6charts", "qt6datavisualization",
    "qt6graphs", "qt6virtualkeyboard", "qt6location", "qt6positioning", "qt6sensors", "qt6scxml",
    "qt6statemachine", "qt6remoteobjects", "qt6test", "qt6sql", "qt6designer", "qt6help", "qt6bluetooth",
    "qt6nfc", "qt6serialport", "qt6webchannel", "qt6websockets", "qt6webview", "qt6texttospeech",
    "qt6httpserver", "qt63d", "qt6quickparticles", "qt6quickshapes", "qt6quicktimeline", "qt6quickeffects",
    "qt6quickvectorimage", "qt6qmlxmllistmodel", "qt6qmllocalstorage", "qt6uitools",
    "qt6quickwidgets", "qt6svgwidgets", "qt6concurrent", "qt6dbus", "qt6pdfquick",
)


def keep(entry):
    name = entry[0].replace("\\", "/").lower()
    if "/qml/" in name or name.startswith("pyside6/qml"):
        return False  # QML-модули: файлов .qml у MiniDock нет
    if "/translations/" in name and not ("qtbase_ru" in name or "qtbase_en" in name):
        return False
    base = name.rsplit("/", 1)[-1]
    return not any(d in base for d in DROP)


a.binaries = [b for b in a.binaries if keep(b)]
a.datas = [d for d in a.datas if keep(d)]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MiniDock",
    contents_directory="lib",  # рядом с MiniDock.exe — одна папка lib, а не россыпь файлов
    icon=os.path.join(SPECPATH, "MiniDock.ico"),
    version=os.path.join(SPECPATH, "version_info.txt"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="MiniDock", upx=False)

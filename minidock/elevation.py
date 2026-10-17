"""Режим «с правами администратора».

Windows не даёт обычной программе перехватывать клавиши, пока активно окно, запущенное от администратора
(диспетчер задач, лаунчеры игр, античиты): там срабатывает стандартный Alt+Tab. Поэтому MiniDock можно
запускать с повышенными правами — через задание Планировщика при входе в систему (без запроса UAC каждый раз).
"""
import ctypes
import getpass
import os
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

import win32con
import win32event
import win32process
import win32security
from win32com.shell import shell, shellcon

TASK = "MiniDock"
NO_WINDOW = subprocess.CREATE_NO_WINDOW


def is_elevated():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def open_security():
    """Права для именованных объектов MiniDock (событий, «занято»): доступны и обычному, и повышенному
    экземпляру — иначе ярлык «Выйти», повторный запуск и обновление не достучатся до MiniDock с правами админа."""
    sd = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        "D:(A;;GA;;;WD)(A;;GA;;;SY)S:(ML;;NW;;;LW)", win32security.SDDL_REVISION_1)
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    sa.bInheritHandle = False
    return sa


def command():
    """(программа, аргументы) — как запустить MiniDock."""
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    main = getattr(sys.modules.get("__main__"), "__file__", None) or sys.argv[0]
    return (exe if os.path.exists(exe) else sys.executable), f'"{os.path.abspath(main)}"'


def task_exists():
    try:
        return subprocess.run(["schtasks", "/Query", "/TN", TASK], capture_output=True,
                              creationflags=NO_WINDOW, timeout=15).returncode == 0
    except Exception:
        return False


def _run_elevated(file, params):
    """Запуск с правами администратора (Windows спросит разрешение). Код возврата или None, если отказались."""
    try:
        info = shell.ShellExecuteEx(fMask=shellcon.SEE_MASK_NOCLOSEPROCESS | shellcon.SEE_MASK_FLAG_NO_UI,
                                    lpVerb="runas", lpFile=file, lpParameters=params, nShow=win32con.SW_HIDE)
    except Exception:
        return None
    handle = info.get("hProcess")
    if not handle:
        return None
    win32event.WaitForSingleObject(handle, 60000)
    return win32process.GetExitCodeProcess(handle)


def _task_xml():
    exe, args = command()
    user = f'{os.environ.get("USERDOMAIN", "")}\\{getpass.getuser()}'.lstrip("\\")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>MiniDock с правами администратора</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>4</Priority>
    <AllowStartOnDemand>true</AllowStartOnDemand>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(exe)}</Command><Arguments>{escape(args)}</Arguments></Exec>
  </Actions>
</Task>
"""


def enable():
    """Создать задание «MiniDock при входе, с наивысшими правами». True — получилось."""
    path = os.path.join(tempfile.gettempdir(), "MiniDockTask.xml")
    with open(path, "w", encoding="utf-16") as f:
        f.write(_task_xml())
    try:
        return _run_elevated("schtasks.exe", f'/Create /TN "{TASK}" /XML "{path}" /F') == 0
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def disable():
    return _run_elevated("schtasks.exe", f'/Delete /TN "{TASK}" /F') == 0


def restart_elevated():
    """Через пару секунд (когда этот экземпляр закроется) запустить MiniDock заданием — уже с правами."""
    subprocess.Popen(f'cmd /c ping -n 3 127.0.0.1 >nul & schtasks /Run /TN "{TASK}"', creationflags=NO_WINDOW)


def restart_normal():
    """Через пару секунд запустить MiniDock с обычными правами (Проводник всегда запускает без повышения)."""
    exe, args = command()
    if getattr(sys, "frozen", False):
        subprocess.Popen(f'cmd /c ping -n 3 127.0.0.1 >nul & explorer.exe "{exe}"', creationflags=NO_WINDOW)
    else:
        subprocess.Popen(f'cmd /c ping -n 3 127.0.0.1 >nul & start "" "{exe}" {args}', creationflags=NO_WINDOW)

"""MiniDock — док, верхняя панель, Launchpad и переключатель окон в стиле macOS для Windows 10.

Запуск:      pythonw MiniDock.pyw
Выход:       pythonw MiniDock.pyw --quit
Зависимости: pip install PySide6 psutil pywin32 pycaw comtypes
"""
import os
import sys

# numpy нужен только для размытия фона. Без этого его OpenBLAS заранее резервирует буферы и потоки
# под каждое ядро процессора — около 500 МБ памяти впустую.
for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(var, "1")
# Python чаще переключает потоки — поток хука клавиатуры не ждёт основной дольше 2 мс
sys.setswitchinterval(0.002)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if getattr(sys, "frozen", False) and "--" not in " ".join(sys.argv[1:]):
    # у собранного MiniDock.exe нет консоли — ошибки пишем в файл, чтобы их можно было прислать
    _log_dir = os.path.join(os.environ.get("APPDATA", "."), "MiniDock")
    os.makedirs(_log_dir, exist_ok=True)
    sys.stderr = sys.stdout = open(os.path.join(_log_dir, "error.log"), "a", encoding="utf-8", buffering=1)
    import faulthandler
    faulthandler.enable(file=sys.stderr, all_threads=True)  # падение внутри Windows/Qt тоже попадёт в журнал

from minidock.app import main  # noqa: E402

if __name__ == "__main__":
    main(os.path.abspath(__file__))

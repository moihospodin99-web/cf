"""
config.py — пошук зовнішніх програм без прив'язки до конкретного комп'ютера.

Порядок пошуку для кожної програми:
    1. tools/ поруч зі скриптом — те, що поклав setup.bat (портативний варіант);
    2. системний PATH;
    3. типові місця встановлення (Windows або macOS).

Завдяки цьому одну й ту саму теку можна перенести на інший комп'ютер
(або на флешку) і вона запрацює без правок у коді.
"""

import os
import shutil
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = BASE_DIR / "tools"

# Windows / macOS(Linux). У Windows виконувані файли мають .exe, у решти —
# без розширення, і лежать вони в інших місцях. Одна змінна замість
# розкиданих по коду "ffmpeg.exe" — інакше на Mac програма шукала б файл,
# якого там не буває в принципі.
IS_WINDOWS = os.name == "nt"
EXE_SUFFIX = ".exe" if IS_WINDOWS else ""

LOCAL_PROGRAMS = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"

# Куди дивитись, якщо програми немає ні в tools/, ні в PATH.
if IS_WINDOWS:
    _FFMPEG_DIRS = (LOCAL_PROGRAMS / "ffmpeg", Path("C:/ffmpeg"),
                    Path("C:/Program Files/ffmpeg"))
    _OLLAMA_DIRS = (LOCAL_PROGRAMS / "Ollama", Path("C:/Program Files/Ollama"))
    _DENO_DIRS = (LOCAL_PROGRAMS / "deno", Path.home() / ".deno" / "bin")
else:
    # macOS: Homebrew на Apple Silicon ставить у /opt/homebrew, на Intel —
    # у /usr/local. Ollama як застосунок кладе виконуваний файл усередину
    # свого пакета .app.
    _BREW = (Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
    _FFMPEG_DIRS = _BREW
    _OLLAMA_DIRS = _BREW + (
        Path("/Applications/Ollama.app/Contents/Resources"),
        Path.home() / "Applications" / "Ollama.app" / "Contents" / "Resources",
    )
    _DENO_DIRS = _BREW + (Path.home() / ".deno" / "bin",)


def _first_existing(*candidates: Optional[Path]) -> Optional[Path]:
    for c in candidates:
        if c and c.exists():
            return c
    return None


def find_exe(exe_name: str, *extra_dirs: Path) -> Optional[Path]:
    """Шукає програму: спершу в tools/, потім у PATH, потім у вказаних теках.

    Ім'я передається БЕЗ розширення — .exe додається лише у Windows.
    """
    exe_name = exe_name + EXE_SUFFIX
    # 1. Поруч із програмою (tools/ffmpeg/ffmpeg.exe, tools/deno/deno.exe тощо)
    for sub in TOOLS_DIR.rglob(exe_name):
        if sub.is_file():
            return sub

    # 2. Системний PATH
    found = shutil.which(exe_name)
    if found:
        return Path(found)

    # 3. Типові місця встановлення. Шукаємо і вглиб: офіційні збірки ffmpeg
    # розпаковуються у теку з версією (ffmpeg-9.0.1-essentials_build/bin).
    for d in extra_dirs:
        direct = d / exe_name
        if direct.exists():
            return direct
        if d.is_dir():
            for nested in d.rglob(exe_name):
                if nested.is_file():
                    return nested
    return None


def ffmpeg_dir() -> Optional[str]:
    exe = find_exe("ffmpeg", *_FFMPEG_DIRS)
    return str(exe.parent) if exe else None


def ollama_exe() -> Optional[str]:
    # tools/ollama (портативна копія від setup.bat) має пріоритет над
    # системною — щоб перенесена тека працювала на комп'ютері, де Ollama
    # ніхто не встановлював.
    exe = find_exe("ollama", *_OLLAMA_DIRS)
    return str(exe) if exe else None


def ollama_models_dir() -> Optional[str]:
    """Тека моделей поруч із портативною Ollama.

    Моделі важать ~7 ГБ. Якщо Ollama портативна, тримаємо і моделі в теці
    програми — інакше вони осядуть у профілі користувача, і перенесення
    теки на інший комп'ютер знову вимагатиме качати їх заново.
    """
    exe = ollama_exe()
    if exe and Path(exe).is_relative_to(TOOLS_DIR):
        models = TOOLS_DIR / "ollama_models"
        models.mkdir(parents=True, exist_ok=True)
        return str(models)
    return None


def deno_exe() -> Optional[str]:
    exe = find_exe("deno", *_DENO_DIRS)
    return str(exe) if exe else None


def describe() -> dict:
    """Що знайдено — для діагностики при першому запуску."""
    return {
        "ffmpeg": ffmpeg_dir(),
        "ollama": ollama_exe(),
        "deno": deno_exe(),
    }


if __name__ == "__main__":
    for name, path in describe().items():
        print(f"{name:8} -> {path or 'НЕ ЗНАЙДЕНО'}")

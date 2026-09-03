"""selfcheck.py — самоперевірка того, від чого залежить робота з TikTok.

Навіщо це є. TikTok ламає доступ до себе постійно, і yt-dlp латають услід —
кілька разів на місяць. Копія yt-dlp, поставлена два місяці тому, спокійно
запускається, спокійно імпортується і при цьому не вміє ні догорнути каталог,
ні завантажити відео. Ззовні це виглядає як зламана програма: перегляди є,
лайків немає, транскрипції немає, каталог обрізаний. Причина одна, а
симптомів три, і жоден із них про неї не каже.

Тому програма перевіряє себе сама, каже прямо, що не так, і сама себе лікує.
"""

import logging
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

log = logging.getLogger("analyzer")

# Після скількох днів версія yt-dlp вважається застарілою для TikTok.
# yt-dlp виходить приблизно раз на тиждень; три тижні — це вже той вік,
# коли поломки TikTok починають прориватися.
STALE_DAYS = 21


def ytdlp_version() -> str:
    try:
        import yt_dlp
        return getattr(yt_dlp.version, "__version__", "") or ""
    except Exception:
        return ""


def _version_date(version: str):
    """Версія yt-dlp — це дата випуску: 2026.08.19."""
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", version or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None


def ytdlp_age_days() -> int:
    """Скільки днів версії yt-dlp. -1, якщо визначити не вдалось."""
    released = _version_date(ytdlp_version())
    if not released:
        return -1
    return max(0, (datetime.now(timezone.utc) - released).days)


def have_impersonation() -> bool:
    """Чи вміє yt-dlp прикидатись Chrome (це вміння дає curl_cffi).

    Питаємо саме yt-dlp, а не `import curl_cffi`: curl_cffi буває
    встановлений, але непридатний (не та збірка, побите колесо), і тоді
    імпорт проходить, а імітація не працює. Значення має лише те, чи
    прийме ціль сам yt-dlp.
    """
    try:
        import yt_dlp
        from yt_dlp.networking.impersonate import ImpersonateTarget

        probe = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
        return bool(probe._impersonate_target_available(ImpersonateTarget("chrome")))
    except Exception:
        return False


def problems() -> list[str]:
    """Список того, що зараз завадить працювати. Порожній — усе гаразд."""
    found = []
    version, age = ytdlp_version(), ytdlp_age_days()
    if not version:
        found.append("yt-dlp не встановлено — TikTok і YouTube працювати не будуть. "
                     "Запусти setup.bat.")
    elif age < 0:
        found.append(f"Не вдалось визначити вік yt-dlp (версія {version}).")
    elif age > STALE_DAYS:
        found.append(
            f"yt-dlp застарів: версія {version}, їй {age} дн. TikTok ламає доступ "
            "кілька разів на місяць, і стара версія не догортає каталог і не "
            "завантажує відео. Оновлюю."
        )
    if not have_impersonation():
        found.append(
            "Немає імітації браузера (curl_cffi). Це найгірша з поломок: без неї "
            "TikTok не віддає ні каталог, ні лайки з коментарями, ні саме відео "
            "для транскрипції — усе одразу. Ставлю."
        )
    return found


def _pip_install(package: str, upgrade: bool = False) -> tuple[bool, str]:
    """Ставить пакет у той самий Python, у якому працює програма."""
    cmd = [sys.executable, "-m", "pip", "install",
           "--disable-pip-version-check", "--no-warn-script-location", "-q"]
    if upgrade:
        cmd.append("--upgrade")
    cmd.append(package)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return False, f"{package}: встановлення не вклалось у 10 хвилин."
    except Exception as e:
        return False, f"{package}: не вдалось запустити встановлення ({e})"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, f"{package}: " + (tail[-1] if tail else "невідома помилка")
    return True, ""


def update_ytdlp() -> tuple[bool, str]:
    """Оновлює yt-dlp у тому ж Python, у якому працює програма."""
    before = ytdlp_version()
    ok, error = _pip_install("yt-dlp", upgrade=True)
    if not ok:
        return False, "Оновити yt-dlp не вийшло: " + error
    # Нова версія підхопиться лише в наступному процесі: модуль уже в пам'яті.
    after = _installed_version_via_pip()
    if after and after != before:
        return True, (f"yt-dlp оновлено: {before or '—'} → {after}. "
                      "Перезапусти start.bat, щоб нова версія почала працювати.")
    return True, f"yt-dlp уже найновіший ({before})."


def install_impersonation() -> tuple[bool, str]:
    """Ставить curl_cffi — те, без чого TikTok не працює взагалі."""
    ok, error = _pip_install("curl_cffi", upgrade=True)
    if not ok:
        return False, ("Не вдалось поставити curl_cffi: " + error +
                       " Працюватиму без імітації браузера — TikTok "
                       "віддаватиме дані через раз.")
    return True, ("curl_cffi встановлено — імітація браузера буде доступна. "
                  "Перезапусти start.bat, щоб вона почала працювати.")


def _installed_version_via_pip() -> str:
    """Версія на диску, а не та, що вже завантажена в пам'ять."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c",
             "import importlib.metadata as m; print(m.version('yt-dlp'))"],
            capture_output=True, text=True, timeout=60,
        )
        return proc.stdout.strip()
    except Exception:
        return ""


def report() -> list[str]:
    """Рядки для журналу й для сторінки: що стоїть і чи воно придатне."""
    version, age = ytdlp_version(), ytdlp_age_days()
    age_text = f", вік {age} дн." if age >= 0 else ""
    return [
        f"yt-dlp {version or 'не встановлено'}{age_text}",
        f"імітація браузера (curl_cffi): {'працює' if have_impersonation() else 'НЕ ПРАЦЮЄ'}",
        f"Python {sys.version.split()[0]}",
    ]


def autoheal(on_message) -> None:
    """Перевіряє себе і лікує те, що вміє. Викликається у фоні при старті.

    Сенс у тому, щоб програма не мовчала про власну несправність і не
    вимагала від людини лізти в pip. Обидві поломки, які тут лікуються,
    ззовні виглядають однаково — «TikTok віддав мало відео».
    """
    for line in report():
        log.info(line)
    found = problems()
    if not found:
        log.info("Самоперевірка: усе гаразд")
        return
    for line in found:
        on_message(line)
        log.warning(line)
    if any("Немає імітації браузера" in line for line in found):
        ok, message = install_impersonation()
        on_message(message)
        log.info(f"Встановлення curl_cffi: ok={ok} {message}")
    if any("застарів" in line for line in found):
        ok, message = update_ytdlp()
        on_message(message)
        log.info(f"Оновлення yt-dlp: ok={ok} {message}")

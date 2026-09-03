"""
analyzer.py — пайплайн аналізу топ-відео конкурентів на YouTube.

Логіка (викликається з app.py, самостійно не запускається):
    1. Для каналу бере до max_scan останніх відео (yt-dlp, без API-ключа)
       і сортує їх за переглядами — щоб знайти найпопулярніші без обходу
       всього каталогу каналу.
    2. Топ-N відео отримують повні метадані: лайки, коментарі, дату.
    3. Транскрипція: спершу готові субтитри YouTube (SUB_LANGS), якщо
       їх нема — розпізнавання аудіо локально через faster-whisper.
    4. Формат відео (за бажанням): кілька кадрів (ffmpeg) класифікує
       локальна vision-модель питанням з варіантами відповіді, а результат
       обирається голосуванням по кадрах (Ollama, без API-ключа).

Встановлення: див. setup.bat — він ставить усе автоматично.
"""

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

import requests
import yt_dlp

import config

# ---------- Конфігурація середовища ----------

SUB_LANGS = ["uk", "en"]   # мови субтитрів, які пробуємо забрати перед Whisper
WHISPER_MODEL = "medium"   # tiny/base/small/medium/large-v3 — компроміс швидкість/якість на CPU.
                            # "small" плутав межі слів і калічив терміни ("вуглекислий" -> "уголокислих").
                            # medium ~2.5x повільніша, але суттєво точніша — на тестовому відео
                            # виправила саме такі помилки.
FRAME_COUNT = 5            # скільки кадрів витягувати з відео для опису

# Шляхи не прописані жорстко: config шукає програми в tools/ поруч зі
# скриптом, у PATH і в типових місцях — щоб теку можна було перенести на
# інший комп'ютер без правок у коді.
FFMPEG_DIR = config.ffmpeg_dir() or ""
OLLAMA_EXE = config.ollama_exe() or ("ollama" + config.EXE_SUFFIX)
DENO_EXE = config.deno_exe()
# Без JS-рушія yt-dlp дедалі частіше впирається в перевірку "я не бот" на
# YouTube — deno дає йому змогу виконати JS-виклик підпису, як робить браузер.
YDL_JS_RUNTIME_OPTS = {"js_runtimes": {"deno": {"path": DENO_EXE}}} if DENO_EXE else {}
OLLAMA_URL = "http://localhost:11434/api/generate"
# moondream: питаємо лише англійською і лише варіантами відповіді (див.
# FRAME_MCQ_OPTIONS) — у вільному описі вона галюцинує. Вона швидка, але на
# частині кадрів мовчки віддає порожню відповідь саме на питання з
# варіантами (при цьому вільний опис того ж кадру дає нормально). Тому там,
# де вона промовчала, добираємо повільнішою, але надійною qwen2.5vl.
VISION_MODEL = "moondream"
FALLBACK_VISION_MODEL = "qwen2.5vl:3b"
MAX_FALLBACK_FRAMES = 3  # обмежуємо, бо повільна модель ~30с на кадр
MIN_VOTES = 2            # скільки голосів уже вважаємо достатнім для рішення

FIELDNAMES = [
    "platform", "channel", "title", "url", "upload_date", "duration_sec",
    "views", "likes", "comments", "shares", "transcript", "visual_description",
]

WORKDIR = Path(tempfile.gettempdir()) / "yt_competitor_analysis"
WORKDIR.mkdir(exist_ok=True)


# ---------- Ollama: перевірка/автозапуск сервера ----------

def ollama_healthy(timeout: float = 5) -> bool:
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=timeout)
        return r.status_code == 200
    except requests.exceptions.RequestException:
        return False


def ollama_env() -> dict:
    """Оточення для запуску Ollama: портативна версія тримає моделі в теці програми."""
    env = dict(os.environ)
    models = config.ollama_models_dir()
    if models:
        env["OLLAMA_MODELS"] = models
    return env


def ensure_ollama_running() -> None:
    if ollama_healthy():
        return
    if not OLLAMA_EXE:
        raise RuntimeError(
            "Ollama не знайдена. Вона потрібна лише для колонки «Формат відео» — "
            "зніми цю галочку або запусти setup.bat, щоб її встановити."
        )
    # creationflags=CREATE_NO_WINDOW ховає чорне вікно консолі, але цей
    # прапорець є ТІЛЬКИ у Windows: на macOS звернення до нього — це
    # AttributeError, тобто програма впала б ще до запуску Ollama.
    popen_extra = ({"creationflags": subprocess.CREATE_NO_WINDOW}
                   if config.IS_WINDOWS else {})
    subprocess.Popen(
        [OLLAMA_EXE, "serve"],
        env=ollama_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **popen_extra,
    )
    start = time.time()
    while time.time() - start < 60:
        if ollama_healthy():
            return
        time.sleep(2)
    raise RuntimeError("Ollama не піднявся за 60с — перевір встановлення вручну.")


# ---------- Список топ-відео каналу ----------

# Звичайні відео, Shorts і трансляції лежать на РІЗНИХ вкладках каналу.
# Скануючи лише /videos, ми не бачили популярних Shorts зовсім — через це в
# топ потрапляли слабші відео, а сильніші пропускались.
CHANNEL_TABS = ("videos", "shorts", "streams")

# Наскільки більше кандидатів перевіряти точними переглядами, ніж треба у
# підсумку: у списку каналу YouTube віддає ОКРУГЛЕНІ перегляди (14000000
# замість 14096310), тож на межі топу порядок був довільним.
REFINE_MARGIN = 5


def _channel_base_url(channel_url: str) -> str:
    base = channel_url.rstrip("/")
    for tab in CHANNEL_TABS:
        if base.endswith(f"/{tab}"):
            return base[: -len(tab) - 1]
    return base


def _flat_entries(url: str, max_scan: int) -> list[dict]:
    ydl_opts = {
        **YDL_JS_RUNTIME_OPTS,
        "extract_flat": True,
        "quiet": True,
        "playlist_items": f"1-{max_scan}",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return [e for e in (info.get("entries") or []) if e and e.get("id")]


def get_top_videos(channel_url: str, max_scan: int, top_n: int) -> list[dict]:
    """Топ-N відео каналу за переглядами — з усіх вкладок і за точними цифрами."""
    base = _channel_base_url(channel_url)

    by_id: dict[str, dict] = {}
    for tab in CHANNEL_TABS:
        try:
            for entry in _flat_entries(f"{base}/{tab}", max_scan):
                by_id.setdefault(entry["id"], entry)
        except Exception:
            continue  # у каналу може не бути такої вкладки — це нормально
    if not by_id:
        raise RuntimeError("Не вдалось отримати список відео каналу")

    candidates = [e for e in by_id.values() if e.get("view_count")]
    candidates.sort(key=lambda e: e["view_count"], reverse=True)

    # Уточнюємо перегляди трохи ширшому колу, ніж потрібно: метадані тих, хто
    # зрештою пройде у топ, одразу кладемо в запис — щоб не тягнути їх двічі.
    pool = candidates[: top_n + REFINE_MARGIN]
    for entry in pool:
        try:
            meta = get_full_metadata(f"https://www.youtube.com/watch?v={entry['id']}")
        except Exception:
            continue
        entry["_meta"] = meta
        if meta.get("view_count"):
            entry["view_count"] = meta["view_count"]

    pool.sort(key=lambda e: e["view_count"], reverse=True)
    return pool[:top_n]


log = logging.getLogger("analyzer")

_IMPERSONATE_OPTS: Optional[dict] = None


def impersonate_opts() -> dict:
    """Опції "прикинутись Chrome" (потребує curl_cffi).

    Для TikTok це вирішальне: без імітації його ендпоінти віддають порожню
    відповідь приблизно через раз (заміряно: 0 успіхів з 2 без імітації і
    3 з 3 з нею), через що список відео профілю й метадані здавались
    непрацездатними.

    Перевірка доступності тут ОБОВ'ЯЗКОВА, і ось чому. Імпорт
    ImpersonateTarget вдається завжди — він частина самого yt-dlp. А
    працювати імітація може лише за наявності curl_cffi. Якщо його немає,
    yt-dlp падає ще на створенні YoutubeDL(...), тобто ДО будь-якого запиту:

        Impersonate target "chrome" is not available.

    Валиться геть усе, що йде через yt-dlp: гортання каталогу, метадані,
    завантаження відео. А оскільки картка профілю читається звичайним
    requests і працює, збоку це має вигляд «TikTok віддав мало відео»:
    перегляди є, лайків немає, транскрипції немає. Три різні симптоми на
    одну причину, і жоден із них на неї не вказує.

    Тому: не вміємо імітувати — працюємо без імітації. Гірше, ніж з нею,
    але незрівнянно краще, ніж ніяк.
    """
    global _IMPERSONATE_OPTS
    if _IMPERSONATE_OPTS is not None:
        return _IMPERSONATE_OPTS
    _IMPERSONATE_OPTS = {}
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget

        target = ImpersonateTarget("chrome")
        probe = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
        if probe._impersonate_target_available(target):
            _IMPERSONATE_OPTS = {"impersonate": target}
        else:
            log.warning(
                "curl_cffi недоступний — працюю без імітації браузера. "
                "TikTok у такому режимі віддає дані приблизно через раз. "
                "Полагодити: pip install curl_cffi"
            )
    except Exception as e:
        log.warning(f"Перевірка імітації браузера не вдалась ({e}) — працюю без неї")
    return _IMPERSONATE_OPTS


def get_full_metadata(video_url: str, impersonate: bool = False) -> dict:
    """Повні метадані одного відео: лайки, коментарі, дата публікації."""
    ydl_opts = {**YDL_JS_RUNTIME_OPTS, "quiet": True, "skip_download": True}
    if impersonate:
        ydl_opts.update(impersonate_opts())
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(video_url, download=False)


# ---------- Транскрипція: субтитри → Whisper як fallback ----------

def vtt_to_text(vtt_path: Path) -> str:
    lines = vtt_path.read_text(encoding="utf-8").splitlines()
    text_lines: list[str] = []
    for line in lines:
        if "-->" in line or line.strip().isdigit() or line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        clean = re.sub(r"<[^>]+>", "", line).strip()
        if clean and (not text_lines or text_lines[-1] != clean):
            text_lines.append(clean)
    return " ".join(text_lines)


def get_subtitles_text(video_url: str, video_id: str, info: Optional[dict] = None) -> Optional[str]:
    """Пробує стягнути готові субтитри (ручні або автогенеровані) — без Whisper.

    Запитує в YouTube лише ту мову, яка реально є в наявності: запит
    неіснуючої мови (напр. uk для англомовного відео) повертає 429 і
    "валить" завантаження решти мов теж, тож спершу звіряємось зі списком
    доступних субтитрів/автосубтитрів з info-словника.
    """
    if info is None:
        with yt_dlp.YoutubeDL({**YDL_JS_RUNTIME_OPTS, "quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(video_url, download=False)

    available = {**(info.get("automatic_captions") or {}), **(info.get("subtitles") or {})}
    lang = next((l for l in SUB_LANGS if l in available), None)
    if lang is None:
        return None

    sub_stub = WORKDIR / video_id
    ydl_opts = {
        **YDL_JS_RUNTIME_OPTS,
        "quiet": True,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [lang],
        "subtitlesformat": "vtt",
        "outtmpl": str(sub_stub) + ".%(ext)s",
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
    except yt_dlp.utils.DownloadError:
        return None

    vtt = Path(f"{sub_stub}.{lang}.vtt")
    if vtt.exists():
        text = vtt_to_text(vtt)
        vtt.unlink(missing_ok=True)
        return text
    return None


_whisper_model = None


def _model_cached_locally(model_size: str) -> bool:
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub" / f"models--Systran--faster-whisper-{model_size}"
    return cache_dir.is_dir()


def get_whisper():
    global _whisper_model
    from faster_whisper import WhisperModel

    if _whisper_model is None:
        # Якщо модель уже закешована — не ходимо в мережу навіть за перевіркою
        # версії. Без цього одного разу трапилось: з'єднання до huggingface.co
        # зависло (0% CPU, відкритий сокет, що нікуди не рухався) і обробка
        # застрягла на 15+ хвилин, хоча модель вже лежала локально повністю.
        local_only = _model_cached_locally(WHISPER_MODEL)
        _whisper_model = WhisperModel(
            WHISPER_MODEL, device="cpu", compute_type="int8", local_files_only=local_only
        )
    return _whisper_model


def transcribe_file(media_path: Path) -> str:
    """Розпізнає мовлення з готового локального файлу (відео або аудіо).

    vad_filter вирізає музику/тишу перед розпізнаванням — типово для TikTok,
    де майже завжди є фонова музика, і без цього вона іноді перетворювалась
    на вигадані слова.
    """
    segments, _ = get_whisper().transcribe(str(media_path), language=None, vad_filter=True)
    return " ".join(s.text.strip() for s in segments)


def download_media_file(
    video_url: str,
    video_id: str,
    fmt: str = "best",
    retries: int = 7,
    delay: float = 4.0,
    impersonate: bool = False,
) -> Optional[Path]:
    """Одне завантаження медіафайлу через yt-dlp — для транскрипції І кадрів разом.

    Раніше транскрипція, кадри й метадані качались окремими викликами, і кожне
    таке видобування могло незалежно впасти на тимчасовій помилці TikTok —
    через це транскрипція зникала на частині відео. Прямий CDN-лінк TikTok
    захищений (403), тому качаємо саме через yt-dlp, але один раз і з
    повторними спробами.
    """
    out_stub = WORKDIR / f"{video_id}_media"
    ydl_opts = {
        **YDL_JS_RUNTIME_OPTS,
        "quiet": True,
        "format": fmt,
        "outtmpl": str(out_stub) + ".%(ext)s",
        "ffmpeg_location": FFMPEG_DIR,
    }
    if impersonate:
        ydl_opts.update(impersonate_opts())

    # Прибираємо сліди попередніх (можливо, обірваних) запусків по цьому відео,
    # щоб не підхопити старий чи недокачаний файл замість свіжого.
    for stale in WORKDIR.glob(f"{video_id}_media.*"):
        stale.unlink(missing_ok=True)

    for attempt in range(1, retries + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])
            # .part/.ytdl — сліди обірваного завантаження; якщо підхопити такий
            # файл як готовий, транскрипція виходить обрізаною.
            matches = [
                p for p in WORKDIR.glob(f"{video_id}_media.*")
                if p.suffix not in (".part", ".ytdl") and p.stat().st_size > 0
            ]
            if matches:
                return matches[0]
        except Exception:
            pass
        if attempt < retries:
            # Прогресивна пауза: TikTok віддає файл приблизно в 3 випадках з 5,
            # і при обробці відео підряд короткі паузи не встигають зняти
            # обмеження за частотою.
            time.sleep(min(delay * attempt, 20))
    return None


def download_media(url: str, dest: Path, headers: Optional[dict] = None, timeout: int = 120) -> Path:
    """Пряме завантаження медіафайлу за готовим URL.

    Для TikTok/Instagram це критично: кожен окремий виклик yt-dlp робить нове
    видобування зі сторінки, яке в TikTok регулярно падає з тимчасовою
    помилкою. Тому метадані беремо один раз, а далі качаємо файл напряму — і
    один і той самий файл використовуємо і для транскрипції, і для кадрів.
    """
    r = requests.get(url, headers=headers or {}, stream=True, timeout=timeout)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
    return dest


def probe_duration(media_path: Path) -> float:
    ffprobe = str(Path(FFMPEG_DIR) / ("ffprobe" + config.EXE_SUFFIX))
    try:
        out = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", str(media_path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def transcribe_with_whisper(video_url: str, video_id: str) -> str:
    """Fallback для YouTube: завантажує аудіо через yt-dlp і розпізнає локально."""
    get_whisper()
    audio_stub = WORKDIR / video_id
    ydl_opts = {
        **YDL_JS_RUNTIME_OPTS,
        "quiet": True,
        "format": "bestaudio/best",
        "outtmpl": str(audio_stub) + ".%(ext)s",
        "ffmpeg_location": FFMPEG_DIR,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "64"}
        ],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])

    audio_path = Path(f"{audio_stub}.mp3")
    if not audio_path.exists():
        return ""

    text = transcribe_file(audio_path)
    audio_path.unlink(missing_ok=True)
    return text


# ---------- Опис відео: кадри → Moondream ----------

def download_video_for_frames(video_url: str, video_id: str) -> Optional[Path]:
    """Качає невелику копію відео (найгірша якість) лише для вирізання кадрів."""
    # Відео потрібне лише для кадрів — звук не якаємо, тому годиться
    # video-only потік (сучасний YouTube віддає роздільні video/audio DASH-стріми).
    out_stub = WORKDIR / f"{video_id}_lowres"
    ydl_opts = {
        **YDL_JS_RUNTIME_OPTS,
        "quiet": True,
        "format": "worstvideo[ext=mp4]/worstvideo/worst",
        "outtmpl": str(out_stub) + ".%(ext)s",
        "ffmpeg_location": FFMPEG_DIR,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])
    matches = list(WORKDIR.glob(f"{video_id}_lowres.*"))
    return matches[0] if matches else None


def extract_frames(video_path: Path, duration_sec: float) -> list[Path]:
    ffmpeg_exe = str(Path(FFMPEG_DIR) / ("ffmpeg" + config.EXE_SUFFIX))
    frames = []
    for i in range(1, FRAME_COUNT + 1):
        ts = duration_sec * i / (FRAME_COUNT + 1)
        frame_path = video_path.with_name(f"{video_path.stem}_frame{i}.jpg")
        subprocess.run(
            [ffmpeg_exe, "-y", "-ss", str(ts), "-i", str(video_path),
             "-frames:v", "1", "-q:v", "3", str(frame_path)],
            capture_output=True,
        )
        if frame_path.exists():
            frames.append(frame_path)
    return frames


# Формат відео визначаємо питанням з варіантами, а не вільним описом:
# маленькі vision-моделі у вільному переказі кадру галюцинують (moondream на
# крупному плані людини стабільно "бачила" урну), але з готових варіантів
# обирають правильно. Літера -> (англ. формулювання для моделі, укр. назва).
FRAME_MCQ_OPTIONS = [
    ("A", "a person talking directly to the camera", "Говоряча голова"),
    ("B", "a person demonstrating an exercise or body movement", "Демонстрація вправ"),
    ("C", "a close-up of a product or object being shown", "Демонстрація продукту"),
    ("D", "a screen recording of an app, website or phone screen", "Запис екрана"),
    ("E", "mostly text or captions filling the screen", "Текст на екрані"),
    ("F", "hands preparing, cooking or assembling something", "Процес / інструкція"),
    ("G", "an outdoor scene, street, room or event without a presenter", "Зйомка на локації"),
]
_MCQ_PROMPT = (
    "Which option best describes this image?\n"
    + "\n".join(f"{letter}) {desc}" for letter, desc, _ in FRAME_MCQ_OPTIONS)
    + "\nAnswer with only the letter."
)
_LETTER_TO_UK = {letter: uk for letter, _, uk in FRAME_MCQ_OPTIONS}


class VisionUnavailable(RuntimeError):
    """Ollama є, але не може виконати модель (неповна/пошкоджена установка)."""


def _ask_frame(b64: str, model: str, timeout: int = 90) -> Optional[str]:
    """Одне питання з варіантами по кадру -> літера відповіді (або None)."""
    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": model,
                "prompt": _MCQ_PROMPT,
                "images": [b64],
                "stream": False,
                "options": {"temperature": 0, "num_predict": 5},
            },
            timeout=timeout,
        )
        payload = r.json()
    except requests.exceptions.RequestException:
        return None

    # Ollama може відповісти 200-помилкою у тілі. Найпоширеніший випадок —
    # розпакована не повністю: ollama.exe є, а бінарників у lib/ немає, і
    # тоді КОЖЕН кадр тихо давав порожній результат замість явної помилки.
    error = payload.get("error")
    if error:
        raise VisionUnavailable(str(error)[:200])

    answer = (payload.get("response") or "").strip().upper()
    return next((ch for ch in answer if ch in _LETTER_TO_UK), None)


def check_vision_ready() -> None:
    """Швидка перевірка, що модель дійсно виконується.

    Робиться ОДИН раз на старті: інакше про несправність дізнавались лише
    після довгої обробки — у вигляді мовчки порожньої колонки формату.
    """
    tiny_png = base64.b64encode(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6300010000050001" "0d0a2db4" "0000000049454e44ae426082"
    )).decode()
    try:
        r = requests.post(
            OLLAMA_URL,
            json={"model": VISION_MODEL, "prompt": "hi", "images": [tiny_png],
                  "stream": False, "options": {"num_predict": 1}},
            timeout=180,
        )
        error = r.json().get("error")
    except requests.exceptions.RequestException as e:
        raise VisionUnavailable(f"Ollama не відповідає ({str(e)[:120]}).")
    if error:
        raise VisionUnavailable(
            f"Ollama не може запустити модель: {str(error)[:160]} "
            "Найчастіше це неповна установка — перезапусти setup.bat."
        )


def classify_frames(frames: list[Path]) -> str:
    """Формат відео як найчастіша відповідь по кадрах (голосування)."""
    images: list[str] = []
    for frame in frames:
        try:
            images.append(base64.b64encode(frame.read_bytes()).decode())
        except OSError:
            pass
        finally:
            frame.unlink(missing_ok=True)

    votes: list[str] = []
    unanswered: list[str] = []
    for b64 in images:
        letter = _ask_frame(b64, VISION_MODEL) or _ask_frame(b64, VISION_MODEL)
        if letter:
            votes.append(letter)
        else:
            unanswered.append(b64)

    # Кадри, на яких швидка модель промовчала, добираємо надійнішою — інакше
    # ціле відео лишалось без визначеного формату. Але зупиняємось, щойно
    # голосів вистачає для рішення: повільна модель ~30с на кадр.
    if len(votes) < MIN_VOTES:
        for b64 in unanswered[:MAX_FALLBACK_FRAMES]:
            letter = _ask_frame(b64, FALLBACK_VISION_MODEL, timeout=300)
            if letter:
                votes.append(letter)
            if len(votes) >= MIN_VOTES:
                break

    if not votes:
        return ""
    winner = max(set(votes), key=votes.count)
    return _LETTER_TO_UK[winner]


def analyze_media_file(media_path: Path, duration_sec: float, transcript: str = "") -> str:
    """Готовий файл → формат відео (напр. 'Демонстрація вправ').

    Тему навмисно НЕ дописуємо: локальна 3B-модель формулювала її покаліченою
    мішанкою мов ("Трави для шумок"), а реальний підпис відео і так є в
    сусідній колонці. Чиста категорія лишається придатною для групування.
    """
    if not duration_sec:
        duration_sec = probe_duration(media_path)
    if not duration_sec:
        return ""
    return classify_frames(extract_frames(media_path, duration_sec))


# ---------- Обробка одного відео ----------

def process_video(channel_name: str, entry: dict, analyze_frames: bool) -> dict:
    video_id = entry["id"]
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    # get_top_videos уже уточнював перегляди повними метаданими — переиспользуємо.
    meta = entry.get("_meta") or get_full_metadata(video_url)
    duration = meta.get("duration") or entry.get("duration") or 0

    transcript = get_subtitles_text(video_url, video_id, info=meta)
    if not transcript:
        transcript = transcribe_with_whisper(video_url, video_id)

    visual_description = ""
    if analyze_frames and duration:
        video_path = download_video_for_frames(video_url, video_id)
        if video_path:
            visual_description = analyze_media_file(video_path, duration, transcript)
            video_path.unlink(missing_ok=True)

    return {
        "platform": "YouTube",
        "channel": channel_name,
        "title": entry.get("title"),
        "url": video_url,
        "upload_date": meta.get("upload_date"),
        "duration_sec": duration,
        "views": meta.get("view_count") or entry.get("view_count"),
        "likes": meta.get("like_count"),
        "comments": meta.get("comment_count"),
        "shares": None,  # YouTube не публікує кількість "поширень"
        "transcript": transcript,
        "visual_description": visual_description,
    }


# ---------- Головний прохід по каналах ----------

def run_analysis(
    channels: list[str],
    top_n: int = 10,
    max_scan: int = 60,
    analyze_frames: bool = True,
    on_row: Optional[Callable[[dict], None]] = None,
    on_status: Optional[Callable[[str], None]] = None,
) -> None:
    def status(msg: str) -> None:
        if on_status:
            on_status(msg)

    if analyze_frames:
        status("Перевіряю Ollama...")
        ensure_ollama_running()

    for channel_url in channels:
        status(f"Канал: {channel_url} — шукаю топ-відео...")
        try:
            top_videos = get_top_videos(channel_url, max_scan, top_n)
        except Exception as e:
            status(f"Помилка отримання списку відео для {channel_url}: {e}")
            continue

        channel_name = channel_url.rstrip("/").split("/")[-1]
        for entry in top_videos:
            status(f"{channel_name}: {entry.get('title', entry.get('id'))}")
            try:
                row = process_video(channel_name, entry, analyze_frames)
                if on_row:
                    on_row(row)
            except Exception as e:
                status(f"Помилка обробки відео {entry.get('id')}: {e}")

    status("Готово")

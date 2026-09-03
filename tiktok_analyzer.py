"""
tiktok_analyzer.py — топ-відео профілю TikTok + обробка окремих відео.

Як дістаємо список відео профілю (звичайна сторінка @user віддає автоматиці
CAPTCHA-заглушку):
    1. Публічний embed-віджет (tiktok.com/embed/@user) — призначений для
       вбудовування на сторонні сайти, тому віддається без верифікації. Але
       показує лише ~13 ОСТАННІХ відео.
    2. З будь-якого з них дістаємо secUid профілю і запитуємо ПОВНИЙ каталог
       через tiktokuser:secUid — це сотні відео з переглядами. Саме тут
       рахується справжній топ; без цього кроку популярні старіші відео
       просто не потрапляли у вибірку.
    TikTok відповідає на крок 2 приблизно через раз, тому там кілька спроб;
    якщо не вийшло зовсім — відкочуємось на список з кроку 1.
"""

import html
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yt_dlp

import analyzer

# Той самий журнал, що й в analyzer.py, — щоб повідомлення про TikTok
# лягали в один app.log, а не губились.
log = logging.getLogger("analyzer")

EMBED_URL = "https://www.tiktok.com/embed/@{user}"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
FRONTITY_RE = re.compile(
    r"<script[^>]+id=['\"]__FRONTITY_CONNECT_STATE__['\"][^>]*>(.*?)</script>", re.S
)

# Станом на вересень 2026 TikTok закрив сторінку профілю й сторінку відео
# заслінкою WAF: yt-dlp отримує на них порожню відповідь ("Unable to extract
# secondary user ID", "Unexpected response from webpage request"), і cookies
# браузера не допомагають — з ними приходить 403. Єдине, що лишилось
# відкритим, — embed КОНКРЕТНОГО відео: він призначений для вбудовування на
# чужі сайти, тому верифікації не вимагає. Звідти беремо і secUid профілю,
# і посилання на сам файл.
VIDEO_EMBED_URL = "https://www.tiktok.com/embed/v2/{video_id}"
SEC_UID_RE = re.compile(r'"secUid":"([^"]{20,})"')
# Посилання на файл лежить у теґу <video src="...">, де & записані як &amp;.
# Без html.unescape параметри підпису приїжджають побиті, і CDN дає 403.
VIDEO_SRC_RE = re.compile(r'<video[^>]+src="([^"]+)"')


def _embed_page_html(video_id: str) -> str:
    """HTML сторінки embed конкретного відео (порожній рядок, якщо не вийшло)."""
    if not video_id:
        return ""
    try:
        r = requests.get(VIDEO_EMBED_URL.format(video_id=video_id),
                         headers={"User-Agent": BROWSER_UA}, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log.warning(f"{video_id}: embed відео недоступний — {e}")
        return ""


def _sec_uid_from_video_embed(video_id: str) -> Optional[str]:
    """secUid профілю зі сторінки embed будь-якого його відео."""
    m = SEC_UID_RE.search(_embed_page_html(video_id))
    return m.group(1) if m else None


def _video_embed_media_url(video_id: str) -> Optional[str]:
    """Пряме посилання на файл відео зі сторінки його embed."""
    for raw in VIDEO_SRC_RE.findall(_embed_page_html(video_id)):
        url = html.unescape(raw)
        if url.startswith("http"):
            return url
    return None


def _download_direct(url: str, video_id: str) -> Optional[Path]:
    """Качає файл за прямим посиланням. Referer обов'язковий — інакше 403."""
    target = analyzer.WORKDIR / f"{video_id}_embed.mp4"
    headers = {"User-Agent": BROWSER_UA, "Referer": "https://www.tiktok.com/",
               "Accept": "*/*"}
    try:
        try:
            from curl_cffi import requests as cffi_requests
            resp = cffi_requests.get(url, headers=headers, timeout=120,
                                     impersonate="chrome", stream=True)
        except Exception:
            resp = requests.get(url, headers=headers, timeout=120, stream=True)
        if resp.status_code >= 400:
            log.warning(f"{video_id}: пряме посилання дало {resp.status_code}")
            return None
        size = 0
        with open(target, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
                    size += len(chunk)
        if size < 10_000:        # надто мало для відео — це сторінка помилки
            target.unlink(missing_ok=True)
            log.warning(f"{video_id}: пряме посилання віддало {size} байт — не відео")
            return None
        return target
    except Exception as e:
        log.warning(f"{video_id}: пряме завантаження не вдалось — {e}")
        target.unlink(missing_ok=True)
        return None


def extract_username(profile_url: str) -> str:
    """@handle з посилання на профіль (або з голого '@user'/'user')."""
    m = re.search(r"tiktok\.com/@([\w.\-]+)", profile_url)
    if m:
        return m.group(1)
    return profile_url.strip().lstrip("@").strip("/")


def _sleep_unless_stopped(seconds: float, should_stop=None) -> bool:
    """Пауза, яку можна перервати. Повертає True, якщо просили зупинитись.

    Довгі time.sleep() були головною причиною, чому «Зупинити» спрацьовувало
    аж через півтори хвилини: перевірка стояла лише між кроками, а сама
    пауза між ними тривала до хвилини.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if should_stop and should_stop():
            return True
        time.sleep(min(0.5, max(0.0, deadline - time.time())))
    return bool(should_stop and should_stop())


def _embed_page(user: str, should_stop=None) -> dict:
    """Дані публічного embed-віджета: картка профілю + ~13 останніх відео."""
    # При частих запитах embed починає віддавати 503 — відступаємо все довше,
    # бо таке обмеження знімається за кілька десятків секунд, а не миттєво.
    last_error: Optional[Exception] = None
    text = ""
    for wait in (5, 15, 30, 0):
        if should_stop and should_stop():
            break
        try:
            r = requests.get(EMBED_URL.format(user=user), headers={"User-Agent": BROWSER_UA}, timeout=30)
            r.raise_for_status()
            text = r.text
            break
        except Exception as e:
            last_error = e
            if wait and _sleep_unless_stopped(wait, should_stop):
                break
    if not text:
        raise RuntimeError(
            f"TikTok тимчасово не віддає профіль @{user} ({last_error}). "
            "Схоже на обмеження за частотою запитів — спробуй за кілька хвилин."
        )

    m = FRONTITY_RE.search(text)
    if not m:
        raise RuntimeError(
            f"TikTok не віддав дані профілю @{user} "
            "(можливо, акаунт приватний або вимкнув вбудовування)"
        )

    src = json.loads(m.group(1)).get("source", {}).get("data", {})
    return next((v for k, v in src.items() if k.startswith("/embed")), {})


def get_profile_info(user: str) -> dict:
    """Картка профілю: підписники, всього лайків, опис. Один легкий запит."""
    try:
        return _embed_page(user).get("userInfo") or {}
    except Exception:
        return {}


def _profile_video_count(info: dict) -> int:
    """Скільки відео в профілі ЗА ЙОГО ВЛАСНОЮ КАРТКОЮ.

    Це головна цифра всього файлу. Без неї «повний каталог» ні з чим
    порівняти: тринадцять відео виглядають однаково і як увесь профіль,
    і як обрізок, що його TikTok віддав замість каталогу. Раніше межу
    брали з того, скільки цей профіль давав МИНУЛОГО РАЗУ, — і якщо
    перший же прогін був обрізком, ця цифра запам'ятовувалась як норма
    і назавжди виправдовувала обрізок.
    """
    for key in ("videoCount", "video_count", "awemeCount"):
        value = info.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    stats = info.get("stats") or info.get("statsV2") or {}
    if isinstance(stats, dict):
        for key in ("videoCount", "video_count"):
            value = stats.get(key)
            try:
                if value and int(value) > 0:
                    return int(value)
            except (TypeError, ValueError):
                pass
    return 0


def _sec_uid_from_info(info: dict) -> Optional[str]:
    """secUid прямо з картки профілю — без завантаження метаданих відео."""
    for key in ("secUid", "sec_uid", "secId"):
        value = info.get(key)
        if isinstance(value, str) and len(value) > 20:
            return value
    user_block = info.get("user") or {}
    if isinstance(user_block, dict):
        value = user_block.get("secUid")
        if isinstance(value, str) and len(value) > 20:
            return value
    return None


def _embed_videos(user: str) -> list[dict]:
    """Список відео з публічного embed-віджета (лише ~13 останніх)."""
    videos = [v for v in (_embed_page(user).get("videoList") or []) if not v.get("privateItem")]
    if not videos:
        raise RuntimeError(f"У профілі @{user} не знайдено публічних відео")
    return videos


SEC_UID_CACHE = Path(__file__).with_name("tiktok_profiles.json")


def _load_cache() -> dict:
    try:
        return json.loads(SEC_UID_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _remember_sec_uid(user: str, sec_uid: str) -> None:
    cache = _load_cache()
    if cache.get(user) == sec_uid:
        return
    cache[user] = sec_uid
    try:
        SEC_UID_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    except OSError:
        pass


def _sec_uid(user: str, embed_videos: list[dict]) -> Optional[str]:
    """Внутрішній ідентифікатор профілю (secUid) — з метаданих будь-якого його відео.

    Кешуємо на диск: цей ідентифікатор для профілю незмінний, а сам крок його
    добування — найкрихкіше місце. Через його зрив увесь топ відкочувався на
    13 останніх відео, хоча повний каталог був доступний.
    """
    cache = _load_cache()
    if cache.get(user):
        return cache[user]

    # Спершу embed відео — єдиний шлях, який TikTok лишив відкритим.
    # Раніше secUid добували через yt-dlp зі сторінки відео, а вона тепер
    # за заслінкою: без secUid немає каталогу, і топ рахувався серед
    # кількох останніх відео замість усього профілю.
    for item in embed_videos[:4]:
        sec_uid = _sec_uid_from_video_embed(str(item.get("id") or ""))
        if sec_uid:
            _remember_sec_uid(user, sec_uid)
            return sec_uid

    for item in embed_videos[:4]:
        try:
            meta = _get_metadata_with_retry(video_url_for(user, item["id"]), attempts=3)
        except Exception:
            continue
        sec_uid = meta.get("channel_id")
        if sec_uid:
            cache[user] = sec_uid
            try:
                SEC_UID_CACHE.write_text(
                    json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError:
                pass
            return sec_uid
    return None


CATALOG_CACHE = Path(__file__).resolve().parent / "tiktok_catalog.json"

# Паузи між спробами. Спершу часті: маршрут спрацьовує приблизно раз із
# трьох, тож три швидкі заходи часто вирішують справу за півхвилини. Далі
# довші — якщо TikTok увімкнув обмеження за частотою, його треба перечекати,
# а не довбати. Разом у найгіршому разі ~5 хвилин.
RETRY_WAITS = (3, 4, 6, 8, 12, 18, 25, 35, 45, 60, 60, 60)


# Скільки днів зібраний каталог вважається придатним. Перегляди старих
# відео майже не рухаються, тому місяць — безпечно; після цього запис
# протухає, щоб топ не рахувався за торішніми цифрами.
CATALOG_TTL_DAYS = 30

# Версія формату кешу. Перша версія зберігала лише перегляди — без лайків,
# коментарів і репостів. Такі записи, підставлені в таблицю, давали рядок
# "199 переглядів, решта прочерки", і виглядало це як зламана програма.
# Кеш старого формату тепер просто ігнорується й перезбирається.
CATALOG_FORMAT = 2


def _load_catalog_cache() -> dict:
    if CATALOG_CACHE.exists():
        try:
            return json.loads(CATALOG_CACHE.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def _cached_entries(user: str) -> list[dict]:
    """Відео, зібрані ПОПЕРЕДНІМИ запусками для цього профілю.

    Це і є відповідь на «запустив ще раз — нічого не змінилось». TikTok
    майже ніколи не віддає весь каталог за один прогін, але щоразу віддає
    ІНШИЙ його шматок. Якщо шматки не складати, кожен запуск починається з
    нуля й дає ті самі 13 відео. Складені — вони за два-три запуски дають
    увесь профіль.
    """
    saved = _load_catalog_cache().get(user) or {}
    if not isinstance(saved, dict):
        return []
    if saved.get("format") != CATALOG_FORMAT:
        # Кеш попереднього формату: у ньому немає лайків, коментарів і
        # репостів. Підставити його — це показати таблицю з прочерками й
        # заодно переконати цикл, що каталог уже зібраний. Краще перезібрати.
        return []
    age_days = (time.time() - saved.get("updated", 0)) / 86400
    if age_days > CATALOG_TTL_DAYS:
        return []
    return [e for e in (saved.get("entries") or []) if isinstance(e, dict) and e.get("id")]


def _save_catalog(user: str, entries: list[dict]) -> None:
    """Зберігаємо сам каталог, а не лише його розмір.

    Раніше тут лежало тільки число — скільки відео профіль дав минулого
    разу. Користі з нього не було: якщо перший же прогін був обрізком, це
    число ставало «нормою» і назавжди виправдовувало обрізок.
    """
    cache = _load_catalog_cache()
    # Зберігаємо всі метрики, а не лише перегляди: аналітика рахує ще й
    # лайки-коментарі-репости, і якщо їх не зберегти, відео, підтягнуте з
    # кешу, приходило в таблицю з порожніми клітинками.
    slim = [{
        "id": e.get("id"),
        "title": e.get("title") or e.get("description") or "",
        "view_count": e.get("view_count") or e.get("playCount") or 0,
        "like_count": e.get("like_count"),
        "comment_count": e.get("comment_count"),
        "repost_count": e.get("repost_count"),
        "duration": e.get("duration") or 0,
        "timestamp": e.get("timestamp") or 0,
        "uploader": e.get("uploader") or user,
    } for e in entries if e.get("id")]
    cache[user] = {"updated": int(time.time()), "count": len(slim),
                   "format": CATALOG_FORMAT, "entries": slim}
    try:
        CATALOG_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# Скільки часу максимум збирати каталог ОДНОГО профілю. TikTok віддає його
# шматками й через раз, тому «одним натиском і напевно» коштує часу — але
# це час, коли можна відійти, а не сидіти й перезапускати.
CATALOG_BUDGET_SEC = int(os.getenv("TIKTOK_CATALOG_BUDGET", "1200"))   # 20 хвилин
# Після стількох спроб поспіль, що не принесли ЖОДНОГО нового відео,
# вважаємо, що TikTok більше нічого не віддасть, і зупиняємось раніше.
STALE_LIMIT = 8


def _extract_into(sec_uid: str, opts: dict, sink: list, errors: list) -> None:
    """Одна спроба гортання каталогу; результат складається у sink.

    Живе в окремому потоці саме для того, щоб «Зупинити» не чекало на неї.
    yt-dlp віддає сторінки ліниво, тож навіть покинута спроба лишає по собі
    те, що встигла зібрати, — нічого не пропадає марно.

    Текст помилки НЕ ковтається. Раніше тут стояло `except: pass`, і через
    це будь-яка поломка виглядала однаково — «зібрано 10 відео» без жодної
    причини. Ані користувач, ані я не могли зрозуміти, що саме сталося.
    """
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"tiktokuser:{sec_uid}", download=False)
        for entry in (info.get("entries") or []) if info else []:
            if entry and entry.get("id"):
                sink.append(entry)
    except Exception as e:
        errors.append(str(e))


def _full_profile_videos(sec_uid: str, expected: int = 0, on_progress=None,
                         should_stop=None, budget_sec: int = CATALOG_BUDGET_SEC,
                         attempts: int = 0, seed: Optional[list[dict]] = None,
                         on_batch=None, on_note=None) -> list[dict]:
    """Збирає каталог, доки не збере ВЕСЬ. Один натиск — і можна відійти.

    Раніше тут стояла фіксована кількість спроб, і після неї програма
    здавалась незалежно від результату — тому доводилось запускати вручну
    ще і ще. Тепер цикл крутиться, доки не виконається одна з умов:

      * зібрано стільки, скільки обіцяє картка профілю (головний вихід);
      * вичерпано час (budget_sec) — щоб не крутитись вічно;
      * STALE_LIMIT спроб поспіль не дали жодного НОВОГО відео, тобто
        TikTok більше нічого не має;
      * користувач натиснув «Зупинити».

    Кожна спроба віддає свій шматок, шматки зливаються за id — саме тому
    десяток неповних відповідей разом дає повний каталог.

    seed — те, що зібрали попередні запуски. Воно лежить у лічильнику з
    самого початку, тому якщо профіль уже зібраний, цикл виходить на першій
    же спробі, а не витрачає весь бюджет на повторний збір того самого.
    on_batch — куди віддавати проміжний результат: каталог зберігається
    після кожної вдалої спроби, тож навіть закрита посеред роботи програма
    нічого не втрачає.
    """
    opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,      # збій однієї сторінки не має валити гортання
        # НЕ ЗМЕНШУВАТИ. TikTok регулярно віддає ту саму сторінку по колу, і
        # єдиний вихід із цієї петлі — скинути device_id та піти ще раз. Робить
        # це саме RetryManager, і кількість його спроб задає extractor_retries.
        # Сам yt-dlp у всіх тестах цього екстрактора ставить 10. При меншому
        # значенні гортання гине на першій сторінці, і профіль на 42 відео
        # віддає ті самі 10 скільки не повторюй.
        "extractor_retries": 10,
        "socket_timeout": 20,      # щоб зависле з'єднання не тримало все
        "sleep_interval_requests": 1,
        **analyzer.impersonate_opts(),
    }
    merged: dict[str, dict] = {e["id"]: e for e in (seed or []) if e.get("id")}
    seen_errors: set[str] = set()
    stale = 0
    started = time.time()
    attempt = 0
    while True:
        attempt += 1
        before = len(merged)
        # Гортання йде в окремому потоці, а не тут. Одна спроба з десятьма
        # внутрішніми повторами триває хвилинами, і поки вона йшла, кнопка
        # «Зупинити» виглядала мертвою. Тепер очікування переривається, а
        # довантаження повторів лишається таким, як його задумав yt-dlp.
        sink: list[dict] = []
        errors: list[str] = []
        worker = threading.Thread(target=_extract_into,
                                  args=(sec_uid, opts, sink, errors), daemon=True)
        worker.start()
        while worker.is_alive():
            if should_stop and should_stop():
                break          # потік демонський — догорить сам і нікого не тримає
            worker.join(0.5)
        for entry in sink[:]:  # зріз: потік може ще дописувати
            if entry.get("id"):
                # Свіжий запис перекриває збережений: перегляди ростуть.
                merged[entry["id"]] = entry
        for text in errors[:]:
            if text not in seen_errors:
                seen_errors.add(text)
                if on_note:
                    on_note(text)

        added = len(merged) - before
        stale = 0 if added else stale + 1
        elapsed = int(time.time() - started)
        left = max(0, budget_sec - elapsed)
        if added and on_batch:
            try:
                on_batch(list(merged.values()))
            except Exception:
                pass
        if on_progress:
            on_progress(attempt, len(merged), expected, added, left)

        if expected and len(merged) >= expected:
            break                                   # зібрали все — головний вихід
        if attempts and attempt >= attempts:
            break                                   # межа спроб (для тестів)
        # Коли каталог уже зібраний, а профіль не сказав, скільки відео всього
        # (expected=0), кожен зайвий захід — до хвилини очікування ні за що.
        stale_limit = 2 if (not expected and len(merged) >= 50) else STALE_LIMIT
        if stale >= stale_limit:
            break                                   # TikTok більше нічого не дає
        if should_stop and should_stop():
            break
        if time.time() - started >= budget_sec:
            break

        # Пауза: доки збирається щось нове — коротка, як застрягли — довша,
        # бо це вже схоже на обмеження за частотою, і його треба перечекати.
        wait = 3 if added else min(10 + stale * 10, 60)
        if _sleep_unless_stopped(wait, should_stop):
            break
    return list(merged.values())


def get_top_videos(profile_url: str, top_n: int, on_progress=None,
                   should_stop=None, on_note=None) -> tuple[list[dict], list[dict], str]:
    """Топ-N відео профілю за переглядами.

    Повертає (топ-N, весь каталог, джерело). Джерело — "full" або "embed":
    TikTok віддає повний каталог приблизно через раз, і коли не віддав,
    лишаються тільки ~13 останніх відео з embed. Без цієї позначки такий
    прогін виглядає як «у профілі мало відео», хоча насправді їх сотні.
    """
    user = extract_username(profile_url)

    # Картка профілю — один легкий запит, з якого беремо ОДРАЗУ дві речі:
    # secUid (без нього немає каталогу) і кількість відео (без неї немає з
    # чим порівняти результат). Раніше secUid добували, качаючи метадані
    # чотирьох відео поспіль — найкрихкіше місце всього ланцюжка.
    embed_videos: list[dict] = []
    page: dict = {}
    try:
        page = _embed_page(user, should_stop=should_stop)
    except Exception:
        page = {}
    info = page.get("userInfo") or {}
    declared = _profile_video_count(info)

    sec_uid = _load_cache().get(user) or _sec_uid_from_info(info)
    if not sec_uid:
        embed_videos = [v for v in (page.get("videoList") or []) if not v.get("privateItem")]
        if not embed_videos:
            embed_videos = _embed_videos(user)
        sec_uid = _sec_uid(user, embed_videos)
    if sec_uid:
        _remember_sec_uid(user, sec_uid)

    # Ціль: скільки відео МАЄ бути. Пріоритет — жива цифра з картки профілю;
    # якщо картка змовчала, беремо найбільше, що цей профіль давав раніше.
    saved = _load_catalog_cache().get(user) or {}
    expected = declared or int(saved.get("count", 0))
    # Запас на відео, яких у списку не буде НІКОЛИ: приватні, видалені,
    # обмежені за віком чи регіоном — картка профілю рахує їх, а список ні.
    # Відсотка самого мало: у профілі на 12 відео 3% — це нуль, і одне
    # приховане відео змушувало програму 20 хвилин доганяти недосяжне, а
    # потім лякати червоним написом. Тому не менше двох штук запасу.
    if expected:
        expected = max(1, expected - max(2, int(expected * 0.03)))

    if sec_uid:
        # Зібране минулими запусками йде в роботу з першої ж секунди: цикл
        # рахує СПІЛЬНИЙ каталог, тому вже зібраний профіль не збирається
        # заново, а недозібраний доганяє лише те, чого бракує.
        entries = _full_profile_videos(
            sec_uid, expected=expected, on_progress=on_progress,
            should_stop=should_stop, seed=_cached_entries(user),
            on_batch=lambda items: _save_catalog(user, items),
            on_note=on_note,
        )
        if entries:
            _save_catalog(user, entries)
        # Джерело чесне: "full" лише тоді, коли зібране справді схоже на
        # весь профіль. Якщо картка каже 216, а зібрали 13 — це обрізок,
        # і називати його повним каталогом не можна.
        complete = (not expected) or len(entries) >= expected
        items = [
            {
                "id": e["id"],
                "desc": e.get("title") or e.get("description") or "",
                "playCount": e.get("view_count") or 0,
                # Список профілю вже містить ПОВНІ метрики — окремий запит на
                # кожне відео (який TikTok віддає лише приблизно через раз) для
                # цифр більше не потрібен зовсім.
                "likeCount": e.get("like_count"),
                "commentCount": e.get("comment_count"),
                "shareCount": e.get("repost_count"),
                "duration": e.get("duration") or 0,
                "timestamp": e.get("timestamp"),
                "authorUniqueId": e.get("uploader") or user,
            }
            for e in entries
            if e.get("view_count")
        ]
        if items:
            items.sort(key=lambda v: v["playCount"], reverse=True)
            # третім значенням — звідки взяті дані: інакше «13 відео» через
            # відмову TikTok неможливо відрізнити від «профіль справді малий»
            return items[:top_n], items, ("full" if complete else "partial")

    # Запасний варіант: лише те, що показує embed (останні ~13 відео).
    if not embed_videos:
        embed_videos = _embed_videos(user)
    embed_videos.sort(key=lambda v: v.get("playCount") or 0, reverse=True)
    return embed_videos[:top_n], embed_videos, "embed"


def video_url_for(user: str, video_id: str) -> str:
    return f"https://www.tiktok.com/@{user}/video/{video_id}"


def _date_from_timestamp(ts) -> Optional[str]:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
    except (OSError, ValueError, TypeError):
        return None


def _get_metadata_with_retry(video_url: str, attempts: int = 6) -> dict:
    """TikTok віддає метадані приблизно через раз — той самий запит за кілька
    секунд зазвичай проходить. Тому пробуємо кілька разів із дедалі довшою
    паузою: при обробці підряд десятка відео короткі паузи не встигають
    "розтиснути" обмеження за частотою, і рядок лишався без лайків/коментарів.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return analyzer.get_full_metadata(video_url, impersonate=True)
        except Exception as e:
            last_error = e
            if attempt < attempts:
                time.sleep(min(4 * attempt, 20))
    raise last_error


def process_video_url(video_url: str, analyze_frames: bool, embed_item: Optional[dict] = None) -> dict:
    """embed_item — дані відео зі списку профілю (id/desc/метрики/тривалість).

    Якщо метрики там уже є (а зі списку профілю вони приходять повні), окремий
    запит на це відео НЕ робиться взагалі: саме він віддавав помилку приблизно
    в половині випадків і лишав рядки без лайків/коментарів. Запит лишається
    тільки для прямого посилання на одне відео, коли списку немає.
    """
    if embed_item and embed_item.get("likeCount") is not None:
        meta = {
            "id": embed_item.get("id"),
            "title": (embed_item.get("desc") or "")[:120],
            "uploader": embed_item.get("authorUniqueId"),
            "view_count": embed_item.get("playCount"),
            "like_count": embed_item.get("likeCount"),
            "comment_count": embed_item.get("commentCount"),
            "repost_count": embed_item.get("shareCount"),
            "duration": embed_item.get("duration") or 0,
            "upload_date": _date_from_timestamp(embed_item.get("timestamp")),
        }
    else:
        try:
            meta = _get_metadata_with_retry(video_url)
        except Exception:
            if not embed_item:
                raise
            meta = {
                "id": embed_item.get("id"),
                "title": (embed_item.get("desc") or "")[:120],
                "uploader": embed_item.get("authorUniqueId"),
                "view_count": embed_item.get("playCount"),
            }

    video_id = str(meta.get("id") or "").strip() or "tt_video"
    duration = meta.get("duration") or 0

    # Один файл на все: транскрипція і кадри з тієї самої копії. Якщо медіа
    # не вдалось завантажити — рядок із метриками все одно має потрапити в
    # таблицю, тому помилки тут не фатальні.
    transcript = ""
    visual_description = ""
    # Порядок саме такий, і це важливо для швидкості. TikTok зараз стабільно
    # відбиває yt-dlp, а той на кожне відео робить кілька заходів із паузами:
    # заміряно 157 секунд марних спроб перед тим, як спрацює запасний шлях.
    # Embed віддає файл за секунди, тому пробуємо його ПЕРШИМ, а yt-dlp
    # лишаємо як запас — на випадок, якщо TikTok знову його пустить.
    media_path = None
    direct = _video_embed_media_url(video_id)
    if direct:
        media_path = _download_direct(direct, video_id)
    if media_path is None:
        media_path = analyzer.download_media_file(video_url, video_id,
                                                  fmt="best", impersonate=True)
    if media_path is None:
        transcript = "(не вдалось завантажити відео для транскрипції)"
    else:
        try:
            if not duration:
                duration = analyzer.probe_duration(media_path)
            transcript = analyzer.transcribe_file(media_path)
            if analyze_frames:
                visual_description = analyzer.analyze_media_file(media_path, duration, transcript)
        finally:
            media_path.unlink(missing_ok=True)

    title = meta.get("title") or (meta.get("description") or "")[:120] or video_id

    return {
        "platform": "TikTok",
        "channel": meta.get("uploader") or meta.get("channel") or meta.get("uploader_id") or "",
        "title": title,
        "url": video_url,
        "upload_date": meta.get("upload_date"),
        "duration_sec": duration,
        "views": meta.get("view_count"),
        "likes": meta.get("like_count"),
        "comments": meta.get("comment_count"),
        "shares": meta.get("repost_count"),
        "transcript": transcript,
        "visual_description": visual_description,
    }

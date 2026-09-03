"""
app.py — веб-інтерфейс для analyzer.py.

Запуск:
    python app.py
    (або подвійний клік по ярлику "Competitor Analyzer — nzarxo ai")

Відкриває http://127.0.0.1:5000 — вставляєш посилання на канали, тиснеш
"Запустити", результати з'являються в таблиці по мірі обробки. Кожен
рядок одразу дописується в results.csv поруч зі скриптом.
"""

import csv
import io
import logging
import sys
import threading
import time
import traceback
from pathlib import Path

# Ембедована збірка Python з цього середовища не додає теку скрипта в
# sys.path автоматично (._pth файл фіксує шляхи), тож без цього імпорт
# сусіднього analyzer.py падає з ModuleNotFoundError.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from flask import Flask, jsonify, request

import analytics
import analyzer
import instagram_analyzer
import selfcheck
import tiktok_analyzer

app = Flask(__name__)

BASE_DIR = Path(__file__).parent
RESULTS_CSV = BASE_DIR / "results.csv"

# Консоль сервера ховається в окремому вікні при запуску через ярлик, тож
# усі статуси й повні traceback-и помилок дублюємо у файл — щоб можна було
# зрозуміти, що пішло не так, навіть не бачачи того вікна.
logging.basicConfig(
    filename=BASE_DIR / "app.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("competitor_analyzer")

STATE_LOCK = threading.Lock()
STATE = {
    "running": False,
    "status": "Очікування запуску",
    "rows": [],
    "messages": [],  # попередження/помилки, що лишаються видимими (на відміну від status, який заміщується)
    "analytics": [],  # зведення по кожному профілю
    # Збір каталогу TikTok може тривати хвилини — без кнопки «Зупинити»
    # єдиним способом його перервати було закрити програму.
    "cancel": False,
    # Що показала самоперевірка (версія yt-dlp, чи працює імітація браузера).
    "health": [],
}


def add_message(text: str) -> None:
    """Повідомлення, яке лишається на сторінці, а не змінює лише статус."""
    log.info(text)
    with STATE_LOCK:
        if text not in STATE["messages"]:
            STATE["messages"].append(text)


def run_selfcheck() -> None:
    """Самоперевірка у фоні при старті: нічого не блокує, але і не мовчить."""
    try:
        selfcheck.autoheal(add_message)
    except Exception as e:
        log.error(f"Самоперевірка впала: {e}")
    finally:
        try:
            lines = selfcheck.report()
        except Exception:
            lines = []
        with STATE_LOCK:
            STATE["health"] = lines


def append_csv_row(row: dict) -> None:
    is_new = not RESULTS_CSV.exists()
    with open(RESULTS_CSV, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=analyzer.FIELDNAMES)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def classify_url(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "tiktok.com" in u:
        return "tiktok_video" if "/video/" in u else "tiktok_profile"
    if "instagram.com" in u:
        return "instagram"
    return "unknown"


def worker(links: list[str], top_n: int, max_scan: int, analyze_frames: bool) -> None:
    def on_status(msg: str) -> None:
        log.info(msg)
        with STATE_LOCK:
            STATE["status"] = msg

    def on_error(msg: str) -> None:
        log.error(msg)
        log.error(traceback.format_exc())
        with STATE_LOCK:
            STATE["status"] = msg
            STATE["messages"].append(msg)

    def on_skip(msg: str) -> None:
        log.info(msg)
        with STATE_LOCK:
            STATE["status"] = msg
            STATE["messages"].append(msg)

    def on_row(row: dict) -> None:
        with STATE_LOCK:
            STATE["rows"].append(row)
        append_csv_row(row)

    def cancelled() -> bool:
        with STATE_LOCK:
            return bool(STATE.get("cancel"))

    try:
        if analyze_frames:
            on_status("Перевіряю Ollama...")
            try:
                analyzer.ensure_ollama_running()
                analyzer.check_vision_ready()
            except Exception as e:
                # Формат відео — необов'язкова колонка. Якщо Ollama непридатна,
                # кажемо про це прямо і рахуємо все інше, а не валимо весь запуск.
                analyze_frames = False
                on_skip(
                    f"Формат відео визначати не буду: {e} "
                    "Решта даних (перегляди, лайки, транскрипція, аналітика) збереться як звичайно."
                )

        for link in links:
            if cancelled():
                break
            kind = classify_url(link)

            if kind == "youtube":
                on_status(f"YouTube: {link} — шукаю топ-відео...")
                try:
                    top_videos = analyzer.get_top_videos(link, max_scan, top_n)
                except Exception as e:
                    on_error(f"Помилка каналу {link}: {e}")
                    continue
                channel_name = link.rstrip("/").split("/")[-1]
                for entry in top_videos:
                    on_status(f"{channel_name}: {entry.get('title', entry.get('id'))}")
                    try:
                        on_row(analyzer.process_video(channel_name, entry, analyze_frames))
                    except Exception as e:
                        on_error(f"Помилка відео {entry.get('id')}: {e}")

            elif kind == "instagram":
                on_status(f"Instagram: {link} — шукаю топ-пости...")
                try:
                    username = instagram_analyzer.extract_username(link)
                    top_posts = instagram_analyzer.get_top_posts(link, max_scan, top_n)
                except Exception as e:
                    on_error(f"Помилка профілю {link}: {e}")
                    continue
                for post in top_posts:
                    on_status(f"{username}: {post.shortcode}")
                    try:
                        on_row(instagram_analyzer.process_post(username, post, analyze_frames))
                    except Exception as e:
                        on_error(f"Помилка поста {post.shortcode}: {e}")

            elif kind == "tiktok_video":
                on_status(f"TikTok: {link}")
                try:
                    on_row(tiktok_analyzer.process_video_url(link, analyze_frames))
                except Exception as e:
                    on_error(f"Помилка відео {link}: {e}")

            elif kind == "tiktok_profile":
                on_status(f"TikTok: {link} — шукаю топ-відео...")
                try:
                    username = tiktok_analyzer.extract_username(link)

                    # Спроби можуть тривати хвилини — показуємо, що робота
                    # йде, інакше здається, що програма зависла.
                    def stopped() -> bool:
                        with STATE_LOCK:
                            return bool(STATE.get("cancel"))

                    def tick(attempt: int, have: int, want: int,
                             added: int, left: int) -> None:
                        # Після натиску «Зупинити» прогрес не пишемо: інакше
                        # він затирає повідомлення про зупинку, і здається,
                        # що кнопка не спрацювала.
                        if stopped():
                            on_status(f"Зупиняю... зібрано {have} відео, збережено")
                            return
                        target = f" із {want}" if want else ""
                        grow = f" (+{added})" if added else ""
                        mins = left // 60
                        on_status(
                            f"TikTok @{username}: зібрано {have}{target} відео{grow} · "
                            f"спроба {attempt} · лишилось часу ~{mins} хв"
                        )

                    def note(text: str) -> None:
                        # Справжній текст помилки від yt-dlp. Без нього будь-яка
                        # поломка виглядала однаково — «зібрано 10 відео» — і
                        # причину неможливо було встановити ні тобі, ні мені.
                        add_message(f"TikTok @{username}: {text}")

                    top_items, catalog, source = tiktok_analyzer.get_top_videos(
                        link, top_n, on_progress=tick, should_stop=stopped,
                        on_note=note)
                except Exception as e:
                    on_error(f"Помилка профілю {link}: {e}")
                    continue
                # Розмір каталогу нічого не доводить: у профілі справді може
                # бути 12 відео. Значення має лише те, ЗВІДКИ вони взялись.
                if source in ("embed", "partial"):
                    on_skip(
                        f"TikTok @{username}: TikTok віддав не весь каталог — "
                        f"зібрано {len(catalog)} відео, топ порахований серед них. "
                        "Зібране збережено: якщо запустиш цей профіль ще раз "
                        "пізніше, збір продовжиться з цього місця, а не з нуля."
                    )

                # Аналітика по всьому каталогу — дані вже завантажені, тож це
                # майже безкоштовно (додається лише легкий запит картки профілю).
                try:
                    summary = analytics.build(catalog, tiktok_analyzer.get_profile_info(username))
                    if summary:
                        summary["platform"] = "TikTok"
                        summary["source"] = link
                        with STATE_LOCK:
                            STATE["analytics"].append(summary)
                except Exception as e:
                    log.error(f"Не вдалось порахувати аналітику для {link}: {e}")
                for index, item in enumerate(top_items):
                    if cancelled():
                        break
                    if index:
                        time.sleep(3)  # не збиваємо обмеження за частотою при обробці підряд
                    vurl = tiktok_analyzer.video_url_for(username, item["id"])
                    on_status(f"{username}: {(item.get('desc') or item['id'])[:60]}")
                    try:
                        on_row(tiktok_analyzer.process_video_url(vurl, analyze_frames, embed_item=item))
                    except Exception as e:
                        on_error(f"Помилка відео {vurl}: {e}")

            else:
                on_skip(f"Не розпізнав платформу: {link}")

        on_status("Зупинено — зібране збережено" if cancelled() else "Готово")
    except Exception as e:
        on_error(f"Помилка: {e}")
    finally:
        with STATE_LOCK:
            STATE["running"] = False


@app.route("/")
def index():
    resp = app.make_response(INDEX_HTML)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """М'яка зупинка: збір каталогу вийде на найближчій перевірці."""
    with STATE_LOCK:
        if not STATE["running"]:
            return jsonify({"ok": True, "note": "нічого не виконується"})
        STATE["cancel"] = True
        STATE["status"] = "Зупиняю після поточного кроку..."
    return jsonify({"ok": True})


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(force=True)
    links = [c.strip() for c in (data.get("channels") or "").splitlines() if c.strip()]
    if not links:
        return jsonify({"error": "Додай хоча б одне посилання"}), 400
    if len(links) > 10:
        return jsonify({"error": "Максимум 10 посилань за раз"}), 400

    with STATE_LOCK:
        if STATE["running"]:
            return jsonify({"error": "Обробка вже триває"}), 409
        STATE["running"] = True
        STATE["cancel"] = False
        STATE["status"] = "Старт..."
        STATE["rows"] = []
        STATE["messages"] = []
        STATE["analytics"] = []

    # Стеля висока навмисно: обмежує лише випадковий ввід на кшталт 100000,
    # а не реальні наміри. Скільки задаси — стільки й буде сценаріїв та хуків.
    top_n = max(1, min(int(data.get("top_n", 10)), 500))
    max_scan = max(top_n, min(int(data.get("max_scan", 60)), 300))
    analyze_frames = bool(data.get("analyze_frames", True))

    log.info(f"Запуск: links={links} top_n={top_n} max_scan={max_scan} analyze_frames={analyze_frames}")

    threading.Thread(
        target=worker, args=(links, top_n, max_scan, analyze_frames), daemon=True
    ).start()
    return jsonify({"ok": True})


def current_hooks() -> list[dict]:
    """Хуки з уже оброблених відео.

    Рахуються на льоту, а не разом з рештою аналітики: та будується ще до
    обробки відео, а хук можна дістати лише з готової транскрипції. Завдяки
    цьому список поповнюється по ходу роботи, а не з'являється в кінці.
    """
    with STATE_LOCK:
        rows = list(STATE["rows"])
        summaries = list(STATE["analytics"])
    median = None
    for s in summaries:
        median = (s.get("catalog") or {}).get("views_median")
        if median:
            break
    return analytics.build_hooks(rows, median)


@app.route("/api/status")
def api_status():
    with STATE_LOCK:
        state = dict(STATE)
    state["hooks"] = current_hooks()
    return jsonify(state)


def _csv_response(rows: list[dict], fieldnames: list[str], filename: str):
    buf = io.StringIO()
    buf.write("﻿")  # BOM — щоб Excel не поламав кирилицю
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    resp = app.make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@app.route("/download/videos.csv")
def download_videos():
    """Відео з повними сценаріями. Хук винесено окремою колонкою перед
    транскрипцією — щоб у таблиці одразу було видно, чим кожне чіпляє."""
    with STATE_LOCK:
        rows = list(STATE["rows"])

    enriched = []
    for r in rows:
        row = dict(r)
        row["hook"] = analytics.extract_hook(r.get("transcript") or "")
        enriched.append(row)

    fields = list(analyzer.FIELDNAMES)
    fields.insert(fields.index("transcript"), "hook")
    return _csv_response(enriched, fields, "nzarxo_ai_videos.csv")


@app.route("/download/hooks.csv")
def download_hooks():
    """Лише хуки — окремим файлом, щоб зручно було переглядати списком."""
    hooks = current_hooks()
    rows = [
        {
            "№": i,
            "хук": h["hook"],
            "перегляди": h["views"],
            "разів від медіани": h["ratio"],
            "формат": h["format"],
            "канал": h["channel"],
            "посилання": h["url"],
        }
        for i, h in enumerate(hooks, 1)
    ]
    return _csv_response(
        rows,
        ["№", "хук", "перегляди", "разів від медіани", "формат", "канал", "посилання"],
        "nzarxo_ai_hooks.csv",
    )


@app.route("/download/analytics.csv")
def download_analytics():
    """Одна пласка таблиця: показник | значення — зручно читати в Excel."""
    with STATE_LOCK:
        summaries = list(STATE["analytics"])

    rows: list[dict] = []
    for s in summaries:
        who = (s.get("profile") or {}).get("username") or s.get("source") or ""

        def add(section: str, name: str, value) -> None:
            rows.append({"профіль": who, "розділ": section, "показник": name, "значення": value})

        p, c = s.get("profile") or {}, s.get("catalog") or {}
        add("Профіль", "Підписники", p.get("followers"))
        add("Профіль", "Всього лайків", p.get("total_likes"))
        add("Профіль", "Назва", p.get("nickname"))
        add("Каталог", "Всього відео", c.get("videos_total"))
        add("Каталог", "Період з", c.get("period_from"))
        add("Каталог", "Період по", c.get("period_to"))
        add("Каталог", "Відео на тиждень", c.get("per_week"))
        add("Каталог", "Медіана переглядів", c.get("views_median"))
        add("Каталог", "Середні перегляди", c.get("views_avg"))
        add("Каталог", "Максимум переглядів", c.get("views_max"))
        add("Каталог", "Сума переглядів", c.get("views_total"))
        add("Каталог", "Середня залученість %", c.get("engagement_avg_pct"))
        add("Каталог", "Відео, що залетіли", s.get("outperformers_total"))

        for o in s.get("outperformers", []):
            add("Залетіло", f"{o['views']:,} ({o['ratio']}x медіани)".replace(",", " "), o["desc"])
        for t in s.get("hashtags", []):
            add("Теми", f"#{t['tag']} ({t['count']} відео)", f"медіана {t['median_views']:,}".replace(",", " "))
        for d in s.get("durations", []):
            add("Тривалість", f"{d['bucket']} ({d['count']} відео)", f"медіана {d['median_views']:,}".replace(",", " "))
        for w in s.get("weekdays", []):
            add("День тижня", f"{w['day']} ({w['count']} відео)", f"медіана {w['median_views']:,}".replace(",", " "))

    # Хуки — в тому ж файлі, щоб уся аналітика була в одному місці.
    for i, h in enumerate(current_hooks(), 1):
        rows.append({
            "профіль": h.get("channel") or "",
            "розділ": "Хук",
            "показник": f"{i}. {h['views']:,} переглядів".replace(",", " "),
            "значення": h["hook"],
        })

    return _csv_response(rows, ["профіль", "розділ", "показник", "значення"], "nzarxo_ai_analytics.csv")


INDEX_HTML = """<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<title>Competitor Analyzer — nzarxo ai</title>
<style>
  :root {
    --bg: #0f1216; --panel: #171b21; --border: #2a2f38; --text: #e6e9ee;
    --muted: #9aa4b2; --accent: #5b8cff; --accent-hover: #4674e6;
    --danger: #ff6b6b; --good: #4ade80;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif;
    padding: 24px;
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 20px; }
  .hint { color: var(--muted); font-size: 12px; margin: 8px 0 0; }
  .platform-tag {
    display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px;
    background: #202632; color: var(--muted); white-space: nowrap;
  }
  .panel {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 18px; margin-bottom: 20px;
  }
  label { display: block; margin-bottom: 6px; color: var(--muted); font-size: 13px; }
  textarea, input[type=number] {
    width: 100%; background: #0d1014; color: var(--text);
    border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
    font-family: inherit; font-size: 14px;
  }
  textarea { min-height: 90px; resize: vertical; }
  .row { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 14px; }
  .row > div { flex: 1; min-width: 160px; }
  .checkbox-row { display: flex; align-items: center; gap: 8px; margin-top: 14px; }
  .checkbox-row input { width: auto; }
  .checkbox-row label { margin: 0; color: var(--text); }
  button {
    background: var(--accent); color: white; border: none; border-radius: 6px;
    padding: 10px 20px; font-size: 14px; cursor: pointer; margin-top: 16px;
  }
  button:hover { background: var(--accent-hover); }
  button:disabled { background: #3a3f4a; cursor: not-allowed; }
  #stopBtn { background: #6b3540; margin-left: 8px; }
  #stopBtn:hover { background: #8a3f4e; }
  .status {
    display: flex; align-items: center; gap: 8px; margin-top: 12px;
    color: var(--muted); font-size: 13px; min-height: 18px;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .dot.running { background: var(--accent); animation: pulse 1.2s infinite; }
  .dot.error { background: var(--danger); }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { border-bottom: 1px solid var(--border); padding: 8px 10px; text-align: left; vertical-align: top; }
  th { color: var(--muted); font-weight: 600; position: sticky; top: 0; background: var(--panel); }
  td.num { text-align: right; white-space: nowrap; }
  td.title a { color: var(--accent); text-decoration: none; }
  td.title a:hover { text-decoration: underline; }
  details summary { cursor: pointer; color: var(--accent); }
  details[open] summary { margin-bottom: 6px; }
  .cell-text { max-width: 340px; }
  .empty { color: var(--muted); text-align: center; padding: 30px; }
  .downloads { display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
  .dl {
    display: inline-block; padding: 7px 14px; border-radius: 6px; font-size: 13px;
    background: #202632; color: var(--text); text-decoration: none; border: 1px solid var(--border);
  }
  .dl:hover { background: #2a3240; }
  .an-head { display: flex; align-items: baseline; gap: 10px; margin: 0 0 12px; flex-wrap: wrap; }
  .an-head h2 { font-size: 16px; margin: 0; }
  .an-head .sig { color: var(--muted); font-size: 12px; }
  .stats { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
  .stat {
    background: #12161c; border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 14px; min-width: 120px;
  }
  .stat .v { font-size: 18px; font-weight: 600; }
  .stat .k { color: var(--muted); font-size: 11px; margin-top: 2px; }
  .an-grid { display: flex; gap: 18px; flex-wrap: wrap; }
  .an-col { flex: 1; min-width: 260px; }
  .an-col h3 { font-size: 13px; color: var(--muted); margin: 0 0 8px; font-weight: 600; }
  .an-col table { font-size: 12px; }
  .an-col td { padding: 5px 8px; }
  .hot { color: var(--good); font-weight: 600; }
  .brand {
    font-size: 11px; font-weight: 600; letter-spacing: .1em; text-transform: uppercase;
    color: var(--accent); border: 1px solid var(--accent); border-radius: 4px;
    padding: 2px 7px; margin-left: 10px; vertical-align: middle;
  }
  .hook-text { font-size: 14px; line-height: 1.5; max-width: 900px; }
  .hook-text a { color: var(--accent); text-decoration: none; }
  #hooksBody td { vertical-align: top; padding: 10px 12px; }
  #hooksBody .k { color: var(--muted); font-size: 11px; margin-top: 4px; }
  .messages { list-style: none; margin: 10px 0 0; padding: 0; }
  .messages li {
    color: var(--danger); font-size: 12px; padding: 6px 10px; margin-top: 6px;
    background: #2a1a1a; border: 1px solid #4a2828; border-radius: 6px;
  }
</style>
</head>
<body>
  <h1>Competitor Analyzer <span class="brand">nzarxo ai</span></h1>
  <p class="sub">Топ-контент конкурентів на YouTube, Instagram і TikTok: перегляди, лайки, коментарі, репости, транскрипція, опис відео.</p>

  <div class="panel">
    <label for="channels">Посилання на профілі (по одному на рядок, до 10): YouTube, Instagram, TikTok</label>
    <textarea id="channels" placeholder="https://www.youtube.com/@mkbhd
https://www.instagram.com/nasa
https://www.tiktok.com/@nazchol"></textarea>
    <p class="hint">Топ рахується по всьому каталогу профілю. Для TikTok також працює пряме посилання на конкретне відео.
    Скільки відео візьмеш — стільки буде сценаріїв і хуків; орієнтовно 2-8 хвилин на відео, тож 100 відео — це кілька годин.</p>
    <p class="hint">TikTok віддає каталог шматками, тому програма сама повторює запити, доки не збере весь профіль
    (до 20 хвилин на профіль). Натиснув один раз — можна відійти. Зібране зберігається по ходу, тож ніщо не пропадає,
    навіть якщо зупинити або закрити програму.</p>
    <div class="row">
      <div>
        <label for="topN">Скільки відео брати з профілю</label>
        <input type="number" id="topN" value="10" min="1" max="500">
      </div>
      <div>
        <label for="maxScan">Скільки останніх сканувати (YouTube, Instagram)</label>
        <input type="number" id="maxScan" value="60" min="10" max="300">
      </div>
    </div>
    <div class="checkbox-row">
      <input type="checkbox" id="analyzeFrames" checked>
      <label for="analyzeFrames">Визначати формат відео (говоряча голова, демонстрація тощо) — повільніше</label>
    </div>
    <button id="runBtn" onclick="runAnalysis()">Запустити</button>
    <button id="stopBtn" onclick="stopAnalysis()" style="display:none">⏹ Зупинити</button>
    <div class="status"><span class="dot" id="statusDot"></span><span id="statusText">Очікування запуску</span></div>
    <div id="healthLine" class="hint" style="margin-top:8px"></div>
    <ul id="messagesList" class="messages"></ul>
  </div>

  <div class="panel" id="analyticsPanel" style="display:none">
    <div id="analyticsBody"></div>
  </div>

  <div class="panel" id="hooksPanel" style="display:none">
    <div class="an-head">
      <h2>Топ-хуки</h2>
      <span class="sig">перші секунди озвучки — те, чим відео чіпляє. Від найуспішнішого</span>
    </div>
    <div id="hooksBody"></div>
  </div>

  <div class="panel">
    <div class="downloads">
      <a class="dl" href="/download/videos.csv">⬇ Відео (CSV)</a>
      <a class="dl" href="/download/analytics.csv">⬇ Аналітика (CSV)</a>
      <a class="dl" href="/download/hooks.csv">⬇ Хуки (CSV)</a>
    </div>
    <table id="resultsTable">
      <thead>
        <tr>
          <th>Платформа</th><th>Канал</th><th>Відео</th><th class="num">Перегляди</th>
          <th class="num">Лайки</th><th class="num">Коментарі</th><th class="num">Репости</th>
          <th>Формат відео</th><th>Транскрипція</th>
        </tr>
      </thead>
      <tbody id="resultsBody">
        <tr><td colspan="9" class="empty">Ще немає результатів</td></tr>
      </tbody>
    </table>
  </div>

<script>
let polling = null;

function fmtNum(n) {
  if (n === null || n === undefined) return "—";
  return Number(n).toLocaleString("uk-UA");
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function renderRows(rows) {
  const body = document.getElementById("resultsBody");
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="9" class="empty">Ще немає результатів</td></tr>';
    return;
  }
  body.innerHTML = rows.map(r => `
    <tr>
      <td><span class="platform-tag">${escapeHtml(r.platform)}</span></td>
      <td>${escapeHtml(r.channel)}</td>
      <td class="title"><a href="${r.url}" target="_blank">${escapeHtml(r.title)}</a></td>
      <td class="num">${fmtNum(r.views)}</td>
      <td class="num">${fmtNum(r.likes)}</td>
      <td class="num">${fmtNum(r.comments)}</td>
      <td class="num">${fmtNum(r.shares)}</td>
      <td class="cell-text">${escapeHtml(r.visual_description) || "—"}</td>
      <td class="cell-text">
        <details><summary>показати</summary>${escapeHtml(r.transcript) || "—"}</details>
      </td>
    </tr>
  `).join("");
}

function setStatus(text, mode) {
  document.getElementById("statusText").textContent = text;
  const dot = document.getElementById("statusDot");
  dot.className = "dot" + (mode ? " " + mode : "");
}

function renderAnalytics(list) {
  const panel = document.getElementById("analyticsPanel");
  const body = document.getElementById("analyticsBody");
  if (!list || !list.length) { panel.style.display = "none"; return; }
  panel.style.display = "";

  body.innerHTML = list.map(a => {
    const p = a.profile || {}, c = a.catalog || {};
    const stat = (v, k) => `<div class="stat"><div class="v">${v ?? "—"}</div><div class="k">${k}</div></div>`;

    const rows = (arr, cells) => arr.length
      ? `<table>${arr.map(cells).join("")}</table>`
      : '<div class="k">—</div>';

    return `
      <div class="an-head">
        <h2>${escapeHtml(p.nickname || p.username || a.source || "")}</h2>
        <span class="sig">@${escapeHtml(p.username || "")} · ${escapeHtml(a.platform || "")}</span>
        ${p.signature ? `<span class="sig">${escapeHtml(p.signature)}</span>` : ""}
      </div>
      <div class="stats">
        ${stat(fmtNum(p.followers), "підписники")}
        ${stat(fmtNum(p.total_likes), "всього лайків")}
        ${stat(fmtNum(c.videos_total), "відео в каталозі")}
        ${stat(c.per_week ?? "—", "відео / тиждень")}
        ${stat(fmtNum(c.views_median), "медіана переглядів")}
        ${stat(fmtNum(c.views_max), "найкращий результат")}
        ${stat(c.engagement_avg_pct != null ? c.engagement_avg_pct + "%" : "—", "залученість")}
        ${stat(fmtNum(a.outperformers_total), "відео, що залетіли")}
      </div>
      <div class="an-grid">
        <div class="an-col">
          <h3>Залетіли найбільше (від медіани каналу)</h3>
          ${rows((a.outperformers || []).slice(0, 8), o => `
            <tr><td class="hot">${o.ratio}x</td><td class="num">${fmtNum(o.views)}</td>
            <td>${escapeHtml(o.desc || "(без опису)")}</td></tr>`)}
        </div>
        <div class="an-col">
          <h3>Теми, що заходять найкраще</h3>
          ${rows(a.hashtags || [], t => `
            <tr><td>#${escapeHtml(t.tag)}</td><td class="num">${fmtNum(t.median_views)}</td>
            <td class="k">${t.count} відео${t.vs_channel ? ` · ${t.vs_channel}x` : ""}</td></tr>`)}
        </div>
        <div class="an-col">
          <h3>Тривалість відео</h3>
          ${rows(a.durations || [], d => `
            <tr><td>${escapeHtml(d.bucket)}</td><td class="num">${fmtNum(d.median_views)}</td>
            <td class="k">${d.count} відео</td></tr>`)}
        </div>
        <div class="an-col">
          <h3>День публікації</h3>
          ${rows(a.weekdays || [], w => `
            <tr><td>${escapeHtml(w.day)}</td><td class="num">${fmtNum(w.median_views)}</td>
            <td class="k">${w.count} відео</td></tr>`)}
        </div>
      </div>`;
  }).join("<hr style='border:none;border-top:1px solid var(--border);margin:20px 0'>");
}

function renderHooks(hooks) {
  const panel = document.getElementById("hooksPanel");
  const body = document.getElementById("hooksBody");
  if (!hooks || !hooks.length) { panel.style.display = "none"; return; }
  panel.style.display = "";

  body.innerHTML = `<table>${hooks.map((h, i) => `
    <tr>
      <td class="num k">${i + 1}</td>
      <td class="num">${fmtNum(h.views)}${h.ratio ? `<div class="k">${h.ratio}x медіани</div>` : ""}</td>
      <td class="hook-text">
        «${escapeHtml(h.hook)}»
        <div class="k">${escapeHtml(h.channel || "")}${h.format ? " · " + escapeHtml(h.format) : ""}
          ${h.url ? ` · <a href="${h.url}" target="_blank">відкрити</a>` : ""}</div>
      </td>
    </tr>`).join("")}</table>`;
}

function renderHealth(lines) {
  // Стан того, від чого залежить TikTok. Червоним — коли зламане, бо саме
  // ця поломка виглядає як "TikTok віддав мало відео".
  const el = document.getElementById("healthLine");
  if (!lines || !lines.length) { el.textContent = ""; return; }
  const broken = lines.some(l => l.indexOf("НЕ ПРАЦЮЄ") >= 0 || l.indexOf("не встановлено") >= 0);
  el.style.color = broken ? "#f0a0a8" : "";
  el.textContent = lines.join("  ·  ");
}

function renderMessages(messages) {
  const list = document.getElementById("messagesList");
  list.innerHTML = (messages || []).map(m => `<li>${escapeHtml(m)}</li>`).join("");
}

async function poll() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    setStatus(data.status, data.running ? "running" : (data.status.startsWith("Помилка") ? "error" : ""));
    renderRows(data.rows);
    renderMessages(data.messages);
    renderAnalytics(data.analytics);
    renderHooks(data.hooks);
    renderHealth(data.health);
    document.getElementById("runBtn").disabled = data.running;
    document.getElementById("stopBtn").style.display = data.running ? "" : "none";
    if (!data.running) {
      document.getElementById("stopBtn").disabled = false;
      if (polling) {
        clearInterval(polling);
        polling = null;
      }
    }
  } catch (e) {
    setStatus("Немає зв'язку з сервером — перевір, чи запущено app.py", "error");
    if (polling) {
      clearInterval(polling);
      polling = null;
    }
    document.getElementById("runBtn").disabled = false;
    document.getElementById("stopBtn").style.display = "none";
  }
}

async function stopAnalysis() {
  // М'яка зупинка: сервер дозбирає поточний крок і вийде з циклу.
  const btn = document.getElementById("stopBtn");
  btn.disabled = true;
  setStatus("Зупиняю після поточного кроку...", "running");
  try {
    await fetch("/api/stop", { method: "POST" });
  } catch (e) {
    setStatus("Немає зв'язку з сервером — перевір, чи запущено app.py", "error");
    btn.disabled = false;
  }
}

async function runAnalysis() {
  const channels = document.getElementById("channels").value;
  const top_n = document.getElementById("topN").value;
  const max_scan = document.getElementById("maxScan").value;
  const analyze_frames = document.getElementById("analyzeFrames").checked;

  document.getElementById("runBtn").disabled = true;
  document.getElementById("stopBtn").disabled = false;
  document.getElementById("stopBtn").style.display = "";
  setStatus("Запускаю...", "running");

  let res, data;
  try {
    res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channels, top_n, max_scan, analyze_frames }),
    });
    data = await res.json();
  } catch (e) {
    setStatus("Немає зв'язку з сервером — перевір, чи запущено app.py", "error");
    document.getElementById("runBtn").disabled = false;
    document.getElementById("stopBtn").style.display = "none";
    return;
  }
  if (!res.ok) {
    setStatus(data.error, "error");
    document.getElementById("runBtn").disabled = false;
    document.getElementById("stopBtn").style.display = "none";
    return;
  }
  if (!polling) polling = setInterval(poll, 1500);
  poll();
}

poll();
</script>
</body>
</html>
"""

def already_running(port: int = 5000) -> bool:
    """Windows дозволяє кільком процесам слухати той самий порт, і тоді запити
    потрапляють до випадкового (зокрема до старої копії зі застарілим кодом).
    Тому перед стартом перевіряємо, чи сервер уже піднято."""
    import socket

    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    if already_running():
        print("Competitor Analyzer — nzarxo ai вже запущено — відкрий http://127.0.0.1:5000")
        sys.exit(0)
    # Самоперевірка у фоні: сторінка відкривається одразу, а несправності
    # (застарілий yt-dlp, відсутня імітація браузера) знаходяться й лікуються
    # самі — без них TikTok мовчки віддає обрізки замість каталогу.
    threading.Thread(target=run_selfcheck, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False)

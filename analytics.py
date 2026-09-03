"""
analytics.py — зведена аналітика профілю.

Усе рахується з даних, які вже й так завантажені для пошуку топ-відео:
повний каталог профілю (сотні відео з метриками) плюс картка профілю.
Жодних додаткових важких запитів — тому аналітика майже безкоштовна.
"""

import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Optional

# \w{2,} — щоб у теми не потрапляло сміття на кшталт "#..." з описів
HASHTAG_RE = re.compile(r"#(\w{2,40})", re.UNICODE)

# Межі груп за тривалістю (сек) — щоб побачити, який формат заходить краще.
DURATION_BUCKETS = [
    (0, 15, "до 15с"),
    (15, 30, "15-30с"),
    (30, 60, "30-60с"),
    (60, 180, "1-3хв"),
    (180, 10 ** 9, "понад 3хв"),
]

OUTPERFORM_RATIO = 2.0  # у скільки разів треба перевищити медіану, щоб вважати "залетіло"
MIN_TAG_USES = 2        # хештеги, вжиті рідше, статистично беззмістовні

# Хук — перші секунди озвучки, які утримують глядача. Беремо початок
# транскрипції до кінця речення, але не довше HOOK_MAX_CHARS.
HOOK_MAX_CHARS = 180
HOOK_MIN_CHARS = 25
SENTENCE_END_RE = re.compile(r"[.!?…]+[\s»\"']*")


def extract_hook(transcript: str) -> str:
    """Перше-друге речення озвучки — те, чим відео чіпляє з перших секунд."""
    text = " ".join((transcript or "").split())
    if len(text) < HOOK_MIN_CHARS:
        return ""

    hook = ""
    for m in SENTENCE_END_RE.finditer(text):
        candidate = text[: m.end()].strip()
        if len(candidate) > HOOK_MAX_CHARS:
            break
        hook = candidate
        # Одного короткого речення для хука замало — добираємо друге.
        if len(hook) >= 60:
            break

    if not hook:
        # Немає розділових знаків (буває в автотранскрипції) — ріжемо по слову.
        hook = text[:HOOK_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return hook


def _median(values: list) -> float:
    return statistics.median(values) if values else 0


def _engagement_pct(item: dict) -> Optional[float]:
    views = item.get("playCount") or 0
    if not views:
        return None
    reactions = sum(
        item.get(k) or 0 for k in ("likeCount", "commentCount", "shareCount")
    )
    return round(reactions / views * 100, 2)


def _date(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OSError, ValueError, TypeError):
        return None


def build(items: list[dict], profile: Optional[dict] = None) -> dict:
    """items — весь каталог профілю у форматі, який віддає get_top_videos."""
    views = [i.get("playCount") or 0 for i in items if i.get("playCount")]
    if not views:
        return {}

    median_views = _median(views)
    dates = sorted(d for d in (_date(i.get("timestamp")) for i in items) if d)

    # --- частота публікацій ---
    per_week = None
    period_from = period_to = None
    if len(dates) >= 2:
        period_from, period_to = dates[0], dates[-1]
        weeks = max((period_to - period_from).days / 7, 1)
        per_week = round(len(dates) / weeks, 1)

    engagements = [e for e in (_engagement_pct(i) for i in items) if e is not None]

    # --- відео, що "залетіли": помітно вище власної медіани каналу ---
    outperformers = []
    for item in items:
        v = item.get("playCount") or 0
        if median_views and v >= median_views * OUTPERFORM_RATIO:
            outperformers.append({
                "id": item.get("id"),
                "desc": (item.get("desc") or "")[:90],
                "views": v,
                "ratio": round(v / median_views, 1),
                "likes": item.get("likeCount"),
                "comments": item.get("commentCount"),
                "shares": item.get("shareCount"),
                "engagement_pct": _engagement_pct(item),
                "duration": item.get("duration") or 0,
            })
    outperformers.sort(key=lambda x: x["views"], reverse=True)

    # --- теми: хештеги з описів, з медіанними переглядами по кожному ---
    tag_views: dict[str, list] = defaultdict(list)
    for item in items:
        v = item.get("playCount") or 0
        for tag in set(HASHTAG_RE.findall((item.get("desc") or "").lower())):
            tag_views[tag].append(v)
    hashtags = [
        {
            "tag": tag,
            "count": len(vals),
            "median_views": int(_median(vals)),
            # у скільки разів тема заходить краще/гірше за середнє по каналу
            "vs_channel": round(_median(vals) / median_views, 2) if median_views else None,
        }
        for tag, vals in tag_views.items()
        if len(vals) >= MIN_TAG_USES
    ]
    hashtags.sort(key=lambda t: t["median_views"], reverse=True)

    # --- який хронометраж заходить краще ---
    duration_stats = []
    for low, high, label in DURATION_BUCKETS:
        vals = [
            i.get("playCount") or 0
            for i in items
            if low <= (i.get("duration") or 0) < high and i.get("playCount")
        ]
        if vals:
            duration_stats.append({
                "bucket": label,
                "count": len(vals),
                "median_views": int(_median(vals)),
            })

    # --- у які дні тижня публікації заходять краще ---
    weekday_views: dict[int, list] = defaultdict(list)
    for item in items:
        d = _date(item.get("timestamp"))
        if d and item.get("playCount"):
            weekday_views[d.weekday()].append(item["playCount"])
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
    weekdays = [
        {"day": names[i], "count": len(v), "median_views": int(_median(v))}
        for i, v in sorted(weekday_views.items())
    ]

    result = {
        "catalog": {
            "videos_total": len(items),
            "views_median": int(median_views),
            "views_avg": int(sum(views) / len(views)),
            "views_max": max(views),
            "views_total": sum(views),
            "engagement_avg_pct": round(sum(engagements) / len(engagements), 2) if engagements else None,
            "per_week": per_week,
            "period_from": period_from.strftime("%Y-%m-%d") if period_from else None,
            "period_to": period_to.strftime("%Y-%m-%d") if period_to else None,
        },
        "outperformers": outperformers[:20],
        "outperformers_total": len(outperformers),
        "hashtags": hashtags[:12],
        "durations": duration_stats,
        "weekdays": weekdays,
    }

    if profile:
        result["profile"] = {
            "nickname": profile.get("nickname"),
            "username": profile.get("uniqueId"),
            "followers": profile.get("followerCount"),
            "total_likes": profile.get("heartCount"),
            "following": profile.get("followingCount"),
            "signature": profile.get("signature"),
            "verified": profile.get("verified"),
        }
    return result


def build_hooks(rows: list[dict], median_views: Optional[int] = None) -> list[dict]:
    """Хуки з оброблених відео — від найуспішнішого до найслабшого.

    Береться з rows (де вже є транскрипція), а не з каталогу: озвучка є лише
    у тих відео, які реально пройшли обробку. Скільки візьмеш у топ —
    стільки хуків і буде.
    """
    hooks = []
    for r in rows:
        hook = extract_hook(r.get("transcript") or "")
        if not hook:
            continue
        views = r.get("views") or 0
        hooks.append({
            "hook": hook,
            "views": views,
            "likes": r.get("likes"),
            "url": r.get("url"),
            "channel": r.get("channel"),
            "platform": r.get("platform"),
            "title": (r.get("title") or "")[:70],
            "format": r.get("visual_description") or "",
            "ratio": round(views / median_views, 1) if median_views and views else None,
        })
    hooks.sort(key=lambda h: h["views"], reverse=True)
    return hooks

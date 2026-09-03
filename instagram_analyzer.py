"""
instagram_analyzer.py — топ-пости профілю Instagram.

На відміну від TikTok, список постів публічного профілю Instagram тягнеться
без проблем через Instaloader (без логіну). Instagram не публікує кількість
"поширень" — лише лайки/коментарі (і перегляди для відео).
"""

import instaloader

import analyzer

_L = instaloader.Instaloader(
    quiet=True,
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    save_metadata=False,
    compress_json=False,
)


def extract_username(profile_url: str) -> str:
    parts = [p for p in profile_url.split("/") if p and "instagram.com" not in p and p not in ("http:", "https:")]
    return parts[0] if parts else profile_url


def get_top_posts(profile_url: str, max_scan: int, top_n: int) -> list:
    """Останні max_scan постів профілю, відсортовані за переглядами/лайками."""
    username = extract_username(profile_url)
    profile = instaloader.Profile.from_username(_L.context, username)

    posts = []
    for i, post in enumerate(profile.get_posts()):
        if i >= max_scan:
            break
        posts.append(post)

    def score(p):
        return (p.video_view_count if p.is_video and p.video_view_count else p.likes) or 0

    posts.sort(key=score, reverse=True)
    return posts[:top_n]


def process_post(username: str, post, analyze_frames: bool) -> dict:
    duration = 0.0
    transcript = ""
    visual_description = ""

    if post.is_video:
        video_path = analyzer.WORKDIR / f"ig_{post.shortcode}.mp4"
        try:
            analyzer.download_media(post.video_url, video_path)
            duration = post.video_duration or analyzer.probe_duration(video_path)
            transcript = analyzer.transcribe_file(video_path)
            if analyze_frames:
                visual_description = analyzer.analyze_media_file(video_path, duration, transcript)
        finally:
            video_path.unlink(missing_ok=True)

    caption_line = (post.caption or "").strip().splitlines()[0][:120] if post.caption else ""

    return {
        "platform": "Instagram",
        "channel": username,
        "title": caption_line or post.shortcode,
        "url": f"https://www.instagram.com/p/{post.shortcode}/",
        "upload_date": post.date_utc.strftime("%Y%m%d"),
        "duration_sec": round(duration) if duration else 0,
        "views": post.video_view_count if post.is_video else None,
        "likes": post.likes,
        "comments": post.comments,
        "shares": None,  # Instagram не публікує кількість поширень
        "transcript": transcript,
        "visual_description": visual_description,
    }

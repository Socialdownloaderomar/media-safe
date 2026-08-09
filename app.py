#!/usr/bin/env python3
"""
Social Video Downloader - Single page web app
Supports YouTube, TikTok, Instagram, Facebook, Twitter/X, and many more via yt-dlp
"""

import os
import re
import uuid
import shutil
import tempfile
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file, after_this_request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # for any form data

# Temporary download directory
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "social_video_dl"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Allowed URL schemes
URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)

# yt-dlp common options
YDL_OPTS_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "js_runtimes": {"node": {}},  # use system node if available
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
        }
    },
}


def is_safe_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if not URL_PATTERN.match(url):
        return False
    # Block obvious local / file attempts
    lowered = url.lower()
    if any(x in lowered for x in ["file://", "localhost", "127.0.0.1", "0.0.0.0", "[::]"]):
        return False
    return True


def get_video_info(url: str) -> dict:
    """Extract metadata and available formats without downloading."""
    import yt_dlp

    opts = {
        **YDL_OPTS_BASE,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise ValueError("تعذر استخراج معلومات الفيديو")

    # Build simplified quality options
    formats = info.get("formats") or []
    video_qualities = set()
    has_audio = False

    for f in formats:
        height = f.get("height")
        acodec = f.get("acodec")
        vcodec = f.get("vcodec")
        if height and vcodec and vcodec != "none":
            # Round to common buckets
            if height >= 2160:
                video_qualities.add(2160)
            elif height >= 1440:
                video_qualities.add(1440)
            elif height >= 1080:
                video_qualities.add(1080)
            elif height >= 720:
                video_qualities.add(720)
            elif height >= 480:
                video_qualities.add(480)
            elif height >= 360:
                video_qualities.add(360)
            elif height >= 240:
                video_qualities.add(240)
        if acodec and acodec != "none":
            has_audio = True

    # Always offer common ones if any video exists
    sorted_qualities = sorted(video_qualities, reverse=True)

    # Thumbnail
    thumb = info.get("thumbnail")
    if not thumb and info.get("thumbnails"):
        thumb = info["thumbnails"][-1].get("url")

    return {
        "id": info.get("id"),
        "title": info.get("title") or "فيديو بدون عنوان",
        "uploader": info.get("uploader") or info.get("channel") or "غير معروف",
        "duration": info.get("duration"),
        "thumbnail": thumb,
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "qualities": sorted_qualities,
        "has_audio": has_audio or bool(info.get("acodec")),
        "description": (info.get("description") or "")[:300],
    }


def download_media(url: str, media_type: str, quality: int | None = None) -> tuple[Path, str, str]:
    """
    Download video or audio.
    Returns: (file_path, filename, mime_type)
    """
    import yt_dlp

    job_id = str(uuid.uuid4())[:8]
    out_dir = DOWNLOAD_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Output template
    outtmpl = str(out_dir / "%(title).80B.%(ext)s")

    opts = {
        **YDL_OPTS_BASE,
        "outtmpl": outtmpl,
        "restrictfilenames": True,
        "windowsfilenames": True,
    }

    if media_type == "mp3":
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
        preferred_ext = "mp3"
        mime = "audio/mpeg"
    else:
        # Video MP4
        if quality:
            # Prefer mp4 container, height <= requested, best video + best audio
            format_str = (
                f"bestvideo[height<=?{quality}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<=?{quality}]+bestaudio/"
                f"best[height<=?{quality}][ext=mp4]/"
                f"best[height<=?{quality}]"
            )
        else:
            format_str = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        opts["format"] = format_str
        opts["merge_output_format"] = "mp4"
        preferred_ext = "mp4"
        mime = "video/mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # Find the actual downloaded file
        filename = ydl.prepare_filename(info)
        # After postprocess, extension may change
        path = Path(filename)
        if media_type == "mp3":
            # Audio extraction changes ext
            path = path.with_suffix(".mp3")
            if not path.exists():
                # Fallback search
                candidates = list(out_dir.glob("*.mp3"))
                if candidates:
                    path = candidates[0]
        else:
            if not path.exists():
                candidates = list(out_dir.glob("*.mp4")) + list(out_dir.glob("*.webm")) + list(out_dir.glob("*.mkv"))
                if candidates:
                    path = candidates[0]

        if not path.exists():
            raise FileNotFoundError("فشل حفظ الملف بعد التحميل")

        # Sanitize final name
        safe_title = re.sub(r'[\\/*?:"<>|]', "", info.get("title") or "video")[:80]
        final_name = f"{safe_title}.{preferred_ext}"

        return path, final_name, mime


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not is_safe_url(url):
        return jsonify({"ok": False, "error": "الرابط غير صالح. يجب أن يبدأ بـ http أو https"}), 400

    try:
        info = get_video_info(url)
        return jsonify({"ok": True, "info": info})
    except Exception as e:
        msg = str(e)
        # Clean common yt-dlp errors for user
        if "Unsupported URL" in msg or "No video" in msg:
            user_msg = "هذا الرابط غير مدعوم أو لا يحتوي على فيديو"
        elif "Private video" in msg or "Sign in" in msg:
            user_msg = "الفيديو خاص أو يتطلب تسجيل دخول"
        elif "unavailable" in msg.lower():
            user_msg = "الفيديو غير متاح حالياً"
        else:
            user_msg = f"حدث خطأ أثناء تحليل الرابط: {msg[:120]}"
        return jsonify({"ok": False, "error": user_msg}), 400


@app.route("/api/download", methods=["POST"])
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    media_type = (data.get("type") or "mp4").lower()
    quality = data.get("quality")

    if not is_safe_url(url):
        return jsonify({"ok": False, "error": "رابط غير صالح"}), 400

    if media_type not in ("mp4", "mp3"):
        return jsonify({"ok": False, "error": "نوع الملف غير مدعوم"}), 400

    try:
        quality_int = int(quality) if quality else None
    except (TypeError, ValueError):
        quality_int = None

    try:
        file_path, filename, mime = download_media(url, media_type, quality_int)

        @after_this_request
        def cleanup(response):
            try:
                # Remove the whole job folder after sending
                job_dir = file_path.parent
                if job_dir.exists() and job_dir.parent == DOWNLOAD_DIR:
                    shutil.rmtree(job_dir, ignore_errors=True)
            except Exception:
                pass
            return response

        return send_file(
            file_path,
            mimetype=mime,
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        msg = str(e)
        return jsonify({"ok": False, "error": f"فشل التحميل: {msg[:150]}"}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # Clean old temp files on start (optional)
    for old in DOWNLOAD_DIR.iterdir():
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)

    print("=" * 50)
    print("  Social Video Downloader is running!")
    print("  Open: http://127.0.0.1:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)

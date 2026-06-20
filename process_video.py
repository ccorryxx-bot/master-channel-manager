import os
import sys
import asyncio
import subprocess
import json
import requests
import traceback
import time
import math
import random
from telethon import TelegramClient
from telethon.tl import types

# ── Env ───────────────────────────────────────────────────────────────────────
API_ID            = os.environ.get("API_ID", "0")
API_HASH          = os.environ.get("API_HASH", "")
BOT_TOKEN         = os.environ.get("BOT_TOKEN", "")
VIDEO_URL         = os.environ.get("VIDEO_URL", "")
TARGET_CHANNEL_ID = os.environ.get("TARGET_CHANNEL_ID", "0")
PHOTO_CAPTION     = os.environ.get("PHOTO_CAPTION", "auto")
VIDEO_CAPTION     = os.environ.get("VIDEO_CAPTION", "auto")
NUM_PHOTOS        = os.environ.get("NUM_PHOTOS", "auto")
POST_MODE         = os.environ.get("POST_MODE", "auto")
CHAT_ID           = os.environ.get("CHAT_ID", "")
WORKER_URL        = os.environ.get("WORKER_URL", "")
WORKFLOW_NAME     = os.environ.get("WORKFLOW_NAME", "V1")
SKIP_WATERMARK    = os.environ.get("SKIP_WATERMARK", "false").lower() == "true"
TASK_ID           = os.environ.get("TASK_ID", "")

# CF Workers AI for caption generation
CF_ACCOUNT_ID     = os.environ.get("CF_ACCOUNT_ID", "")
CF_AUTH_EMAIL     = os.environ.get("CF_AUTH_EMAIL", "")
CF_AUTH_KEY       = os.environ.get("CF_AUTH_KEY", "")
CF_AI_MODEL       = os.environ.get("CF_AI_MODEL", "@cf/aisingapore/gemma-sea-lion-v4-27b-it")

# Cookies for protected sites
PH_COOKIES_B64    = os.environ.get("PH_COOKIES_B64", "")

MAX_FILE_SIZE_MB  = 2000

# ── Channel ID parse ──────────────────────────────────────────────────────────
def parse_channel_id(raw):
    raw = raw.strip()
    try:
        if raw.startswith("-100"): return int(raw)
        elif raw.lstrip("-").isdigit(): return int(raw)
        else: raise ValueError
    except ValueError:
        print(f"❌ Invalid TARGET_CHANNEL_ID: '{raw}'")
        sys.exit(1)

CHANNEL_ID = parse_channel_id(TARGET_CHANNEL_ID)

# ── Progress Reporter ─────────────────────────────────────────────────────────
def send_progress(text):
    msg = f"[{WORKFLOW_NAME}] {text}"
    print(msg)
    if CHAT_ID and WORKER_URL:
        try:
            requests.post(WORKER_URL, json={"chat_id": CHAT_ID, "progress_text": msg}, timeout=10)
        except Exception:
            pass

def send_tg(method, payload):
    for _ in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                              json=payload, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(2)
    return None

# ── Preflight ─────────────────────────────────────────────────────────────────
def preflight_check():
    required = {"API_ID": API_ID, "API_HASH": API_HASH,
                "BOT_TOKEN": BOT_TOKEN, "VIDEO_URL": VIDEO_URL}
    missing = [k for k, v in required.items() if not v or v == "0"]
    if missing:
        send_progress(f"❌ Missing secrets: {', '.join(missing)}")
        sys.exit(1)
    send_progress("✅ Pre-flight OK")

# ── AI Caption Generation ─────────────────────────────────────────────────────
def generate_caption(title, description, caption_type="video"):
    """Generate Burmese caption using CF Workers AI (SEA-Lion v4)"""
    if not CF_ACCOUNT_ID or not CF_AUTH_KEY:
        return None

    prompt_map = {
        "photo": (
            f"Video title: {title}\n"
            f"Description: {description[:300] if description else 'N/A'}\n\n"
            "ဒီ video အတွက် Burmese (Myanmar) မှာ ဆွဲဆောင်မှုရှိတဲ့ photo caption တစ်ကြောင်း ရေးပေး။ "
            "Short (2-3 lines), emoji ပါရမယ်၊ hashtag 2-3 ခု ထည့်ပေး။ Caption ကိုပဲ ထုတ်ပေး၊ explanation မပါနဲ့။"
        ),
        "video": (
            f"Video title: {title}\n"
            f"Description: {description[:300] if description else 'N/A'}\n\n"
            "ဒီ video အတွက် Burmese (Myanmar) မှာ viral ဖြစ်တဲ့ video caption တစ်ကြောင်း ရေးပေး။ "
            "Curious ဖြစ်တဲ့ tone, 3-4 lines, emoji ပါရမယ်၊ hashtag 2-3 ခု ထည့်ပေး။ Caption ကိုပဲ ထုတ်ပေး။"
        )
    }

    try:
        r = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_AI_MODEL}",
            headers={"X-Auth-Email": CF_AUTH_EMAIL, "X-Auth-Key": CF_AUTH_KEY, "Content-Type": "application/json"},
            json={"messages": [
                {"role": "system", "content": "You are a social media content writer specializing in Burmese (Myanmar) language content."},
                {"role": "user", "content": prompt_map.get(caption_type, prompt_map["video"])}
            ]},
            timeout=30
        )
        if r.status_code == 200:
            result = r.json()
            caption = result.get("result", {}).get("response", "").strip()
            if caption:
                send_progress(f"✅ AI caption generated ({caption_type})")
                return caption
    except Exception as e:
        print(f"[WARN] AI caption error: {e}")
    return None

# ── Video helpers ─────────────────────────────────────────────────────────────
def get_video_info(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True
        )
        data = json.loads(r.stdout)
        duration = int(float(data["format"]["duration"]))
        w = h = 0
        for s in data["streams"]:
            if s["codec_type"] == "video":
                w, h = int(s["width"]), int(s["height"])
                break
        return duration, w, h
    except Exception as e:
        print(f"ffprobe error: {e}")
        return 0, 0, 0

def capture_screenshot(video_path, time_pos, out_path):
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(time_pos), "-i", video_path,
             "-frames:v", "1", "-update", "1", "-q:v", "2", out_path],
            check=True, capture_output=True
        )
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception as e:
        print(f"Screenshot error at {time_pos}s: {e}")
        return False

def smart_num_photos(duration):
    """Auto select number of screenshots based on video duration"""
    if NUM_PHOTOS != "auto":
        try:
            return int(NUM_PHOTOS)
        except Exception:
            pass
    if duration < 300:    return 2   # < 5 min
    elif duration < 900:  return 4   # < 15 min
    elif duration < 1800: return 6   # < 30 min
    else:                 return 8   # >= 30 min

def smart_post_mode(size_mb, duration):
    """Auto detect post mode based on size and duration"""
    if POST_MODE != "auto":
        return POST_MODE
    if size_mb > 1800 or duration > 1200:  # > 1.8GB or > 20min
        return "both"
    return "video"  # combined group

# ── Cookies ───────────────────────────────────────────────────────────────────
def setup_cookies():
    if not PH_COOKIES_B64:
        return None
    try:
        import base64
        content = base64.b64decode(PH_COOKIES_B64).decode()
        path = "/tmp/ph_cookies.txt"
        with open(path, "w") as f:
            f.write(content)
        send_progress("🍪 Cookies loaded")
        return path
    except Exception as e:
        print(f"Cookies error: {e}")
        return None

# ── Download ──────────────────────────────────────────────────────────────────
def download_video(out_path):
    cookies_path = setup_cookies()
    cookie_args = ["--cookies", cookies_path] if cookies_path else []
    base = ["yt-dlp", "--merge-output-format", "mp4", "-o", out_path]

    strategies = [
        base + cookie_args + ["-f", "bestvideo+bestaudio/best", "--impersonate", "chrome", VIDEO_URL],
        base + cookie_args + [VIDEO_URL],
        base + [VIDEO_URL],
    ]

    last_err = ""
    for i, cmd in enumerate(strategies, 1):
        send_progress(f"📥 Download strategy {i}/3...")
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(out_path):
            send_progress(f"✅ Downloaded (strategy {i})")
            return True
        last_err = r.stderr
        print(f"Strategy {i} failed: {last_err[:200]}")
    raise Exception(f"All download strategies failed:\n{last_err[:300]}")

def get_video_title():
    cookies_path = "/tmp/ph_cookies.txt" if os.path.exists("/tmp/ph_cookies.txt") else None
    cookie_args = ["--cookies", cookies_path] if cookies_path else []
    r = subprocess.run(["yt-dlp", "--get-title"] + cookie_args + [VIDEO_URL],
                       capture_output=True, text=True)
    title = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else ""
    desc_r = subprocess.run(["yt-dlp", "--get-description"] + cookie_args + [VIDEO_URL],
                            capture_output=True, text=True)
    description = desc_r.stdout.strip() if desc_r.returncode == 0 else ""
    return title or "Premium Video", description

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    preflight_check()

    raw_video   = "raw_video.mp4"
    final_video = "final_video.mp4"
    thumbnail   = "thumb.jpg"
    screenshots = []
    video_parts = []

    try:
        # ── 1. Download ────────────────────────────────────────────────────────
        download_video(raw_video)
        video_title, video_desc = get_video_title()
        send_progress(f"✅ Downloaded: {video_title}")

        # ── 2. Video info ──────────────────────────────────────────────────────
        duration, width, height = get_video_info(raw_video)
        size_mb = os.path.getsize(raw_video) / 1024 / 1024

        # ── 3. Smart auto-detection ────────────────────────────────────────────
        num_photos = smart_num_photos(duration)
        post_mode  = smart_post_mode(size_mb, duration)
        send_progress(f"📊 Auto-config: {num_photos} photos, mode={post_mode}, dur={duration//60}min, {size_mb:.0f}MB")

        # ── 4. AI Caption ─────────────────────────────────────────────────────
        send_progress("🤖 Generating AI captions...")
        if PHOTO_CAPTION == "auto":
            photo_caption = generate_caption(video_title, video_desc, "photo") or f"📸 {video_title}"
        else:
            photo_caption = PHOTO_CAPTION.replace("{title}", video_title)

        if VIDEO_CAPTION == "auto":
            video_caption = generate_caption(video_title, video_desc, "video") or f"🎬 {video_title}"
        else:
            video_caption = VIDEO_CAPTION.replace("{title}", video_title)

        # ── 5. Screenshots ─────────────────────────────────────────────────────
        send_progress(f"📸 Capturing {num_photos} screenshots...")
        if duration > 0:
            capture_screenshot(raw_video, duration * 0.08, thumbnail)
            for i in range(1, num_photos + 1):
                pos = (duration / (num_photos + 1)) * i
                path = f"screenshot_{i}.jpg"
                if capture_screenshot(raw_video, pos, path):
                    screenshots.append(path)
        send_progress(f"✅ {len(screenshots)} screenshots ready")

        # ── 6. Process (watermark) ─────────────────────────────────────────────
        if SKIP_WATERMARK:
            send_progress("⏭️ Watermark skipped (V5)")
            final_video = raw_video
        else:
            send_progress("⚙️ Applying watermark...")
            try:
                ref_h    = height if height > 0 else 720
                bar_h    = max(40, int(ref_h * 0.07))
                font_sz  = max(18, int(bar_h * 0.55))
                text_y   = max(4, (bar_h - font_sz) // 2)

                scale_filter = ""
                if height >= 1080: scale_filter = ",scale=-2:1080"
                elif height >= 720: scale_filter = ",scale=-2:720"
                else: scale_filter = ",scale=trunc(iw/2)*2:trunc(ih/2)*2"

                bar_filter  = f"drawbox=x=0:y=0:w=iw:h={bar_h}:color=black@0.88:t=fill"
                text_filter = (
                    f"drawtext=text='    KYAWGYI FAMILYS    ':"
                    f"x=w-mod(t*55\\,w+tw):y={text_y}:"
                    f"fontsize={font_sz}:fontcolor=white@0.95"
                )
                vf = f"{bar_filter},{text_filter}{scale_filter}"

                subprocess.run([
                    "ffmpeg", "-y", "-i", raw_video, "-vf", vf,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", final_video
                ], check=True, capture_output=True)
                send_progress("✅ Watermark applied")
            except Exception as e:
                send_progress(f"⚠️ Encode warning: {e} — raw video သုံးမည်")
                final_video = raw_video

        # ── 7. Split if > 2GB ──────────────────────────────────────────────────
        video_parts = [final_video]
        final_size_mb = os.path.getsize(final_video) / 1024 / 1024
        if final_size_mb > MAX_FILE_SIZE_MB:
            send_progress(f"✂️ Splitting ({final_size_mb:.0f}MB)...")
            video_parts = []
            dur, _, _ = get_video_info(final_video)
            n = math.ceil(final_size_mb / MAX_FILE_SIZE_MB)
            part_dur = dur / n
            for i in range(n):
                pf = f"part_{i}.mp4"
                subprocess.run([
                    "ffmpeg", "-y", "-i", final_video,
                    "-ss", str(i * part_dur), "-t", str(part_dur),
                    "-c", "copy", "-movflags", "+faststart", pf
                ], check=True, capture_output=True)
                video_parts.append(pf)
            send_progress(f"✅ Split into {n} parts")

        # ── 8. Upload via Telethon ────────────────────────────────────────────
        send_progress("🚀 Connecting to Telegram...")
        client = TelegramClient("bot_session", int(API_ID), API_HASH,
                                connection_retries=None, request_retries=5)
        await client.start(bot_token=BOT_TOKEN)

        async with client:
            thumb = thumbnail if os.path.exists(thumbnail) else None

            def album_caps(files, last_cap):
                return [""] * (len(files) - 1) + [last_cap]

            send_progress(f"📤 Uploading (mode={post_mode})...")

            if post_mode == "album":
                if screenshots:
                    caps = album_caps(screenshots, f"📸 **{video_title}**\n\n{photo_caption}")
                    await client.send_file(CHANNEL_ID, screenshots, caption=caps, parse_mode="markdown")
                    send_progress("✅ Photos uploaded")

            elif post_mode == "both":
                if screenshots:
                    caps = album_caps(screenshots, f"📸 **{video_title}**\n\n{photo_caption}")
                    await client.send_file(CHANNEL_ID, screenshots, caption=caps, parse_mode="markdown")
                    send_progress("✅ Photos uploaded")

                for i, part in enumerate(video_parts):
                    dur_p, w_p, h_p = get_video_info(part)
                    suffix = f" (Part {i+1}/{len(video_parts)})" if len(video_parts) > 1 else ""
                    cap = f"🎬 **{video_title}{suffix}**\n\n{video_caption}"
                    await client.send_file(
                        CHANNEL_ID, part, caption=cap, parse_mode="markdown",
                        thumb=thumb if i == 0 else None,
                        supports_streaming=True,
                        attributes=[types.DocumentAttributeVideo(
                            duration=dur_p, w=w_p, h=h_p, supports_streaming=True)]
                    )
                    send_progress(f"✅ Video part {i+1}/{len(video_parts)} uploaded")

            elif post_mode == "video":
                all_files = screenshots + [video_parts[0]]
                caps = album_caps(all_files, f"🎬 **{video_title}**\n\n{video_caption}")
                await client.send_file(CHANNEL_ID, all_files, caption=caps,
                                       parse_mode="markdown", supports_streaming=True)
                send_progress("✅ Combined group uploaded")
                for i, part in enumerate(video_parts[1:], 2):
                    dur2, w2, h2 = get_video_info(part)
                    await client.send_file(
                        CHANNEL_ID, part,
                        caption=f"🎬 **{video_title} (Part {i}/{len(video_parts)})**\n\n{video_caption}",
                        parse_mode="markdown", supports_streaming=True,
                        attributes=[types.DocumentAttributeVideo(
                            duration=dur2, w=w2, h=h2, supports_streaming=True)]
                    )
                    send_progress(f"✅ Part {i} uploaded")

        send_progress("🎉 Task Completed Successfully!")

        # Notify user via bot
        if CHAT_ID and BOT_TOKEN:
            send_tg("sendMessage", {
                "chat_id": CHAT_ID, "parse_mode": "Markdown",
                "text": (
                    f"🎉 *Upload Complete!*\n\n"
                    f"🎬 `{video_title}`\n"
                    f"📊 Photos: `{len(screenshots)}` | Parts: `{len(video_parts)}`\n"
                    f"📡 Mode: `{post_mode}`\n"
                    f"✅ Channel ထဲ တင်ပြီးပါပြီ!"
                )
            })

    except Exception as e:
        stack = traceback.format_exc()
        err_msg = (
            f"❌ *Upload Failed*\n\n"
            f"**Workflow:** `{WORKFLOW_NAME}`\n"
            f"**Error:** `{type(e).__name__}: {str(e)[:200]}`\n"
            f"**URL:** `{VIDEO_URL[:80]}`"
        )
        send_progress(err_msg)
        if CHAT_ID and BOT_TOKEN:
            send_tg("sendMessage", {"chat_id": CHAT_ID, "text": err_msg, "parse_mode": "Markdown"})
        print(stack)
        sys.exit(1)

    finally:
        cleanup = [raw_video, final_video, thumbnail] + screenshots + video_parts
        for f in cleanup:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

if __name__ == "__main__":
    asyncio.run(main())

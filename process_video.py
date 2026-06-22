import os, sys, asyncio, subprocess, json, requests, traceback, time, math
from telethon import TelegramClient
from telethon.tl import types

# ── Env ───────────────────────────────────────────────────────────────────────
API_ID            = os.environ.get("API_ID","0")
API_HASH          = os.environ.get("API_HASH","")
BOT_TOKEN         = os.environ.get("BOT_TOKEN","")
VIDEO_URL         = os.environ.get("VIDEO_URL","")
TARGET_CHANNEL_ID = os.environ.get("TARGET_CHANNEL_ID","0")
PHOTO_CAPTION     = os.environ.get("PHOTO_CAPTION","auto")
VIDEO_CAPTION     = os.environ.get("VIDEO_CAPTION","auto")
NUM_PHOTOS        = os.environ.get("NUM_PHOTOS","auto")
POST_MODE         = os.environ.get("POST_MODE","auto")
CHAT_ID           = os.environ.get("CHAT_ID","")
WORKER_URL        = os.environ.get("WORKER_URL","").rstrip("/")
WORKFLOW_NAME     = os.environ.get("WORKFLOW_NAME","V1")
TASK_ID           = os.environ.get("TASK_ID","")
CHANNEL_ALIAS     = os.environ.get("CHANNEL_ALIAS","default")
PH_COOKIES_B64    = os.environ.get("PH_COOKIES_B64","")  # fallback only

CF_ACCOUNT_ID     = os.environ.get("CF_ACCOUNT_ID","")
CF_AUTH_EMAIL     = os.environ.get("CF_AUTH_EMAIL","")
CF_AUTH_KEY       = os.environ.get("CF_AUTH_KEY","")
CF_AI_MODEL       = os.environ.get("CF_AI_MODEL","@cf/aisingapore/gemma-sea-lion-v4-27b-it")

SUPABASE_URL      = "https://guotpdwaswaybjiiezax.supabase.co"
SUPABASE_KEY      = os.environ.get("SUPABASE_KEY","")

MAX_FILE_SIZE_MB  = 2000

def parse_channel_id(raw):
    raw = raw.strip()
    try:
        return int(f"-100{raw}") if raw.isdigit() else int(raw)
    except ValueError:
        print(f"❌ Invalid TARGET_CHANNEL_ID: '{raw}'"); sys.exit(1)

CHANNEL_ID = parse_channel_id(TARGET_CHANNEL_ID)

# ── URL Type Detection ────────────────────────────────────────────────────────
def is_direct_url(url):
    import re
    if not url:
        return False
    patterns = [
        r'X-Amz-Signature=',
        r'X-Amz-Algorithm=',
        r'\.backblazeb2\.com',
        r'idrivee2',
        r'\.(mp4|mkv|avi|mov|webm|m4v)(\?|$)',
        r's3\.[a-z0-9-]+\.amazonaws\.com',
    ]
    return any(re.search(p, url, re.IGNORECASE) for p in patterns)

# ── Supabase ──────────────────────────────────────────────────────────────────
def sb_headers():
    return {"apikey":SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}","Content-Type":"application/json","Prefer":"return=minimal"}

def sb_update_task(task_id, status, error_msg=None):
    if not SUPABASE_KEY or not task_id: return
    try:
        body = {"status":status}
        if status in ("completed","failed"): body["end_time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
        if error_msg: body["error_message"] = error_msg[:500]
        requests.patch(f"{SUPABASE_URL}/rest/v1/tasks?task_id=eq.{task_id}",headers=sb_headers(),json=body,timeout=10)
    except Exception as e: print(f"[WARN] sb_update_task:{e}")

def sb_log(task_id, level, message):
    if not SUPABASE_KEY or not task_id: return
    try: requests.post(f"{SUPABASE_URL}/rest/v1/task_logs",headers=sb_headers(),json={"task_id":task_id,"level":level,"message":message[:500]},timeout=10)
    except Exception as e: print(f"[WARN] sb_log:{e}")

# ── AI Prompt from Supabase ───────────────────────────────────────────────────
_prompt_cache = {}

def get_ai_prompt(prompt_type):
    key = f"{CHANNEL_ALIAS}:{prompt_type}"
    if key in _prompt_cache:
        return _prompt_cache[key]
    if SUPABASE_KEY:
        hdrs = {"apikey":SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}"}
        try:
            if CHANNEL_ALIAS and CHANNEL_ALIAS != "default":
                r = requests.get(f"{SUPABASE_URL}/rest/v1/ai_prompts?name=eq.channel:{CHANNEL_ALIAS}:{prompt_type}&limit=1",headers=hdrs,timeout=10)
                rows = r.json() if r.status_code==200 else []
                if rows:
                    tpl = rows[0]["template"]
                    print(f"[INFO] Custom prompt: channel:{CHANNEL_ALIAS}:{prompt_type}")
                    _prompt_cache[key] = tpl
                    return tpl
            r2 = requests.get(f"{SUPABASE_URL}/rest/v1/ai_prompts?name=eq.default_{prompt_type}_caption&limit=1",headers=hdrs,timeout=10)
            rows2 = r2.json() if r2.status_code==200 else []
            if rows2:
                tpl2 = rows2[0]["template"]
                print(f"[INFO] Default prompt: default_{prompt_type}_caption")
                _prompt_cache[key] = tpl2
                return tpl2
        except Exception as e:
            print(f"[WARN] Prompt fetch:{e}")
    return _hardcoded_prompt(prompt_type)

def _hardcoded_prompt(t):
    return ("Video title: {title}\nDescription: {description}\n\n"
            "ဒီ video အတွက် Burmese မှာ ဆွဲဆောင်မှုရှိတဲ့ photo caption တစ်ကြောင်း ရေးပေး။ 2-3 lines, emoji, hashtag 2-3 ခု ထည့်ပေး။ Caption ကိုပဲ ထုတ်ပေး။"
            if t=="photo" else
            "Video title: {title}\nDescription: {description}\n\n"
            "ဒီ video အတွက် Burmese မှာ viral video caption တစ်ကြောင်း ရေးပေး။ Curious tone, 3-4 lines, emoji, hashtag 2-3 ခု ထည့်ပေး။ Caption ကိုပဲ ထုတ်ပေး။")

# ── Progress Reporter ─────────────────────────────────────────────────────────
def send_progress(text):
    msg = f"[{WORKFLOW_NAME}] {text}"
    print(msg)
    sb_log(TASK_ID,"error" if "❌" in text else "info",text[:400])
    if CHAT_ID and WORKER_URL:
        try:
            requests.post(f"{WORKER_URL}/progress",json={"chat_id":CHAT_ID,"progress_text":msg,"task_id":TASK_ID},timeout=10)
        except Exception as e:
            print(f"[WARN] progress push failed: {e}")

# ── AI Caption ────────────────────────────────────────────────────────────────
def generate_caption(title, description, caption_type="video"):
    if not CF_ACCOUNT_ID or not CF_AUTH_KEY:
        return None
    prompt_template = get_ai_prompt(caption_type)
    user_prompt = prompt_template.replace("{title}",title or "N/A").replace("{description}",(description or "N/A")[:400])
    try:
        r = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_AI_MODEL}",
            headers={"X-Auth-Email":CF_AUTH_EMAIL,"X-Auth-Key":CF_AUTH_KEY,"Content-Type":"application/json"},
            json={"messages":[
                {"role":"system","content":"You are a Burmese social media content writer."},
                {"role":"user","content":user_prompt}
            ]}, timeout=45
        )
        if r.status_code==200:
            result_data = r.json().get("result", {})
            choices = result_data.get("choices", [])
            caption = (choices[0].get("message", {}).get("content", "") if choices else "") or result_data.get("response", "")
            caption = caption.strip() if caption else ""
            if caption: send_progress(f"✅ AI caption ({caption_type})"); return caption
    except Exception as e: send_progress(f"⚠️ AI error:{e}")
    return None

# ── Download ──────────────────────────────────────────────────────────────────
def download_direct(url, out_path):
    send_progress("📥 Direct download (presigned URL)...")
    with requests.get(url, stream=True, timeout=3600) as r:
        r.raise_for_status()
        total = int(r.headers.get('content-length',0))
        downloaded = 0
        last_prog = 0
        with open(out_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8*1024*1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded/total*100
                        if pct - last_prog >= 10:
                            last_prog = pct
                            send_progress(f"📥 Downloading... {pct:.0f}% ({downloaded//1024//1024}MB/{total//1024//1024}MB)")
    if not os.path.exists(out_path) or os.path.getsize(out_path)==0:
        raise Exception("Direct download failed — empty file")
    send_progress(f"✅ Downloaded ({os.path.getsize(out_path)//1024//1024}MB)")

def setup_cookies():
    """
    Cookies fetch priority:
    1. Supabase (bot /updatecookies command မှ သိမ်းထားတာ)
    2. PH_COOKIES_B64 env var (GitHub Secret fallback)
    """
    import base64
    path = "/tmp/ph_cookies.txt"

    # Priority 1: Supabase
    if SUPABASE_KEY:
        try:
            hdrs = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
            r = requests.get(
                f"{SUPABASE_URL}/rest/v1/ai_prompts?name=eq.cookies:ph&limit=1",
                headers=hdrs, timeout=10
            )
            rows = r.json() if r.status_code == 200 else []
            if rows and rows[0].get("template"):
                open(path, "w").write(base64.b64decode(rows[0]["template"]).decode())
                send_progress("🍪 Cookies loaded (Supabase)")
                return path
        except Exception as e:
            print(f"[WARN] Supabase cookies fetch: {e}")

    # Priority 2: env var fallback
    if PH_COOKIES_B64:
        try:
            open(path, "w").write(base64.b64decode(PH_COOKIES_B64).decode())
            send_progress("🍪 Cookies loaded (env fallback)")
            return path
        except Exception as e:
            print(f"[WARN] Cookies env: {e}")

    send_progress("⚠️ No cookies found — age-restricted sites may fail")
    return None

def download_ytdlp(out_path):
    """Download using yt-dlp (nightly) for public sites (YouTube, PH, etc.)"""
    ck = setup_cookies()
    ck_args = ["--cookies", ck] if ck else []
    base = ["yt-dlp","--merge-output-format","mp4","-o",out_path]
    strategies = [
        base+ck_args+["-f","bestvideo+bestaudio/best","--impersonate","chrome",VIDEO_URL],
        base+ck_args+[VIDEO_URL],
        base+[VIDEO_URL],
    ]
    last_err = ""
    for i,cmd in enumerate(strategies,1):
        send_progress(f"📥 yt-dlp strategy {i}/3...")
        r = subprocess.run(cmd,capture_output=True,text=True)
        if r.returncode==0 and os.path.exists(out_path):
            send_progress(f"✅ Downloaded (strategy {i})"); return True
        last_err=r.stderr
        print(f"Strategy {i} failed:{last_err[:150]}")
    raise Exception(f"All yt-dlp strategies failed:\n{last_err[:300]}")

def download_video(out_path):
    if is_direct_url(VIDEO_URL):
        send_progress("🔍 Detected: presigned/direct URL → requests download")
        download_direct(VIDEO_URL, out_path)
    else:
        send_progress("🔍 Detected: public site → yt-dlp (nightly)")
        download_ytdlp(out_path)

def get_video_title():
    if is_direct_url(VIDEO_URL):
        import re
        match = re.search(r'/([^/?#]+\.mp4)', VIDEO_URL, re.IGNORECASE)
        fname = match.group(1) if match else "Video"
        title = fname.replace(".mp4","").replace("_"," ").replace("-"," ").strip()
        return title or "Premium Video", ""
    ck = "/tmp/ph_cookies.txt" if os.path.exists("/tmp/ph_cookies.txt") else None
    ck_args = ["--cookies",ck] if ck else []
    t = subprocess.run(["yt-dlp","--get-title"]+ck_args+[VIDEO_URL],capture_output=True,text=True)
    d = subprocess.run(["yt-dlp","--get-description"]+ck_args+[VIDEO_URL],capture_output=True,text=True)
    return (t.stdout.strip() or "Premium Video"), (d.stdout.strip() or "")

# ── Video Helpers ─────────────────────────────────────────────────────────────
def get_video_info(path):
    try:
        r = subprocess.run(["ffprobe","-v","quiet","-print_format","json","-show_format","-show_streams",path],capture_output=True,text=True)
        data=json.loads(r.stdout)
        dur=int(float(data["format"]["duration"]))
        w=h=0
        for s in data["streams"]:
            if s["codec_type"]=="video": w,h=int(s["width"]),int(s["height"]); break
        return dur,w,h
    except Exception as e: print(f"ffprobe:{e}"); return 0,0,0

def capture_screenshot(path, pos, out):
    try:
        subprocess.run(["ffmpeg","-y","-ss",str(pos),"-i",path,"-frames:v","1","-update","1","-q:v","2",out],check=True,capture_output=True)
        return os.path.exists(out) and os.path.getsize(out)>0
    except Exception as e: print(f"Screenshot@{pos}s:{e}"); return False

def smart_num_photos(duration):
    if NUM_PHOTOS!="auto":
        try: return int(NUM_PHOTOS)
        except: pass
    if duration<300: return 2
    elif duration<900: return 4
    elif duration<1800: return 6
    else: return 8

def smart_post_mode(size_mb, duration):
    if POST_MODE!="auto": return POST_MODE
    return "both" if size_mb>1800 or duration>1200 else "video"

# ── Preflight ─────────────────────────────────────────────────────────────────
def preflight_check():
    missing=[k for k,v in {"API_ID":API_ID,"API_HASH":API_HASH,"BOT_TOKEN":BOT_TOKEN,"VIDEO_URL":VIDEO_URL}.items() if not v or v=="0"]
    if missing: send_progress(f"❌ Missing:{', '.join(missing)}"); sys.exit(1)
    url_type = "presigned/direct" if is_direct_url(VIDEO_URL) else "public/yt-dlp"
    send_progress(f"✅ Pre-flight OK | alias={CHANNEL_ALIAS} | url_type={url_type}")

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    preflight_check()
    sb_update_task(TASK_ID,"running")
    sb_log(TASK_ID,"info",f"Channel Mgr start | WF:{WORKFLOW_NAME} | alias:{CHANNEL_ALIAS}")

    raw_video="raw_video.mp4"; final_video="final_video.mp4"; thumbnail="thumb.jpg"
    screenshots=[]; video_parts=[]

    try:
        # 1. Smart download
        download_video(raw_video)
        video_title, video_desc = get_video_title()
        send_progress(f"✅ Downloaded: {video_title}")

        # 2. Info + auto-config
        dur, width, height = get_video_info(raw_video)
        size_mb = os.path.getsize(raw_video)/1024/1024
        n_photos = smart_num_photos(dur)
        mode     = smart_post_mode(size_mb, dur)
        send_progress(f"📊 {n_photos} photos | mode={mode} | {dur//60}min | {size_mb:.0f}MB")

        # 3. AI captions
        send_progress(f"🤖 AI captions (alias={CHANNEL_ALIAS})...")
        photo_cap = (generate_caption(video_title,video_desc,"photo") or f"📸 {video_title}") if PHOTO_CAPTION=="auto" else PHOTO_CAPTION.replace("{title}",video_title)
        video_cap = (generate_caption(video_title,video_desc,"video") or f"🎬 {video_title}") if VIDEO_CAPTION=="auto" else VIDEO_CAPTION.replace("{title}",video_title)

        # 4. Screenshots
        send_progress(f"📸 Capturing {n_photos} screenshots...")
        if dur>0:
            capture_screenshot(raw_video,dur*0.08,thumbnail)
            for i in range(1,n_photos+1):
                pos=(dur/(n_photos+1))*i; path=f"screenshot_{i}.jpg"
                if capture_screenshot(raw_video,pos,path): screenshots.append(path)
        send_progress(f"✅ {len(screenshots)} screenshots ready")

        # 5. Watermark / encode
        try:
            ref_h=height if height>0 else 720; bar_h=max(40,int(ref_h*0.07)); fs=max(18,int(bar_h*0.55)); ty=max(4,(bar_h-fs)//2)
            sc=",scale=-2:1080" if height>=1080 else (",scale=-2:720" if height>=720 else ",scale=trunc(iw/2)*2:trunc(ih/2)*2")
            vf=(f"drawbox=x=0:y=0:w=iw:h={bar_h}:color=black@0.88:t=fill,"
                f"drawtext=text='    KYAWGYI FAMILYS    ':x=w-mod(t*55\\,w+tw):y={ty}:fontsize={fs}:fontcolor=white@0.95{sc}")
            subprocess.run(["ffmpeg","-y","-i",raw_video,"-vf",vf,"-c:v","libx264","-preset","veryfast","-crf","23","-c:a","aac","-b:a","128k","-pix_fmt","yuv420p","-movflags","+faststart",final_video],check=True,capture_output=True)
            send_progress("✅ Watermark applied")
        except Exception as e:
            send_progress(f"⚠️ Encode skip:{e}"); final_video=raw_video

        # 6. Split if > 2GB
        video_parts=[final_video]
        fsz=os.path.getsize(final_video)/1024/1024
        if fsz>MAX_FILE_SIZE_MB:
            send_progress(f"✂️ Splitting ({fsz:.0f}MB)...")
            video_parts=[]; d2,_,_=get_video_info(final_video); n=math.ceil(fsz/MAX_FILE_SIZE_MB); pd=d2/n
            for i in range(n):
                pf=f"part_{i}.mp4"
                subprocess.run(["ffmpeg","-y","-i",final_video,"-ss",str(i*pd),"-t",str(pd),"-c","copy","-movflags","+faststart",pf],check=True,capture_output=True)
                video_parts.append(pf)
            send_progress(f"✅ Split {n} parts")

        # 7. Telethon upload
        send_progress("🚀 Connecting Telegram...")
        client=TelegramClient("bot_session",int(API_ID),API_HASH,connection_retries=None,request_retries=5)
        await client.start(bot_token=BOT_TOKEN)

        async with client:
            thumb=thumbnail if os.path.exists(thumbnail) else None
            def ac(files,last): return [""]*( len(files)-1)+[last]

            send_progress(f"📤 Upload mode={mode}...")

            if mode=="album":
                if screenshots:
                    await client.send_file(CHANNEL_ID,screenshots,caption=ac(screenshots,f"📸 **{video_title}**\n\n{photo_cap}"),parse_mode="markdown")
                    send_progress("✅ Photos uploaded")

            elif mode=="both":
                if screenshots:
                    await client.send_file(CHANNEL_ID,screenshots,caption=ac(screenshots,f"📸 **{video_title}**\n\n{photo_cap}"),parse_mode="markdown")
                    send_progress("✅ Photos uploaded")
                for i,part in enumerate(video_parts,1):
                    dp,wp,hp=get_video_info(part); sfx=f" (Part {i}/{len(video_parts)})" if len(video_parts)>1 else ""
                    await client.send_file(CHANNEL_ID,part,
                        caption=f"🎬 **{video_title}{sfx}**\n\n{video_cap}" if i==1 else f"🎬 **{video_title}{sfx}**",
                        parse_mode="markdown",thumb=thumb if i==1 else None,supports_streaming=True,
                        attributes=[types.DocumentAttributeVideo(duration=dp,w=wp,h=hp,supports_streaming=True)])
                    send_progress(f"✅ Video part {i}/{len(video_parts)}")

            elif mode=="video":
                if screenshots:
                    await client.send_file(CHANNEL_ID,screenshots,caption=ac(screenshots,f"📸 **{video_title}**\n\n{photo_cap}"),parse_mode="markdown")
                    send_progress("✅ Photos uploaded")
                for i,part in enumerate(video_parts,1):
                    dp,wp,hp=get_video_info(part); sfx=f" (Part {i}/{len(video_parts)})" if len(video_parts)>1 else ""
                    await client.send_file(CHANNEL_ID,part,
                        caption=f"🎬 **{video_title}{sfx}**\n\n{video_cap}" if i==1 else f"🎬 **{video_title}{sfx}**",
                        parse_mode="markdown",thumb=thumb if i==1 else None,supports_streaming=True,
                        attributes=[types.DocumentAttributeVideo(duration=dp,w=wp,h=hp,supports_streaming=True)])
                    send_progress(f"✅ Video part {i}/{len(video_parts)}")

        send_progress("🎉 All done!")
        sb_update_task(TASK_ID,"completed")

    except Exception as e:
        send_progress(f"❌ Error:{str(e)[:200]}")
        print(f"[ERROR]{traceback.format_exc()}")
        sb_update_task(TASK_ID,"failed",str(e)[:300])
    finally:
        for f in [raw_video, final_video if final_video!=raw_video else None, thumbnail]+screenshots+video_parts:
            if f and os.path.exists(f):
                try: os.remove(f)
                except: pass

if __name__=="__main__": asyncio.run(main())

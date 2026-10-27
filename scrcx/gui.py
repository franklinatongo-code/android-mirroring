import html
import json
import os
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .adb import adb as adb_cmd, adb_cmd as _run_adb, get_server_jar

PORT = int(os.environ.get("SCRCPX_GUI_PORT", "8787"))
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(BASE, "bin", "scrcx")
TMP = "/tmp/opencode"
STATE = os.path.expanduser("~/.local/state/scrcx")
GB = {"gest_file": os.path.join(TMP, "gui-gest.json"), "rec": [], "audio": [], "camera": [], "last_words": {}}

CSS = """
:root{--bg:#0d1321;--bg2:#151d2f;--fg:#e6edf3;--acc:#4cc2ff}
*{box-sizing:border-box;font-family:system-ui,sans-serif}
body{background:var(--bg);color:var(--fg);margin:0;padding:18px}
h1{font-size:22px;margin:2px 0}h2{color:var(--acc);font-size:16px;margin:18px 0 8px}
.card{background:var(--bg2);border:1px solid #263148;border-radius:10px;padding:14px;margin:10px 0}
button{background:#21283b;border:1px solid #33415f;color:var(--fg);padding:7px 13px;border-radius:8px;cursor:pointer;font-size:14px;margin:2px}
button:hover{border-color:var(--acc)}.p{background:#1a4976;border-color:#2f6db1}.gr{background:#14432a;border-color:#2ea043}.rd{background:#4a1d24;border-color:#a53a44}
input,select{background:#0b1120;border:1px solid #33415f;color:var(--fg);padding:7px 10px;border-radius:8px;font-size:14px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(350px,1fr));gap:12px}
small{color:#9db1c5}.phimg{width:100%;border-radius:10px;border:1px solid #33415f;display:block}
.wbtn{font-size:12px;padding:3px 9px}
#log{white-space:pre-wrap;font-family:monospace;font-size:12px;color:#9db1c5;max-height:150px;overflow:auto;background:#0b1120;padding:8px;border-radius:8px}
.head{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.tag{font-size:11px;padding:2px 8px;border-radius:20px;border:1px solid #33415f}.tag.ok{color:#7ee787}.tag.no{color:#ff7b72}
"""

JS = """
async function api(op, extra={}){
  const body = Object.assign({}, extra, {op});
  if (typeof SERIAL !== 'undefined') body.serial = SERIAL;
  const r = await fetch('/api', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j = await r.json(); glog(op+' → '+JSON.stringify(j).slice(0,260)); return j;
}
function glog(s){ const el=document.getElementById('log'); el.textContent=(s+'\\n'+el.textContent).slice(0,2500); }
function ipath(id){ return document.getElementById(id).value; }
let MODE='tap';
function pick(m){ MODE=m; document.getElementById('pickmode').innerText='mode: '+m; }
function phClick(ev){
  const img=document.getElementById('ph');
  const x=Math.round(ev.offsetX/img.clientWidth*img.naturalWidth);
  const y=Math.round(ev.offsetY/img.clientHeight*img.naturalHeight);
  if(MODE==='swipe2'){ api('swipe',{x1:img.dataset.x1,y1:img.dataset.y1,x2:x,y2:y,ms:250}); pick('tap'); return; }
  if(MODE==='swipe1'){ img.dataset.x1=x; img.dataset.y1=y; pick('swipe2'); glog('A picked ('+x+','+y+') — pick B'); return; }
  api(MODE,{x:x,y:y});
}
function snap(){ document.getElementById('ph').src='/snap/'+SERIAL+'?t='+Date.now(); }
function ringTime(){ const n=+document.getElementById('ringN').value||15;
  const im=document.getElementById('ringimg'); im.src='/ring/'+SERIAL+'?ago='+n+'&r='+Date.now(); im.style.display='block'; }
function appWindow(){ const pkg=document.getElementById('pkg').value;
  api('app_window',{pkg:pkg,max:document.getElementById('wmax').checked,size:document.getElementById('wsizechk').checked?document.getElementById('wsizev').value:null,title:document.getElementById('wtitle').value}); }
function refreshPkgs(){ api('app_list',{}).then(j=>{ if(j.packages){ const sel=document.getElementById('pkg'); sel.innerHTML='';
  (j.packages||[]).slice(0,2000).forEach(p=>{const o=document.createElement('option');o.value=p;o.text=p;sel.appendChild(o);}); } }); }
"""

HEAD = f"""<!doctype html><html><head><meta charset='utf-8'><title>scrcpy-x control center</title><style>{CSS}</style></head><body>"""


def page(body, script=""):
    return HEAD + body + f"<pre id='log'>ready.</pre><script>{script}</script></body></html>"


def read_state(serial):
    try:
        return json.load(open(f"{STATE}/{serial}.json"))
    except Exception:
        return {}


def daemon_port(serial):
    return read_state(serial).get("api_port")


def png_bytes(serial):
    return adb_cmd("-s", serial, "exec-out", "screencap", "-p", binary=True)


def png_dims(png):
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def api_call(serial, payload, timeout=20):
    port = daemon_port(serial)
    if not port:
        return {"ok": False, "error": "no session"}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_cli(args_list, timeout=90):
    try:
        res = subprocess.run([EXE] + args_list, capture_output=True, text=True, timeout=timeout)
        return {"ok": res.returncode == 0, "out": (res.stdout or res.stderr).strip()[:1500]}
    except subprocess.TimeoutExpired:
        return {"ok": True, "out": "left running (timeout reached = still active)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def spawn(cmd, logname):
    os.makedirs(TMP, exist_ok=True)
    logf = open(os.path.join(TMP, logname), "wb")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    return proc.pid


def ocr_words(png):
    w, h = png_dims(png)
    tmp_png = os.path.join(TMP, f"ocr-{int(time.time())}.png")
    open(tmp_png, "wb").write(png)
    res = subprocess.run(["tesseract", tmp_png, "-", "--psm", "11", "tsv", "-l", "eng"], capture_output=True, text=True, timeout=60)
    words = []
    for line in res.stdout.splitlines()[1:]:
        c = line.split("\t")
        if len(c) >= 12 and c[11].strip() and float(c[10] or 0) > 55:
            words.append({"t": c[11], "x": int(c[6]) + int(c[8]) // 2, "y": int(c[7]) + int(c[9]) // 2})
    try:
        os.unlink(tmp_png)
    except OSError:
        pass
    return w, h, words[:40]


KEYMAP = {"home": 3, "back": 4, "power": 26, "vol-up": 24, "vol-down": 25, "menu": 82, "tab": 61, "enter": 66, "rotate": "rotate"}


def scale_tap(serial, x, y):
    port = daemon_port(serial)
    if not port:
        return ("adb", x, y)
    info = api_call(serial, {"op": "info"}).get("info", {})
    screen = info.get("screen") or []
    if not screen or not screen[0] or not x or not y:
        return ("adb", x, y)
    try:
        png = png_bytes(serial)
        pw, _ = png_dims(png)
        f = screen[0] / pw
        return ("daemon", int(x * f), int(y * f))
    except Exception:
        return ("adb", x, y)


def dispatch(p):
    op = p.get("op")
    serial = p.get("serial") or first_serial()
    try:
        if op == "devices":
            out = [{"serial": s, "state": st} for s, st in
                   [l.split()[:2] for l in adb_cmd("devices").splitlines()[1:] if len(l.split()) >= 2]]
            return {"ok": True, "devices": out}
        if op == "pair":
            return run_cli(["pair", p["ip"], p["code"]])
        if op == "wifi_connect":
            res = subprocess.run(["adb", "connect", p["ip"]], capture_output=True, text=True, timeout=30)
            return {"ok": "connected" in (res.stdout + res.stderr), "out": (res.stdout + res.stderr).strip()}
        if op == "session_connect":
            return run_cli(["--serial", serial, "connect"])
        if op == "session_disconnect":
            return run_cli(["--serial", serial, "disconnect"])
        if op in ("tap", "long"):
            how, sx, sy = scale_tap(serial, p["x"], p["y"])
            if how == "daemon":
                payload = {"op": "tap", "x": sx, "y": sy}
                if op == "long":
                    payload["op"] = "long_press"
                r = api_call(serial, payload)
                return {"ok": r.get("ok"), "at": [sx, sy]}
            cmd = ["input", "swipe" if op == "long" else "tap" if False else "tap"] if op == "tap" else ["input", "swipe", str(sx), str(sy), str(sx), str(sy), "500"]
            if op == "tap":
                res = subprocess.run(["adb", "-s", serial, "shell", "input", "tap", str(sx), str(sy)], capture_output=True, timeout=30)
                return {"ok": res.returncode == 0, "at": [sx, sy]}
            res = subprocess.run(["adb", "-s", serial, "shell", *cmd], capture_output=True, timeout=30)
            return {"ok": res.returncode == 0, "at": [sx, sy]}
        if op == "swipe":
            how, sx, sy = scale_tap(serial, p["x1"], p["y1"])
            if how == "daemon":
                r = api_call(serial, {"op": "tap"})
            one = {"x1": p["x1"], "y1": p["y1"], "x2": p["x2"], "y2": p["y2"]}
            if daemon_port(serial):
                f = None
            info = api_call(serial, {"op": "info"}).get("info", {})
            scr = (info.get("screen") or [0])[0] or 0
            if scr:
                try:
                    pw, _ = png_dims(png_bytes(serial))
                    f = scr / pw
                    one = {k: int(v * f) for k, v in one.items()}
                except Exception:
                    pass
            if daemon_port(serial):
                r = api_call(serial, {"op": "swipe", **one, "ms": p.get("ms", 250)})
                return {"ok": r.get("ok"), **one}
            subprocess.run(["adb", "-s", serial, "shell", "input", "swipe", str(one["x1"]), str(one["y1"]), str(one["x2"]), str(one["y2"]), str(p.get("ms", 250))], capture_output=True, timeout=30)
            return {"ok": True, **one}
        if op == "key":
            k = p.get("k")
            if k == "rotate":
                return run_cli(["--serial", serial, "--nomsg", "--rotate"]) if False else run_cli(["action", k, "--serial", serial]) if False else run_cli(["api", "--post", '{"op":"rotate"}'])
            code = KEYMAP.get(k)
            if daemon_port(serial):
                r = api_call(serial, {"op": "key", "key": k})
                return r
            if isinstance(code, int):
                subprocess.run(["adb", "-s", serial, "shell", "input", "keyevent", str(code)], capture_output=True, timeout=30)
                return {"ok": True, "key": k}
            return {"ok": False, "error": f"unknown key {k} (needs session)"}
        if op == "text":
            if daemon_port(serial):
                return api_call(serial, {"op": "text", "text": p.get("t", "")})
            subprocess.run(["adb", "-s", serial, "shell", "input", "text", '"' + p.get("t", "") + '"'], capture_output=True, timeout=30)
            return {"ok": True}
        if op == "app_list":
            res = adb_cmd("-s", serial, "shell", "pm", "list", "packages")
            pkgs = sorted(l.replace("package:", "") for l in res.split())
            return {"ok": True, "packages": pkgs}
        if op == "app_window":
            args = ["app-window", p["pkg"]]
            if p.get("title"):
                args += ["--title", p["title"]]
            if p.get("max"):
                args.append("--maximize")
            if p.get("size"):
                args += ["--size", p["size"]]
            return run_cli(["--serial", serial, *args])
        if op == "app_start":
            return api_call(serial, {"op": "app_start", "package": p["pkg"]}) if daemon_port(serial) else run_cli(["app", "start", p["pkg"], "--serial", serial])
        if op == "app_stop":
            return api_call(serial, {"op": "app_stop", "package": p["pkg"]}) if daemon_port(serial) else run_cli(["app", "stop", p["pkg"], "--serial", serial])
        if op == "gesture":
            if p.get("start"):
                GB["gest_file"] = os.path.join(TMP, f"gest-{int(time.time())}.json")
                r = run_cli(["--serial", serial, "record", "start", GB["gest_file"]])
                r["file"] = GB["gest_file"]
                return r
            return run_cli(["--serial", serial, "record", "stop"])
        if op == "gesture_replay":
            return run_cli(["--serial", serial, "replay", GB["gest_file"]])
        if op == "rec_video":
            if p.get("on"):
                return run_cli(["record-video", os.path.join(TMP, f"rec-{int(time.time())}.mp4", ), "--time", "30", "--serial", serial, "--max-size", "720"]) if False else run_cli(["record-video", f"/tmp/opencode/guirec-{int(time.time())}.mp4", "--time", "30", "--serial", serial])
            return run_cli(["api", "--post", '{"op":"noop"}'])
        if op == "audio_play":
            if p.get("on"):
                if not GB["audio"]:
                    af = ["--audio-codec=aac", "--audio-encoder=OMX.google.aac.encoder"]
                    try:
                        with open(os.path.join(STATE, "audio-%s.json" % serial)) as fh:
                            cached = json.load(fh).get("flags") or []
                        if cached and "--no-audio" not in cached:
                            af = cached
                    except Exception:
                        pass
                    GB["audio"].append(spawn([os.path.join(BASE, "..", "scrcpy", "build-test", "app", "scrcpy") if os.path.exists(os.path.join(BASE, "..", "scrcpy", "build-test", "app", "scrcpy")) else "scrcpy", "-s", serial, "--no-window", "--no-video"] + af, "gui-audio.log"))
                return {"ok": True, "pid": GB["audio"][0]}
            for pid in GB["audio"]:
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
            GB["audio"].clear()
            return {"ok": True}
        if op == "audio_capture":
            mic = p.get("type") == "mic"
            out = os.path.join(TMP, f"recudio-{int(time.time())}.m4a")
            r = run_cli(["audio-capture", out, "--time", "15", "--serial", serial] + (["--mic"] if mic else []))
            r["file"] = out
            return r
        if op == "camera":
            return run_cli(["camera", "--facing", p.get("facing", "back"), "--serial", serial])
        if op == "push":
            r = subprocess.run(["adb", "-s", serial, "push", p["path"], "/sdcard/Download/"], capture_output=True, text=True, timeout=300)
            return {"ok": r.returncode == 0, "out": (r.stdout + r.stderr).strip()[:300]}
        if op == "pull":
            dst = os.path.join(TMP, os.path.basename(p["path"]) or "pull")
            r = subprocess.run(["adb", "-s", serial, "pull", p["path"], dst], capture_output=True, text=True, timeout=300)
            return {"ok": r.returncode == 0, "out": dst, "log": (r.stdout + r.stderr).strip()[:300]}
        if op == "install":
            r = subprocess.run(["adb", "-s", serial, "install", p["path"]], capture_output=True, text=True, timeout=600)
            return {"ok": "Success" in (r.stdout + r.stderr), "out": (r.stdout + r.stderr).strip()[:300]}
        if op == "clip_get":
            return run_cli(["clip", "get", "--serial", serial])
        if op == "clip_set":
            return run_cli(["clip", "set", p.get("t", ""), "--serial", serial])
        if op == "hud":
            return api_call(serial, {"op": "hud"}) if daemon_port(serial) else run_cli(["hud", "--serial", serial])
        if op == "see_words":
            png = png_bytes(serial)
            w, h, words = ocr_words(png)
            GB["last_words"][serial] = words
            return {"ok": True, "png": [w, h], "words": words}
        if op == "status":
            st = read_state(serial)
            return {"ok": True, "state": st}
        return {"ok": False, "error": f"unknown op {op}"}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def first_serial():
    for line in adb_cmd("devices").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return parts[0]
    return None


def home():
    cards = []
    try:
        for line in adb_cmd("devices").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                serial, state = parts[0], parts[1]
                conn = "ok" if state == "device" else "no"
                cards.append(f"""<div class='card'><h2>📱 {html.escape(serial)} <span class='tag {conn}'>{state}</span></h2>
<div class='row'><a href='/dev/{serial}'><button class='p'>Open control panel</button></a></div></div>""")
    except Exception as e:
        cards = [f"<div class='card'>adb error: {e}</div>"]
    body = f"""
<h1>scrcpy-x — phones</h1>
<div class='grid'>{''.join(cards) or "<div class='card'>No devices. Plug in phone (USB) or pair over Wi-Fi below.</div>"}</div>
<div class='card'><h2>Pair over Wi-Fi</h2>
<ol><li>Phone: Settings → Developer options → enable <b>Wireless debugging</b></li>
<li>Tap <b>Pair device with code</b> — note the IP:port and the 6-digit code</li></ol>
<div class='row'><input id='pip' placeholder='192.168.0.x:port' style='width:150px'>
<input id='pcode' placeholder='6-digit code' style='width:110px'>
<button class='p' onclick="pair()">Pair</button></div>
<hr>
<div class='row'><input id='wip' placeholder='192.168.0.x:5555' style='width:150px'>
<button onclick="wific()">Connect (after pairing)</button></div></div>
"""
    script = """
async function pair(){ await fetch('/api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({});
""";
    return page(body, """
async function pair(){
  await api('pair', {ip: ipath('pip'), code: ipath('pcode')}); }
async function wific(){
  await api('wifi_connect', {ip: ipath('wip')}); location.reload(); }
""") + page("", "") if False else page(body, """
async function pair(){ await api('pair', {ip: ipath('pip'), code: ipath('pcode')}); }
async function wific(){ await api('wifi_connect', {ip: ipath('wip')}); location.reload(); }
""")


def dev(serial):
    port = daemon_port(serial)
    live = bool(port)
    body = f"""
<div class='head'><h1>scrcpy-x — {html.escape(serial)}</h1>
<div><span class='tag {"ok" if live else "no"}'>{"session live" if live else "no session"}</span>
<a href='/'><button>← devices</button></a>/<button onclick="sessionToggle()">{("disconnect" if live else "connect session")}</button></div></div>
<div class='grid'>
<div class='card'><h2>Click to control</h2>
<div class='row'><button class='p' onclick="pick('tap')">tap</button>
<button onclick="pick('long')">long-press</button>
<button onclick="pick('swipe1')">swipe A→B</button>
<b id='pickmode'>mode: tap</b></div>
<img id='ph' class='phimg' src='/snap/{serial}' onclick='phClick(event)'>
<div class='row'><button onclick="snap()">📷 refresh</button><button onclick="seeText()">🔤 find text</button></div>
<div id='ocrbtns'></div>
<input id='typebox' placeholder='type & send' style='width:120px'>
<button onclick="api('text',{{t:document.getElementById('typebox').value}})">send</button>
</div>
<div class='card'><h2>Buttons & rewind</h2>
<button onclick="api('key',{{k:'home'}})">⌂ home</button><button onclick="api('key',{{k:'back'}})">◀ back</button>
<button onclick="api('key',{{k:'power'}})">⏻ power</button><button onclick="api('key',{{k:'vol-up'}})">🔊 up</button>
<button onclick="api('key',{{k:'vol-down'}})">🔉 dn</button><button onclick="api('key',{{k:'rotate'}})">⟳ rot</button>
<hr>
<small>rewind buffer</small>
<div class='row'><input id='ringN' value='15' size='4'><button onclick="ringTime()">⏪ show N sec ago</button>
</div><img id='ringimg' class='phimg' style='display:none'>
</div>
<div class='card'><h2>Apps → desktop windows</h2>
<div class='row'><select id='pkg' style='min-width:230px'></select><button class='p' onclick="appWindow()">🪟 open window</button></div>
<div class='row'><label><input type='checkbox' id='wmax'>maximize</label>
<label><input type='checkbox' id='wsizechk'>fixed <input id='wsizev' size='9' value='1080x2340'></label>
<input id='wtitle' size='10' placeholder='title'></div>
<div class='row'><button onclick="refreshPkgs()">🔄 app list</button>
<input id='astart' size='16' placeholder='com.pkg'>
<button onclick="api('app_start',{{pkg:ipath('astart')}})">▶ start on phone</button>
<button onclick="api('app_stop',{{pkg:ipath('astart')}})">■ stop</button></div>
</div>
<div class='card'><h2>Media & record</h2>
<div class='row'>
<button class='gr' onclick="git()">● gesture ●NOQNA</button>
<button class='rd' onclick="gstop()">■ gest stop</button>
<button onclick="greplay()">▶ replay</button>
<button onclick="api('rec_video',{{on:true}})">🎥 screen rec 30s</button>
</div>
<hr>
<div class='row'>
<button class='p' onclick="api('audio_play',{{on:true}})">🔊 phone sound on PC</button>
<button class='rd' onclick="api('audio_play',{{on:false}})">■ audio off</button>
<button onclick="api('audio_capture',{{type:'out'}})">🎙 out→file</button>
<button onclick="api('audio_capture',{{type:'mic'}})">🎙 mic→file</button>
<button onclick="api('camera',{{facing:'back'}})">📷 back cam</button>
<button onclick="api('camera',{{facing:'front'}})">🤳 front cam</button>
</div></div>
<div class='card'><h2>Files, apk, clipboard</h2>
<div class='row'><input id='f1' size='14' placeholder='local path'><button onclick="api('push',{{path:ipath('f1')}})">⬆ push</button></div>
<div class='row'><input id='f2' size='14' placeholder='/sdcard/…'><button onclick="api('pull',{{path:ipath('f2')}})">⬇ pull</button></div>
<div class='row'><input id='apk' size='14' placeholder='/path/app.apk'><button onclick="api('install',{{path:ipath('apk')}})">📦 install</button></div>
<div class='row'><input id='cl' size='14' placeholder='text'><button onclick="api('clip_set',{{t:ipath('cl')}})">📋 set clip</button><button onclick="api('clip_get',{{}})">read clip</button></div>
</div>
{'<div class="card"><h2>Watch (live all-access page)</h2><div class="row"><a href="http://127.0.0.1:'+str(port or "")+'/stream" target="_blank"><button>Open stream</button></a><small>works only while session connected</small></div></div>' if live else ""}
</div>"""
    script = JS + f"const SERIAL={json.dumps(serial)};" + """
async function seeText(){
  const j = await api('see_words',{});
  const host=document.getElementById('ocrbtns'); host.innerHTML='';
  (j.words||[]).forEach(w=>{ const b=document.createElement('button'); b.className='wbtn'; b.textContent=w.t;
    b.onclick=()=>api('tap',{x:w.x,y:w.y}); host.appendChild(b); }); }
async function sessionToggle(){ await api(liveSession? 'session_disconnect':'session_connect',{}); location.reload(); }
const liveSession = """ + json.dumps(bool(live)) + """;
function git(){ api('gesture',{start:true}); }
function gstop(){ api('gesture',{start:false}); }
function greplay(){ api('gesture_replay',{}); }
"""
    return page(body, script)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _data(self, data, ctype="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/":
                self._data(home().encode(), "text/html; charset=utf-8")
            elif path.startswith("/dev/"):
                self._data(dev(path.split("/dev/")[1]).encode(), "text/html; charset=utf-8")
            elif path.startswith("/snap/"):
                self._data(png_bytes(path[6:]), "image/png")
            elif path.startswith("/ring/"):
                serial = path.split("/")[2]
                args = dict(p.split("=") for p in self.path.split("?")[1].split("&")) if "?" in self.path else {}
                port = daemon_port(serial)
                data = None
                if port:
                    try:
                        data = urllib.request.urlopen(f"http://127.0.0.1:{port}/ring?ago={args.get('ago','15')}", timeout=10).read()
                    except Exception:
                        data = None
                self._data(data if data else png_bytes(serial), "image/jpeg" if data else "image/png")
            else:
                self._data(b'{"ok":false,"error":"nf"}')
        except BrokenPipeError:
            pass
        except Exception as e:
            self._data(json.dumps({"ok": False, "error": str(e)}).encode())

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n).decode() if n else "{}"
        try:
            p = json.loads(body)
        except Exception:
            p = {}
        res = dispatch(p)
        self._data(json.dumps(res).encode())


def run_gui(host="127.0.0.1"):
    srv = ThreadingHTTPServer((host, PORT), H)
    print(f"scrcpy-x GUI: http://{host}:{PORT}")
    srv.serve_forever()

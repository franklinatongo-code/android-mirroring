import io
import json
import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .adb import adb as adb_cmd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(BASE, "bin", "scrcx")
STATE = os.path.expanduser("~/.local/state/scrcx")
TMP = "/tmp/opencode"

BG = "#0d1321"
BG2 = "#151d2f"
FG = "#e6edf3"
ACC = "#4cc2ff"

SNAP_CACHE = {"png": b"", "time": 0}


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


def scale_xy(serial, px, py):
    info = {}
    port = daemon_port(serial)
    if port:
        import urllib.request
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/info", timeout=6)
            info = json.loads(r.read().decode())
        except Exception:
            info = {}
    screen = (info.get("screen") or [0, 0])
    try:
        png = SNAP_CACHE["png"] or png_bytes(serial)
        pw, ph = png_dims(png)
        if screen[0] and pw:
            f = screen[0] / pw
            return int(px * f), int(py * f)
    except Exception:
        pass
    return px, py


def cli(*args, timeout=90):
    try:
        res = subprocess.run([EXE, *args], capture_output=True, text=True, timeout=timeout)
        return res.stdout or res.stderr
    except subprocess.TimeoutExpired:
        return "(running in background)"
    except Exception as e:
        return f"error: {e}"


def api_call(serial, payload):
    import urllib.request
    port = daemon_port(serial)
    if not port:
        return {"ok": False, "error": "no session"}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def ocr_words(png):
    tmp = os.path.join(TMP, f"ocr-gui-{int(time.time())}.png")
    open(tmp, "wb").write(png)
    res = subprocess.run(["tesseract", tmp, "-", "--psm", "11", "tsv", "-l", "eng"], capture_output=True, text=True, timeout=60)
    words = []
    for line in res.stdout.splitlines()[1:]:
        c = line.split("\t")
        if len(c) >= 12 and c[11].strip() and float(c[10] or 0) > 55:
            words.append({"t": c[11], "x": int(c[6]) + int(c[8]) // 2, "y": int(c[7]) + int(c[9]) // 2})
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return words


class App:
    def __init__(self, root):
        self.root = root
        root.title("scrcpy-x — phone control center")
        root.configure(bg=BG)
        root.geometry("400x760")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=BG2, background=BG2, foreground=FG)
        style.configure("TSpin", fieldbackground=BG2, background=BG2, foreground=FG)

        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=8, pady=6)
        tk.Label(top, text="scrcpy-x", bg=BG, fg=ACC, font=("sans", 13, "bold")).pack(side="left")
        self.status = tk.Label(top, text="", bg=BG, fg="#9db1c5", font=("sans", 9))
        self.status.pack(side="right")

        devrow = tk.Frame(root, bg=BG)
        devrow.pack(fill="x", padx=8)
        tk.Label(devrow, text="device", bg=BG, fg=FG).pack(side="left")
        self.dev = ttk.Combobox(devrow, width=30)
        self.dev.pack(side="left", padx=6)
        tk.Button(devrow, text="⟳", command=self.refresh_devices, bg=BG2, fg=FG).pack(side="left")

        btnrow = tk.Frame(root, bg=BG)
        btnrow.pack(fill="x", padx=8, pady=4)
        for text, cmd in (("Connect", self.connect), ("Disconnect", self.disconnect), ("Mirror window", self.mirror), ("Status", self.show_status)):
            tk.Button(btnrow, text=text, command=cmd, bg=BG2, fg=FG, relief="groove").pack(side="left", padx=2, pady=2)

        screenfr = tk.LabelFrame(root, text=" screen (click to tap) ", bg=BG, fg=ACC, relief="flat")
        screenfr.pack(fill="both", expand=True, padx=8, pady=4)
        self.ph = tk.Label(screenfr, bg="#000", fg=FG, text="load snapshot...")
        self.ph.bind("<Button-1>", self.on_pic_click)
        self.ph.pack(fill="both", expand=True)
        sb = tk.Frame(screenfr, bg=BG)
        sb.pack(fill="x", pady=2)
        for text, cmd in (("📷 shot", self.shot), ("🔤 find text", self.findtext), ("rotate", lambda: self.send_key("rotate"))):
            tk.Button(sb, text=text, command=cmd, bg=BG2, fg=FG, relief="groove").pack(side="left", padx=2)
        self.mode = "tap"
        self.mode_lbl = tk.Label(sb, text="mode: tap", bg=BG, fg="#9db1c5")
        self.mode_lbl.pack(side="left", padx=6)
        for text, m in (("tap", "tap"), ("long", "long"), ("swipe→", "swipe")):
            tk.Button(sb, text=text, command=lambda mm=m: setmode(mm), bg="#1a4976" if m == "tap" else BG2, fg=FG, relief="groove").pack(side="left", padx=2)

        def setmode(mm):
            if mm == "swipe" and self.mode == "swipeA":
                self.mode = "swipeB"
            elif mm == "swipe" and self.mode != "swipeA":
                self.mode = "swipeA"
                self.swipea = None
                self.mode_lbl.config(text="mode: pick A")
                return
            self.mode = mm
            self.mode_lbl.config(text=f"mode: {mm}")

        self.setmode = setmode

        keys = tk.Frame(root, bg=BG)
        keys.pack(fill="x", padx=8, pady=2)
        for k, label in (("home", "⌂"), ("back", "◀"), ("power", "⏻"), ("vol-up", "🔊"), ("vol-down", "🔉"), ("menu", "☰")):
            tk.Button(keys, text=label, width=4, command=lambda kk=k: self.send_key(kk), bg=BG2, fg=FG, relief="groove").pack(side="left", padx=2)
        trow = tk.Frame(root, bg=BG)
        trow.pack(fill="x", padx=8, pady=2)
        self.textbox = tk.Entry(trow, bg="#0b1120", fg=FG, insertbackground=FG)
        self.textbox.pack(side="left", fill="x", expand=True)
        tk.Button(trow, text="send text", command=self.send_text, bg=BG2, fg=FG, relief="groove").pack(side="left")
        tk.Button(trow, text="set clip", command=self.setclip, bg=BG2, fg=FG, relief="groove").pack(side="left", padx=2)
        tk.Button(trow, text="get clip", command=self.getclip, bg=BG2, fg=FG, relief="groove").pack(side="left", padx=2)

        ocrfr = tk.LabelFrame(root, text=" on-screen text (click to tap) ", bg=BG, fg=ACC, relief="flat")
        ocrfr.pack(fill="x", padx=8, pady=4)
        self.ocrbox = tk.Listbox(ocrfr, height=5, bg="#0b1120", fg=FG, activestyle="none")
        self.ocrbox.pack(fill="both", expand=True)
        self.ocrbox.bind("<Double-Button-1>", self.on_word)

        ffr = tk.LabelFrame(root, text=" apps / files / record ", bg=BG, fg=ACC, relief="flat")
        ffr.pack(fill="x", padx=8, pady=4)
        arow = tk.Frame(ffr, bg=BG)
        arow.pack(fill="x", pady=1)
        self.appcombo = ttk.Combobox(arow, width=24)
        self.appcombo.pack(side="left")
        tk.Button(arow, text="🪟 desktop window", command=self.app_window, bg="#1a4976", fg=FG, relief="groove").pack(side="left", padx=2)
        tk.Button(arow, text="refresh", command=self.refresh_apps, bg=BG2, fg=FG, relief="groove").pack(side="left", padx=2)
        brow = tk.Frame(ffr, bg=BG)
        brow.pack(fill="x", pady=1)
        for text, cmd in (("● gesture", self.gest_on), ("■ gest stop", self.gest_off), ("▶ replay", self.gest_play),
                          ("🎥 rec 30s", self.rec_video), ("⏪ rewind", self.rewind), ("🎙 audio→file", self.aud_capture),
                          ("🔊 sound on PC", self.audio_on), ("■ audio off", self.audio_off),
                          ("📷 camera", self.camera), ("📦 install apk", self.install_apk),
                          ("⬆ push", self.push), ("⬇ pull", self.pull), ("🌐 watch", self.watch)):
            tk.Button(brow, text=text, command=cmd, bg=BG2, fg=FG, relief="groove").pack(side="left", padx=2, pady=1)

        self.swipea = None
        self.words = []
        self.gest_file = None
        self.audio_pid = None
        self.root.after(300, self.refresh_devices)
        self.root.after(1500, self.shot)
        self.root.after(9000, self.refresh_apps)

    def serial(self):
        txt = self.dev.get().strip().split()
        return txt[0] if txt else self.pick_first()

    def pick_first(self):
        try:
            for line in adb_cmd("devices").splitlines()[1:]:
                p = line.split()
                if len(p) >= 2 and p[1] == "device":
                    return p[0]
        except Exception:
            pass
        return None

    def refresh_devices(self):
        try:
            devs = [l.split()[0] + "  (" + l.split()[1] + ")" for l in adb_cmd("devices").splitlines()[1:] if len(l.split()) >= 2]
        except Exception:
            devs = []
        self.dev["values"] = devs
        if devs and not self.dev.get():
            self.dev.current(0)
        self.status.config(text=f"{len(devs)} device(s)")
        self.root.after(10000, self.refresh_devices)

    def connect(self):
        s = self.pick_first()
        if s:
            threading.Thread(target=lambda: self.done(cli("--serial", s, "connect")), daemon=True).start()

    def disconnect(self):
        s = self.pick_first()
        if s:
            threading.Thread(target=lambda: self.done(cli("--serial", s, "disconnect")), daemon=True).start()

    def mirror(self):
        s = self.pick_first()
        if not s:
            return
        for cand in (os.path.join(BASE, "..", "scrcpy", "build-test", "app", "scrcpy"), "/usr/bin/scrcpy"):
            if os.path.exists(cand):
                subprocess.Popen([cand, "-s", s], stdin=subprocess.DEVNULL, start_new_session=True)
                self.log(f"mirror window: {cand}")
                break

    def show_status(self):
        s = self.pick_first()
        if s:
            self.log(cli("status"))

    def log(self, msg):
        self.status.config(text=str(msg)[:80])

    def done(self, msg):
        self.root.after(0, lambda: self.log(msg))

    def refresh_pic(self):
        s = self.pick_first()
        if not s:
            return
        png = png_bytes(s)
        SNAP_CACHE["png"] = png
        self.show_png(png)

    def show_png(self, png, img_key="snap"):
        tmp = os.path.join(TMP, "desk-shot.png")
        open(tmp, "wb").write(png)
        try:
            self.cur = tk.PhotoImage(file=tmp)
        except tk.TclError:
            try:
                subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-i", tmp, "-c:v", "png", "-pix_fmt", "rgb24", tmp + ".x"], capture_output=True, timeout=20)
                self.cur = tk.PhotoImage(file=tmp + ".x")
            except Exception:
                return
        self.ph.config(image=self.cur, width=380, height=min(620, int(380 * self.cur.height() / self.cur.width())))

    def shot(self):
        threading.Thread(target=self.refresh_pic, daemon=True).start()

    def findtext(self):
        def work():
            s = self.pick_first()
            if not s:
                return
            png = png_bytes(s)
            SNAP_CACHE["png"] = png
            self.words = ocr_words(png)
            self.root.after(0, lambda: self.fill_words())
            self.root.after(0, lambda: self.show_png(png))
        threading.Thread(target=work, daemon=True).start()

    def fill_words(self):
        self.ocrbox.delete(0, "end")
        for w in self.words:
            self.ocrbox.insert("end", f"{w['t']:<28} ({w['x']},{w['y']})")

    def on_word(self, ev):
        sel = self.ocrbox.curselection()
        if not sel:
            return
        w = self.words[sel[0]]
        self.do_tap(w["x"], w["y"])

    def on_pic_click(self, ev):
        s = self.pick_first()
        if not s:
            return
        img = getattr(self, "cur", None)
        if img is None:
            return
        px = int(ev.x / self.ph.winfo_width() * img.width())
        py = int(ev.y / self.ph.winfo_height() * img.height())
        if self.mode == "swipeA":
            self.swipea = (px, py)
            self.setmode("swipe2")
            self.mode_lbl.config(text=f"A=({px},{py}) → pick B")
            return
        if self.mode == "swipe2" or self.mode == "swipeB":
            if self.swipea:
                self.do_swipe(self.swipea[0], self.swipea[1], px, py)
                self.setmode("tap")
            return
        self.do_tap(px, py, long=(self.mode == "long"))

    def do_tap(self, px, py, long=False):
        s = self.pick_first()
        if not s:
            return
        vx, vy = scale_xy(s, px, py)

        def work():
            if daemon_port(s):
                op = "long_press" if long else "tap"
                r = api_call(s, {"op": op, "x": vx, "y": vy})
                self.done("tap ok" if r.get("ok") else f"tap err {r.get('error')}")
            else:
                cmd = ["input", "swipe", str(vx), str(vy), str(vx), str(vy), "500"] if long else ["input", "tap", str(vx), str(vy)]
                subprocess.run(["adb", "-s", s, "shell", *cmd], capture_output=True, timeout=30)
                self.done("adb tap sent")
        threading.Thread(target=work, daemon=True).start()

    def do_swipe(self, x1, y1, x2, y2):
        s = self.pick_first()
        if not s:
            return
        if daemon_port(s):
            sx1, sy1 = scale_xy(s, x1, y1)
            sx2, sy2 = scale_xy(s, x2, y2)
            threading.Thread(target=lambda: self.done(str(api_call(s, {"op": "swipe", "x1": sx1, "y1": sy1, "x2": sx2, "y2": sy2, "ms": 250}))), daemon=True).start()
        else:
            threading.Thread(target=lambda: subprocess.run(["adb", "-s", s, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), "300"], capture_output=True), daemon=True).start()

    def send_key(self, k):
        s = self.pick_first()
        if not s:
            return

        def work():
            if daemon_port(s):
                self.done(str(api_call(s, {"op": "key", "key": k})))
            elif k != "rotate":
                code = {"home": 3, "back": 4, "power": 26, "vol-up": 24, "vol-down": 25, "menu": 82}.get(k)
                if code is not None:
                    subprocess.run(["adb", "-s", s, "shell", "input", "keyevent", str(code)], capture_output=True)
                    self.done(f"key {k}")
        threading.Thread(target=work, daemon=True).start()

    def send_text(self):
        s = self.pick_first()
        t = self.textbox.get()
        if not s or not t:
            return
        if daemon_port(s):
            threading.Thread(target=lambda: self.done(str(api_call(s, {"op": "text", "text": t}))), daemon=True).start()
        else:
            self.log("connect session for text")

    def setclip(self):
        s = self.pick_first()
        t = self.textbox.get()
        if s and t:
            threading.Thread(target=lambda: self.done(cli("clip", "set", t, "--serial", s)), daemon=True).start()

    def getclip(self):
        s = self.pick_first()
        if s:
            threading.Thread(target=lambda: self.root.after(0, lambda: self.log(cli("clip", "get", "--serial", s))), daemon=True).start()

    def refresh_apps(self):
        def work():
            s = self.pick_first()
            if not s:
                return
            res = adb_cmd("-s", s, "shell", "pm", "list", "packages")
            pkgs = sorted(l.replace("package:", "") for l in res.split())
            self.root.after(0, lambda: self._set_apps(pkgs))
        threading.Thread(target=work, daemon=True).start()

    def _set_apps(self, pkgs):
        self.appcombo["values"] = pkgs
        if pkgs:
            self.appcombo.current(0)
        self.log(f"{len(pkgs)} apps")

    def app_window(self):
        s = self.pick_first()
        p = self.appcombo.get()
        if s and p:
            threading.Thread(target=lambda: self.done(cli("--serial", s, "app-window", p) or "window launching"), daemon=True).start()

    def gest_on(self):
        s = self.pick_first()
        if not s:
            return
        self.gest_file = os.path.join(TMP, f"gest-{int(time.time())}.json")
        threading.Thread(target=lambda: self.done(cli("--serial", s, "record", "start", self.gest_file)), daemon=True).start()

    def gest_off(self):
        s = self.pick_first()
        if s:
            threading.Thread(target=lambda: self.done(cli("--serial", s, "record", "stop")), daemon=True).start()

    def gest_play(self):
        s = self.pick_first()
        if s and self.gest_file:
            threading.Thread(target=lambda: self.done(cli("--serial", s, "replay", self.gest_file)), daemon=True).start()

    def rec_video(self):
        s = self.pick_first()
        if s:
            threading.Thread(target=lambda: self.done(cli("record-video", os.path.join(TMP, f"rec-{int(time.time())}.mp4"), "--time", "30", "--serial", s)), daemon=True).start()

    def rewind(self):
        def work():
            s = self.pick_first()
            if not s:
                return
            port = daemon_port(s)
            if not port:
                self.log("no session for rewind")
                return
            import urllib.request
            ago = 15
            try:
                data = urllib.request.urlopen(f"http://127.0.0.1:{port}/ring?ago={ago}", timeout=10).read()
            except Exception as e:
                self.done(f"rewind err {e}")
                return
            tmpj = os.path.join(TMP, "ring.jpg")
            open(tmpj, "wb").write(data)
            outp = tmpj.replace(".jpg", ".png")
            subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-i", tmpj, outp], capture_output=True, timeout=20)
            png = open(outp, "rb").read()
            SNAP_CACHE["png"] = png
            self.root.after(0, lambda: self.show_png(png))
            self.done(f"rewind {ago}s shown")
        threading.Thread(target=work, daemon=True).start()

    def aud_capture(self):
        s = self.pick_first()
        if s:
            threading.Thread(target=lambda: self.done(cli("audio-capture", os.path.join(TMP, "gui-audio.m4a"), "--time", "15", "--serial", s)), daemon=True).start()

    def audio_on(self):
        s = self.pick_first()
        if not s:
            return
        binp = None
        for cand in (os.path.join(BASE, "..", "scrcpy", "build-test", "app", "scrcpy"), "/usr/bin/scrcpy"):
            if os.path.exists(cand):
                binp = cand
                break
        if not binp:
            return
        audio_flags = ["--audio-codec=aac", "--audio-encoder=OMX.google.aac.encoder"]
        try:
            with open(os.path.join(STATE, "audio-%s.json" % s)) as fh:
                cached = json.load(fh).get("flags") or []
            if cached and "--no-audio" not in cached:
                audio_flags = cached
        except Exception:
            pass
        self.audio_pid = subprocess.Popen([binp, "-s", s, "--no-window", "--no-video"] + audio_flags, stdin=subprocess.DEVNULL, start_new_session=True).pid
        self.log(f"audio→PC pid {self.audio_pid}")

    def audio_off(self):
        if self.audio_pid:
            try:
                os.kill(self.audio_pid, 15)
            except OSError:
                pass
            self.log("audio off")

    def camera(self):
        s = self.pick_first()
        if s:
            threading.Thread(target=lambda: self.done(cli("camera", "--facing", "back", "--serial", s)), daemon=True).start()

    def install_apk(self):
        s = self.pick_first()
        if not s:
            return
        path = filedialog.askopenfilename(title="APK", filetypes=[("APK", "*.apk"), ("all", "*.*")])
        if path:
            threading.Thread(target=lambda: self.done(cli("install", path, "--serial", s, "-r")), daemon=True).start()

    def push(self):
        s = self.pick_first()
        if not s:
            return
        path = filedialog.askopenfilename(title="file to push")
        if path:
            threading.Thread(target=lambda: self.done(str(subprocess.run(["adb", "-s", s, "push", path, "/sdcard/Download/"], capture_output=True, text=True).stdout)), daemon=True).start()

    def pull(self):
        s = self.pick_first()
        if not s:
            return
        path = filedialog.askstring("pull", "path on phone:")
        if path:
            threading.Thread(target=lambda: self.done(str(subprocess.run(["adb", "-s", s, "pull", path, TMP], capture_output=True, text=True).stdout)), daemon=True).start()

    def watch(self):
        s = self.pick_first()
        port = daemon_port(s or "")
        if port:
            self.log(f"wacth: http://127.0.0.1:{port}/stream")
        else:
            self.log("connect session first")


def run_app():
    root = tk.Tk()
    app = App(root)
    root.mainloop()

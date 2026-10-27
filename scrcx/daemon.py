import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import adb
from . import protocol as P
from .control import ControlChannel
from .video_reader import FrameRing, VideoThread

DEFAULT_RING_SECONDS = 300
P_KEY_BACK = 4
HEAL = {"off_at": 0.0, "hold": 45.0, "stale": 0}


def rebuild_session(serial, args):
    old = ApiHandler.session
    try:
        old.stop()
    except Exception:
        pass
    s = Session(serial, args)
    ApiHandler.session = s
    bound = False
    try:
        bound = s.bind_screen()
    except Exception:
        pass
    HEAL["off_at"] = time.time()
    st = adb.read_state(serial) or {}
    adb.write_state(serial, {"serial": serial, "api_port": st.get("api_port"), "pid": os.getpid(), "bound": bound, "info": s.info()})
    return bound


class Session:
    def __init__(self, serial, args):
        self.serial = serial
        self.jar = args.server_jar
        self.ring = FrameRing(fps=args.ring_fps, width=args.ring_width, max_age=args.ring_seconds)
        self.started = time.time()
        self.proc = adb.ServerProcess(
            serial,
            self.jar,
            max_size=args.max_size,
            max_fps=args.max_fps or None,
            bit_rate=args.bit_rate or None,
            log_path=os.path.join(adb.STATE_DIR, f"{serial}.log"),
        )
        rails = self.proc.sockets(2)
        video_sock = rails[0]
        control_sock = rails[1]
        self.video = VideoThread(video_sock, self.ring, forward_dummy=self.proc.tunnel_forward)
        self.video.start()
        self.control = ControlChannel(control_sock, messages=None)
        self.stopping = threading.Event()

    def bind_screen(self):
        t0 = time.time()
        while time.time() - t0 < 8:
            if self.video.width and self.video.height:
                self.control.bind_screen(self.video.width, self.video.height)
                return True
            if self.video.error:
                return False
            time.sleep(0.1)
        if self.video.width:
            self.control.bind_screen(self.video.width, self.video.height)
            return True
        return False

    def info(self):
        span = self.ring.span()
        return {
            "serial": self.serial,
            "device": self.video.device_name,
            "codec": self.video.codec,
            "screen": [self.video.width, self.video.height],
            "fps_buffer": self.ring.fps,
            "ring_seconds": self.ring.max_age,
            "ring_size": self.ring.size(),
            "ring_range": None if not span[0] else [round(span[1] - span[0], 1), span[0], span[1]],
            "video_error": self.video.error,
            "process_alive": self.proc.alive(),
            "uptime_s": round(time.time() - self.started, 1),
        }

    def stop(self):
        self.stopping.set()
        try:
            self.video.stop()
        except Exception:
            pass
        try:
            self.control.sock.close()
        except Exception:
            pass
        try:
            self.proc.stop()
        except Exception:
            pass


class ApiHandler(BaseHTTPRequestHandler):
    session = None
    args = None

    def log_message(self, fmt, *a):
        pass

    def _json(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _jpeg(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        s = self.session
        path = urllib.parse.urlparse(self.path).path
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path == "/" or path == "/index.html":
            self._html()
            return
        if s is None:
            self._json({"error": "no session"}, 503)
            return
        if path == "/info":
            self._json(s.info())
        elif path == "/snap":
            frame = s.ring.newest()
            if not frame:
                self._json({"error": "no frame yet"}, 503)
                return
            self._jpeg(frame[1])
        elif path == "/stream":
            self._stream()
        elif path == "/ring":
            ago = float((query.get("ago") or ["0"])[0])
            frame = s.ring.at_ago(ago)
            if not frame:
                self._json({"error": "ring empty"}, 503)
                return
            self._jpeg(frame[1])
        elif path == "/clip":
            self._json({"clip": s.control.get_clipboard()})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        s = self.session
        path = urllib.parse.urlparse(self.path).path
        if path != "/api":
            self._json({"error": "not found"}, 404)
            return
        if s is None:
            self._json({"error": "no session"}, 503)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json({"error": "bad json"}, 400)
            return
        op = req.get("op")
        try:
            out = self.dispatch(op, req, s)
            self._json({"ok": True, "result": out})
        except Exception as exc:
            self._json({"ok": False, "error": repr(exc)}, 500)

    def dispatch(self, op, req, s):
        if op == "session_restart":
            rebuild_session(s.serial, self.args)
            return None
        scr = s.control.screen
        if op == "tap":
            s.control.tap(req["x"], req["y"])
            return None
        if op == "long_press":
            s.control.long_press(req["x"], req["y"], req.get("ms", 600))
            return None
        if op == "swipe":
            s.control.swipe(req["x1"], req["y1"], req["x2"], req["y2"], req.get("ms", 300))
            return None
        if op == "scroll":
            s.control.scroll(req["x"], req["y"], req.get("dx", 0), req.get("dy", 0))
            return None
        if op == "text":
            s.control.text(req["text"])
            return None
        if op == "key":
            s.control.key(req.get("key"), req.get("action", "tap"))
            return None
        if op == "back":
            s.control.key(P_KEY_BACK)
            return None
        if op == "back_or_screen_on":
            s.control.back_or_screen_on(down=True)
            return None
        if op == "expand":
            s.control.expand_notifications()
            return None
        if op == "expand_settings":
            s.control.expand_settings()
            return None
        if op == "collapse":
            s.control.collapse_panels()
            return None
        if op == "rotate":
            s.control.rotate()
            return None
        if op == "screen_on":
            s.control.display_power(True)
            return None
        if op == "screen_off":
            HEAL["off_at"] = time.time()
            s.control.display_power(False)
            return None
        if op == "clipboard_get":
            return {"clip": s.control.get_clipboard()}
        if op == "clipboard_set":
            return {"ack": s.control.set_clipboard(req.get("text", ""), bool(req.get("paste", False)))}
        if op == "screenshot":
            return {"file": save_screencap(s.serial, req.get("out"))}
        if op == "ring_get":
            t = s.ring.at_ago(req.get("ago", 0))
            if not t:
                raise RuntimeError("ring empty")
            out = req.get("out")
            if out:
                with open(out, "wb") as fh:
                    fh.write(t[1])
                return {"file": os.path.abspath(out), "t": t[0]}
            return {"bytes": len(t[1]), "age": round(time.time() - t[0], 2)}
        if op == "record_start":
            sink = RecordingSink(req.get("file"))
            s.control.set_recording(sink)
            s.recording_sink = sink
            return {"file": sink.path}
        if op == "record_stop":
            sink = getattr(s, "recording_sink", None)
            if not sink:
                raise RuntimeError("not recording")
            sink.close()
            s.recording_sink = None
            s.control.set_recording(None)
            return {"file": sink.path, "steps": len(sink.steps)}
        if op == "replay":
            rate = float(req.get("speed", 1.0))
            times = int(req.get("times", 1))
            exec_steps(s, req["file"], rate, times)
            return None
        if op == "hud":
            return {"battery": battery(s.serial), "thermal": thermal(s.serial), "ring": s.ring.size()}
        if op == "apps_list":
            return {"apps": apps_list(s.serial)}
        if op == "apps_named":
            return {"apps": named_apps(s.serial)}
        if op == "app_start":
            adb.shell(s.serial, f"monkey -p {req['package']} -c android.intent.category.LAUNCHER 1", timeout=20)
            return None
        if op == "app_stop":
            adb.shell(s.serial, f"am force-stop {req['package']}", timeout=15)
            return None
        if op == "app_install":
            adb("install", "-r", req["apk"], serial=s.serial, timeout=300)
            return None
        raise RuntimeError(f"unknown op {op}")

    def _stream(self):
        s = self.session
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=jpgframe")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        last = None
        try:
            while not s.stopping.is_set():
                frame = s.ring.newest()
                if frame and frame != last:
                    last = frame
                    data = frame[1]
                    self.wfile.write(b"--jpgframe\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write("Content-Length: %d\r\n\r\n" % len(data))
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                time.sleep(0.08)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def _html(self):
        page = (
            "<!doctype html><title>scrcpy-x watch</title>"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>body{margin:0;background:#111;color:#eee;font:14px sans-serif;text-align:center}</style>'
            '<img src="/stream" style="max-width:100%;max-height:100vh">'
            '<p>read-only MJPEG mirror &mdash; scrcpy-x</p>'
        )
        raw = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def save_screencap(serial, out):
    os.makedirs(os.path.dirname(os.path.abspath(out or "shot.png")), exist_ok=True)
    path = os.path.abspath(out or "") or "/tmp/opencode/scrc_shot.png"
    raw = adb.subprocess.run(["adb", "-s", serial, "exec-out", "screencap", "-p"], capture_output=True, timeout=30)
    if not raw.stdout:
        raise RuntimeError("screencap failed: " + raw.stderr.decode("utf-8", "replace"))
    with open(path, "wb") as fh:
        fh.write(raw.stdout)
    return path


def battery(serial):
    data = adb.shell(serial, "dumpsys battery")
    fields = {}
    for line in data.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip().lower()] = v.strip()
    b = {"level": fields.get("level"), "temp": fields.get("temperature"), "status": fields.get("status")}
    if b.get("temp"):
        try:
            b["temp_c"] = int(b["temp"]) / 10.0
        except (TypeError, ValueError):
            b["temp_c"] = None
    return b


def thermal(serial):
    try:
        data = adb.shell(serial, "dumpsys thermalservice", timeout=15)
        for line in data.splitlines():
            line = line.strip()
            if "Battery" in line and "=" in line:
                part = line.split("=", 1)[1]
                for token in part.split(","):
                    if "mCurrentTemp" in token:
                        return token.strip()
    except Exception:
        pass
    return None


def apps_list(serial):
    out = adb.shell(serial, "pm list packages", timeout=20)
    return [ln.replace("package:", "").strip() for ln in out.splitlines() if ln.startswith("package:")]


APPS_CACHE = os.path.join(adb.STATE_DIR, "apps.json")


def named_apps(serial):
    """
    Returns [{"name": displayName, "package": id}, ...] using scrcpy's --list-apps.
    Falls back to plain package names. Also writes ~/.local/state/scrcx/apps.json.
    """
    try:
        res = subprocess.run(["scrcpy", "-s", serial, "--list-apps", "--no-video-playback", "--no-audio-playback"], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        res = None
    entries = []
    if res and res.returncode == 0:
        import re as _re
        for line in res.stdout.splitlines():
            line = line.strip()
            if line.startswith("* ") or line.startswith("- "):
                body = line[2:].strip()
                parts = _re.split(r"\s{2,}", body, maxsplit=1)
                if len(parts) == 2 and parts[1]:
                    entries.append({"name": parts[0].strip(), "package": parts[1].strip()})
    if not entries:
        entries = [{"name": p.split(".")[-1], "package": p} for p in apps_list(serial)]
    try:
        with open(APPS_CACHE, "w") as fh:
            json.dump(entries, fh)
    except OSError:
        pass
    return entries


def resolve_app(token, entries):
    if "." in token:
        return token
    t = token.lower().replace(" ", "")
    exact = [e for e in entries if e["name"].lower().replace(" ", "") == t]
    if exact:
        return exact[0]["package"]
    cand = [e for e in entries if t in e["name"].lower().replace(" ", "") or t in e["package"].lower()]
    if not cand:
        raise RuntimeError(f"no app matches token {token!r}; run: scrcx app list --names")
    cand.sort(key=lambda e: len(e["name"]))
    return cand[0]["package"]


class RecordingSink:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.steps = []
        self._t0 = None
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def __call__(self, step):
        self.steps.append(list(step))

    def close(self):
        with open(self.path, "w") as fh:
            json.dump({"version": 1, "steps": self.steps}, fh, indent=1)


def exec_steps(session, path, speed, times):
    from . import protocol

    with open(path) as fh:
        data = json.load(fh)
    steps = data.get("steps", [])
    if not steps:
        return 0
    for t in range(times):
        t0 = time.time()
        for ts, cmd, a in steps:
            target = t0 + ts * speed
            delay = target - time.time()
            if delay > 0:
                time.sleep(delay)
            if cmd == "tap-down":
                session.control.send(protocol.encode_touch(0, -1, a[0], a[1], int(a[2]), int(a[3])))
            elif cmd == "tap-up":
                session.control.send(protocol.encode_touch(1, -1, a[0], a[1], int(a[2]), int(a[3])))
            elif cmd == "tap-move":
                session.control.send(protocol.encode_touch(2, -1, a[0], a[1], int(a[2]), int(a[3])))
            elif cmd == "key-down":
                session.control.send(protocol.encode_keycode(0, int(a[0])))
            elif cmd == "key-up":
                session.control.send(protocol.encode_keycode(1, int(a[0])))
            elif cmd == "text":
                session.control.send(protocol.encode_text(a[0]))
            elif cmd == "scroll":
                w, h = session.control.screen
                session.control.send(protocol.encode_scroll(a[0], a[1], w, h, a[2], a[3]))
    return len(steps)


def main():
    args = parse_args()
    serial = args.serial or pick_only()
    os.makedirs(adb.STATE_DIR, exist_ok=True)
    running = adb.read_state(serial)
    if running:
        print(json.dumps({"error": "already running for " + serial, "state": running}))
        sys.exit(2)
    session = None
    try:
        session = Session(serial, args)
        ApiHandler.session = session
        ApiHandler.args = args
        bind = "0.0.0.0" if args.lan else "127.0.0.1"
        httpd = ThreadingHTTPServer((bind, args.port), ApiHandler)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
    ok = session.bind_screen()
    state = {
        "serial": serial,
        "api_port": httpd.server_address[1],
        "pid": os.getpid(),
        "bound": ok,
        "info": session.info(),
    }
    adb.write_state(serial, state)
    print(json.dumps(state))
    sys.stdout.flush()

    def heal_loop():
        while True:
            time.sleep(6)
            try:
                if time.time() - HEAL["off_at"] < HEAL["hold"]:
                    continue
                try:
                    info = ApiHandler.session.info()
                except Exception:
                    info = {"process_alive": False, "video_error": "dead"}
                bad = (not info.get("process_alive")) or bool(info.get("video_error"))
                if not bad:
                    HEAL["stale"] = 0
                    HEAL["hold"] = 45.0
                    continue
                HEAL["stale"] += 1
                if HEAL["stale"] >= 4:
                    print("heal: session wedged, restarting server", flush=True)
                    try:
                        rebuild_session(serial, args)
                        HEAL["stale"] = 0
                        info2 = ApiHandler.session.info()
                        if info2.get("video_error") or not info2.get("process_alive"):
                            HEAL["hold"] = min(HEAL["hold"] * 2, 600.0)
                    except Exception as exc:
                        print("heal failed: " + repr(exc), flush=True)
            except Exception as exc:
                print("heal error: " + repr(exc), flush=True)

    threading.Thread(target=heal_loop, daemon=True).start()

    def shutdown(sig, frame):
        try:
            os.remove(adb.state_path(serial))
        except Exception:
            pass
        try:
            httpd.shutdown()
        except Exception:
            pass
        session.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        session.stop()
        try:
            os.remove(adb.state_path(serial))
        except Exception:
            pass


def pick_only():
    devices = adb.list_devices()
    if not devices:
        raise RuntimeError("no adb devices found; plug a phone (USB debugging ON)")
    if len(devices) > 1:
        raise RuntimeError("multiple devices; pass --serial. " + ", ".join(d["serial"] for d in devices))
    return devices[0]["serial"]


def parse_args():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--serial")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--lan", action="store_true")
    p.add_argument("--ring-fps", type=int, default=5)
    p.add_argument("--ring-width", type=int, default=640)
    p.add_argument("--ring-seconds", type=int, default=DEFAULT_RING_SECONDS)
    p.add_argument("--max-size", type=int, default=1280)
    p.add_argument("--max-fps", type=int, default=0)
    p.add_argument("--bit-rate", default=None)
    p.add_argument("--server-jar", required=False, default=None)
    a = p.parse_args()
    if not a.server_jar:
        a.server_jar = adb.get_server_jar()
    return a


if __name__ == "__main__":
    main()

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

from . import adb
from . import protocol as P

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = "/tmp/opencode"
LAST_REBUILD = {"t": 0.0}
SESSION_TAIL_ARGS = [
    "--ring-seconds",
    "--ring-fps",
    "--ring-width",
    "--max-size",
]


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, default=str))


def resolve_serial(args):
    serial = getattr(args, "serial", None)
    if serial:
        return serial
    devices = adb.list_devices()
    if not devices:
        raise RuntimeError("no adb devices attached (plug phone, enable USB debugging)")
    if len(devices) == 1:
        return devices[0]["serial"]
    raise RuntimeError("multiple devices attached; pass --serial. attached=" + ",".join(d["serial"] for d in devices))


def api_url(serial, path):
    state = adb.read_state(serial)
    if not state:
        raise RuntimeError(f"no session for {serial}; run: scrcx connect")
    return f"http://127.0.0.1:{state['api_port']}{path}"


def api_call(serial, payload, timeout=60):
    op = payload.get("op")
    try:
        req = urllib.request.Request(
            api_url(serial, "/api"),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError:
        if op == "session_restart" or time.time() - LAST_REBUILD["t"] < 25:
            raise
        LAST_REBUILD["t"] = time.time()
        r = urllib.request.Request(
            api_url(serial, "/api"),
            data=json.dumps({"op": "session_restart"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(r, timeout=45).read()
        req = urllib.request.Request(
            api_url(serial, "/api"),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())


def api_get(serial, path, timeout=60):
    with urllib.request.urlopen(api_url(serial, path), timeout=timeout) as resp:
        return json.loads(resp.read())


def api_raw(serial, path, timeout=60):
    with urllib.request.urlopen(api_url(serial, path), timeout=timeout) as resp:
        return resp.read()


# ------------------------------------------------- devices/sessions


AUDIO_CANDIDATES = [
    ("default", []),
    ("aac", ["--audio-codec=aac", "--audio-encoder=OMX.google.aac.encoder"]),
    ("flac", ["--audio-codec=flac", "--audio-encoder=OMX.google.flac.encoder"]),
    ("none", ["--no-audio"]),
]


def _audio_cache_path(serial):
    return os.path.join(adb.STATE_DIR, "audio-%s.json" % serial)


def _cached_audio(serial):
    try:
        with open(_audio_cache_path(serial)) as fh:
            d = json.load(fh)
        return str(d.get("name")), list(d.get("flags") or [])
    except Exception:
        return None, None


def _cache_audio(serial, name, flags):
    try:
        os.makedirs(adb.STATE_DIR, exist_ok=True)
        with open(_audio_cache_path(serial), "w") as fh:
            json.dump({"name": name, "flags": flags}, fh)
    except Exception:
        pass


def _probe_audio(binary, serial, extra, cap=8.0, soak=4.0):
    logp = os.path.join(TMP, "scrcx-audio-probe.log")
    recp = os.path.join(TMP, "scrcx-audio-probe.mkv")
    try:
        os.remove(recp)
    except OSError:
        pass
    cmd = [binary, "-s", serial, "--no-window", "--no-video", "--no-audio-playback"]
    if "--no-audio" not in extra:
        cmd.append("--record=" + recp)
    cmd += extra
    if shutil.which("stdbuf"):
        cmd = ["stdbuf", "-oL", "-eL"] + cmd
    logf = open(logp, "wb")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT)
    t0 = time.time()
    ok = False
    try:
        while time.time() - t0 < cap:
            if proc.poll() is not None:
                break
            if "--no-audio" in extra:
                if time.time() - t0 > 2.5:
                    ok = True
                    break
            else:
                marker = False
                try:
                    with open(logp, "r", errors="replace") as fh:
                        marker = "Recording started" in fh.read()
                except OSError:
                    pass
                size = 0
                try:
                    size = os.path.getsize(recp)
                except OSError:
                    pass
                if marker or size > 100:
                    ok = True
                    break
            time.sleep(0.1)
        if ok and soak:
            end = time.time() + soak
            while time.time() < end:
                if proc.poll() is not None:
                    ok = False
                    break
                time.sleep(0.2)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
        else:
            proc.wait()
        logf.close()
    if ok:
        time.sleep(1.0)
    return ok


def _pick_audio(serial, with_none=True):
    binary = binary_path()
    candidates = [(n, f) for n, f in AUDIO_CANDIDATES if with_none or n != "none"]
    cname, cflags = _cached_audio(serial)
    if cflags is not None and (with_none or cname != "none"):
        candidates = [(cname, cflags)] + [(n, f) for n, f in candidates if n != cname]
    for name, extra in candidates:
        print("[scrcx] audio auto: trying %s" % name, flush=True)
        if _probe_audio(binary, serial, extra, soak=1.0 if name == cname else 4.0):
            _cache_audio(serial, name, extra)
            return name, extra
        print("[scrcx] audio auto: %s failed" % name, flush=True)
    return "none", ["--no-audio"]


def cmd_mirror(args):
    binary = find_scrcpy_binary()
    if not binary:
        raise RuntimeError("scrcpy binary not found (build scrcpy first)")
    serial = resolve_serial(args)
    flags = [f for f in args.flags if f != "--"]
    if any(f == "--no-audio" or f.startswith(("--audio-codec", "--audio-source", "--audio-dup")) for f in flags):
        os.execvp(binary, [binary, "-s", serial] + flags)
    candidates = list(AUDIO_CANDIDATES)
    cname, cflags = _cached_audio(serial)
    if cflags is not None:
        candidates = [(cname, cflags)] + [(n, f) for n, f in candidates if n != cname]
    for name, extra in candidates:
        print("[scrcx] audio auto: trying %s" % name, flush=True)
        proc = subprocess.Popen([binary, "-s", serial] + extra + flags, stdin=subprocess.DEVNULL)
        died = False
        t0 = time.time()
        while time.time() - t0 < 10.0:
            rc = proc.poll()
            if rc is not None:
                died = rc != 0
                break
            time.sleep(0.25)
        if not died:
            _cache_audio(serial, name, extra)
            print("[scrcx] audio auto: using %s" % name, flush=True)
            try:
                rc = proc.wait()
            except KeyboardInterrupt:
                proc.send_signal(signal.SIGINT)
                try:
                    rc = proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                    rc = 0
            sys.exit(rc if isinstance(rc, int) and rc > 0 else 0)
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
        else:
            proc.wait()
        print("[scrcx] audio auto: %s failed" % name, flush=True)
    raise RuntimeError("no working audio configuration found (tried: %s)" % ", ".join(n for n, _ in candidates))


def _run_scrcpy_rec(cmd, args, dur):
    if dur:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        log = ""
        try:
            log = proc.communicate(timeout=dur)[0].decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGINT)
            try:
                log = proc.communicate(timeout=5)[0].decode("utf-8", "replace")
            except subprocess.TimeoutExpired:
                proc.terminate()
                log = ""
    else:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, start_new_session=True)
        log = f"recording PID {proc.pid} — stop with: pkill -p {proc.pid}"
    return proc, log


def cmd_record_video(args):
    binary = find_scrcpy_binary()
    if not binary:
        raise RuntimeError("scrcpy binary not found")
    serial = resolve_serial(args)
    outpath = os.path.abspath(args.out)
    cmd = [binary, "-s", serial, "--record", outpath, "--no-window", "--no-audio"]
    if args.max_fps:
        cmd += [f"--max-fps={args.max_fps}"]
    if args.max_size:
        cmd += [f"--max-size={args.max_size}"]
    cmd += list(args.extra or [])
    proc, log = _run_scrcpy_rec(cmd, args, args.time)
    out({"ok": True, "record": outpath, "seconds": args.time, "note": log})


def cmd_audio_capture(args):
    binary = find_scrcpy_binary()
    if not binary:
        raise RuntimeError("scrcpy binary not found")
    serial = resolve_serial(args)
    path = os.path.abspath(args.out)
    cmd = [binary, "-s", serial, "--no-window", "--no-video", "--record", path]
    if args.mic:
        cmd += ["--audio-source=mic"]
    lower = path.lower()
    if lower.endswith(".wav"):
        cmd += ["--audio-codec=raw"]
    elif lower.endswith(".flac"):
        cmd += ["--audio-codec=flac", "--audio-encoder=OMX.google.flac.encoder"]
    else:
        cmd += ["--audio-codec=aac", "--audio-encoder=OMX.google.aac.encoder"]
    if args.stereo:
        cmd += ["--audio-dup"]
    proc, log = _run_scrcpy_rec(cmd, args, args.time)
    out({"ok": True, "audio": path, "seconds": args.time, "note": log})


def cmd_camera(args):
    binary = find_scrcpy_binary()
    if not binary:
        raise RuntimeError("scrcpy binary not found")
    serial = resolve_serial(args)
    cmd = [binary, "-s", serial, "--no-audio", "--video-source=camera", f"--camera-facing={args.facing}"]
    if args.size:
        cmd += [f"--camera-size={args.size}"]
    if args.maximize:
        cmd.append("-x")
    if args.record:
        cmd += ["--record", os.path.abspath(args.record)]
    cmd += list(args.extra or [])
    logf = open(os.path.join(TMP, "scrcx-camera.log"), "wb")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    out({"ok": True, "pid": proc.pid, "facing": args.facing, "window": True})


def cmd_audio_play(args):
    serial = resolve_serial(args)
    rec = os.path.abspath(args.record) if args.record else None
    rec_arg = []
    if rec and rec.lower().endswith(".wav"):
        extra = ["--audio-codec=raw"]
        rec_arg = ["--record", rec]
    else:
        name, extra = _pick_audio(serial, with_none=False)
        print("[scrcx] audio auto: using %s" % name, flush=True)
    cmd = [binary_path(), "-s", serial, "--no-window", "--no-video"] + extra + rec_arg
    logf = open(os.path.join(TMP, "scrcx-audio-play.log"), "wb")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    out({"ok": True, "pid": proc.pid, "note": "phone audio now plays on PC speakers (headless); stop: pkill -p %d" % proc.pid, "record": rec})


def binary_path():
    return find_scrcpy_binary()


def cmd_v4l2(args):
    serial = resolve_serial(args)
    cmd = [binary_path(), "-s", serial, "--no-window", "--no-audio", f"--v4l2-sink={args.device}"]
    if args.size:
        cmd.append(f"--max-size={args.size}")
    logf = open(os.path.join(TMP, "scrcx-v4l2.log"), "wb")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    out({"ok": True, "pid": proc.pid, "device": args.device, "note": "phone is now a webcam source; requires v4l2loopback module"})


def cmd_hid(args):
    serial = resolve_serial(args)
    cmd = [binary_path(), "-s", serial, "--no-audio"]
    if args.otg:
        cmd += ["--otg"]
        out({"ok": True, "cmd": cmd, "note": "OTG mode (no display, device-side HID)"})
    else:
        if args.keyboard:
            cmd += ["--keyboard=uhid"] if args.keyboard == "uhid" else ["--hid-keyboard"]
        if args.mouse:
            cmd += ["--mouse=uhid"] if args.mouse == "uhid" else ["--hid-mouse"]
        if not args.keyboard and not args.mouse:
            cmd += ["--hid-keyboard", "--hid-mouse"]
    logf = open(os.path.join(TMP, "scrcx-hid.log"), "wb")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    out({"ok": True, "pid": proc.pid, "mode": "otg" if args.otg else "hid"})


def cmd_install(args):
    serial = resolve_serial(args)
    path = os.path.abspath(args.apk)
    if not os.path.exists(path):
        raise RuntimeError(f"apk not found: {path}")
    extra = ["-r"] if args.replace else []
    res = subprocess.run(["adb", "-s", serial, "install", *extra, path], capture_output=True, text=True, timeout=300)
    out({"ok": res.returncode == 0 and "Success" in (res.stdout + res.stderr), "output": (res.stdout + res.stderr).strip()})


def cmd_send_file(args):
    serial = resolve_serial(args)
    src = os.path.abspath(args.local)
    if not os.path.exists(src):
        raise RuntimeError(f"file not found: {src}")
    name = os.path.basename(src)
    dst = args.remote or f"/sdcard/Download/{name}"
    subprocess.run(["adb", "-s", serial, "push", src, dst], capture_output=True, text=True, timeout=300, check=True)
    if args.scan:
        subprocess.run(["adb", "-s", serial, "shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", f"file://{dst}"], capture_output=True, timeout=60)
    out({"ok": True, "pushed": dst, "scanned": bool(args.scan)})


def cmd_dcaps(args):
    serial = resolve_serial(args)
    binary = binary_path()
    what = {"encoders": ["--list-encoders"], "cameras": ["--list-camera-sizes"], "apps": ["--list-apps"]}[args.what]
    res = subprocess.run([binary, "-s", serial, *what, "--no-video-playback", "--no-audio-playback"], capture_output=True, text=True, timeout=120)
    rows = [ln for ln in (res.stdout + res.stderr).splitlines() if ln.startswith("    --") or ln.lstrip().startswith("- ")]
    body = res.stdout + res.stderr
    if "List of apps" in body:
        blocks = body.split("List of apps")[-1]
        rows = [b" " + ln.strip() for ln in blocks.splitlines() if ln.strip().startswith("[") or (ln.strip() and not ln.strip().lower().startswith(("scrcpy", "info:", "adb", "/usr")))]
    out({"ok": bool(rows), "list": "\n".join(rows) if rows else body.strip()})


def cmd_gui(args):
    from . import gui
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    if args.port:
        os.environ["SCRCPX_GUI_PORT"] = str(args.port)
        gui.PORT = args.port
    gui.run_gui(host)


def cmd_desktop(args):
    from .desktop import run_app
    run_app()


def find_scrcpy_binary():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        os.path.join(here, "..", "..", "scrcpy", "build-test", "app", "scrcpy"),
        shutil.which("scrcpy") or None,
        "/usr/bin/scrcpy",
    ):
        if cand and os.path.exists(cand):
            return os.path.abspath(cand)
    return None


def cmd_app_window(args):
    serial = resolve_serial(args)
    binary = find_scrcpy_binary()
    if not binary:
        raise RuntimeError("scrcpy binary not found (build scrcpy first)")
    pkg = [resolve_app_token(t, serial) for t in args.package]
    audio = ["--no-audio"]
    if not args.no_audio:
        aname, aflags = _pick_audio(serial, with_none=False)
        audio = list(aflags) if aname != "none" else ["--no-audio"]
    procs = []
    for one in pkg:
        title = args.title if len(pkg) == 1 and args.title else one
        cmd = [binary, "-s", serial, *audio, "--window-title", title]
        if args.size:
            cmd.append(f"--new-display={args.size}")
        else:
            cmd.append("--new-display=500x1000")
        cmd.append("--flex-display")
        if args.fps:
            cmd.append(f"--max-fps={args.fps}")
        if args.bitrate:
            cmd.append(f"--video-bit-rate={args.bitrate}")
        if args.fullscreen or args.maximize:
            cmd.append("-f")
        if args.stay_awake:
            cmd.append("--stay-awake")
        if args.on_top:
            cmd.append("--always-on-top")
        if args.show_touches:
            cmd.append("--show-touches")
        if args.turn_screen_off:
            cmd.append("--turn-screen-off")
        cmd += list(args.pass_args or [])
        cmd += ["--start-app", one]
        logf = open(os.path.join(TMP, f"scrcx-window-{one}.log"), "wb")
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        procs.append({"package": one, "pid": proc.pid, "title": title, "log": logf.name})
        time.sleep(4)
    out({"ok": True, "windows": procs, "note": "each window is free-floating; drag/resize like a desktop program"})


def cmd_devices(args):
    devs = []
    for d in adb.list_devices():
        state = adb.read_state(d["serial"])
        devs.append({"serial": d["serial"], "model": d["model"], "session": bool(state)})
    out({"devices": devs})


def cmd_connect(args):
    serial = resolve_serial(args)
    state = adb.read_state(serial)
    if state:
        broken = (not state.get("bound")) or state.get("info", {}).get("video_error")
        if not broken:
            out({"ok": True, "note": "session already running", "state": state})
            return
        try:
            os.kill(state["pid"], 15)
        except Exception:
            pass
        deadline = time.time() + 6
        while time.time() < deadline:
            time.sleep(0.3)
            if not adb.read_state(serial):
                break
        try:
            os.kill(state["pid"], 9)
        except Exception:
            pass
    cmd = [sys.executable, "-m", "scrcx.daemon", "--serial", serial, "--port", str(adb.free_port())]
    if args.server_jar:
        cmd += ["--server-jar", args.server_jar]
    cmd += ["--ring-seconds", str(args.ring_seconds), "--ring-fps", str(args.ring_fps), "--ring-width", str(args.ring_width), "--max-size", str(args.max_size)]
    os.makedirs(adb.STATE_DIR, exist_ok=True)
    log_path = os.path.join(adb.STATE_DIR, f"daemon-{serial}.log")
    logf = open(log_path, "ab")
    env = dict(os.environ)
    env["PYTHONPATH"] = PROJ
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True, env=env)
    deadline = time.time() + 15
    while time.time() < deadline:
        state = adb.read_state(serial)
        if state:
            out({"ok": True, "pid": proc.pid, "daemon_log": log_path, "state": state})
            return
        if proc.poll() is not None:
            tail = ""
            try:
                with open(log_path, "rb") as fh:
                    tail = fh.read()[-600:].decode("utf-8", "replace")
            except Exception:
                pass
            out({"ok": False, "error": "daemon exited", "daemon_log_tail": tail})
            return
        time.sleep(0.25)
    out({"ok": False, "error": "timed out waiting for daemon"})


def cmd_status(args):
    serial = resolve_serial(args)
    state = adb.read_state(serial)
    if not state:
        out({"serial": serial, "session": None})
        return
    info = api_get(serial, "/info")
    out({"serial": serial, "session": info})


def cmd_disconnect(args):
    serial = resolve_serial(args)
    state = adb.read_state(serial)
    if not state:
        out({"ok": True, "note": "no session"})
        return
    os.kill(state["pid"], 15)
    deadline = time.time() + 5
    while time.time() < deadline and adb.read_state(serial):
        time.sleep(0.2)
    out({"ok": True})


# ------------------------------------------------- agent loop


def screenshot_file(serial, path=None):
    path = path or os.path.join(TMP, f"scrcx-{int(time.time() * 1000)}.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    res = api_call(serial, {"op": "screenshot", "out": os.path.abspath(path)})
    body = res.get("result", res)
    return body["file"]


def ocr_words(png, box=None):
    cmd = ["tesseract", png, "stdout", "tsv", "--psm", "11", "-l", "eng"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("tesseract failed: " + proc.stderr.decode()[:300])
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    if not lines:
        return []
    words = []
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) != 12:
            continue
        text = parts[11].strip()
        if not text or not any(ch.isalnum() for ch in text):
            continue
        try:
            conf = float(parts[10])
        except ValueError:
            continue
        if conf < 45 or conf == 95:
            continue
        l, t, w, h = int(parts[6]), int(parts[7]), int(parts[8]), int(parts[9])
        if box:
            l += box[0]
            t += box[1]
        words.append({"text": text, "x": l + w // 2, "y": t + h // 2, "left": l, "top": t, "w": w, "h": h, "conf": conf})
    return words


def find_in_words(words, needle, nth):
    nl = needle.lower()
    rows = {}
    for w in words:
        key = (w["top"], w["y"])
        rows.setdefault(key, []).append(w)
    candidates = []
    for key, ws in sorted(rows.items()):
        ws.sort(key=lambda w: w["x"])
        joined = " ".join(w["text"] for w in ws)
        if nl in joined.lower():
            first = ws[0]
            last = ws[-1]
            candidates.append(
                {
                    "text": joined,
                    "x": first["left"] + (last["x"] + last["w"] // 2 - first["left"]) // 2,
                    "y": first["y"],
                }
            )
    if not candidates:
        exact = [w for w in words if nl in w["text"].lower()]
        candidates = [{"text": w["text"], "x": w["x"], "y": w["y"]} for w in exact]
    if nth < len(candidates):
        return candidates[nth]
    return None


def png_dims(path):
    with open(path, "rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("not a png")
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def cmd_see(args):
    serial = resolve_serial(args)
    png = screenshot_file(serial)
    words = ocr_words(png)
    try:
        info = api_get(serial, "/info")
        hud = api_call(serial, {"op": "hud"}).get("result", {})
    except Exception as exc:
        out({"ok": False, "error": f"session error: {exc}"})
        sys.exit(2)
    vw, vh = info.get("screen") or (0, 0)
    cw, ch = 0, 0
    try:
        cw, ch = png_dims(png)
    except Exception:
        cw = ch = 0
    fx = (vw / cw) if vw and cw else 1.0
    fy = (vh / ch) if vh and ch else 1.0
    for w in words:
        w["sx"], w["sy"] = w["x"], w["y"]
        w["x"] = int(round(w["x"] * fx))
        w["y"] = int(round(w["y"] * fy))
        w["left"] = int(round(w["left"] * fx))
        w["top"] = int(round(w["top"] * fy))
    out(
        {
            "ok": True,
            "device": info.get("device"),
            "capture_size": [cw, ch],
            "screen": [vw, vh],
            "scale": [round(fx, 4), round(fy, 4)],
            "battery": hud.get("battery"),
            "thermal": hud.get("thermal"),
            "screenshot": png,
            "note": "x,y are tap-ready in video coords; sx,sy are raw screenshot coords",
            "words": words,
        }
    )


def cmd_find(args):
    serial = resolve_serial(args)
    png = screenshot_file(serial)
    hit = find_in_words(ocr_words(png), args.text, args.nth)
    out({"found": hit})


def cmd_tap(args):
    serial = resolve_serial(args)
    if args.text is not None:
        png = screenshot_file(serial)
        hit = find_in_words(ocr_words(png), args.text, args.nth)
        if not hit:
            out({"ok": False, "error": f"OCR: no match for {args.text!r}"})
            sys.exit(3)
        res = api_call(serial, {"op": "tap", "x": hit["x"], "y": hit["y"]})
        out({"ok": True, "tapped": hit, "res": res})
        return
    if len(args.xy) != 2:
        raise RuntimeError("tap needs X Y coordinates (or --text)")
    out(api_call(serial, {"op": "tap", "x": args.xy[0], "y": args.xy[1]}))


def simple_xy(args, op, extra=None):
    serial = resolve_serial(args)
    payload = {"op": op}
    if len(args.xy) < 2:
        raise RuntimeError(f"{op} needs at least X Y")
    payload["x"] = args.xy[0]
    payload["y"] = args.xy[1]
    if extra == "long_press":
        payload["ms"] = args.xy[2] if len(args.xy) > 2 else 600
    elif extra == "scroll":
        payload["dx"] = args.xy[2] if len(args.xy) > 2 else 0
        payload["dy"] = args.xy[3] if len(args.xy) > 3 else 0
    out(api_call(serial, payload))


def cmd_swipe(args):
    serial = resolve_serial(args)
    if len(args.xy) < 4:
        raise RuntimeError("swipe needs x1 y1 x2 y2 [ms]")
    ms = args.xy[4] if len(args.xy) > 4 else 300
    out(api_call(serial, {"op": "swipe", "x1": args.xy[0], "y1": args.xy[1], "x2": args.xy[2], "y2": args.xy[3], "ms": ms}))


def cmd_text(args):
    out(api_call(resolve_serial(args), {"op": "text", "text": args.text}))


def cmd_key(args):
    key = args.key
    action = args.action or "tap"
    out(api_call(resolve_serial(args), {"op": "key", "key": key, "action": action}))


def cmd_action(args):
    out(api_call(resolve_serial(args), {"op": args.op}))


def cmd_clip_get(args):
    out(api_call(resolve_serial(args), {"op": "clipboard_get"}))


def cmd_clip_set(args):
    out(api_call(resolve_serial(args), {"op": "clipboard_set", "text": " ".join(args.text), "paste": bool(args.paste)}))


def cmd_ocr(args):
    serial = resolve_serial(args)
    box = args.box
    if box and len(args.xy) >= 2:
        raise RuntimeError("give either --box L T W H or plain L T (defaults)")
    if not box:
        if len(args.xy) >= 2:
            box = [args.xy[0], args.xy[1], args.w or 700, args.h or 300]
    png = screenshot_file(serial)
    words = ocr_words(png, box)
    text = " ".join(w["text"] for w in words)
    res = {"ok": True, "screenshot": png, "box": box, "text": text, "words": words}
    out(res)
    if args.clip:
        api_call(serial, {"op": "clipboard_set", "text": text, "paste": False})


def cmd_screenshot(args):
    f = screenshot_file(resolve_serial(args), args.out)
    out({"ok": True, "file": f})


# ------------------------------------------------- time machine


def cmd_rewind(args):
    serial = resolve_serial(args)
    path = os.path.abspath(args.save or os.path.join(TMP, f"rewind-{int(time.time() * 1000)}.png"))
    res = api_call(serial, {"op": "ring_get", "ago": args.ago, "out": path})
    body = res.get("result", res)
    out({"ok": True, "ago": args.ago, "file": body.get("file"), "frame_age_s": body.get("age")})


def cmd_timeline(args):
    info = api_get(resolve_serial(args), "/info")
    span = info.get("ring_range") or [0, None, None]
    out({"buffered_seconds": span[0], "ring_frames": info.get("ring_size"), "oldest_age_s": span[0], "fps": info.get("fps_buffer"), "use": "scrcx rewind --ago <seconds up to this>"})


# ------------------------------------------------- record/replay


def cmd_record(args):
    serial = resolve_serial(args)
    path = os.path.abspath(args.file)
    if args.action == "start":
        out(api_call(serial, {"op": "record_start", "file": path}))
    else:
        out(api_call(serial, {"op": "record_stop"}))


def cmd_replay(args):
    out(api_call(resolve_serial(args), {"op": "replay", "file": os.path.abspath(args.file), "speed": args.speed, "times": args.times}, timeout=1800))


# ------------------------------------------------- hud/watch


def cmd_hud(args):
    serial = resolve_serial(args)
    info = api_get(serial, "/info")
    res = api_call(serial, {"op": "hud"}).get("result", {})
    out({"ok": True, "info": info, "battery": res.get("battery"), "thermal": res.get("thermal")})


def cmd_watch(args):
    serial = resolve_serial(args)
    state = adb.read_state(serial)
    if not state:
        out({"ok": False, "error": "no session; run scrcx connect"})
        sys.exit(2)
    port = state["api_port"]
    host = "127.0.0.1"
    if args.lan:
        host = "<this-pc-ip>"
    out({"ok": True, "url": f"http://{host}:{port}/stream", "page": f"http://{host}:{port}/", "note": "--lan at connect time binds 0.0.0.0"})


# ------------------------------------------------- apps / files


def cmd_app(args):
    serial = resolve_serial(args)
    if args.action == "list":
        named = bool(args.names)
        data = api_call(serial, {"op": "apps_named" if named else "apps_list"}).get("result", {})
        if named:
            apps = data.get("apps", [])
            cached = [{"name": a["name"], "package": a["package"]} for a in apps]
            out({"count": len(cached), "apps": cached[:400]})
        else:
            apps = data.get("apps", [])
            out({"count": len(apps), "packages": apps[:400]})
    elif args.action == "start":
        if not args.package:
            raise RuntimeError("app start needs <package>")
        out(api_call(serial, {"op": "app_start", "package": args.package}))
    else:
        if not args.package:
            raise RuntimeError("app stop needs <package>")
        out(api_call(serial, {"op": "app_stop", "package": args.package}))


def _cached_apps():
    try:
        with open(os.path.expanduser("~/.local/state/scrcx/apps.json")) as fh:
            return json.load(fh)
    except Exception:
        return []


def resolve_app_token(token, serial):
    if "." in token:
        return token
    cache = _cached_apps()
    if not cache:
        body = api_call(serial, {"op": "apps_named"}).get("result", {})
        cache = body.get("apps", [])
    if not cache:
        raise RuntimeError(f"no app cache; run: scrcx app list --names  (token={token!r})")
    from .daemon import resolve_app
    return resolve_app(token, cache)


def split_spec(spec):
    if spec.startswith("pc:"):
        return "pc", spec[3:]
    serial, _, path = spec.partition(":")
    if not path:
        raise RuntimeError(f"file spec should be <serial>:/path or pc:/path (got {spec!r})")
    if serial != "pc":
        if serial not in {d["serial"] for d in adb.list_devices()}:
            raise RuntimeError(f"serial {serial} not attached")
    return serial, path


def cmd_file(args):
    spec_a, spec_b = args.spec
    serial_a, path_a = split_spec(spec_a)
    serial_b, path_b = split_spec(spec_b)
    tmp = os.path.join(TMP, "scrcx-file")
    os.makedirs(tmp, exist_ok=True)
    carrier = os.path.join(tmp, f"{int(time.time() * 1000)}-{os.path.basename(path_b) or 'file.bin'}")
    if serial_a == "pc":
        subprocess.run(["cp", path_a, carrier], check=True)
    else:
        adb.adb_cmd("pull", path_a, carrier, serial=serial_a, binary=True, timeout=300)
    if serial_b == "pc":
        os.makedirs(os.path.dirname(os.path.abspath(path_b)), exist_ok=True)
        subprocess.run(["cp", carrier, path_b], check=True)
        res = {"ok": True, "from": spec_a, "to": path_b}
    else:
        adb.adb_cmd("push", carrier, path_b, serial=serial_b, binary=True, timeout=300)
        res = {"ok": True, "from": spec_a, "to": spec_b}
    try:
        os.remove(carrier)
    except OSError:
        pass
    out(res)


def cmd_push(args):
    serial = resolve_serial(args)
    adb.adb_cmd("push", args.local, args.remote, serial=serial, binary=True, timeout=300)
    out({"ok": True, "pushed": [args.local, args.remote]})


def cmd_pull(args):
    serial = resolve_serial(args)
    adb.adb_cmd("pull", args.remote, args.local, serial=serial, binary=True, timeout=300)
    out({"ok": True, "pulled": [args.remote, args.local]})


def cmd_api(args):
    serial = resolve_serial(args)
    if args.post:
        try:
            payload = json.loads(args.post)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"bad json: {exc}")
        out(api_call(serial, payload))
    elif args.get_path:
        out(api_get(serial, args.get_path))
    else:
        raise RuntimeError("api needs --post '{...}' or --get /info")


# ------------------------------------------------- selftest


def jpg_marker():
    return bytes([0xFF, 0xD8]) + bytes([0xFF, 0xE0, 0x00, 0x02, 0x00, 0x00]) + bytes([0xFF, 0xD9])


def cmd_selftest(args=None):
    from .control import parse_device_message
    from .video_reader import FrameRing, _MjpegSlicer

    t = P.encode_touch(0, -1, 100, 200, 1080, 2400)
    assert t[0] == P.TYPE_INJECT_TOUCH_EVENT and len(t) == 32
    k = P.encode_keycode(0, 4)
    assert len(k) == 14 and k[0] == P.TYPE_INJECT_KEYCODE
    x = P.encode_text("héllo")
    assert x[0] == P.TYPE_INJECT_TEXT and int.from_bytes(x[1:5], "big") == len("héllo".encode())
    s = P.encode_scroll(10, 20, 1080, 2400, 0.0, -1.0)
    assert len(s) == 21 and int.from_bytes(s[15:17], "big", signed=True) == -2048
    c = P.encode_set_clipboard(9, "abc", True)
    assert c[0] == P.TYPE_SET_CLIPBOARD and len(c) == 17 and int.from_bytes(c[1:9], "big", signed=True) == 9
    assert parse_device_message(b"\x00" + (3).to_bytes(4, "big", signed=True) + b"abc") == ("clipboard", "abc")

    ring = FrameRing(fps=5, width=64, max_age=5)
    slicer = _MjpegSlicer()
    blob = jpg_marker() * 4
    chunks = [blob[i : i + 5] for i in range(0, len(blob), 5)]
    frames = []
    for chunk in chunks:
        frames.extend(slicer.feed(chunk))
    assert len(frames) == 4, len(frames)
    assert all(f.endswith(b"\xff\xd9") and f.startswith(b"\xff\xd8") for f in frames)
    now = time.time()
    ring.add(now - 4, frames[0])
    ring.add(now - 1, frames[1])
    got = ring.at_ago(3)
    assert got and got[1] == frames[0]
    newest = ring.newest()
    assert newest[1] == frames[1]

    out({"ok": True, "checks": ["control-encoders", "device-parsing", "mjpeg-slicing", "ring-buffer"]})


# ------------------------------------------------- main


def add_xy(p, help_="X Y (and optional extra values)"):
    p.add_argument("xy", nargs="*", type=int, help=help_)


def main(argv=None):
    p = argparse.ArgumentParser(prog="scrcx")
    p.add_argument("--serial")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices").set_defaults(fn=cmd_devices)

    q = sub.add_parser("connect")
    q.add_argument("--lan", action="store_true")
    q.add_argument("--ring-seconds", type=int, default=300)
    q.add_argument("--ring-fps", type=int, default=5)
    q.add_argument("--ring-width", type=int, default=640)
    q.add_argument("--max-size", type=int, default=1280)
    q.add_argument("--server-jar", default=None)
    q.set_defaults(fn=cmd_connect)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("disconnect").set_defaults(fn=cmd_disconnect)

    q = sub.add_parser("tap")
    add_xy(q)
    q.add_argument("--text")
    q.add_argument("--nth", type=int, default=0)
    q.set_defaults(fn=cmd_tap)

    q = sub.add_parser("long_press")
    add_xy(q)
    q.set_defaults(fn=lambda a: simple_xy(a, "long_press", "long_press"))

    q = sub.add_parser("swipe")
    add_xy(q)
    q.set_defaults(fn=cmd_swipe)

    q = sub.add_parser("scroll")
    add_xy(q)
    q.set_defaults(fn=lambda a: simple_xy(a, "scroll", "scroll"))

    q = sub.add_parser("text")
    q.add_argument("text")
    q.set_defaults(fn=cmd_text)

    q = sub.add_parser("key")
    q.add_argument("key")
    q.add_argument("action", nargs="?", default="tap", choices=["tap", "down", "up"])
    q.set_defaults(fn=cmd_key)

    q = sub.add_parser("action")
    q.add_argument("op", choices=["back", "home", "power", "rotate", "expand", "collapse", "screen-on", "screen-off", "back-or-screen-on"])
    q.set_defaults(fn=lambda a: cmd_key_alias(a))

    sub.add_parser("see").set_defaults(fn=cmd_see)

    q = sub.add_parser("find")
    q.add_argument("text")
    q.add_argument("--nth", type=int, default=0)
    q.set_defaults(fn=cmd_find)

    q = sub.add_parser("ocr")
    add_xy(q)
    q.add_argument("--box", nargs=4, type=int, metavar=("L", "T", "W", "H"))
    q.add_argument("--w", type=int, default=None)
    q.add_argument("--h", type=int, default=None)
    q.add_argument("--clip", action="store_true")
    q.set_defaults(fn=cmd_ocr)

    q = sub.add_parser("screenshot")
    q.add_argument("out", nargs="?", default=None)
    q.set_defaults(fn=cmd_screenshot)

    q = sub.add_parser("clip")
    q.add_argument("op", choices=["get", "set"])
    q.add_argument("text", nargs="*")
    q.add_argument("--paste", action="store_true")
    q.set_defaults(fn=lambda a: cmd_clip_get(a) if a.op == "get" else cmd_clip_set(a))

    q = sub.add_parser("rewind")
    q.add_argument("--ago", type=float, default=30.0)
    q.add_argument("--save", default=None)
    q.set_defaults(fn=cmd_rewind)

    sub.add_parser("timeline").set_defaults(fn=cmd_timeline)

    q = sub.add_parser("record")
    q.add_argument("action", choices=["start", "stop"])
    q.add_argument("file", nargs="?", default=os.path.join(TMP, "gestures.json"))
    q.set_defaults(fn=cmd_record)

    q = sub.add_parser("replay")
    q.add_argument("file")
    q.add_argument("--speed", type=float, default=1.0)
    q.add_argument("--times", type=int, default=1)
    q.set_defaults(fn=cmd_replay)

    sub.add_parser("hud").set_defaults(fn=cmd_hud)

    q = sub.add_parser("watch")
    q.add_argument("--lan", action="store_true")
    q.set_defaults(fn=cmd_watch)

    q = sub.add_parser("app")
    q.add_argument("action", choices=["start", "stop", "list"])
    q.add_argument("--names", action="store_true")
    q.add_argument("package", nargs="?")
    q.set_defaults(fn=cmd_app)

    q = sub.add_parser("app-window")
    q.add_argument("package", nargs="+", help="package(s); one window each")
    q.add_argument("--title", default=None)
    q.add_argument("--size", default=None, metavar="WxH", help="fixed phone resolution, window letterboxes (default: flex = window-resizable re-layout)")
    q.add_argument("--maximize", action="store_true", help="start maximized")
    q.add_argument("--no-audio", action="store_true", help="mute the window (default: audio on, auto codec)")
    q.add_argument("--fps", type=int, default=None, help="capture fps cap")
    q.add_argument("--bitrate", default=None, help="e.g. 8M")
    q.add_argument("--fullscreen", action="store_true")
    q.add_argument("--stay-awake", action="store_true")
    q.add_argument("--on-top", action="store_true")
    q.add_argument("--show-touches", action="store_true")
    q.add_argument("--turn-screen-off", action="store_true", help="phone screen blanks while mirroring")
    q.add_argument("--pass", dest="pass_args", nargs=argparse.REMAINDER, default=[], metavar="ARG", help="raw scrcpy flags after this")
    q.set_defaults(fn=cmd_app_window)

    q = sub.add_parser("mirror")
    q.add_argument("--serial", default=None)
    q.add_argument("flags", nargs=argparse.REMAINDER, help="all remaining args go to real scrcpy (first one may need --)")
    q.set_defaults(fn=cmd_mirror)

    q = sub.add_parser("record-video")
    q.add_argument("out", help="destination .mp4/.mkv")
    q.add_argument("--time", type=int, default=None, help="auto-stop after N seconds")
    q.add_argument("--max-fps", type=int, default=None)
    q.add_argument("--max-size", type=int, default=None)
    q.add_argument("--serial", default=None)
    q.add_argument("extra", nargs="*", help=argparse.SUPPRESS)
    q.set_defaults(fn=cmd_record_video)

    q = sub.add_parser("audio-capture")
    q.add_argument("out", help="destination .wav/.m4a/.opus/.flac/.mkv")
    q.add_argument("--time", type=int, default=None, help="auto-stop after N seconds")
    q.add_argument("--mic", action="store_true", help="record phone microphone instead of output")
    q.add_argument("--stereo", action="store_true", help="duplicate channels when input has fewer (stereo force)")
    q.add_argument("--serial", default=None)
    q.set_defaults(fn=cmd_audio_capture)

    q = sub.add_parser("camera")
    q.add_argument("--facing", default="back", choices=["back", "front", "external"])
    q.add_argument("--size", default=None, metavar="WxH")
    q.add_argument("--maximize", action="store_true")
    q.add_argument("--record", default=None, help="also record to file")
    q.add_argument("--serial", default=None)
    q.add_argument("extra", nargs="*", help=argparse.SUPPRESS)
    q.set_defaults(fn=cmd_camera)

    q = sub.add_parser("audio-play")
    q.add_argument("--record", default=None, help="also write the stream to file")
    q.add_argument("--serial", default=None)
    q.set_defaults(fn=cmd_audio_play)

    q = sub.add_parser("v4l2")
    q.add_argument("device", help="/dev/videoN (v4l2loopback)")
    q.add_argument("--size", default=None, help="max capture size like 720")
    q.add_argument("--serial", default=None)
    q.set_defaults(fn=cmd_v4l2)

    q = sub.add_parser("hid")
    q.add_argument("--otg", action="store_true", help="OTG-only mode (no display)")
    q.add_argument("--keyboard", nargs="?", const="true", choices=["uhid", "true"], default=None)
    q.add_argument("--mouse", nargs="?", const="true", choices=["uhid", "true"], default=None)
    q.add_argument("--serial", default=None)
    q.set_defaults(fn=cmd_hid)

    q = sub.add_parser("install")
    q.add_argument("apk")
    q.add_argument("-r", "--replace", action="store_true")
    q.add_argument("--serial", default=None)
    q.set_defaults(fn=cmd_install)

    q = sub.add_parser("send-file")
    q.add_argument("local")
    q.add_argument("--remote", default=None, help="destination path on phone (default /sdcard/Download/<name>)")
    q.add_argument("--scan", action="store_true", help="triggers media scanner broadcast")
    q.add_argument("--serial", default=None)
    q.set_defaults(fn=cmd_send_file)

    q = sub.add_parser("dcaps")
    q.add_argument("what", choices=["encoders", "cameras", "apps"])
    q.add_argument("--serial", default=None)
    q.set_defaults(fn=cmd_dcaps)

    q = sub.add_parser("push")
    q.add_argument("local")
    q.add_argument("remote")
    q.set_defaults(fn=cmd_push)

    q = sub.add_parser("pull")
    q.add_argument("remote")
    q.add_argument("local")
    q.set_defaults(fn=cmd_pull)

    q = sub.add_parser("file")
    q.add_argument("spec", nargs=2)
    q.set_defaults(fn=cmd_file)

    q = sub.add_parser("api")
    q.add_argument("--post")
    q.add_argument("--get", dest="get_path")
    q.set_defaults(fn=cmd_api)

    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)

    q = sub.add_parser("gui")
    q.add_argument("--port", type=int, default=None)
    q.add_argument("--lan", action="store_true")
    q.set_defaults(fn=cmd_gui)

    q = sub.add_parser("unlock")
    q.add_argument("--pin", default=None, help="digits only")
    q.add_argument("--stealth", action="store_true", help="unlock with the screen black (brightness 0 during unlock, brightness restored after)")
    q.add_argument("--wake", action="store_true", help="with --stealth: end with the screen ON showing the phone (otherwise it stays black)")
    q.add_argument("--serial", default=None)
    q.set_defaults(fn=cmd_unlock)

    q = sub.add_parser("gui-app")
    q.set_defaults(fn=cmd_desktop)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except RuntimeError as exc:
        out({"ok": False, "error": str(exc)})
        sys.exit(1)
    except urllib.error.HTTPError as exc:  # noqa: F821
        try:
            body = json.loads(exc.read().decode())
        except Exception:
            body = {"error": str(exc)}
        out({"ok": False, "http": exc.code, "error": body.get("error") if isinstance(body, dict) else body})
        sys.exit(1)


def _phone_state(serial):
    r = subprocess.run(["adb", "-s", serial, "shell", "dumpsys", "power"], capture_output=True, text=True, timeout=20)
    wake = "Dozing" if "mWakefulness=Dozing" in r.stdout else ("Awake" if "mWakefulness=Awake" in r.stdout else "Asleep")
    r2 = subprocess.run(["adb", "-s", serial, "shell", "dumpsys", "window"], capture_output=True, text=True, timeout=20)
    locked = "mdreaminglockscreen=true" in r2.stdout.lower() and "mkeyguardoccluded=false" in r2.stdout.lower()
    return wake, locked


def _screen_dark(serial):
    f = os.path.join(TMP, "unlock-frame.jpg")
    try:
        raw = api_raw(serial, "/ring?ago=0.5", timeout=8)
        if raw[:3] == b"\xff\xd8\xff":
            with open(f, "wb") as fh:
                fh.write(raw)
        else:
            raise RuntimeError("ring empty")
    except Exception:
        try:
            outp = subprocess.run(["adb", "-s", serial, "exec-out", "screencap", "-p"], capture_output=True, timeout=15).stdout
            with open(f, "wb") as fh:
                fh.write(outp)
        except Exception:
            return None
    yavg = None
    try:
        tp = os.path.join(TMP, "yavg.txt")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", f,
             "-lavfi", "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=" + tp,
             "-f", "null", "-"], capture_output=True, timeout=15)
        for line in open(tp):
            if "YAVG" in line:
                yavg = float(line.split("=")[-1].strip().strip("'\""))
                break
    except Exception:
        return None
    if yavg is None:
        return None
    return yavg < 40.0


def cmd_unlock(args):
    serial = resolve_serial(args)
    seq = (args.pin or "").strip()
    if not seq or not seq.isdigit():
        out({"ok": False, "error": "need --pin <digits>"})
        return
    stealth = bool(getattr(args, "stealth", False))
    t0 = time.time()
    wake, locked = _phone_state(serial)
    a = lambda *a_: subprocess.run(["adb", "-s", serial, "shell", "input", *a_], capture_output=True)
    sh = lambda *c_: subprocess.run(["adb", "-s", serial, "shell", *c_], capture_output=True, text=True)
    was = "%s/%s" % (wake, "locked" if locked else "open")
    if wake == "Awake" and not locked:
        out({"ok": True, "already": "unlocked, awake — nothing done", "milliseconds": int((time.time() - t0) * 1000)})
        return
    bri = None
    if stealth:
        r = sh("settings", "get", "system", "screen_brightness")
        v = (r.stdout or "").strip()
        bri = v if v.isdigit() else None
        sh("settings", "put", "system", "screen_brightness", "0")
        time.sleep(0.4)
    if locked:
        a("keyevent", "26")
        time.sleep(0.7)
        wake, locked = _phone_state(serial)
        if wake != "Awake":
            a("keyevent", "26")
            time.sleep(0.7)
            wake, locked = _phone_state(serial)
    for attempt in range(2):
        a("swipe", "540", "2200", "540", "700", "100")
        time.sleep(0.8)
        a("text", seq)
        a("keyevent", "66")
        time.sleep(0.7)
        wake2, locked2 = _phone_state(serial)
        if not locked2:
            break
        a("keyevent", "4")
        time.sleep(0.3)
    if locked2:
        time.sleep(1.5)
        wake2, locked2 = _phone_state(serial)
    dark = False
    if stealth and not locked2 and not args.wake:
        try:
            api_call(serial, {"op": "screen_off"})
            dark = True
        except Exception:
            if _screen_off_shot(serial):
                dark = True
        time.sleep(0.5)
    if stealth and bri is not None:
        sh("settings", "put", "system", "screen_brightness", bri)
        time.sleep(0.6)
    tail = {}
    if stealth:
        w3, l3 = _phone_state(serial)
        tail = {"screen": "on" if args.wake else ("off" if dark else "kept-black"), "now_unlocked": not l3, "brightness_restored": bri}
    res = {"ok": not locked2, "was": was, "now": wake2 + ("/locked" if locked2 else "/open"), "attempts": attempt + 1, "milliseconds": int((time.time() - t0) * 1000)}
    if not stealth:
        res["pin"] = seq
    res.update(tail)
    out(res)


def _screen_off_shot(serial):
    binary = find_scrcpy_binary()
    if not binary:
        return False
    try:
        subprocess.Popen(
            [binary, "-s", serial, "--no-window", "--no-video", "--no-audio", "--turn-screen-off", "--time-limit=2"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).wait(timeout=10)
        return True
    except Exception:
        return False


def _screen_off_adb(serial):
    wake, _locked = _phone_state(serial)
    if wake == "Dozing":
        return
    subprocess.run(["adb", "-s", serial, "shell", "input", "keyevent", "26"], capture_output=True)


def _screen_on_adb(serial):
    wake, locked = _phone_state(serial)
    if wake == "Awake" and not locked:
        return
    subprocess.run(["adb", "-s", serial, "shell", "input", "keyevent", "224"], capture_output=True)
    time.sleep(0.9)
    wake, locked = _phone_state(serial)
    if wake == "Awake" and not locked:
        return
    subprocess.run(["adb", "-s", serial, "shell", "input", "keyevent", "26"], capture_output=True)
    time.sleep(0.9)


def cmd_key_alias(a):
    keymap = {
        "back": "back",
        "home": "home",
        "power": "power",
        "rotate": "rotate_cmd",
        "expand": "expand",
        "collapse": "collapse",
        "screen-on": "screen_on",
        "screen-off": "screen_off",
        "back-or-screen-on": "back_or_screen_on",
    }
    op = keymap[a.op]
    serial = resolve_serial(a)
    if op.endswith("_cmd"):
        out(api_call(serial, {"op": "rotate"}))
    elif op in ("screen_on", "screen_off"):
        serial = resolve_serial(a)
        try:
            res = api_call(serial, {"op": op})
            route = "daemon"
        except Exception:
            res = {}
            route = "adb"
        if not res.get("ok"):
            if op == "screen_on":
                _screen_on_adb(serial)
            else:
                _screen_off_adb(serial)
        elif op == "screen_on":
            _screen_on_adb(serial)
        elif op == "screen_off":
            time.sleep(1.5)
            w2, l2 = _phone_state(serial)
            if w2 == "Awake" and not l2:
                subprocess.run(["adb", "-s", serial, "shell", "input", "keyevent", "26"], capture_output=True)
                route = "adb"
        out({"ok": True, "result": route})
    elif op in ("back_or_screen_on", "expand", "collapse"):
        out(api_call(serial, {"op": op if op != "back_or_screen_on" else "back_or_screen_on"}))
    else:
        out(api_call(serial, {"op": "key", "key": op, "action": "tap"}))


if __name__ == "__main__":
    main()

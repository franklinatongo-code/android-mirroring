import json
import os
import shutil
import socket
import subprocess
import time

SERVER_VERSION = "5.0.1"
SERVER_REMOTE = "/data/local/tmp/scrcpy-server-sx.jar"
STATE_DIR = os.path.expanduser("~/.local/state/scrcx")


def adb(*args, serial=None, binary=False, timeout=30):
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    res = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if binary:
        return res.stdout
    out = res.stdout.decode("utf-8", "replace")
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"adb {' '.join(args)} failed: {err or out.strip()}")
    return out


adb_cmd = adb


def list_devices():
    out = adb("devices", "-l")
    devices = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "device":
            info = {}
            for item in parts[3:]:
                _, _, kv = item.partition(":")
                info[kv.partition(":")[0]] = item
            devices.append(
                {
                    "serial": parts[0],
                    "model": parts[2].replace("model:", ""),
                    "raw": line.strip(),
                }
            )
    return devices


def device_name(serial=None):
    prop = adb("shell", "getprop", "ro.product.model", serial=serial).strip()
    if not prop:
        prop = adb("shell", "getprop", "ro.product.device", serial=serial).strip()
    return prop or "android"


def ensure_server(serial=None, server_jar=None):
    if server_jar is None:
        raise ValueError("server_jar required")
    md5_local = subprocess.run(["md5sum", server_jar], capture_output=True).stdout.split()[0]
    md5_remote = ""
    try:
        md5_remote = adb("shell", "md5sum", SERVER_REMOTE, serial=serial, binary=False).split()[0]
    except RuntimeError:
        md5_remote = ""
    if md5_local != md5_remote:
        adb("push", server_jar, SERVER_REMOTE, serial=serial, timeout=120)


def shell(serial, cmd, timeout=30):
    return adb("shell", cmd, serial=serial, timeout=timeout)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def random_scid():
    return int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF


class ServerProcess:
    def __init__(self, serial, server_jar, scid=None, video=True, audio=False, control=True, log_path=None, max_size=1600, max_fps=None, bit_rate=None, tunnel_forward=False):
        self.serial = serial
        self.scid = scid if scid is not None else random_scid()
        self.tunnel_port = free_port()
        self.socket_name = f"scrcpy_{self.scid:08x}"
        self.tunnel_forward = tunnel_forward
        ensure_server(serial, server_jar)

        params = [
            f"scid={self.scid:08x}",
            "log_level=info",
            f"video={'true' if video else 'false'}",
            f"audio={'true' if audio else 'false'}",
            f"control={'true' if control else 'false'}",
            "send_device_meta=true",
            "send_frame_meta=true",
            "send_stream_meta=true",
            "clipboard_autosync=false",
            f"max_size={max_size}",
        ]
        if tunnel_forward:
            params.append("tunnel_forward=true")
        if max_fps:
            params.append(f"max_fps={max_fps}")
        if bit_rate:
            params.append(f"video_bit_rate={bit_rate}")

        cmd = f"CLASSPATH={SERVER_REMOTE} app_process / com.genymobile.scrcpy.Server {SERVER_VERSION} " + " ".join(params)

        if tunnel_forward:
            adb("forward", f"tcp:{self.tunnel_port}", f"localabstract:{self.socket_name}", serial=serial)
        else:
            self.listener = socket.socket()
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind(("127.0.0.1", self.tunnel_port))
            self.listener.listen(8)
            adb("reverse", f"localabstract:{self.socket_name}", f"tcp:{self.tunnel_port}", serial=serial)

        if log_path:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            logf = open(log_path, "ab")
        else:
            logf = subprocess.DEVNULL
        self.proc = subprocess.Popen(
            ["adb", "-s", serial, "shell", cmd],
            stdin=subprocess.PIPE,
            stdout=logf,
            stderr=subprocess.STDOUT,
        )

    def sockets(self, count, timeout=15):
        out = []
        if self.tunnel_forward:
            for _ in range(count):
                s = socket.create_connection(("127.0.0.1", self.tunnel_port), timeout=timeout)
                s.settimeout(None)
                out.append(s)
            return out
        deadline = time.time() + timeout
        while len(out) < count and time.time() < deadline:
            self.listener.settimeout(max(0.2, deadline - time.time()))
            try:
                conn, _ = self.listener.accept()
            except OSError:
                break
            conn.settimeout(None)
            out.append(conn)
        return out

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            if not self.tunnel_forward:
                adb("reverse", "--remove", f"localabstract:{self.socket_name}", serial=self.serial, timeout=5)
            else:
                adb("forward", "--remove", f"tcp:{self.tunnel_port}", serial=self.serial, timeout=5)
        except Exception:
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        try:
            if hasattr(self, "listener") and self.listener:
                self.listener.close()
        except Exception:
            pass


def state_path(serial):
    return os.path.join(STATE_DIR, f"{serial}.json")


def read_state(serial):
    p = state_path(serial)
    if os.path.exists(p):
        try:
            with open(p) as fh:
                state = json.load(fh)
            port = state.get("api_port")
            if port:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                        return state
                except OSError:
                    os.remove(p)
        except Exception:
            os.remove(p)
    return None


def write_state(serial, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(state_path(serial), "w") as fh:
        json.dump(state, fh)


def get_server_jar():
    env = os.environ.get("SCRCX_SERVER_JAR")
    if env and os.path.exists(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    proj = os.path.abspath(os.path.join(here, ".."))
    vendored = os.path.join(proj, "vendor", "scrcpy-server-v5.0.1")
    if os.path.exists(vendored):
        return vendored
    legacy = os.path.abspath(os.path.join(here, "..", "..", "offline-setup", "scrcpy-server-v5.0.1"))
    if os.path.exists(legacy):
        return legacy
    if shutil.which("scrcpy"):
        guess = "/usr/libexec/scrcpy-update-server"
        if os.path.exists(guess):
            return guess
    raise FileNotFoundError("scrcpy-server jar not found; run ./setup.sh or pass --server-jar")

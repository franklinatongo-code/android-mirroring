import subprocess
import threading
import time
from collections import deque

CODEC_H264 = 0x68323634
CODEC_H265 = 0x68323635
CODEC_AV1 = 0x00617631

FLAG_SESSION = 0x8000000000000000
FLAG_CONFIG = 0x4000000000000000


def _read_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("video socket closed")
        buf += chunk
    return bytes(buf)


class FrameRing:
    def __init__(self, fps=5, width=640, quality=4, max_age=300):
        self.fps = fps
        self.width = width
        self.quality = quality
        self.max_age = max_age
        self.ring = deque(maxlen=max(60, fps * max_age))
        self.lock = threading.Lock()
        self.latest = None
        self.frames_seen = 0
        self.bytes_seen = 0

    def add(self, t, data):
        with self.lock:
            self.ring.append((t, data))
            self.latest = (t, data)
            self.frames_seen += 1

    def size(self):
        with self.lock:
            return len(self.ring)

    def newest(self):
        with self.lock:
            return self.latest

    def at_ago(self, seconds):
        with self.lock:
            if not self.ring:
                return None
            now = self.ring[-1][0]
            target = now - max(0.0, float(seconds))
            best = None
            for t, data in self.ring:
                if best is None or abs(t - target) < abs(best[0] - target):
                    best = (t, data)
            return best

    def span(self):
        with self.lock:
            if len(self.ring) < 2:
                return (None, None)
            return (self.ring[0][0], self.ring[-1][0])


class _MjpegSlicer:
    def __init__(self):
        self.buf = bytearray()
        self.start = None

    def feed(self, chunk):
        self.buf += chunk
        out = []
        while True:
            if self.start is None:
                idx = self.buf.find(b"\xff\xd8")
                if idx == -1:
                    if len(self.buf) > 2:
                        self.buf = self.buf[-1:]
                    break
                self.buf = self.buf[idx:]
                self.start = 0
            end = self.buf.find(b"\xff\xd9", 2)
            if end == -1:
                break
            out.append(bytes(self.buf[: end + 2]))
            self.buf = bytearray(self.buf[end + 2 :])
            self.start = None
        return out


class VideoThread(threading.Thread):
    FORMAT_BY_ID = {CODEC_H264: "h264", CODEC_H265: "h265", CODEC_AV1: "av1"}

    def __init__(self, sock, ring, forward_dummy=False):
        super().__init__(daemon=True)
        self.sock = sock
        self.ring = ring
        self.forward_dummy = forward_dummy
        self.device_name = ""
        self.width = 0
        self.height = 0
        self.codec = "h264"
        self.error = None
        self.ready = threading.Event()
        self._proc = None
        self._stop = threading.Event()
        self._reader_done = threading.Event()

    def run(self):
        try:
            if self.forward_dummy:
                _read_exact(self.sock, 1)
            self.device_name = _read_exact(self.sock, 64).split(b"\x00")[0].decode("utf-8", "replace") or "android"
            codec_id = int.from_bytes(_read_exact(self.sock, 4), "big")
            if codec_id in (0, 1):
                self.error = "device reported stream-disabled"
                return
            self.codec = self.FORMAT_BY_ID.get(codec_id)
            if self.codec is None:
                self.error = f"unknown codec id 0x{codec_id:x}"
                return
            self._pump()
        except Exception as exc:
            self.error = repr(exc)
        finally:
            self.ready.set()
            self._reader_done.wait(timeout=30)
            self._stop.set()

    def _pump(self):
        self._proc = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                self.codec,
                "-i",
                "pipe:0",
                "-an",
                "-sn",
                "-vf",
                f"fps={self.ring.fps},scale={self.ring.width}:-2",
                "-q:v",
                str(self.ring.quality),
                "-f",
                "mjpeg",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=-1,
        )
        reader = threading.Thread(target=self._read_decoded, daemon=True)
        reader.start()
        try:
            while not self._stop.is_set():
                pkt = self._read_packet()
                if pkt is None:
                    break
                kind, body = pkt
                if kind in ("media", "config"):
                    self._proc.stdin.write(body)
                    self.ring.bytes_seen += len(body)
                elif kind == "session":
                    self.width = int.from_bytes(body[4:8], "big")
                    self.height = int.from_bytes(body[8:12], "big")
                elif kind == "disable":
                    self.error = "device disabled the stream"
                    self._stop.set()
                    break
        except (ConnectionError, OSError, BrokenPipeError, ValueError):
            pass
        finally:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            reader.join(timeout=30)
            self._reader_done.set()

    def _read_packet(self):
        first = _read_exact(self.sock, 12)
        if len(first) < 12:
            return None
        u64 = int.from_bytes(first[0:8], "big")
        size = int.from_bytes(first[8:12], "big")
        if u64 & FLAG_SESSION:
            return ("session", first)
        if u64 & FLAG_CONFIG:
            return ("config", _read_exact(self.sock, size))
        return ("media", _read_exact(self.sock, size))

    def _read_decoded(self):
        slicer = _MjpegSlicer()
        proc = self._proc
        err_seen = time.time() + 6
        try:
            while not self._stop.is_set():
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    if not self.error and self.ring.size() == 0 and time.time() > err_seen:
                        self.error = "ffmpeg decoder produced no frames"
                    break
                err_seen = time.time() + 6
                for frame in slicer.feed(chunk):
                    self.ring.add(time.time(), frame)
        except Exception as exc:
            if not self.error:
                self.error = f"decoder-reader: {exc!r}"

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except Exception:
            pass
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass

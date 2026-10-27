import threading
import time

from . import protocol as P


class DeviceMessages:
    def __init__(self):
        self.lock = threading.Lock()
        self.clipboard = None
        self.clipboard_seq = -1
        self.acks = {}
        self.uhid_output = []


def _read_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("control socket closed")
        buf += chunk
    return bytes(buf)


def _read_int(data, off, n):
    return int.from_bytes(data[off : off + n], "big", signed=(n == 4 and data[off] & 0x80))


def parse_device_message(data):
    t = data[0]
    if t == 0:
        ln = int.from_bytes(data[1:5], "big", signed=True)
        text = data[5 : 5 + max(0, ln)].decode("utf-8", "replace")
        return ("clipboard", text)
    if t == 1:
        seq = int.from_bytes(data[1:9], "big", signed=True)
        return ("ack_clipboard", seq)
    if t == 2:
        uid = int.from_bytes(data[1:3], "big")
        ln = int.from_bytes(data[3:5], "big")
        return ("uhid_output", uid, bytes(data[5 : 5 + ln]))
    return ("unknown", data)


class ControlChannel:
    def __init__(self, sock, screen=(1080, 2400), messages=None):
        self.sock = sock
        self.screen = screen
        self.msgs = messages if messages else DeviceMessages()
        self.writable_statuses = {}
        self._seq = 0
        self._recording = None
        self._record_started = 0.0
        self._send_lock = threading.Lock()
        self.receiver = threading.Thread(target=self._rx_loop, daemon=True)
        self.receiver.start()

    def bind_screen(self, width, height):
        self.screen = (width, height)

    # ---------- low-level send / receive ----------

    def send(self, payload):
        with self._send_lock:
            self.sock.sendall(payload)
            if self._recording is not None:
                self._maybe_record(payload)

    def _rx_loop(self):
        try:
            while True:
                head = _read_exact(self.sock, 1)
                t = head[0]
                if t == 0:
                    data = _read_exact(self.sock, 4)
                    ln = int.from_bytes(data, "big", signed=True)
                    text = _read_exact(self.sock, max(0, ln)).decode("utf-8", "replace")
                    with self.msgs.lock:
                        self.msgs.clipboard = text
                        self.msgs.clipboard_seq += 1
                        self.msgs.acks["clipboard"] = time.time()
                elif t == 1:
                    data = _read_exact(self.sock, 8)
                    seq = int.from_bytes(data, "big", signed=True)
                    with self.msgs.lock:
                        self.msgs.acks[seq] = time.time()
                elif t == 2:
                    data = _read_exact(self.sock, 4)
                    uid = int.from_bytes(data[0:2], "big")
                    ln = int.from_bytes(data[2:4], "big")
                    pl = _read_exact(self.sock, ln)
                    with self.msgs.lock:
                        self.msgs.uhid_output.append((uid, pl))
                else:
                    break
        except Exception:
            pass

    # ---------- recording ---------

    def set_recording(self, sink):
        self._recording = sink
        self._record_started = time.time()

    def _maybe_record(self, payload):
        entry = self._classify_send(payload)
        if entry is None:
            return
        step = (round(time.time() - self._record_started, 3),) + entry
        self._recording(step)

    def _classify_send(self, payload):
        t = payload[0]
        scr = self.screen or (1080, 2400)
        if t == P.TYPE_INJECT_TOUCH_EVENT:
            action, x, y = _decode_touch(payload)
            return ({"0": "tap-down", "1": "tap-up", "2": "tap-move"}[str(action)], [x, y, scr[0], scr[1]])
        if t == P.TYPE_INJECT_KEYCODE:
            action = payload[1]
            code = _decode_key(payload)
            return ("key-down" if action == 0 else "key-up", [code])
        if t == P.TYPE_INJECT_TEXT:
            text = payload[5:].decode("utf-8", "replace")
            return ("text", [text])
        if t == P.TYPE_INJECT_SCROLL_EVENT and len(payload) >= 21:
            x, y = _decode_scroll_pos(payload)
            h, v = _decode_scroll(payload)
            return ("scroll", [x, y, h / 2048.0 * 16, v / 2048.0 * 16])
        return None

    # ---------- high level ops ----------

    def key(self, name_or_code, action="tap", meta=0):
        code = P.AKEYCODE_MAP.get(str(name_or_code).lower())
        if code is None:
            code = int(name_or_code)
        if action == "down":
            self.send(P.encode_keycode(P.ACTION_DOWN, code, 0, meta))
        elif action == "up":
            self.send(P.encode_keycode(P.ACTION_UP, code, 0, meta))
        else:
            self.send(P.encode_keycode(P.ACTION_DOWN, code, 0, meta))
            time.sleep(0.045)
            self.send(P.encode_keycode(P.ACTION_UP, code, 0, meta))

    def text(self, s):
        self.send(P.encode_text(s))

    def tap(self, x, y):
        w, h = self.screen
        self.send(P.encode_touch(0, -1, x, y, w, h))
        time.sleep(0.035)
        self.send(P.encode_touch(1, -1, x, y, w, h))

    def long_press(self, x, y, ms=600):
        w, h = self.screen
        self.send(P.encode_touch(0, -1, x, y, w, h))
        time.sleep(ms / 1000.0)
        self.send(P.encode_touch(1, -1, x, y, w, h))

    def swipe(self, x1, y1, x2, y2, ms=300, pointer_id=-1):
        w, h = self.screen
        steps = max(2, int(ms // 20))
        self.send(P.encode_touch(0, pointer_id, x1, y1, w, h))
        sleep_amt = ms / float(steps) / 1000.0
        for i in range(1, steps + 1):
            x = x1 + (x2 - x1) * i / steps
            y = y1 + (y2 - y1) * i / steps
            time.sleep(sleep_amt)
            self.send(P.encode_touch(2, pointer_id, round(x), round(y), w, h))
        time.sleep(0.02)
        self.send(P.encode_touch(1, pointer_id, x2, y2, w, h))

    def scroll(self, x, y, dx, dy, buttons=0):
        w, h = self.screen
        self.send(P.encode_scroll(x, y, w, h, dx, dy, buttons))

    def back_or_screen_on(self, down=True):
        self.send(P.encode_back_or_screen_on(P.ACTION_DOWN if down else P.ACTION_UP))

    def expand_notifications(self):
        self.send(P.encode_simple(P.TYPE_EXPAND_NOTIFICATION_PANEL))

    def expand_settings(self):
        self.send(P.encode_simple(P.TYPE_EXPAND_SETTINGS_PANEL))

    def collapse_panels(self):
        self.send(P.encode_simple(P.TYPE_COLLAPSE_PANELS))

    def rotate(self):
        self.send(P.encode_simple(P.TYPE_ROTATE_DEVICE))

    def display_power(self, on):
        self.send(P.encode_set_display_power(on))

    def get_clipboard(self, timeout=2.0):
        seq = self._new_seq()
        starter = self.msgs.acks.get("clipboard")
        self.send(P.encode_get_clipboard(P.COPY_KEY_COPY))
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self.msgs.lock:
                if self.msgs.acks.get("clipboard", 0) != starter:
                    return self.msgs.clipboard
            time.sleep(0.05)
        with self.msgs.lock:
            return self.msgs.clipboard

    def set_clipboard(self, text, paste=False, timeout=2.0):
        seq = self._new_seq()
        self.send(P.encode_set_clipboard(seq, text, paste))
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self.msgs.lock:
                if self.msgs.acks.get(seq):
                    return True
            time.sleep(0.05)
        return False

    def _new_seq(self):
        self._seq += 1
        return self._seq


def _decode_touch(payload):
    action = payload[1]
    off = 2 + 8
    x = int.from_bytes(payload[off : off + 4], "big", signed=True)
    y = int.from_bytes(payload[off + 4 : off + 8], "big", signed=True)
    return action, x, y


def _decode_key(payload):
    return int.from_bytes(payload[2:6], "big", signed=True)


def _decode_scroll_pos(payload):
    x = int.from_bytes(payload[1:5], "big", signed=True)
    y = int.from_bytes(payload[5:9], "big", signed=True)
    return x, y


def _decode_scroll(payload, scr=None):
    h = int.from_bytes(payload[13:15], "big", signed=True)
    v = int.from_bytes(payload[15:17], "big", signed=True)
    return h, v

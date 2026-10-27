SERVER_VERSION = "5.0.1"

TYPE_INJECT_KEYCODE = 0
TYPE_INJECT_TEXT = 1
TYPE_INJECT_TOUCH_EVENT = 2
TYPE_INJECT_SCROLL_EVENT = 3
TYPE_BACK_OR_SCREEN_ON = 4
TYPE_EXPAND_NOTIFICATION_PANEL = 5
TYPE_EXPAND_SETTINGS_PANEL = 6
TYPE_COLLAPSE_PANELS = 7
TYPE_GET_CLIPBOARD = 8
TYPE_SET_CLIPBOARD = 9
TYPE_SET_DISPLAY_POWER = 10
TYPE_ROTATE_DEVICE = 11
TYPE_UHID_CREATE = 12
TYPE_UHID_INPUT = 13
TYPE_UHID_DESTROY = 14
TYPE_OPEN_HARD_KEYBOARD_SETTINGS = 15
TYPE_START_APP = 16
TYPE_RESET_VIDEO = 17
TYPE_CAMERA_SET_TORCH = 18
TYPE_CAMERA_ZOOM_IN = 19
TYPE_CAMERA_ZOOM_OUT = 20
TYPE_RESIZE_DISPLAY = 21
TYPE_SCAN_FILE = 22

ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2

TOUCH_ACTION_DOWN = 0
TOUCH_ACTION_UP = 1
TOUCH_ACTION_MOVE = 2

COPY_KEY_NONE = 0
COPY_KEY_COPY = 1
COPY_KEY_CUT = 2

KEYCODE_BACK = 4
KEYCODE_HOME = 3
KEYCODE_APP_SWITCH = 187
KEYCODE_VOLUME_UP = 24
KEYCODE_VOLUME_DOWN = 25
KEYCODE_POWER = 26
KEYCODE_WAKEUP = 224
KEYCODE_SLEEP = 223
KEYCODE_ENTER = 66
KEYCODE_TAB = 61
KEYCODE_DEL = 67
KEYCODE_ESC = 111

META_CTRL_ON = 0x10000000
META_SHIFT_ON = 0x00000002
META_ALT_ON = 0x00000001

AKEYCODE_MAP = {
    "back": KEYCODE_BACK,
    "home": KEYCODE_HOME,
    "recent": KEYCODE_APP_SWITCH,
    "recents": KEYCODE_APP_SWITCH,
    "app-switch": KEYCODE_APP_SWITCH,
    "vol-up": KEYCODE_VOLUME_UP,
    "vol-down": KEYCODE_VOLUME_DOWN,
    "volume_up": KEYCODE_VOLUME_UP,
    "volume_down": KEYCODE_VOLUME_DOWN,
    "power": KEYCODE_POWER,
    "wake": KEYCODE_WAKEUP,
    "wakeup": KEYCODE_WAKEUP,
    "sleep": KEYCODE_SLEEP,
    "enter": KEYCODE_ENTER,
    "return": KEYCODE_ENTER,
    "tab": KEYCODE_TAB,
    "backspace": KEYCODE_DEL,
    "del": KEYCODE_DEL,
    "delete": KEYCODE_DEL,
    "escape": KEYCODE_ESC,
    "esc": KEYCODE_ESC,
}


def encode_keycode(action, keycode, repeat=0, meta_state=0):
    return (
        bytes([TYPE_INJECT_KEYCODE, action])
        + keycode.to_bytes(4, "big", signed=True)
        + repeat.to_bytes(4, "big", signed=True)
        + meta_state.to_bytes(4, "big", signed=True)
    )


def encode_text(text):
    raw = text.encode("utf-8")
    if len(raw) > 300:
        raise ValueError("text too long (>300 bytes); use clip set + key paste workflows")
    return bytes([TYPE_INJECT_TEXT]) + len(raw).to_bytes(4, "big") + raw


def encode_position(x, y, screen_w, screen_h):
    return (
        int(x).to_bytes(4, "big", signed=True)
        + int(y).to_bytes(4, "big", signed=True)
        + int(screen_w).to_bytes(2, "big")
        + int(screen_h).to_bytes(2, "big")
    )


def encode_touch(action, pointer_id, x, y, screen_w, screen_h, pressure=1.0, action_button=1, buttons=1):
    p = max(0, min(0xFFFF, int(round(pressure * 0xFFFF))))
    return (
        bytes([TYPE_INJECT_TOUCH_EVENT, action])
        + pointer_id.to_bytes(8, "big", signed=True)
        + encode_position(x, y, screen_w, screen_h)
        + p.to_bytes(2, "big")
        + (action_button & 0xFFFFFFFF).to_bytes(4, "big", signed=True)
        + (buttons & 0xFFFFFFFF).to_bytes(4, "big", signed=True)
    )


def encode_scroll(x, y, screen_w, screen_h, h_scroll=0.0, v_scroll=0.0, buttons=0):
    def i16(v):
        n = int(round(v / 16 * 32768))
        return max(-32768, min(32767, n))

    return (
        bytes([TYPE_INJECT_SCROLL_EVENT])
        + encode_position(x, y, screen_w, screen_h)
        + i16(h_scroll).to_bytes(2, "big", signed=True)
        + i16(v_scroll).to_bytes(2, "big", signed=True)
        + buttons.to_bytes(4, "big", signed=True)
    )


def encode_back_or_screen_on(action):
    return bytes([TYPE_BACK_OR_SCREEN_ON, action])


def encode_get_clipboard(copy_key=COPY_KEY_COPY):
    return bytes([TYPE_GET_CLIPBOARD, copy_key])


def encode_set_clipboard(sequence, text, paste=False):
    raw = text.encode("utf-8")
    return (
        bytes([TYPE_SET_CLIPBOARD])
        + sequence.to_bytes(8, "big", signed=True)
        + (1 if paste else 0).to_bytes(1)
        + len(raw).to_bytes(4, "big")
        + raw
    )


def encode_set_display_power(on):
    return bytes([TYPE_SET_DISPLAY_POWER, 1 if on else 0])


def encode_simple(msg_type):
    return bytes([msg_type])


def encode_start_app(name):
    raw = name.encode("utf-8")
    return bytes([TYPE_START_APP]) + len(raw).to_bytes(1) + raw

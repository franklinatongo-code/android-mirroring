import os
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrcx.video_reader import FrameRing, VideoThread

TMP = "/tmp/opencode"
W, H, FPS = 160, 120, 10
DURATION = 3


def make_h264():
    path = os.path.join(TMP, "testsrc.h264")
    if os.path.exists(path):
        os.remove(path)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=size={W}x{H}:rate={FPS}",
            "-t", str(DURATION), "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-f", "h264", path,
        ],
        check=True,
    )
    with open(path, "rb") as fh:
        return fh.read()


def serve_video(data):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def run():
        conn, _ = srv.accept()
        conn.send(b"\x00")
        name = "Test Emulator".encode()
        conn.send(name + b"\x00" * (64 - len(name)))
        conn.send(b"h264")
        flags = 0x8000000000000000
        session = (flags >> 32).to_bytes(4, "big")
        conn.send(session + W.to_bytes(4, "big") + H.to_bytes(4, "big"))
        chunk = 200000
        pts = 0
        off = 0
        while off < len(data):
            piece = data[off : off + chunk]
            off += len(piece)
            pts += 100000
            header = pts.to_bytes(8, "big") + len(piece).to_bytes(4, "big")
            conn.send(header + piece)
            time.sleep(0.004)
        time.sleep(3.0)
        conn.close()

    threading.Thread(target=run, daemon=True).start()
    return port, srv


def main():
    data = make_h264()
    port, srv = serve_video(data)
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    ring = FrameRing(fps=5, width=160, max_age=60)
    vt = VideoThread(sock, ring, forward_dummy=True)
    vt.start()
    deadline = time.time() + 25
    while time.time() < deadline and ring.size() < 5 and vt.error is None:
        time.sleep(0.3)
    assert not vt.error, f"video error: {vt.error}"
    n = ring.size()
    assert n >= 5, f"expected >=5 ring frames, got {n}"
    frame = ring.newest()
    assert frame and len(frame[1]) > 500 and frame[1].endswith(b"\xff\xd9")
    ago = ring.at_ago(1.0)
    assert ago is not None
    span = ring.span()
    print(
        f"OK: device_name={vt.device_name!r} codec={vt.codec} "
        f"{vt.width}x{vt.height} ring_frames={n} span={span[1] - span[0]:.1f}s"
    )
    vt.stop()
    srv.close()


if __name__ == "__main__":
    main()

# scrcpy-x — User Manual (plain English)

This is your phone↔PC bridge. Everything is a command starting with:

```
./bin/scrcx            (from inside the scrcpy-x folder)
```

or the full path from anywhere:
```
"/media/kali/First_HDD/Linux Stuff/New App Porject with scrcpy/scrcpy-x/bin/scrcx"
```

Two GUIs, if you don't want the terminal:

| GUI | Start it |
|---|---|
| Desktop program window (guiscrcpy-style) | `./bin/scrcx gui-app` |
| Web page (browser control centre) | `./bin/scrcx gui` → open http://127.0.0.1:8787 |

Words used below:
- **session** — a background "bridge" between your phone and the PC. Most commands need one.
- **device** — a phone connected by USB cable or Wi-Fi.
- **video coords** — the screen size the session uses (e.g. 622×1280). When you tap, use `see` to get matching coordinates and things just work.

---

## 1. Sessions (start/stop the bridge)

```
./bin/scrcx devices                 list phones now visible to the PC
./bin/scrcx connect                 start/refresh the session (auto-fixes broken ones)
./bin/scrcx connect --serial X      for a specific phone when several are pluged in
./bin/scrcx status                  is the session alive? screen size, uptime, fps
./bin/scrcx disconnect              fully stop the session and its daemon
```

Notes:
- Plugging the phone shows "Allow USB debugging?" → tap Allow once per PC.
- If a command says "no session", run `connect` and try again.

---

## 2. Look at the phone (the agent loop)

```
./bin/scrcx see                     screenshot + OCR of what's on screen
./bin/scrcx screenshot out.png      save a full-resolution screenshot file
./bin/scrcx find "Settings"         print just the coordinates of that word on screen
./bin/scrcx tap --text "Settings"   tap whatever OCR sees as "Settings" (first match)
./bin/scrcx tap --text "OK" --nth 2 tap the 2nd "OK" on screen
./bin/scrcx ocr                     read the whole screen as words (JSON)
./bin/scrcx ocr --box 100 200 600 300    read only region: x=100 y=200 w=600 h=300
./bin/scrcx ocr --box ... --clip    and also copy the result to the phone clipboard
```

`see` output you care about:
- `words` — each word has an `x,y` that is ALREADY the point to tap.
- `battery` — % and temperature.
- `screenshot` — where the PNG went.

Loop to do anything on the phone:
```
see  →  decide  →  tap/text/key/swipe  →  see again (did it work?)
```

---

## 3. Control it by hand

Coordinates: always numbers you saw in `see`/`find` outputs.

```
./bin/scrcx tap 310 640                 press once
./bin/scrcx long_press 310 640          press-and-hold (~1.5 s)
./bin/scrcx swipe 310 300 310 1000 250  drag from (310,300) to (310,1000) in 250 ms
./bin/scrcx scroll 310 640 0 -1         scroll DOWN (0 -1). dx dy positive = other way
./bin/scrcx text "hello there"          type text into whatever field is focused
./bin/scrcx key back                    keys: back home power vol-up vol-down menu
./bin/scrcx key 4                       raw Android keycode number also works
./bin/scrcx action back                 named actions:
                                        home power back menu
                                        expand (pull notifications)
                                        expand_settings ~ expand (settings panel)
                                        collapse (hide panel)
                                        rotate (autopauseA? rotates screen orientation)
                                        screen-on screen-off lock? (use power)
```

Power practicals:
```
./bin/scrcx action screen-off           blank phone, session keeps running
./bin/scrcx action power                toggle sleep/wake
./bin/scrcx unlock --pin <pin>           power on + unlock PIN in ~3 s
./bin/scrcx unlock --pin <pin> --stealth  unlock with the screen black: brightness dips to 0 during
                                         the unlock, the display turns off again after (stays unlocked),
                                         original brightness is restored. The PIN is not echoed in JSON.
./bin/scrcx unlock --pin <pin> --stealth --wake   same hidden unlock, but ends with the screen ON
                                         showing the phone (never presses power after unlock)
```

---

## 4. Clipboard

```
./bin/scrcx clip get                    read what the phone copied
./bin/scrcx clip set "some text"        put text on the phone's clipboard
./bin/scrcx clip set "text" --paste     ...and paste it into the focused app right away
```

---

## 5. Time machine (look back up to 5 minutes)

The session always keeps the last ~5 minutes of screen in a ring buffer on the PC.

```
./bin/scrcx timeline                    how many seconds are buffered right now
./bin/scrcx rewind --ago 30 --save m30.png    the screen 30 s ago → PNG
./bin/scrcx rewind --ago 120            same, default filename
```

Missed an OTP/toast/disappearing message? Just rewind.

---

## 6. Record gestures → replay them later

```
./bin/scrcx record start my.json        start "drawing" a gesture file
...do taps/swipes/text normally...
./bin/scrcx record stop                 save them into my.json
./bin/scrcx replay my.json              perform the recorded steps on the phone
./bin/scrcx replay my.json --speed 0.5  slower replay
./bin/scrcx replay my.json --times 3    repeat 3 times
```

The JSON is human-readable: steps with timings. Recording captures everything between start/stop, not just touches you do on the phone by hand — the order you scripted it.

---

## 7. Watch it in a browser / other devices

```
./bin/scrcx watch                       ready-only browser mirror URL (localhost)
./bin/scrcx connect --lan               (at connect time only) anyone on your LAN can open it
```

Open the printed URL on any phone/laptop in the same Wi-Fi. Nothing is clickable on the watch page — safe for showing a demo.

---

## 8. Apps as desktop windows (like two monitors)

Each app opens as its OWN PC window in its own phone display. The phone's main screen stays free.

```
./bin/scrcx app-window com.whatsapp                     one window, freely resizable
./bin/scrcx app-window yt insta snap whats dc           fuzzy names, multiple windows in one go
./bin/scrcx app-window instgrm                          typos work too (difflib-based matching)
./bin/scrcx app-window com.whatsapp com.discord          two windows (staggered, no port race)
./bin/scrcx app-window com.whatsapp --maximize           start maximized
./bin/scrcx app-window com.whatsapp --size 720x1280     fixed phone resolution (window letterboxes)
./bin/scrcx app-window com.anything --title "Chat"       name the window
./bin/scrcx app-window X --fps 30 --bitrate 8M           quality tweaks
./bin/scrcx app-window X --fullscreen --on-top --show-touches --stay-awake --turn-screen-off
./bin/scrcx app-window X --no-audio                     mute the window (audio auto otherwise)
./bin/scrcx app-window X --pass <anything-scrcpy>        raw scrcpy flags passthrough
```

Fuzzy token rules (in order): exact name → alias (`yt ig wa fb tg dc gh gm gg sn`) →
name/package startswith → substring (3+ chars) → typo-tolerant difflib. Dotted tokens are treated
as raw package names. `scrcx app list --names` prints what the matcher knows.

Packet tips:
```
./bin/scrcx app list                    all installed package names (need com Name from here)
./bin/scrcx app start com.whatsapp      open the app ON THE PHONE
./bin/scrcx app stop com.whatsapp       kill it on the phone
```

Closing a desktop window closes only that virtual display. The app keeps running on the phone otherwise.

---

## 9. Move files / install

One phone or phone↔phone:

```
./bin/scrcx push ./photo.jpg /sdcard/Download/photo.jpg     PC → phone
./bin/scrcx pull /sdcard/WhatsApp/foo.pdf ./foo.pdf         phone → PC
./bin/scrcx send-file ./app.apk                             push to /sdcard/Download + show in gallery
./bin/scrcx send-file ./x.jpg --scan                        ...and media-scan it
./bin/scrcx install ./app.apk                               install APK
./bin/scrcx install ./app.apk -r                            ...replacing an existing install
./bin/scrcx file serialA:/sdcard/x.jpg serialB:/sdcard/x.jpg  phone A → phone B transfer (through PC)
./bin/scrcx file serialA:/sdcard/x.jpg pc:./x.jpg           phone → PC
```

`--serial A` prefix works on any of these when several phones are attached.

---

## 10. Audio & camera

```
./bin/scrcx audio-play                        phone AUDIO plays on PC speakers (headless)
./bin/scrcx audio-play --record cal.mp4       ...and save that audio to a file
./bin/scrcx audio-capture out.m4a --time 30   record phone output to file (auto stops)
./bin/scrcx audio-capture out.wav --time 30   .wav = raw; .flac also supported
./bin/scrcx audio-capture out.m4a --mic       record the phone MICROPHONE instead
./bin/scrcx camera                            mirror the back camera into a PC window
./bin/scrcx camera --facing front --size 1920x1080 --record cam.mp4
./bin/scrcx v4l2 /dev/video0                  phone becomes a PC webcam source (needs v4l2loopback)
```

On this machine, audio uses AAC/FLAC/raw (device has no Opus encoder — handled automatically).

---

## 11. Screen / video recordings

```
./bin/scrcx record-video out.mp4 --time 30        record screen 30 s (headless)
./bin/scrcx record-video out.mp4                  record until you kill the process: pkill -p PID
./bin/scrcx record-video out.mp4 --max-fps 15 --max-size 720    lighter clip
scrcx g or ./bin/scrcx replay                     for gestures, see §6
```

Note: audio isn't recorded into screen MP4s (keep them clean). For both, run audio-capture separately.

---

## 12. Mirroring the main screen (classic scrcpy window)

```
./bin/scrcx mirror                    plain scrcpy window of the phone's screen
./bin/scrcx mirror -- --v4l2-sink=/dev/video0   ANY raw scrcpy flag works after --
```

`mirror` IS the whole stock scrcpy CLI with the right device picked for you.

---

## 13. HID / OTG (advanced input)

```
./bin/scrcx hid                       HID keyboard/mouse flashes: PC input is device-side glyphs
./bin/scrcx hid --keyboard uhid       only keyboard in UHID mode
./bin/scrcx hid --mouse uhid          only mouse
./bin/scrcx hid --otg                 pure OTG (no display, phone thinks it's a USB peripheral)
```

Use when an app ignores normal keyboard injection, or for biometric prompts.

---

## 14. Debug / pair

```
./bin/scrcx dcaps encoders            what video/audio encoders your phone has
./bin/scrcx dcaps cameras             camera ids, sizes, fps ranges
./bin/scrcx dcaps apps                (same as app list; kept for parity with scrcpy)
selftest: ./bin/scrcx selftest        offline checks (no phone needed)
api:     ./bin/scrcx api --post '{"op":"tap",...}'   raw JSON API — for scripts/AI
```

**Pair over Wi-Fi (GUI or manual):**
```
Phone: Settings → Developer options → turn ON "Wireless debugging"
       enter "Pair device with code" → note IP:port + 6-digit code
PC:    adb pair 192.168.0.x:port <code>     (or use the GUI's Pair form)
       adb connect 192.168.0.x:5555
```
After pairing once, the phone stays trusted (until you reset Wi-Fi debugging).

---

## 15. GUIs in full

### Desktop program window — `./bin/scrcx gui-app`
- phone dropdown, Connect/Disconnect/Status
- live snapshot pane — click it to tap (tap / long-press / swipe A→B modes above it)
- "find text" → every word OCR sees becomes a double-click tap target
- keys row, type-and-send, clipboard get/set
- apps dropdown → "window" button (the §8 launcher with dialogs)
- gesture ●/■/replay, 🎥 screen rec, ⏪ rewind, 🎙 audio→file, 🔊 sound on PC, 📷 camera, 📦 install APK, ⬆ push, ⬇ pull, 🌐 watch
- For APK/push/pull it opens real file pickers.

### Web control center — `./bin/scrcx gui` (http://127.0.0.1:8787)
- devices dash with **Wi-Fi pair + connect forms**
- /dev/… panel: click-to-control image, OCR words as tap buttons, keys, text, rewind slider, apps→windows, all media/record buttons, watch iframe
- works from other PCs on LAN too if started `--lan` (careful: no auth).

---

## 16. cheat-sheet of most used

```
see / find / tap / tap --text / text / key / swipe / clip get|set
connect / status / disconnect / devices
rewind --ago N --save x.png / timeline
app-window pkg / app list
record start/stop + replay
audio-play / audio-capture / record-video / camera
push / pull / install / file A:/B:
watch / mirror / gui / gui-app
```

When in doubt: `./bin/scrcx <command> --help` — nothing destructive without an explicit flag.

## Appendix A — troubleshooting

| symptom | fix |
|---|---|
| "no session" | `./bin/scrcx connect` |
| taps land wrong place | always use coords from `see`/`find`/OCR outputs |
| session died mid-work | `connect` auto-bounces; wait ~10 s (auto-heal) |
| `Server connection failed` | installed jar missing: `sudo cp offline-setup/scrcpy-server-v5.0.1 /usr/local/share/scrcpy/scrcpy-server` |
| audio capture 0 bytes | use `.m4a`/`.flac`/`.wav` only; Opus unsupported |
| VA-API error in logs | harmless — software decode |
| `bind: Address already in use` | launch windows 5 s apart, or use scrcx app-window's built-in stagger |
| rewind says 0 frames | ring refills from session start; wait or reconnect |

## Appendix B — where files live

- gestures/screenshots/audio: `/tmp/opencode/`
- session state: `~/.local/state/scrcx/<serial>.json`
- server jar: `/usr/local/share/scrcpy/scrcpy-server` (and `offline-setup/`)
- web GUI source / desktop GUI source: `scrcx/gui.py`, `scrcx/desktop.py`

# scrcpy-x AGENTS.md — controlling the Android phone

You (the agent) fully control the phone through `./bin/scrcx` (or absolute path:
`/media/kali/First_HDD/Linux Stuff/New App Porject with scrcpy/scrcpy-x/bin/scrcx`).
One background session per device must exist first.

## GUI (user-facing)
```
scrcx gui-app               # native desktop window (Tk) — guiscrcpy-style control center
scrcx gui [--lan]           # web control center at http://127.0.0.1:8787
```
dashboard: device list + Wi-Fi pairing; /dev/<serial> panel: click-to-tap/long-press/swipe on live
screenshot, OCR word buttons (tap-on-text), keys, text input, rewind slider, app list → desktop
windows, gesture record/replay, screen record, audio on PC speakers, audio/mic capture, camera,
push/pull/install APk, clipboard. Daemon self-heals a dead video stream automatically.

## Session lifecycle
```
scrcx devices            # which phones are connected to adb right now
scrcx connect            # start/refresh session for the only device (stale sessions auto-show)
scrcx status             # session alive? screen size, fps
scrcx disconnect         # stop session, kill daemon
```

## See → think → tap loop (use this for every UI task)
1. `scrcx see` — prints JSON: fresh path to a full PNG screenshot + `words` from OCR
   (`{text,x,y,w,h}` where x,y is the CENTER — combine `x-275` etc.) + battery/temp/fps.
2. Decide the next action. Pick coordinates from `words` or free coordinates.
3. `scrcx tap X Y` / `text "..."` / `key back` / `swipe x1 y1 x2 y2 ms` / `scroll x y dx dy`
4. Repeat `scrcx see` to verify the screen changed. Confirm result to the user.

OCR word centers are computed for tap coordinates already; just use `x`,`y` as tap centers.

## Targeting OCR text without seeing it
```
scrcx ui --actionable          # uiautomator tree → JSON (text/desc/id/cls, tap center c:[x,y])
scrcx ui --q settings          # filter nodes containing "settings"; --limit N; --save raw.xml
scrcx tap --text "Settings"     # tap whatever OCR sees as "Settings" (first match)
scrcx tap --text "OK" --nth 2   # nth occurrence on the current screen
scrcx find "WiFi" --nth 0       # just print coords json
```

## Other channels
```
scrcx clip get | clip set "text"            # device clipboard (typed input via paste flag too)
scrcx record-video out.mp4 --time 30 [--max-size 720]   # screen capture to file (headless)
scrcx audio-capture out.m4a --time 10      # phone sound → file (aac); .wav=raw, .flac=flac; --mic for phone mic
scrcx audio-play [--record out.m4a]        # phone sound plays on PC speakers, headless; stop: pkill -p PID
scrcx camera --facing back [--size 1920x1080] [--record out.mp4]   # mirror camera into a window
scrcx v4l2 /dev/video0                     # phone as PC webcam source (needs v4l2loopback module)
scrcx hid [--keyboard uhid] [--mouse uhid] [--otg]  # HID modes
scrcx install app.apk [-r]                  # adb install
scrcx send-file local.bin [--remote path] [--scan]
scrcx dcaps encoders|cameras|apps           # list device encoders/camera modes/installed apps
scrcx mirror -- --v4l2-sink=/dev/video0    # passthrough: ANY raw scrcpy flag works
                                            # audio auto-try: default→aac→flac→no-audio until one works
                                            # (winner cached per device in ~/.local/state/scrcx/audio-<serial>.json)
scrcx app start com.package.name | app stop | app list
scrcx app-window yt insta snap whats dc       # fuzzy tokens/aliases; one desktop window each (flex resizable, audio auto)
scrcx app-window com.package --size 400x780   # initial size; flex keeps following window resizes
scrcx app-window com.package --maximize       # fullscreen start; --title NAME names the window
scrcx app-window com.package --no-audio       # mute the window (audio auto otherwise: default→aac→flac)
scrcx unlock --pin <pin> [--stealth]     # wake+unlock; --stealth never shows the lockscreen (black during, dark after)
scrcx action back|home|power|rotate|expand|collapse|screen-on|screen-off
scrcx screenshot out.png                    # full-res PNG right now
scrcx rewind --ago 120 --save miss.png      # look back in the last 5 min ring buffer
scrcx timeline                              # how much of the past is buffered
scrcx hud                                   # fps, battery, temp snapshot json
scrcx watch                                 # browser mirror URL (read-only)
scrcx file phoneA:/sdcard/x.jpg phoneB:/sdcard/x.jpg   # cross-device transfer via this PC
scrcx push local.jpg /sdcard/x.jpg          # PC → device
scrcx pull /sdcard/movies/x.mp4 ./out.mp4   # device → PC
scrcx record start s.json ... tap/text... ... record stop    # gesture capture
scrcx replay s.json [--speed 0.5] [--times 3]
```

## Rules
- Talk to only ONE device unless the user says otherwise; use `--serial` for specific ones.
- One `see` per decision step; avoid screenshot spam (battery + time).
- Never assume a tap worked — re-`see` before claiming success.
- Gesture JSON replay: keep under 5 min; devices drop slow sessions.

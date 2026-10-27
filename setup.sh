#!/usr/bin/env bash
# scrcx setup — installs PC prerequisites and fetches the scrcpy-server jar.
# Usage:  ./setup.sh          (installs missing packages via apt; no root needed if all present)
set -e
cd "$(dirname "$0")"

JAR="vendor/scrcpy-server-v5.0.1"
JAR_URL="https://github.com/Genymobile/scrcpy/releases/download/v5.0.1/scrcpy-server-v5.0.1"

MISSING=()
for c in python3 adb ffmpeg tesseract; do
    command -v "$c" >/dev/null 2>&1 || MISSING+=("$c")
done

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "[setup] missing: ${MISSING[*]}"
    if command -v apt-get >/dev/null 2>&1; then
        PKGS=""
        for c in "${MISSING[@]}"; do
            case "$c" in
                tesseract) PKGS="$PKGS tesseract-ocr" ;;
                adb) PKGS="$PKGS adb" ;;
                *) PKGS="$PKGS $c" ;;
            esac
        done
        echo "[setup] installing:$PKGS (needs sudo)"
        sudo apt-get update -qq
        sudo apt-get install -y -qq $PKGS
    else
        echo "[setup] install ${MISSING[*]} with your package manager, then re-run."
        exit 1
    fi
fi
echo "[setup] python3/adb/ffmpeg/tesseract OK"

if [ ! -f "$JAR" ]; then
    echo "[setup] downloading scrcpy-server v5.0.1 jar"
    mkdir -p vendor
    if command -v wget >/dev/null 2>&1; then
        wget -q "$JAR_URL" -O "$JAR"
    else
        curl -fsSL "$JAR_URL" -o "$JAR"
    fi
fi

if head -c2 "$JAR" | grep -q PK; then
    echo "[setup] server jar OK ($JAR, $(wc -c <"$JAR") bytes)"
else
    echo "[setup] ERROR: $JAR is not a valid jar — delete it and retry"
    exit 1
fi

echo
echo "scrcx ready. Next steps with a phone connected via USB (USB debugging on):"
echo "  ./bin/scrcx devices"
echo "  ./bin/scrcx connect"
echo "  ./bin/scrcx see            # the main agent loop command"
echo
echo "Optional extras (not required): build the scrcpy client from Genymobile/scrcpy v5.0.1"
echo "for desktop app windows (./bin/scrcx app-window) and raw mirroring (./bin/scrcx mirror)."

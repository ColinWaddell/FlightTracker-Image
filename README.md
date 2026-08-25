# FlightTracker OS Image

Prebuilt Raspberry Pi OS images (32-bit `armhf` and 64-bit `arm64`) with a
built-in web installer. Flash the image, boot the Pi, and visit
`http://flighttracker.local/` to install FlightTracker with a few clicks —
no terminal required.

## How it works

1. **CI builds the image** — GitHub Actions ([`build-image.yml`](.github/workflows/build-image.yml))
   runs `pi-gen` via `usimd/pi-gen-action` for both architectures. The custom
   stage [`image/stage-flighttracker`](image/stage-flighttracker) layers on
   the web installer, Avahi/mDNS, and SSH.
2. **Releases publish the image** — tagging `v*` triggers
   [`release.yml`](.github/workflows/release.yml), which builds both images,
   computes checksums, updates [`os_list.json`](os_list.json), and creates a
   GitHub Release.
3. **Operator flashes & boots** — using Raspberry Pi Imager with the custom
   OS URL (see [`website/index.html`](website/index.html)), or a direct
   download. On boot the installer service starts automatically.
4. **Web installer runs** — the Flask app at
   [`image/stage-flighttracker/files/web-installer/app.py`](image/stage-flighttracker/files/web-installer/app.py)
   serves a wizard that downloads and drives the FlightTracker install script
   (via `pexpect`), streaming progress over SSE. On success it writes a
   sentinel file that disables the service on next boot.

## Running the web installer locally

The web installer is a plain Flask app and can be run on your development
machine for testing (the actual install actions won't complete without a Pi,
but the UI and flow can be exercised).

```bash
cd image/stage-flighttracker/files/web-installer

# Create a virtualenv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run the dev server (use a high port to avoid needing root)
flask run --host=0.0.0.0 --port=5000
```

Then open <http://localhost:5000/> in your browser.

> Hardware detection reads `/proc/device-tree/model`, so on non-Pi hosts it
> will report "Unknown Raspberry Pi". The install step will fail to download
> or run the installer script — that's expected off a real Pi.
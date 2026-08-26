#!/usr/bin/env python3
"""
FlightTracker Web Installer
Runs on the Pi on first boot. Accessible at http://flighttracker.local/
"""

import os
import queue
import threading
import subprocess
import re
from flask import Flask, render_template, request, Response, redirect, url_for

import pexpect

app = Flask(__name__)

# -- Constants -----------------------------------------------------------------

INSTALLER_PI_URL = "https://raw.githubusercontent.com/ColinWaddell/FlightTracker/main/platforms/pi/install.sh"
INSTALLER_PI5_URL = "https://raw.githubusercontent.com/ColinWaddell/FlightTracker/main/platforms/pi5/install.sh"
SENTINEL_FILE = "/opt/flighttracker-installer/.installed"
INSTALL_SCRIPT = "/tmp/ft-install.sh"

# -- Global install state -------------------------------------------------------

_output_queue: queue.Queue = queue.Queue()
_output_log: list[str] = []
_output_lock = threading.Lock()
_install_thread: threading.Thread | None = None
_install_running = False
_install_success = False
_install_error = None


# -- Hardware detection --------------------------------------------------------


def get_pi_model() -> str:
    try:
        with open("/proc/device-tree/model", "r") as f:
            return f.read().strip("\x00").strip()
    except OSError:
        return "Unknown Raspberry Pi"


def is_pi5(model: str) -> bool:
    return "Pi 5" in model


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text)


# -- Installer runner ----------------------------------------------------------


def _emit(line: str) -> None:
    """Push a line of output to the log and SSE queue."""
    with _output_lock:
        _output_log.append(line)
    _output_queue.put(line)


def run_installer(config: dict) -> None:
    global _install_running, _install_success, _install_error

    _install_running = True
    _install_success = False
    _install_error = None
    model = get_pi_model()
    pi5 = is_pi5(model)

    try:
        # -- Download the installer script -------------------------------------
        url = INSTALLER_PI5_URL if pi5 else INSTALLER_PI_URL
        _emit(f"Downloading installer for {model}…\n")
        result = subprocess.run(
            ["curl", "-fsSL", url, "-o", INSTALL_SCRIPT], capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to download installer:\n{result.stderr}")
        os.chmod(INSTALL_SCRIPT, 0o755)
        _emit("Download complete.\n\n")

        # -- Spawn the installer via pexpect -----------------------------------
        child = pexpect.spawn(
            "bash",
            [INSTALL_SCRIPT],
            timeout=600,
            encoding="utf-8",
            codec_errors="replace",
        )

        # Expected prompts in order. pexpect.TIMEOUT and pexpect.EOF always last.
        PROMPTS = [
            r"Ready to begin installation\?",   # 0
            r"Select interface board type:",     # 1
            r"Install realtime clock support\?",  # 2
            r"What is thy bidding\?",            # 3
            r"reserve a core",                  # 4 (cpu isolation menu)
            r"Continue with these settings\?",   # 5
            r"Reboot now\?",                    # 6
            r"Would you like to uninstall",       # 7 (existing install)
            r"Continue anyway\?",               # 8 (non-debian warning)
            pexpect.EOF,                      # 9
            pexpect.TIMEOUT,                   # 10
        ]

        while True:
            idx = child.expect(PROMPTS, timeout=300)

            # Emit whatever was printed before this prompt
            before = strip_ansi(child.before or "")
            if before:
                _emit(before)

            if idx == 0:  # Ready to begin
                _emit("» y\n")
                child.sendline("y")

            elif idx == 1:  # Interface board type (Pi3/4/Zero only)
                _emit("\n» Selecting interface board…\n")
                child.sendline(str(config.get("interface_type", "1")))

            elif idx == 2:  # Install RTC?
                val = "y" if config.get("install_rtc") else "n"
                _emit(f"» {val}\n")
                child.sendline(val)

            elif idx == 3:  # Quality vs convenience
                _emit("\n» Selecting display mode…\n")
                child.sendline(str(config.get("quality_mode", "2")))

            elif idx == 4:  # CPU isolation menu — answer 0 (no core reserved)
                _emit("» 0\n")
                child.sendline("0")

            elif idx == 5:  # Continue with settings
                _emit("» y\n")
                child.sendline("y")

            elif idx == 6:  # Reboot now - always decline; we handle it
                _emit("» n (reboot managed by installer UI)\n")
                child.sendline("n")

            elif idx == 7 or idx == 8:  # Existing install or non-debian warning
                _emit("» y\n")
                child.sendline("y")

            elif idx == 9:  # EOF - installer finished
                after = strip_ansi(child.after or "")
                if after and after is not pexpect.EOF:
                    _emit(after)
                break

            elif idx == 10:  # Timeout
                raise RuntimeError("Installation timed out waiting for a response.")

        child.close()
        if child.exitstatus and child.exitstatus != 0:
            raise RuntimeError(f"Installer exited with code {child.exitstatus}")

        # -- Mark as installed -------------------------------------------------
        open(SENTINEL_FILE, "w").close()
        _install_success = True
        _emit("\n\n✓ Installation complete.\n")

    except Exception as exc:
        _install_error = str(exc)
        _emit(f"\n\n✗ Error: {exc}\n")

    finally:
        _install_running = False
        _output_queue.put(None)  # Sentinel - tells SSE stream to close


# -- Routes --------------------------------------------------------------------


@app.route("/")
def index():
    model = get_pi_model()
    if _install_running:
        return redirect(url_for("progress"))
    if os.path.exists(SENTINEL_FILE):
        return redirect(url_for("done"))
    return render_template("index.html", model=model)


@app.route("/config")
def config():
    model = get_pi_model()
    pi5 = is_pi5(model)
    if _install_running:
        return redirect(url_for("progress"))
    if os.path.exists(SENTINEL_FILE):
        return redirect(url_for("done"))
    return render_template("config.html", model=model, pi5=pi5)


@app.route("/install", methods=["POST"])
def install():
    global _install_thread, _output_queue

    if _install_running:
        return redirect(url_for("progress"))

    # Collect form values
    cfg = {
        "interface_type": request.form.get("interface_type", "1"),
        "install_rtc": request.form.get("install_rtc") == "1",
        "quality_mode": request.form.get("quality_mode", "2"),
    }

    # Fresh queue and log for this run
    _output_queue = queue.Queue()
    with _output_lock:
        _output_log.clear()

    _install_thread = threading.Thread(target=run_installer, args=(cfg,), daemon=True)
    _install_thread.start()

    return redirect(url_for("progress"))


@app.route("/progress")
def progress():
    model = get_pi_model()
    if _install_running:
        return render_template("progress.html", model=model)
    if not _output_log:
        # No install has been started — go to the beginning
        return redirect(url_for("index"))
    # Install already finished — go to done
    return redirect(url_for("done"))


@app.route("/events")
def events():
    """Server-Sent Events stream of installer output."""

    def generate():
        # Replay buffered output first so reconnecting clients see full history
        with _output_lock:
            replay = list(_output_log)
            log_pos = len(_output_log)
        for line in replay:
            escaped = line.replace("\n", "\ndata: ")
            yield f"data: {escaped}\n\n"

        # If install already finished, send the done event immediately
        if not _install_running:
            status = "success" if _install_success else "error"
            yield f"event: done\ndata: {status}\n\n"
            return

        # Live stream: use queue as a wake signal, then read new log entries
        while True:
            try:
                _output_queue.get(timeout=30)
            except queue.Empty:
                # Keep-alive comment
                yield ": keep-alive\n\n"
                continue

            # The queue item was a signal — read any new lines from the log
            with _output_lock:
                new_lines = _output_log[log_pos:]
                log_pos = len(_output_log)

            for line in new_lines:
                escaped = line.replace("\n", "\ndata: ")
                yield f"data: {escaped}\n\n"

            # Check if install finished (None sentinel was emitted)
            if not _install_running:
                status = "success" if _install_success else "error"
                yield f"event: done\ndata: {status}\n\n"
                break

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.route("/done")
def done():
    model = get_pi_model()
    if _install_running:
        return redirect(url_for("progress"))
    return render_template(
        "done.html", model=model, success=_install_success, error=_install_error
    )


@app.route("/status")
def status():
    """JSON status endpoint for AJAX checks."""
    return {
        "running": _install_running,
        "success": _install_success,
        "error": _install_error,
        "installed": os.path.exists(SENTINEL_FILE),
    }


@app.route("/reboot", methods=["POST"])
def reboot():
    """Trigger a system reboot. Called from the done page."""

    def _reboot():
        import time

        time.sleep(1)
        subprocess.run(["reboot"])

    threading.Thread(target=_reboot, daemon=True).start()
    return render_template("rebooting.html")


# -- Entry point ---------------------------------------------------------------

if __name__ == "__main__":
    # Port 80 requires root; the systemd unit runs as root.
    app.run(host="0.0.0.0", port=80, threaded=True, debug=False)

#!/usr/bin/env python3
"""
FlightTracker Web Installer
Runs on the Pi on first boot. Accessible at http://flighttracker.local:8584/
After installation completes and the Pi reboots, the FlightTracker web
interface takes over the same port (8584).
"""

import os
import queue
import threading
import subprocess
import re
from flask import Flask, render_template, request, Response, redirect, url_for

import pexpect

from version import VERSION

app = Flask(__name__)

# -- Version ------------------------------------------------------------------

__version__ = ".".join(str(v) for v in VERSION)
app.jinja_env.globals["app_version"] = __version__

# -- Constants -----------------------------------------------------------------

INSTALLER_PI_URL = "https://raw.githubusercontent.com/ColinWaddell/FlightTracker/main/platforms/pi/install.sh"
INSTALLER_PI5_URL = "https://raw.githubusercontent.com/ColinWaddell/FlightTracker/main/platforms/pi5/install.sh"
SENTINEL_FILE = "/opt/flighttracker-installer/.installed"
INSTALL_SCRIPT = "/tmp/ft-install.sh"

# -- Global install state -------------------------------------------------------
#
# Output fan-out: each connected SSE client gets its own bounded queue and
# _emit() broadcasts to all of them.  There is no replay - a client only
# sees output from the moment it connects (or reconnects).

MAX_CLIENT_QUEUE = 500  # lines buffered per slow client before old ones drop

_client_queues: list[queue.Queue] = []
_client_queues_lock = threading.Lock()
_install_thread: threading.Thread | None = None
_install_running = False
_install_success = False
_install_error = None
_install_has_run = False


# -- Hardware detection --------------------------------------------------------


def get_pi_model() -> str:
    try:
        with open("/proc/device-tree/model", "r") as f:
            return f.read().strip("\x00").strip()
    except OSError:
        return "Unknown Raspberry Pi"


def is_pi5(model: str) -> bool:
    return "Pi 5" in model


def is_unsupported(model: str) -> bool:
    """Return True for Pi models too low-powered for the web installer.

    The web installer runs a full build pipeline that is too heavy for
    Pi Zero, Pi Zero W, Pi Zero 2 W, and Pi 1/2 boards.  Those devices
    should use the quick-install script over SSH instead.
    """
    unsupported_markers = [
        "Pi Zero",
        "Pi Zero W",
        "Pi Zero 2",
        "Pi 1",
        "Pi 2",
    ]
    return any(m in model for m in unsupported_markers)


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text)


def collapse_carriage_returns(text: str) -> str:
    """Collapse pty carriage-return progress spam into plain lines.

    Interactive tools (apt, dpkg, curl) redraw progress bars with \r
    instead of \n.  Within each newline-delimited segment keep only the
    text after the last \r - i.e. exactly what a terminal would show.
    """
    text = text.replace("\r\n", "\n")
    return "\n".join(
        seg.rsplit("\r", 1)[-1] if "\r" in seg else seg
        for seg in text.split("\n")
    )


def _broadcast(item) -> None:
    """Send an item to every connected client's queue.

    Queues are bounded: if a client falls too far behind, its oldest
    buffered lines are dropped rather than blocking the installer.
    """
    with _client_queues_lock:
        queues = list(_client_queues)
    for q in queues:
        try:
            q.put_nowait(item)
        except queue.Full:
            try:
                q.get_nowait()
                q.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass


def _emit(text: str) -> None:
    """Broadcast a chunk of installer output as one SSE event per line.

    Output is normalized (carriage returns collapsed, ANSI stripped
    upstream) so the browser receives clean newline-delimited lines.
    Blank lines are skipped - the SSE client drops empty data events.
    """
    if not text:
        return
    for seg in collapse_carriage_returns(text).split("\n"):
        if seg:
            _broadcast(seg)


# -- Installer runner ----------------------------------------------------------


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
        # The web installer service runs as the configured FT user, so the
        # install script detects the correct CURRENT_USER and CURRENT_HOME,
        # producing correct service file paths.
        # Set FT_VERBOSE=1 so run_quiet() streams command output (apt, git,
        # pip, make) to stdout instead of capturing to a temp file.
        child = pexpect.spawn(
            "bash",
            [INSTALL_SCRIPT],
            timeout=600,
            encoding="utf-8",
            codec_errors="replace",
            env={
                **os.environ,
                "FT_VERBOSE": "1",
            },
        )

        # Build prompt list based on installer variant.
        # Pi5 installer prompts: Continue anyway?, Would you like to uninstall?,
        #   Ready to begin?, Reboot now?
        # Pi 3/4 installer prompts: Continue anyway?, Would you like to uninstall?,
        #   Ready to begin?, SELECT 1-N: (interface board), Install RTC?,
        #   Continue with these settings?, Reboot now?
        #
        # We use a short TIMEOUT (2 s) as the first pattern so that stdout
        # from the install script is streamed to the operator in near real
        # time instead of being buffered until the next prompt match.
        # A separate wall-clock deadline guards against genuine hangs.
        STREAM_TIMEOUT = 2          # seconds between output flushes
        WALL_DEADLINE = 600         # 10 min with zero output = real timeout

        if pi5:
            REAL_PROMPTS = [
                r"Continue anyway\?",                    # 0
                r"Would you like to uninstall",            # 1
                r"Ready to begin installation\?",         # 2
                r"Reboot now\?",                          # 3
            ]
        else:
            REAL_PROMPTS = [
                r"Continue anyway\?",                    # 0
                r"Would you like to uninstall",            # 1
                r"Ready to begin installation\?",         # 2
                r"SELECT 1-\d+:",                        # 3
                r"Install realtime clock support\?",      # 4
                r"Continue with these settings\?",       # 5
                r"Reboot now\?",                          # 6
            ]

        # Full expect list: TIMEOUT first (for streaming), then real prompts,
        # then EOF.
        EXPECT_LIST = [pexpect.TIMEOUT] + REAL_PROMPTS + [pexpect.EOF]
        TIMEOUT_IDX = 0
        EOF_IDX = len(EXPECT_LIST) - 1
        PROMPT_OFFSET = 1   # real prompts start at index 1 in EXPECT_LIST

        import time as _time
        last_output_time = _time.monotonic()

        while True:
            idx = child.expect(EXPECT_LIST, timeout=STREAM_TIMEOUT)

            # Always emit whatever accumulated before the matched pattern.
            before = strip_ansi(child.before or "")
            if before:
                _emit(before)
                last_output_time = _time.monotonic()

            if idx == TIMEOUT_IDX:
                # No prompt - just a streaming checkpoint.  If the wall
                # clock deadline has passed with zero output, bail out.
                if _time.monotonic() - last_output_time > WALL_DEADLINE:
                    raise RuntimeError(
                        "Installation timed out - no output for "
                        f"{WALL_DEADLINE}s."
                    )
                continue

            if idx == EOF_IDX:
                # Installer process exited.
                if isinstance(child.after, str):
                    after = strip_ansi(child.after)
                    if after:
                        _emit(after)
                break

            # Map the expect index back to the real prompt index.
            prompt_idx = idx - PROMPT_OFFSET

            if prompt_idx == 0 or prompt_idx == 1:
                # Continue anyway? / Would you like to uninstall?
                _emit("» y\n")
                child.sendline("y")
            elif prompt_idx == 2:
                # Ready to begin installation?
                _emit("» y\n")
                child.sendline("y")
            elif not pi5 and prompt_idx == 3:
                # SELECT 1-N: (interface board type)
                _emit("\n» Selecting interface board…\n")
                child.sendline(str(config.get("interface_type", "1")))
            elif not pi5 and prompt_idx == 4:
                # Install realtime clock support?
                val = "y" if config.get("install_rtc") else "n"
                _emit(f"» {val}\n")
                child.sendline(val)
            elif not pi5 and prompt_idx == 5:
                # Continue with these settings?
                _emit("» y\n")
                child.sendline("y")
            elif (pi5 and prompt_idx == 3) or (not pi5 and prompt_idx == 6):
                # Reboot now? - decline, web UI handles reboot
                _emit("» n (reboot managed by installer UI)\n")
                child.sendline("n")

        child.close()
        if child.exitstatus and child.exitstatus != 0:
            raise RuntimeError(f"Installer exited with code {child.exitstatus}")

        # -- Mark as installed (needs sudo: /opt is root-owned) ---------------
        subprocess.run(["sudo", "tee", SENTINEL_FILE], input="", check=True)
        _install_success = True
        _emit("\n\n✓ Installation complete.\n")

    except Exception as exc:
        _install_error = str(exc)
        _emit(f"\n\n✗ Error: {exc}\n")

    finally:
        _install_running = False
        _broadcast(None)  # Sentinel - closes every live SSE stream


# -- Routes --------------------------------------------------------------------


@app.route("/")
def index():
    model = get_pi_model()
    if _install_running:
        return redirect(url_for("progress"))
    if os.path.exists(SENTINEL_FILE):
        return redirect(url_for("done"))
    return render_template(
        "index.html", model=model, unsupported=is_unsupported(model)
    )


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
    global _install_thread, _install_has_run

    if _install_running:
        return redirect(url_for("progress"))

    # Collect form values
    cfg = {
        "interface_type": request.form.get("interface_type", "1"),
        "install_rtc": request.form.get("install_rtc") == "1",
        "quality_mode": request.form.get("quality_mode", "2"),
    }

    _install_has_run = True
    _install_thread = threading.Thread(target=run_installer, args=(cfg,), daemon=True)
    _install_thread.start()

    return redirect(url_for("progress"))


@app.route("/progress")
def progress():
    model = get_pi_model()
    if _install_running:
        return render_template("progress.html", model=model)
    if not _install_has_run:
        # No install has been started - go to the beginning
        return redirect(url_for("index"))
    # Install already finished - go to done
    return redirect(url_for("done"))


@app.route("/events")
def events():
    """Server-Sent Events stream of live installer output.

    No replay: output produced before a client connects (or reconnects)
    is not resent - clients get stream-from-now semantics.  A client that
    connects after the install finished receives the done event
    immediately and is redirected by the front end.
    """

    def generate():
        q = queue.Queue(maxsize=MAX_CLIENT_QUEUE)
        with _client_queues_lock:
            _client_queues.append(q)
        try:
            # Register first, then check state; a run finishing between the
            # two is caught by the state re-check after a queue timeout.
            if not _install_running and _install_has_run:
                status = "success" if _install_success else "error"
                yield f"event: done\ndata: {status}\n\n"
                return

            while True:
                try:
                    item = q.get(timeout=25)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    # Covers the sentinel race: the run finished before we
                    # registered (or its sentinel was dropped from a full
                    # queue), so fall back to checking the state flags.
                    if not _install_running and _install_has_run:
                        status = "success" if _install_success else "error"
                        yield f"event: done\ndata: {status}\n\n"
                        return
                    continue

                if item is None:
                    # Sentinel from run_installer: run has finished
                    status = "success" if _install_success else "error"
                    yield f"event: done\ndata: {status}\n\n"
                    return

                yield f"data: {item}\n\n"
        finally:
            with _client_queues_lock:
                try:
                    _client_queues.remove(q)
                except ValueError:
                    pass

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
        subprocess.run(["sudo", "reboot"])

    threading.Thread(target=_reboot, daemon=True).start()
    return render_template("rebooting.html")


# -- Entry point ---------------------------------------------------------------

if __name__ == "__main__":
    # Port 8584 - same port the FlightTracker web interface uses after install.
    # This way the operator bookmarks one URL and after reboot they see the
    # FlightTracker interface instead of the installer.
    app.run(host="0.0.0.0", port=8584, threaded=True, debug=False)

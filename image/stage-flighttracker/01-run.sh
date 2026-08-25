#!/bin/bash -e

# ── FlightTracker OS custom stage ─────────────────────────────────────────────
# Installs the web-based installer that runs on first boot, accessible at
# http://flighttracker.local/ after the user flashes and boots the image.

INSTALLER_DIR="${ROOTFS_DIR}/opt/flighttracker-installer"

# ── Web installer app ──────────────────────────────────────────────────────────
install -d "${INSTALLER_DIR}/templates"
install -m 644 files/web-installer/app.py          "${INSTALLER_DIR}/app.py"
install -m 644 files/web-installer/requirements.txt "${INSTALLER_DIR}/requirements.txt"
install -m 644 files/web-installer/templates/*.html "${INSTALLER_DIR}/templates/"

# ── Install Python dependencies into the image ─────────────────────────────────
# Run pip inside the target rootfs via chroot so we get the right Python
on_chroot << EOF
pip3 install --break-system-packages -r /opt/flighttracker-installer/requirements.txt
EOF

# ── Systemd service ────────────────────────────────────────────────────────────
install -m 644 files/flighttracker-web-installer.service \
    "${ROOTFS_DIR}/etc/systemd/system/flighttracker-web-installer.service"

systemctl --root="${ROOTFS_DIR}" enable flighttracker-web-installer.service

# ── mDNS / Avahi ──────────────────────────────────────────────────────────────
# avahi-daemon is already installed via 00-packages.
# libnss-mdns wires .local resolution into nsswitch.conf automatically.
systemctl --root="${ROOTFS_DIR}" enable avahi-daemon.service

# ── Hostname ──────────────────────────────────────────────────────────────────
echo "flighttracker" > "${ROOTFS_DIR}/etc/hostname"
sed -i "s/127\.0\.1\.1.*/127.0.1.1\tflighttracker/" "${ROOTFS_DIR}/etc/hosts" || \
    echo "127.0.1.1	flighttracker" >> "${ROOTFS_DIR}/etc/hosts"

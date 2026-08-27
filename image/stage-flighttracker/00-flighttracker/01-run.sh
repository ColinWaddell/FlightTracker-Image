#!/bin/bash -e

# -- FlightTracker OS custom stage ---------------------------------------------
# Installs the web-based installer that runs on first boot, accessible at
# http://flighttracker.local:8584/ after the user flashes and boots the image.

INSTALLER_DIR="${ROOTFS_DIR}/opt/flighttracker-installer"

# -- Web installer app ----------------------------------------------------------
install -d "${INSTALLER_DIR}/templates"
install -d "${INSTALLER_DIR}/static/images"
install -m 644 ../files/web-installer/app.py          "${INSTALLER_DIR}/app.py"
install -m 644 ../files/web-installer/version.py        "${INSTALLER_DIR}/version.py"
install -m 644 ../files/web-installer/requirements.txt "${INSTALLER_DIR}/requirements.txt"
install -m 644 ../files/web-installer/templates/*.html "${INSTALLER_DIR}/templates/"
install -m 644 ../files/web-installer/static/images/*   "${INSTALLER_DIR}/static/images/"

# -- Install Python dependencies into the image ---------------------------------
# Flask and pexpect are installed via apt (00-packages), no pip needed

# -- Web installer systemd service -------------------------------------------
# The web installer runs as the same user that FlightTracker will run as.
# We detect the username from pi-gen's FIRST_USER_NAME config so the image
# works correctly even if someone builds with a non-default username.
FT_USER="${FIRST_USER_NAME:-pi}"
FT_HOME="/home/${FT_USER}"

# Patch the service file with the correct user and home directory
install -m 644 ../files/flighttracker-web-installer.service \
    "${ROOTFS_DIR}/etc/systemd/system/flighttracker-web-installer.service"
sed -i \
    -e "s|^User=pi$|User=${FT_USER}|" \
    -e "s|/home/pi/|${FT_HOME}/|g" \
    "${ROOTFS_DIR}/etc/systemd/system/flighttracker-web-installer.service"

systemctl --root="${ROOTFS_DIR}" enable flighttracker-web-installer.service

# -- mDNS / Avahi --------------------------------------------------------------
# avahi-daemon is already installed via 00-packages.
# libnss-mdns wires .local resolution into nsswitch.conf automatically.
systemctl --root="${ROOTFS_DIR}" enable avahi-daemon.service

# -- MOTD ----------------------------------------------------------------------
install -m 644 ../files/motd "${ROOTFS_DIR}/etc/motd"
# Disable the default dynamic motd scripts that print system info
chmod -x "${ROOTFS_DIR}/etc/update-motd.d/"* 2>/dev/null || true

# -- Boot splash ---------------------------------------------------------------
# Firmware splash image shown on HDMI if someone plugs in a monitor
install -m 644 ../files/splash.bmp "${ROOTFS_DIR}/boot/firmware/splash.bmp" 2>/dev/null || \
    install -m 644 ../files/splash.bmp "${ROOTFS_DIR}/boot/splash.bmp"

# Suppress the default boot text/rainbow and enable our splash image
for cfg in "${ROOTFS_DIR}/boot/firmware/config.txt" "${ROOTFS_DIR}/boot/config.txt"; do
  if [ -f "$cfg" ]; then
    grep -q "^disable_splash=" "$cfg" || echo "disable_splash=1" >> "$cfg"
    grep -q "^boot_delay=" "$cfg" || echo "boot_delay=0" >> "$cfg"
    break
  fi
done

# -- Passwordless sudo for the FlightTracker user ----------------------------
# The web installer runs as the FT user and spawns the install script,
# which uses sudo internally (apt-get, systemctl, etc.).
# Also set via PASSWORDLESS_SUDO=1 in pi-gen config, but this sudoers file
# is belt-and-braces.
install -d "${ROOTFS_DIR}/etc/sudoers.d"
echo "${FT_USER} ALL=(ALL) NOPASSWD: ALL" > "${ROOTFS_DIR}/etc/sudoers.d/010-ft-nopasswd"
chmod 440 "${ROOTFS_DIR}/etc/sudoers.d/010-ft-nopasswd"

# -- Hostname ------------------------------------------------------------------
echo "flighttracker" > "${ROOTFS_DIR}/etc/hostname"
sed -i "s/127\.0\.1\.1.*/127.0.1.1\tflighttracker/" "${ROOTFS_DIR}/etc/hosts" || \
    echo "127.0.1.1	flighttracker" >> "${ROOTFS_DIR}/etc/hosts"

# -- Unattended-upgrades (security updates only) -------------------------------
# Pre-accept the debconf prompt so dpkg-reconfigure doesn't hang
on_chroot << EOF
echo "unattended-upgrades unattended-upgrades/enable_auto boolean true" | debconf-set-selections
dpkg-reconfigure -f noninteractive unattended-upgrades
EOF

# Configure: security updates only, no automatic reboot
install -d "${ROOTFS_DIR}/etc/apt/apt.conf.d"
cat > "${ROOTFS_DIR}/etc/apt/apt.conf.d/50flighttracker-unattended" << 'CONF'
// FlightTracker: automatic security updates only
Unattended-Upgrade::Allowed-Origins {
    "origin=Debian,codename=${distro_codename},label=Debian-Security";
    "origin=Raspbian,codename=${distro_codename},label=Raspbian-Security";
};
// Do NOT automatically reboot - this device may be driving an LED display
Unattended-Upgrade::Automatic-Reboot "false";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF

systemctl --root="${ROOTFS_DIR}" enable apt-daily.timer apt-daily-upgrade.timer

# -- Hardware watchdog ---------------------------------------------------------
# Enable the hardware watchdog so the Pi reboots if it hangs.
# Pi 3/4/Zero use bcm2835_wdt, Pi 5 uses bcm2712_wdt - load both,
# the kernel silently ignores the one that doesn't apply.
on_chroot << EOF
mkdir -p /etc/modules-load.d
echo "bcm2835_wdt" >> /etc/modules-load.d/modules.conf 2>/dev/null || true
echo "bcm2712_wdt" >> /etc/modules-load.d/modules.conf 2>/dev/null || true
EOF

# Tell systemd to use the hardware watchdog (15 second timeout)
mkdir -p "${ROOTFS_DIR}/etc/systemd"
grep -q "^RuntimeWatchdogSec=" "${ROOTFS_DIR}/etc/systemd/system.conf" 2>/dev/null || \
    echo "RuntimeWatchdogSec=15s" >> "${ROOTFS_DIR}/etc/systemd/system.conf"

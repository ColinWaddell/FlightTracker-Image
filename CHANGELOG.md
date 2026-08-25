# Changelog

All notable changes to FlightTracker-RPIImage are documented here.

## [Unreleased]

### Added
- Initial repo structure and CI pipeline
- GitHub Actions matrix build (arm64 + armhf) via `pi-gen-action` on ARM runners
- Custom pi-gen stage: installs web installer, avahi/mDNS, SSH
- Web installer Flask app with hardware detection, config form, SSE progress stream, reboot endpoint
- `os_list.json` for Raspberry Pi Imager custom OS integration
- Example landing page (`website/index.html`)

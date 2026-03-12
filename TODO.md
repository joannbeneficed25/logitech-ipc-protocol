# TODO

## Script Improvements

- [x] ~~Auto-discover device IDs by querying `/devices/list` instead of hardcoding~~ -- Done in `kvm_daemon_windows.py`
- [ ] Filter devices that support ChangeHost (Easy-Switch capable only)
- [x] ~~Make monitor input values configurable (or skip monitor switching)~~ -- Done in `kvm_config.ini`
- [ ] Support m1ddc on Intel Macs (different install path)
- [ ] Add config file for Mac side (device IDs, monitor values, m1ddc path)
- [ ] Add `--list-devices` flag to show connected devices and current host
- [ ] Add `--status` flag to show current host without switching

## Windows Side

- [x] Confirmed same wire protocol works on Windows via named pipe (GET and SET both work)
- [x] Dynamic IRoot::GetFeature query instead of hardcoded feature index fallback
- [x] ~~Auto-detect HID device paths from the Logi Options+ agent instead of hardcoding in config.ini~~ -- Done: `kvm_daemon_windows.py` uses named pipe IPC to discover devices and switch hosts. No HID paths or feature indices needed.
- [x] ~~Replace compiled C programs with Python~~ -- Done: `kvm_daemon_windows.py` replaces UnifiedSwitch.exe + LogiSwitch.exe with a single Python daemon

## Protocol Exploration

- [ ] Document more API paths
- [ ] Explore `/lps/emulate/trigger_easy_switch` with correct payload format
- [ ] Explore `/api/v1/actions/invoke` for macro/action triggering
- [ ] Map out SUBSCRIBE endpoints for real-time device status monitoring
- [ ] Investigate the WebSocket server on port 59869

## Packaging

- [ ] Proper CLI arg parsing (argparse)
- [ ] Brew formula or installer for Mac
- [ ] LaunchAgent plist for auto-starting karabiner_console_user_server on boot

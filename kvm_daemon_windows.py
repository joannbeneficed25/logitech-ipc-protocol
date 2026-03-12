"""
Windows KVM daemon using Logi Options+ agent named pipe IPC.

Listens for configurable hotkeys (default: Win+1/2/3) and switches all
Logitech devices + monitor input to the selected host.

Replaces UnifiedSwitch.exe + LogiSwitch.exe with a single Python script.
No direct HID access needed — the Logi Options+ agent handles device
communication through its named pipe.

Usage:
    python kvm_daemon_windows.py              # Run daemon
    python kvm_daemon_windows.py --dry-run    # Show devices without switching
    python kvm_daemon_windows.py --switch 1   # One-shot switch to host 1

Requires:
    - Logi Options+ installed and running
    - pip install keyboard pywin32
    - Run as Administrator (keyboard library needs it for global hooks)
"""
import struct
import json
import os
import sys
import time
import logging
import subprocess
import configparser
import ctypes
import threading

import keyboard
import win32file
import win32pipe
import pywintypes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kvm")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "kvm_config.ini")
MUTEX_NAME = "KvmDaemonWindows_SingleInstance"


# ── Agent IPC (reused from query_agent_windows.py) ──────────────────────────


def find_pipe():
    try:
        pipes = [p for p in os.listdir(r"\\.\pipe") if p.startswith("logitech_kiros_agent")]
        return rf"\\.\pipe\{pipes[0]}" if pipes else None
    except OSError:
        return None


def make_frame(obj):
    data = json.dumps(obj).encode()
    proto = b"json"
    inner = struct.pack(">I", len(proto)) + proto + struct.pack(">I", len(data)) + data
    return struct.pack("<I", len(inner)) + inner


def parse_responses(data):
    results = []
    pos = 0
    while pos + 4 <= len(data):
        total = struct.unpack_from("<I", data, pos)[0]
        if total > 1_000_000 or pos + 4 + total > len(data):
            break
        inner = data[pos + 4 : pos + 4 + total]
        pos += 4 + total
        ipos = 0
        if ipos + 4 > len(inner):
            continue
        plen = struct.unpack_from(">I", inner, ipos)[0]
        ipos += 4
        proto = inner[ipos : ipos + plen]
        ipos += plen
        if ipos + 4 > len(inner):
            continue
        mlen = struct.unpack_from(">I", inner, ipos)[0]
        ipos += 4
        msg = inner[ipos : ipos + mlen]
        if proto == b"json":
            try:
                results.append(json.loads(msg))
            except json.JSONDecodeError:
                pass
    return results


def read_available(handle):
    chunks = b""
    while True:
        try:
            avail = win32pipe.PeekNamedPipe(handle, 0)[1]
            if avail == 0:
                break
            _, chunk = win32file.ReadFile(handle, avail)
            chunks += chunk
        except pywintypes.error:
            break
    return chunks


def open_pipe():
    pipe_path = find_pipe()
    if not pipe_path:
        return None
    try:
        handle = win32file.CreateFile(
            pipe_path,
            win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0,
            None,
            win32file.OPEN_EXISTING,
            0,
            None,
        )
        win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_BYTE, None, None)
        # Handshake
        win32file.WriteFile(handle, make_frame({"msg_id": "1", "verb": "GET", "path": "/permissions"}))
        time.sleep(0.5)
        read_available(handle)
        return handle
    except pywintypes.error as e:
        log.error("Failed to open agent pipe: %s", e)
        return None


def send_and_read(handle, msg, msg_id):
    try:
        win32file.WriteFile(handle, make_frame(msg))
        time.sleep(0.5)
        data = read_available(handle)
        for r in parse_responses(data):
            if isinstance(r, dict) and r.get("msgId") == msg_id:
                return r
        return None
    except pywintypes.error as e:
        log.error("Pipe communication error: %s", e)
        return None


# ── Device discovery ────────────────────────────────────────────────────────


def discover_devices(handle):
    resp = send_and_read(handle, {"msg_id": "2", "verb": "GET", "path": "/devices/list"}, "2")
    if not resp:
        log.error("No response from /devices/list")
        return []

    payload = resp.get("payload", {})
    device_infos = payload.get("deviceInfos", [])
    devices = []
    for d in device_infos:
        conn = d.get("connectionType", "")
        ifaces = d.get("activeInterfaces", [])
        if ifaces:
            conn = ifaces[0].get("connectionType", conn)

        if conn == "VIRTUAL":
            continue
        if not d.get("connected", False):
            continue
        if d.get("deviceType") == "RECEIVER":
            continue

        devices.append({
            "id": d["id"],
            "name": d.get("displayName", d["id"]),
            "type": d.get("deviceType", ""),
        })

    return devices


# ── Switching ───────────────────────────────────────────────────────────────


def switch_devices(devices, target_host):
    handle = open_pipe()
    if not handle:
        log.error("Logi Options+ agent pipe not found")
        return False

    try:
        for i, dev in enumerate(devices):
            msg_id = str(10 + i)
            resp = send_and_read(
                handle,
                {
                    "msg_id": msg_id,
                    "verb": "SET",
                    "path": f"/change_host/{dev['id']}/host",
                    "payload": {
                        "@type": "type.googleapis.com/logi.protocol.devices.ChangeHost",
                        "host": target_host,
                    },
                },
                msg_id,
            )
            if resp:
                code = resp.get("result", {}).get("code", "NO_RESPONSE")
                if code == "SUCCESS":
                    log.info("%s: switched to host %d", dev["name"], target_host)
                elif code == "NO_SUCH_PATH":
                    log.info("%s: on another host (normal)", dev["name"])
                else:
                    log.warning("%s: %s", dev["name"], code)
            else:
                log.warning("%s: no response", dev["name"])
        return True
    finally:
        handle.Close()


def switch_monitor(clickmon_path, monitor_value):
    if not monitor_value:
        return
    if not os.path.isfile(clickmon_path):
        log.warning("ControlMyMonitor not found at %s — skipping monitor switch", clickmon_path)
        return
    try:
        subprocess.run(
            [clickmon_path, "/SetValue", "Primary", "60", str(monitor_value)],
            timeout=5,
            capture_output=True,
        )
        log.info("Monitor: switched to input %s", monitor_value)
    except subprocess.TimeoutExpired:
        log.warning("Monitor: switch timed out")
    except Exception as e:
        log.warning("Monitor: error — %s", e)


# ── Config ──────────────────────────────────────────────────────────────────


def load_config():
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)

    hotkeys = {}
    for key in ["host_0", "host_1", "host_2"]:
        val = config.get("HOTKEYS", key, fallback=None)
        if val:
            host_num = int(key.split("_")[1])
            hotkeys[host_num] = val.strip()

    clickmon = config.get("MONITOR", "clickmon", fallback="dependencies\\ControlMyMonitor.exe")
    if not os.path.isabs(clickmon):
        clickmon = os.path.join(SCRIPT_DIR, clickmon)

    monitor_values = {}
    for key in ["host_0", "host_1", "host_2"]:
        val = config.get("MONITOR", key, fallback="").strip()
        if val:
            host_num = int(key.split("_")[1])
            monitor_values[host_num] = int(val)

    return hotkeys, clickmon, monitor_values


# ── Single instance ─────────────────────────────────────────────────────────


def ensure_single_instance():
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        log.error("Another instance is already running")
        sys.exit(1)
    return mutex


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    args = sys.argv[1:]
    is_dry_run = "--dry-run" in args
    one_shot_idx = None
    if "--switch" in args:
        idx = args.index("--switch")
        if idx + 1 < len(args):
            one_shot_idx = int(args[idx + 1])

    hotkeys, clickmon, monitor_values = load_config()

    # Discover devices
    log.info("Discovering devices...")
    handle = open_pipe()
    if not handle:
        log.error("Logi Options+ agent pipe not found. Is Logi Options+ running?")
        sys.exit(1)

    devices = discover_devices(handle)
    handle.Close()

    if not devices:
        log.error("No switchable devices found")
        sys.exit(1)

    log.info("Found %d device(s):", len(devices))
    for dev in devices:
        log.info("  %s (%s) — %s", dev["name"], dev["id"], dev["type"])

    # Dry run — just show devices and exit
    if is_dry_run:
        log.info("Dry run — not switching. Hotkeys would be:")
        for host, combo in sorted(hotkeys.items()):
            mon = monitor_values.get(host, "—")
            log.info("  %s → host %d, monitor input %s", combo, host, mon)
        return

    # One-shot mode
    if one_shot_idx is not None:
        log.info("Switching to host %d...", one_shot_idx)
        switch_devices(devices, one_shot_idx)
        switch_monitor(clickmon, monitor_values.get(one_shot_idx))
        log.info("Done.")
        return

    # Daemon mode
    ensure_single_instance()

    # Lock to prevent concurrent switch operations
    switch_lock = threading.Lock()

    def on_hotkey(target_host):
        if not switch_lock.acquire(blocking=False):
            log.info("Switch already in progress, ignoring")
            return
        try:
            log.info("Hotkey pressed — switching to host %d", target_host)
            switch_devices(devices, target_host)
            switch_monitor(clickmon, monitor_values.get(target_host))
        finally:
            switch_lock.release()

    for host, combo in sorted(hotkeys.items()):
        keyboard.add_hotkey(combo, on_hotkey, args=(host,), suppress=True)
        log.info("Registered hotkey: %s → host %d", combo, host)

    log.info("KVM daemon running. Press Ctrl+C to stop.")

    try:
        keyboard.wait()
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()

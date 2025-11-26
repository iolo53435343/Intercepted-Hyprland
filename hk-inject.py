#!/usr/bin/env python3
import socket
import json
import subprocess
import time
import sys
import argparse
from Xlib import X, display, error
from Xlib.ext import xtest

# --- DEFAULTS ---
DEFAULT_SOCK = "/tmp/hkd.sock"
DEFAULT_TIMEOUT = 10
DEFAULT_TARGET = "JKPS"

# Default Map: FVNJ
# Format: {Evdev_Code: X11_Code}
# Rule: X11 = Evdev + 8
DEFAULT_MAP = {
    33: 41, # F (Evdev 33 -> X11 41)
    47: 55, # V (Evdev 47 -> X11 55)
    49: 57, # N (Evdev 49 -> X11 57)
    36: 44, # J (Evdev 36 -> X11 44)
}
# ----------------

def parse_key_map(arg_str):
    """Parses 'In:Out,In:Out' string into a dict."""
    if not arg_str: return DEFAULT_MAP
    try:
        new_map = {}
        pairs = arg_str.split(',')
        for p in pairs:
            k, v = p.split(':')
            new_map[int(k)] = int(v)
        return new_map
    except ValueError:
        print("[!] Invalid key format. Using defaults.")
        return DEFAULT_MAP

def get_window_id(name):
    try:
        cmd = f"xwininfo -root -tree | grep -i '{name}' | head -1 | awk '{{print $1}}'"
        out = subprocess.check_output(cmd, shell=True).decode().strip()
        if not out: return None
        return int(out, 16)
    except:
        return None

def main():
    parser = argparse.ArgumentParser(description="Inject raw Hyprland input into XWayland windows.")
    parser.add_argument("--target", "-t", default=DEFAULT_TARGET, help="Partial window title")
    parser.add_argument("--socket", "-s", default=DEFAULT_SOCK, help="Path to hkd daemon socket")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Seconds to wait for window")
    parser.add_argument("--keys", "-k", help="Key override (e.g. '44:52,45:53' for Z/X)")

    args = parser.parse_args()
    ACTIVE_MAP = parse_key_map(args.keys)

    # 1. Connect to X11
    try:
        d = display.Display()
        d.set_error_handler(lambda *a: None) # Suppress Xlib spam
    except Exception as e:
        sys.exit(f"[!] Critical: Could not connect to X server. {e}")

    # 2. Hunt for Target
    print(f"[*] Hunting for window: '{args.target}' (Timeout: {args.timeout}s)...")
    start_time = time.time()
    wid = None

    while wid is None:
        wid = get_window_id(args.target)
        if wid: break
        if time.time() - start_time > args.timeout:
            sys.exit(f"[!] Timeout: Window '{args.target}' not found.")
        time.sleep(0.5)

    try:
        target_window = d.create_resource_object('window', wid)
        target_window.get_attributes()
    except:
         sys.exit("[!] Found ID but window is invalid/dead.")

    print(f"[*] Locked on. Window ID: {hex(wid)}")
    print(f"[*] Active Keys: {ACTIVE_MAP}")

    # 3. Force Focus (The XWayland Wake-Up Call)
    try:
        d.set_input_focus(target_window, X.RevertToNone, X.CurrentTime)
        d.sync()
        print("[*] Focus locked to target (XWayland active).")
    except Exception as e:
        sys.exit(f"[!] Failed to set focus: {e}")

    # 4. Connect to Daemon
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        sock.connect(args.socket)
        print(f"[*] Connected to hkd at {args.socket}")
    except Exception as e:
        sys.exit(f"[!] Daemon connection failed: {e}")

    # 5. The Loop
    buff = ""
    running = True

    while running:
        try:
            # Heartbeat check
            try:
                target_window.get_attributes()
                d.sync()
            except error.BadWindow:
                print("[!] Window closed. Exiting.")
                running = False
                break
            except Exception:
                break

            # Read Socket
            try:
                data = sock.recv(4096).decode()
            except socket.timeout:
                continue
            except OSError:
                print("[!] Daemon disconnected.")
                break

            if not data: break

            buff += data
            while "\n" in buff:
                line, buff = buff.split("\n", 1)
                if not line: continue

                try:
                    ev = json.loads(line)
                    raw_key = ev["key"]

                    if raw_key in ACTIVE_MAP:
                        x_keycode = ACTIVE_MAP[raw_key]
                        is_press = (ev["state"] == "DOWN")

                        # Use XTEST for hardware simulation
                        xtest.fake_input(d, X.KeyPress if is_press else X.KeyRelease, x_keycode)
                        d.sync()

                except json.JSONDecodeError:
                    pass

        except KeyboardInterrupt:
            print("\n[*] Stopping.")
            break
        except Exception as e:
            print(f"[!] Runtime Error: {e}")
            break

if __name__ == "__main__":
    main()

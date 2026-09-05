#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import subprocess
import time
from pathlib import Path

from PIL import ImageGrab

user32 = ctypes.windll.user32


def visible_windows_for_pid(pid: int):
    windows = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value != pid:
            return True
        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w > 200 and h > 150:
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            windows.append((w * h, hwnd, (rect.left, rect.top, rect.right, rect.bottom), buf.value))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return sorted(windows, reverse=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iso", type=Path, required=True)
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--wait", type=float, default=10.0)
    ap.add_argument("--pcsx2-screenshot", action="store_true")
    ap.add_argument("--press", action="append", default=[], help="virtual key name to press after loading")
    ap.add_argument("--press-wait", type=float, default=1.0)
    args = ap.parse_args()
    exe = Path(r"D:\game\pcsx2-v2.6.3-windows-x64-Qt\pcsx2-qt.exe")
    cmd = [str(exe), "-batch", "-nofullscreen", "-statefile", str(args.state.resolve()), "--", str(args.iso.resolve())]
    p = subprocess.Popen(cmd)
    try:
        time.sleep(args.wait)
        wins = visible_windows_for_pid(p.pid)
        if not wins:
            raise RuntimeError(f"no visible PCSX2 window found for pid {p.pid}")
        _, hwnd, bbox, title = wins[0]
        fg_before = user32.GetForegroundWindow()
        current_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg_before, None) if fg_before else 0
        attached = False
        if fg_tid and fg_tid != current_tid:
            attached = bool(user32.AttachThreadInput(current_tid, fg_tid, True))
        user32.ShowWindow(hwnd, 9)
        user32.BringWindowToTop(hwnd)
        fg_ok = bool(user32.SetForegroundWindow(hwnd))
        if attached:
            user32.AttachThreadInput(current_tid, fg_tid, False)
        time.sleep(0.8)
        fg = user32.GetForegroundWindow()
        print(f"foreground_set={fg_ok} target_hwnd={int(hwnd)} foreground_hwnd={int(fg)}")
        key_map = {
            "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
            "ENTER": 0x0D, "BACKSPACE": 0x08,
            "Q": ord("Q"), "E": ord("E"), "W": ord("W"), "A": ord("A"),
            "S": ord("S"), "D": ord("D"), "K": ord("K"), "L": ord("L"),
            "I": ord("I"), "J": ord("J"), "T": ord("T"), "F": ord("F"),
            "G": ord("G"), "H": ord("H"),
        }
        for name in args.press:
            key = key_map.get(name.upper())
            if key is None:
                raise ValueError(f"unsupported --press key: {name}")
            user32.keybd_event(key, 0, 0, 0)
            time.sleep(0.10)
            user32.keybd_event(key, 0, 0x0002, 0)
            time.sleep(args.press_wait)
        if args.pcsx2_screenshot:
            vk_f8 = 0x77
            keyeventf_keyup = 0x0002
            user32.keybd_event(vk_f8, 0, 0, 0)
            time.sleep(0.12)
            user32.keybd_event(vk_f8, 0, keyeventf_keyup, 0)
            time.sleep(2.0)
        shot = ImageGrab.grab(bbox=bbox, all_screens=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        shot.save(args.output)
        print(f"captured {shot.size} bbox={bbox} title={title!r} output={args.output}")
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


if __name__ == "__main__":
    main()

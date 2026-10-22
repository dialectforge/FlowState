"""Windows only, started by tests/terminal_run.py: the "user" at a console of its own.

    python -I conin_typist.py PID ANSWERS_JSON SCREEN_FILE TIMEOUT

Attaches to the console of process PID, watches its screen, and types each answer (plus
Enter) as key presses into the console input when one more "Type ..." prompt has appeared,
the way a person would. When PID ends (or TIMEOUT seconds pass) it writes the screen text to
SCREEN_FILE. Standard library only (ctypes).
"""
import ctypes
import json
import sys
import time
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
GENERIC_RW = 0x80000000 | 0x40000000
SHARE_RW = 0x1 | 0x2
OPEN_EXISTING = 3
INVALID = ctypes.c_void_p(-1).value
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
KEY_EVENT = 0x1
VK_RETURN = 0x0D


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("UnicodeChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]


class _EVENT(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT)]


class CSBI(ctypes.Structure):
    _fields_ = [("dwSize", wintypes._COORD), ("dwCursorPosition", wintypes._COORD),
                ("wAttributes", wintypes.WORD), ("srWindow", wintypes.SMALL_RECT),
                ("dwMaximumWindowSize", wintypes._COORD)]


k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.OpenProcess.restype = wintypes.HANDLE
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.WriteConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(INPUT_RECORD), wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD)]
k32.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(CSBI)]
k32.ReadConsoleOutputCharacterW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD,
                                            wintypes._COORD, ctypes.POINTER(wintypes.DWORD)]


def _open(name):
    h = k32.CreateFileW(name, GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, 0, None)
    if h in (None, INVALID):
        raise OSError(f"can't open {name}: {ctypes.get_last_error()}")
    return h


def screen(out) -> str:
    info = CSBI()
    if not k32.GetConsoleScreenBufferInfo(out, ctypes.byref(info)):
        return ""
    width, rows = info.dwSize.X, info.dwCursorPosition.Y + 1
    buf = ctypes.create_unicode_buffer(width * rows)
    n = wintypes.DWORD()
    k32.ReadConsoleOutputCharacterW(out, buf, width * rows, wintypes._COORD(0, 0), ctypes.byref(n))
    text = buf[:n.value]
    return "\n".join(text[i:i + width].rstrip() for i in range(0, len(text), width)).rstrip()


def type_line(inp, line: str) -> None:
    keys = []
    for ch in line + "\r":
        for down in (True, False):
            r = INPUT_RECORD()
            r.EventType = KEY_EVENT
            ke = r.Event.KeyEvent
            ke.bKeyDown, ke.wRepeatCount, ke.UnicodeChar = down, 1, ch
            ke.wVirtualKeyCode = VK_RETURN if ch == "\r" else (ord(ch.upper()) if ch.isalnum() and ch.isascii() else 0)
            keys.append(r)
    arr = (INPUT_RECORD * len(keys))(*keys)
    n = wintypes.DWORD()
    k32.WriteConsoleInputW(inp, arr, len(keys), ctypes.byref(n))


def main() -> int:
    pid, answers, screen_file, timeout = int(sys.argv[1]), json.loads(sys.argv[2]), sys.argv[3], float(sys.argv[4])
    proc = k32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    k32.FreeConsole()                                   # leave our own (windowless) console
    deadline = time.monotonic() + timeout
    while not k32.AttachConsole(pid):
        if time.monotonic() > deadline or (proc and k32.WaitForSingleObject(proc, 0) == 0):
            return 1                                    # it ended before we got there
        time.sleep(0.05)
    inp, out = _open("CONIN$"), _open("CONOUT$")
    todo, last = list(answers), ""
    while time.monotonic() < deadline:
        last = screen(out)
        if todo and last.count("Type ") > len(answers) - len(todo):
            type_line(inp, todo.pop(0))
        if proc and k32.WaitForSingleObject(proc, 200) == 0:
            break
    last = screen(out) or last
    with open(screen_file, "w", encoding="utf-8") as f:
        f.write(last)
    return 0


if __name__ == "__main__":
    sys.exit(main())

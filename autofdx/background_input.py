"""Window-targeted input: Frida cursor/capture bridge + WM_ACTIVATEAPP.

No SendInput, physical cursor movement, or foreground activation fallback.
Coordinates accepted by move_to are screen pixels, matching existing callers.
"""
import ctypes
from ctypes import wintypes as W
import logging
from pathlib import Path
import sys
import threading
from time import monotonic, sleep


class InputUnavailable(RuntimeError):
    pass


def packed_point(x, y):
    x, y = int(x), int(y)
    if not (-32768 <= x <= 32767 and -32768 <= y <= 32767):
        raise InputUnavailable("Mouse message coordinates exceed signed 16-bit range")
    return ((y & 0xffff) << 16) | (x & 0xffff)


def virtual_key(key):
    name = str(key).lower()
    named = {"esc": 0x1b, "escape": 0x1b, "enter": 0x0d, "return": 0x0d,
             "space": 0x20, "tab": 9, "backspace": 8, "delete": 0x2e,
             "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
             "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22}
    if name in named:
        return named[name]
    if len(name) == 1 and name.isascii() and name.isalnum():
        return ord(name.upper())
    if name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        return 0x70 + int(name[1:]) - 1
    raise ValueError(f"Unsupported game key: {key!r}; no physical-key fallback")


class Win32:
    def __init__(self):
        if sys.platform != "win32":
            raise InputUnavailable("Background input requires Windows")
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        signatures = {
            "IsWindow": (W.BOOL, [W.HWND]), "IsIconic": (W.BOOL, [W.HWND]),
            "GetForegroundWindow": (W.HWND, []),
            "GetWindowThreadProcessId": (W.DWORD, [W.HWND, ctypes.POINTER(W.DWORD)]),
            "GetClientRect": (W.BOOL, [W.HWND, ctypes.POINTER(W.RECT)]),
            "ClientToScreen": (W.BOOL, [W.HWND, ctypes.POINTER(W.POINT)]),
            "ScreenToClient": (W.BOOL, [W.HWND, ctypes.POINTER(W.POINT)]),
            "PostMessageW": (W.BOOL, [W.HWND, W.UINT, W.WPARAM, W.LPARAM]),
            "MapVirtualKeyW": (W.UINT, [W.UINT, W.UINT]),
        }
        for name, (result, arguments) in signatures.items():
            fn = getattr(self.u, name)
            fn.restype, fn.argtypes = result, arguments

    @staticmethod
    def checked(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def identity(self, hwnd):
        if not hwnd or not self.u.IsWindow(hwnd):
            raise InputUnavailable("Game window is missing or closed")
        if self.u.IsIconic(hwnd):
            raise InputUnavailable("Game is minimized; restore it without foreground activation")
        pid = W.DWORD()
        self.checked(self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)))
        return int(hwnd), pid.value

    def client_size(self, hwnd):
        rect = W.RECT()
        self.checked(self.u.GetClientRect(hwnd, ctypes.byref(rect)))
        if rect.right <= 0 or rect.bottom <= 0:
            raise InputUnavailable("Game client area is empty")
        return rect.right, rect.bottom

    def screen(self, hwnd, x, y):
        p = W.POINT(int(x), int(y))
        self.checked(self.u.ClientToScreen(hwnd, ctypes.byref(p)))
        return p.x, p.y

    def client(self, hwnd, x, y):
        p = W.POINT(int(x), int(y))
        self.checked(self.u.ScreenToClient(hwnd, ctypes.byref(p)))
        return p.x, p.y

    def post(self, hwnd, message, wp=0, lp=0):
        self.checked(self.u.PostMessageW(hwnd, message, wp, lp))

    def foreground(self):
        return self.u.GetForegroundWindow()

    def foreground_thread(self):
        return self.u.GetWindowThreadProcessId(self.foreground(), None)

    def scan(self, vk):
        return self.u.MapVirtualKeyW(vk, 4)


def attach_frida(pid):
    try:
        import frida
    except ImportError as exc:
        raise InputUnavailable("Missing frida: python -m pip install -r requirements.txt") from exc
    return frida.attach(pid)


class BackgroundInput:
    """One shared, serialized session for actions, hotkeys and scroll workers."""
    CLICK_HOLD_SEC = 0.08
    KEY_HOLD_SEC = 0.06

    def __init__(self, hwnd_getter, *, api=None, attach=None):
        self._get_hwnd = hwnd_getter
        self._api = api or Win32()
        self._attach = attach or attach_frida
        self._lock = threading.RLock()
        self._session = self._script = None
        self._identity = None
        self._position = None  # client pixels; independent of physical cursor
        self._pressed = {}
        self._mouse_down = False
        self._active = False
        self._fault = None
        self._activation_time = 0.0
        self._closed = False

    def _message(self, message, data):
        if message.get("type") == "error":
            self._fault = message.get("description", str(message))
        elif message.get("type") == "send" and message.get("payload", {}).get("kind") == "error":
            self._fault = str(message["payload"])

    def _detached(self, session, reason, crash=None):
        if self._session is session:
            self._fault = f"Frida detached: {reason}"

    def _ensure(self):
        if self._closed:
            raise InputUnavailable("Input backend is shut down")
        if self._fault:
            fault = self._fault
            self._release_locked()
            raise InputUnavailable(fault)
        try:
            identity = self._api.identity(self._get_hwnd())
        except Exception:
            self._release_locked()
            raise
        if self._identity != identity:
            self._release_locked()
            self._identity = identity
            width, height = self._api.client_size(identity[0])
            self._position = (width // 2, height // 2)
            try:
                session = self._attach(identity[1])
                self._session = session
                session.on("detached", lambda reason, crash=None: self._detached(session, reason, crash))
                agent = Path(__file__).with_name("background_input_agent.js").read_text(encoding="utf-8")
                self._script = session.create_script(agent)
                self._script.on("message", self._message)
                self._script.load()
                self._script.exports_sync.configure(hex(identity[0]))
                if self._fault:
                    raise InputUnavailable(self._fault)
            except Exception as exc:
                self._release_locked()
                raise InputUnavailable(f"Background hook attachment failed: {exc}") from exc
        return identity[0]

    def _prepare(self):
        hwnd = self._ensure()
        x, y = self._position
        width, height = self._api.client_size(hwnd)
        x, y = min(max(x, 0), width - 1), min(max(y, 0), height - 1)
        self._position = (x, y)
        sx, sy = self._api.screen(hwnd, x, y)
        # Re-arm if a real Alt-Tab generated a new deactivation since our last action.
        needs_activation = self._script.exports_sync.prepare(sx, sy)
        if not self._active or needs_activation:
            self._active = True  # cleanup even if posting fails
            self._api.post(hwnd, 0x001c, 1, self._api.foreground_thread())
            self._activation_time = monotonic()
            sleep(0.12)
        if self._fault:
            raise InputUnavailable(self._fault)
        return hwnd

    def ensure_focus(self, settle_sec=0.2):
        """Compatibility name: prepare internal app state, NEVER take OS focus."""
        with self._lock:
            self._prepare()
            return True

    def _move_client(self, x, y):
        hwnd = self._ensure()
        width, height = self._api.client_size(hwnd)
        self._position = (min(max(int(x), 0), width - 1), min(max(int(y), 0), height - 1))
        self._prepare()
        self._api.post(hwnd, 0x0200, int(self._mouse_down), packed_point(*self._position))

    def move_to(self, x, y):
        with self._lock:
            hwnd = self._ensure()
            cx, cy = self._api.client(hwnd, x, y)
            width, height = self._api.client_size(hwnd)
            if not (0 <= cx < width and 0 <= cy < height):
                raise InputUnavailable("Requested input point is outside game client area")
            self._move_client(cx, cy)
            sleep(0.02)

    def move_rel(self, dx, dy=0, duration=0.0):
        """Virtual client motion, NOT guaranteed Raw Input camera rotation."""
        with self._lock:
            self._ensure()
            x, y = self._position
            duration = max(0.0, float(duration))
            steps = max(1, int(duration / 0.02))
            for i in range(1, steps + 1):
                self._move_client(x + round(int(dx)*i/steps), y + round(int(dy)*i/steps))
                if duration:
                    sleep(duration / steps)
        return True

    def camera_move_rel(self, dx, dy=0):
        """Queue a target-only WM_INPUT relative mouse packet, not UI motion."""
        dx, dy = int(dx), int(dy)
        if not dx and not dy:
            return True
        with self._lock:
            hwnd = self._prepare()
            token = self._script.exports_sync.queue_raw(dx, dy)
            try:
                self._api.post(hwnd, 0x00ff, 1, int(token, 16))
            except BaseException:
                self._script.exports_sync.cancel_raw(token)
                raise
        return True

    def camera_status(self):
        with self._lock:
            if self._script is None:
                return {"queued": 0, "dataReads": 0, "pending": 0}
            return self._script.exports_sync.raw_status()

    def nudge(self, px=3):
        with self._lock:
            self._ensure()
            x, y = self._position
            self._move_client(x + int(px), y)
            sleep(0.02)
            self._move_client(x, y)

    def left_click(self, hold=None):
        with self._lock:
            hwnd = self._prepare()
            lp = packed_point(*self._position)
            self._api.post(hwnd, 0x0200, 0, lp)
            sleep(0.03)
            self._mouse_down = True
            try:
                self._api.post(hwnd, 0x0201, 1, lp)
                sleep(max(0.01, self.CLICK_HOLD_SEC if hold is None else float(hold)))
            finally:
                self._api.post(hwnd, 0x0202, 0, lp)
                self._mouse_down = False

    def click(self, clicks=1, interval=0.08):
        with self._lock:
            for i in range(max(1, int(clicks))):
                self.left_click()
                if i < int(clicks)-1:
                    sleep(max(0.0, float(interval)))

    def scroll(self, amount, x=None, y=None):
        """Raw wheel delta, preserving existing units (120 = one notch)."""
        with self._lock:
            if x is not None and y is not None:
                self.move_to(x, y)
            hwnd = self._prepare()
            self._api.post(hwnd, 0x0200, 0, packed_point(*self._position))
            sx, sy = self._api.screen(hwnd, *self._position)
            remaining = int(amount)
            while remaining:
                delta = max(-32768, min(32767, remaining))
                self._api.post(hwnd, 0x020a, ((delta & 0xffff) << 16) | int(self._mouse_down), packed_point(sx, sy))
                remaining -= delta

    def press(self, key, hold=None):
        vk = virtual_key(key)
        with self._lock:
            hwnd = self._prepare()
            scan = self._api.scan(vk)
            lp = 1 | ((scan & 0xff) << 16) | ((1 << 24) if scan >> 8 == 0xe0 else 0)
            self._pressed[vk] = lp
            try:
                self._api.post(hwnd, 0x0100, vk, lp)
                sleep(max(0.01, self.KEY_HOLD_SEC if hold is None else float(hold)))
            finally:
                self._api.post(hwnd, 0x0101, vk, lp | (3 << 30))
                self._pressed.pop(vk, None)
        return True

    def press_after_delay(self, key, delay_sec=0.0, abort_check=None, repeat=1, interval=0.0):
        end = monotonic() + max(0.0, float(delay_sec))
        while monotonic() < end:
            if abort_check and abort_check():
                return False
            sleep(min(0.02, max(0, end-monotonic())))
        for i in range(max(1, int(repeat))):
            if abort_check and abort_check():
                return False
            self.press(key)
            if i < int(repeat)-1:
                sleep(max(0, float(interval)))
        return True

    def _release_locked(self):
        hwnd = self._identity[0] if self._identity else None
        failures = []
        def attempt(label, fn):
            try:
                fn()
            except Exception as exc:
                failures.append(f"{label}: {exc}")
        if hwnd:
            for vk, lp in list(self._pressed.items()):
                attempt("key release", lambda vk=vk, lp=lp: self._api.post(hwnd, 0x0101, vk, lp | (3 << 30)))
            if self._mouse_down:
                attempt("mouse release", lambda: self._api.post(hwnd, 0x0202, 0, packed_point(*self._position)))
            if self._active:
                attempt("restore activation", lambda: self._api.post(hwnd, 0x001c, int(self._api.foreground()==hwnd), self._api.foreground_thread()))
                # Keep guards installed while UE processes deferred restoration.
                sleep(0.3)
        script, session = self._script, self._session
        self._session = self._script = None
        if script is not None:
            attempt("disable", script.exports_sync.stop)
            attempt("unload", script.unload)
        if session is not None:
            attempt("detach", session.detach)
        self._identity = self._position = None
        self._pressed.clear()
        self._mouse_down = self._active = False
        self._fault = None
        if failures:
            logging.error("Background input cleanup incomplete: %s", "; ".join(failures))
        return failures

    def close(self):
        """Release and detach for pause/calibration; next action may reattach."""
        with self._lock:
            return self._release_locked()

    def shutdown(self):
        """Permanent close for application exit, rejecting late worker input."""
        with self._lock:
            self._closed = True
            return self._release_locked()

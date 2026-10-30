"""会话结束通知适配器：不阻止、不延迟系统注销或关机。"""

import sys
import threading


class SessionEvents:
    def __init__(self, callback):
        self.callback = callback
        self.thread_id = None
        self.thread = None
        if sys.platform == "win32":
            self.thread = threading.Thread(target=self._windows, daemon=True)
            self.thread.start()

    def notify(self):
        self.callback()

    def _windows(self):
        import ctypes as c
        from ctypes import wintypes as w

        user, kernel = (
            c.WinDLL("user32", use_last_error=True),
            c.WinDLL("kernel32", use_last_error=True),
        )
        proc_type = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        class WindowClass(c.Structure):
            _fields_ = [
                ("style", w.UINT),
                ("procedure", proc_type),
                ("class_extra", c.c_int),
                ("window_extra", c.c_int),
                ("instance", w.HINSTANCE),
                ("icon", w.HICON),
                ("cursor", w.HANDLE),
                ("background", w.HBRUSH),
                ("menu", w.LPCWSTR),
                ("name", w.LPCWSTR),
            ]

        kernel.GetModuleHandleW.restype = w.HMODULE
        kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
        user.DefWindowProcW.restype = c.c_ssize_t
        user.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        user.CreateWindowExW.restype = w.HWND
        user.CreateWindowExW.argtypes = [
            w.DWORD,
            w.LPCWSTR,
            w.LPCWSTR,
            w.DWORD,
            c.c_int,
            c.c_int,
            c.c_int,
            c.c_int,
            w.HWND,
            w.HMENU,
            w.HINSTANCE,
            c.c_void_p,
        ]
        user.RegisterClassW.argtypes = [c.POINTER(WindowClass)]
        user.DestroyWindow.argtypes = [w.HWND]
        user.UnregisterClassW.argtypes = [w.LPCWSTR, w.HINSTANCE]
        self.thread_id = kernel.GetCurrentThreadId()

        @proc_type
        def procedure(hwnd, message, wp, lp):
            if message in {0x0011, 0x0016}:
                if message == 0x0011 or wp:
                    self.notify()
                return 1
            return user.DefWindowProcW(hwnd, message, wp, lp)

        instance = kernel.GetModuleHandleW(None)
        name = "ClytieSessionEnd"
        wc = WindowClass(0, procedure, 0, 0, instance, None, None, None, None, name)
        if not user.RegisterClassW(c.byref(wc)):
            return
        # Invisible top-level window receives session-end broadcasts; not a message-only window.
        hwnd = user.CreateWindowExW(0, name, "", 0, 0, 0, 0, 0, None, None, instance, None)
        try:
            msg = w.MSG()
            while user.GetMessageW(c.byref(msg), None, 0, 0) > 0:
                user.TranslateMessage(c.byref(msg))
                user.DispatchMessageW(c.byref(msg))
        finally:
            if hwnd:
                user.DestroyWindow(hwnd)
            user.UnregisterClassW(name, instance)

    def close(self):
        if self.thread_id:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)

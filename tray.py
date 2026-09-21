# -*- coding: utf-8 -*-
"""右下角托盘图标。**只用 ctypes 调 Win32，不引入任何第三方库。**

为什么自己写而不用 pystray：本项目全体零依赖（单文件 exe，只用标准库 +
tkinter），pystray 会拖进 Pillow，光那一项就把包撑大十几兆。

## 关键难点：消息谁来泵

Shell_NotifyIcon 注册时要给一个窗口句柄，鼠标点击会变成 WM_ 消息发给它。
**消息得有消息循环去取**，而 Tk 只泵它自己的窗口。

三条路：
  a) 子类化 Tk 的窗口过程 —— 要替换 Tk 的 WNDPROC，ctypes 回调写错就把
     Tk 一起搞崩，风险太高
  b) 自己建隐藏窗口，塞进 Tk 的消息循环 —— 做不到，Tk 不泵别人的窗口
  c) **自己开一个线程，建隐藏窗口 + 自己的 GetMessage/DispatchMessage**
     ← 选这条。跟 Tk 完全隔离，出问题也只死托盘那条线程。

## 线程边界

托盘线程拿到点击后不能直接碰 Tk（Tk 不是线程安全的），
所以回调里只做一件事：交给 `post` 把活儿丢回主线程。
调用方传进来的 post 就是 `root.after`。
"""
import ctypes
import ctypes.wintypes as wintypes
import os
import threading

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

# ---- 常量 ----
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
#: 气泡提示。收进托盘之后窗口就没了，这是唯一还能跟用户说话的地方 ——
#: 用它说一句"已收进托盘，还在盯着开播"，省得用户以为程序退了。
NIF_INFO = 0x10

IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x0010, 0x0040
IDI_APPLICATION = 32512

MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
TPM_NONOTIFY = 0x0080

#: 菜单项的 id。第一个给 1，往后排。
ID_SHOW, ID_MAIN, ID_QUIT = 1, 2, 3




class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


#: **不能直接用 wintypes.WPARAM / LPARAM。** 那两个在 64 位 Python 下仍是
#: 32 位（c_ulong / c_long），而真实的它们是**指针宽度**。鼠标消息的 lparam
#: 里塞着屏幕坐标，一超 32 位就在 DefWindowProcW 上抛 OverflowError。
#:
#: 这是 ctypes 写 Win32 最经典的坑：不声明 argtypes，它就按默认宽度猜。
LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t

WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)

# ---- 声明所有用到的 Win32 签名 ------------------------------------------
#
# 不声明的话 ctypes 按默认宽度猜：32 位下能跑，64 位下在某个参数上炸。
# 与其一个个踩，不如一次写全。
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype = LRESULT

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND

user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.RegisterClassW.restype = wintypes.ATOM

user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                              wintypes.UINT, ctypes.c_int, ctypes.c_int,
                              wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE

user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadIconW.restype = wintypes.HICON

user32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int

user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wintypes.HWND, ctypes.c_void_p]
user32.TrackPopupMenu.restype = ctypes.c_int

user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                               ctypes.c_size_t, wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL

user32.CreatePopupMenu.restype = wintypes.HMENU
user32.GetCursorPos.argtypes = [ctypes.c_void_p]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.PostQuitMessage.argtypes = [ctypes.c_int]

shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetLastError.restype = wintypes.DWORD



class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class TrayIcon(object):
    """一个托盘图标。

    参数：
        tooltip  鼠标悬停时的提示文字
        icon      .ico 路径；找不到就用系统默认图标
        on_toggle 左键点击 / 双击 —— 显示或隐藏主界面
        on_quit   菜单里的「退出」
        on_main   菜单里的「开始 / 停止监控」
        post      **把活儿丢回主线程**用的函数，就是 root.after
        state_text 返回菜单里那一行「开始监控 / 停止监控」的文字
    """

    def __init__(self, tooltip="大肥鱼直播姬", icon="", on_toggle=None,
                 on_quit=None, on_main=None, post=None, state_text=None):
        self.tooltip = tooltip
        self.icon_path = icon
        self.on_toggle = on_toggle
        self.on_quit = on_quit
        self.on_main = on_main
        self.post = post or (lambda fn: fn())
        self.state_text = state_text or (lambda: "开始监控")

        self._hwnd = None
        self._thread = None
        self._ready = threading.Event()
        self._nid = None
        self._hicon = None
        self._wndproc_ref = None       # 必须留引用，否则回调被 GC 掉就崩
        self.ok = False

    # ------------------------------------------------------------------
    def start(self, timeout=3.0):
        """起托盘线程并等它就绪。返回是否成功。"""
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="tray")
        self._thread.start()
        self._ready.wait(timeout)
        return self.ok

    def say(self, title, text):
        """弹一个托盘气泡。失败就算了 —— 提示而已，不该影响主流程。"""
        try:
            self._nid.uFlags = NIF_INFO
            self._nid.szInfoTitle = str(title)[:63]
            self._nid.szInfo = str(text)[:255]
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
        except Exception:
            pass

    def stop(self):
        """移除图标。程序退出前调，否则图标会一直挂在那儿直到鼠标划过。"""
        if self._hwnd:
            try:
                nid = self._make_nid()
                nid.uFlags = 0
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            except Exception:
                pass
            try:
                user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _run(self):
        try:
            self._create_window()
            self._add_icon()
            self.ok = True
            self._ready.set()
            self._pump()
        except Exception:
            self.ok = False
            self._ready.set()
        finally:
            if self._hicon:
                try:
                    user32.DestroyIcon(self._hicon)
                except Exception:
                    pass

    def _create_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._wndproc_ref = WNDPROC(self._wndproc)
        wc = WNDCLASS()
        wc.style = 0
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = "DaFeiYuTrayWnd"
        # 同一个类名注册两次会失败（比如换主题时重建），先试着直接建窗口
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            err = kernel32.GetLastError()
            if err != 1410:            # 1410 = 类已存在，那是好事
                raise OSError("RegisterClassW 失败，err={}".format(err))
        # 纯消息窗口：不显示，只用来收托盘回调
        self._hwnd = user32.CreateWindowExW(
            0, wc.lpszClassName, "tray", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self._hwnd:
            raise OSError("CreateWindowExW 失败，err={}".format(kernel32.GetLastError()))

    def _load_icon(self):
        if self.icon_path and os.path.isfile(self.icon_path):
            h = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0,
                                  LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h
        return user32.LoadIconW(None, wintypes.LPCWSTR(IDI_APPLICATION))

    def _make_nid(self):
        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self.tooltip[:127]
        return nid

    def _add_icon(self):
        self._hicon = self._load_icon()
        self._nid = self._make_nid()
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid)):
            raise OSError("Shell_NotifyIconW(NIM_ADD) 失败")

    def _pump(self):
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    # ------------------------------------------------------------------
    def _wndproc(self, hwnd, msg, wparam, lparam):
        # 窗口过程里漏出去的异常，ctypes 只会往 stderr 打一行 "Exception
        # ignored"，然后回调返回垃圾值 —— 表现成界面莫名卡住。
        # 所以整段包住，最坏也要把消息交回系统。
        try:
            if msg == WM_TRAY:
                low = lparam & 0xFFFF
                if low in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._fire(self.on_toggle)
                elif low in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._menu()
                return 0
            if msg == WM_COMMAND:
                cmd = wparam & 0xFFFF
                if cmd == ID_SHOW:
                    self._fire(self.on_toggle)
                elif cmd == ID_MAIN:
                    self._fire(self.on_main)
                elif cmd == ID_QUIT:
                    self._fire(self.on_quit)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            # 窗口过程里抛异常会直接把进程带走，绝不能漏出去
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _fire(self, fn):
        """把回调丢回主线程。Tk 不是线程安全的，这里绝不能直接调。"""
        if fn is None:
            return
        try:
            self.post(fn)
        except Exception:
            pass

    def _menu(self):
        """右键菜单。

        两处讲究：
          · ShowWindow/SetForegroundWindow —— 不调的话菜单弹出后点别处
            不消失，是 Win32 的老规矩。
          · TPM_RETURNCMD —— 让 TrackPopupMenu 直接返回选中的 id，
            而不是发 WM_COMMAND。少了跨线程那一跳。
        """
        try:
            hmenu = user32.CreatePopupMenu()
            if not hmenu:
                return
            user32.AppendMenuW(hmenu, MF_STRING, ID_SHOW, "显示 / 隐藏主界面")
            user32.AppendMenuW(hmenu, MF_STRING, ID_MAIN, str(self.state_text()))
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, ID_QUIT, "退出")

            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(self._hwnd)
            cmd = user32.TrackPopupMenu(
                hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                pt.x, pt.y, 0, self._hwnd, None)
            user32.PostMessageW(self._hwnd, 0, 0, 0)
            user32.DestroyMenu(hmenu)

            if cmd == ID_SHOW:
                self._fire(self.on_toggle)
            elif cmd == ID_MAIN:
                self._fire(self.on_main)
            elif cmd == ID_QUIT:
                self._fire(self.on_quit)
        except Exception:
            pass

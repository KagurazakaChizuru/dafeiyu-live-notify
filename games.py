# -*- coding: utf-8 -*-
"""游戏名识别 —— 开播通知里那句「正在玩《XXX》」是从哪来的。

为什么需要它
------------
群里问得最多的不是「你播了吗」，是「今天播什么」。通知里光写「开播了」
没什么说服力，写清楚在玩什么，来的人才多。

为什么不用 OCR
--------------
实测过，窗口标题就够了：

    DeltaForceClient-Win64-Shipping / UnrealWindow / "三角洲行动"

OCR 要截图、会认错字、还得把屏幕内容送去识别，代价远大于收益。
真到窗口标题也认不出的时候再说 —— 那是最后兜底，不是主力。

五层识别，从准到兜底
--------------------
1. **手工映射表**   配置里写的，你说了算，最高优先
2. **场景字典**     直播姬 / OBS 的场景文件里，你自己配过的游戏捕获源。
                    它们的格式是 ``<窗口标题>:<窗口类>:<exe名>``，
                    等于一份你自己维护出来的游戏清单，白捡的。
3. **平台库**       Steam appmanifest / WeGame rail_apps 的安装目录 → 游戏名
4. **窗口标题**     清洗后直接当名字用（大部分游戏这层就中了）
5. **exe 版本信息** exe 自带的产品名（《战舰世界》就靠这层）

全是本机读文件 + Win32 调用，不联网、不截图。
"""

from __future__ import annotations

import ctypes
import glob
import json
import os
import re
import time

# --------------------------------------------------------------------------
#  常量
# --------------------------------------------------------------------------

#: 常见游戏引擎的窗口类名。命中就给高分 —— 这比"全屏"可靠得多，
#: 因为浏览器、输入法、桌面也常常是全屏的。
GAME_WINDOW_CLASSES = {
    "unrealwindow",      # 虚幻引擎（三角洲、鸣潮、多数 3A）
    "unitywndclass",     # Unity（原神、VTube Studio、Hearthstone）
    "dagorwclass",       # 战争雷霆
    "valve001",          # Source 引擎
    "glfw30", "glfw",    # GLFW
    "sdl_app",           # SDL
    "cryengine",
    "riotwindowclass",   # 英雄联盟
    "snowstorm",         # 坦克世界
    "wowwindow",
    "arma 3", "arma3",
}

#: 明确不是游戏的窗口类，直接排除，省得误判。
NON_GAME_WINDOW_CLASSES = {
    "shell_traywnd", "progman", "workerw", "sysshadow",
    "applicationframewindow", "windows.ui.core.corewindow",
    "chrome_widgetwin_0", "chrome_widgetwin_1",
    "textinputhost", "tooltips_class32", "notifyiconwindowclass",
}

#: 明确不是游戏的进程名（有窗口类漏网时兜底）。
NON_GAME_PROCESSES = {
    "explorer", "textinputhost", "systemsettings", "applicationframehost",
    "searchhost", "startmenuexperiencehost", "shellexperiencehost",
    "sihost", "taskmgr", "chrome", "msedge", "firefox", "doubao",
    "qq", "wechat", "tim", "dingtalk", "wps", "wpscloudsvr", "wegame",
    "steam", "steamwebhelper", "dsh desktop", "dsh-pet-standalone-webm-chat",
    "python", "pythonw",
}

#: 场景文件里那些"不是游戏名"的源名（"游戏进程 3"、"窗口捕捉 1"），
#: 遇到就改用窗口标题。判断方式是：把所有通用词和数字都抠掉之后，
#: 如果什么都不剩，那它就不是个名字。
_GENERIC_WORDS = ("游戏", "窗口", "捕捉", "采集", "进程", "场景", "直播",
                  "源", "图像", "浏览器", "视频", "画面",
                  "capture", "game", "window", "scene", "source", "screen")

_ZERO_WIDTH = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF], None)
_WRAP_CHARS = "《》「」【】〖〗<>\"'` \u3000\t\r\n"

_GAME_CACHE = {"at": 0.0, "index": None}
_GAME_CACHE_TTL = 300.0


# --------------------------------------------------------------------------
#  文本清洗
# --------------------------------------------------------------------------

def clean_title(text):
    """把窗口标题洗成能直接发出去的游戏名。

    实测踩到的脏数据：
      * ``C\\u200b a\\u200bl\\u200bl`` —— 反作弊/启动器会往标题里插零宽字符，
        不过滤的话群里会收到一串看不见的乱码
      * ``《战舰世界》`` —— 书名号要去掉，不然就是「正在玩《《战舰世界》》」
      * ``FarCry®6``、``Call of Duty™`` —— 商标符号
    """
    if not text:
        return ""
    s = str(text).translate(_ZERO_WIDTH)
    s = s.replace("\u00a0", " ").replace("\u3000", " ")
    s = re.sub(r"[\u00ae\u2122\u00a9]", "", s)          # ® ™ ©
    s = re.sub(r"\s{2,}", " ", s)
    s = s.strip(_WRAP_CHARS)
    return s.strip()


# --------------------------------------------------------------------------
#  Win32：窗口枚举
# --------------------------------------------------------------------------

def _windows_available():
    return os.name == "nt"


if _windows_available():
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    except OSError:                                    # pragma: no cover
        _psapi = None

    _WNDENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class _MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    class _RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _window_title(hwnd):
    n = _user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 2)
    _user32.GetWindowTextW(hwnd, buf, n + 2)
    return buf.value


def _window_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_pid(hwnd):
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_path(pid):
    """取进程的 exe 全路径。

    **反作弊游戏（三角洲、多数网游）这里会失败**：进程受保护，
    非管理员读不到路径。所以它只能当补充信号，不能当主力 ——
    这正是窗口标题那层不可替代的原因。
    """
    if not _windows_available() or not _psapi:
        return None
    handle = _kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        _kernel32.CloseHandle(handle)


def _process_memory(pid):
    """进程工作集大小（字节）。拿不到返回 0。"""
    if not _windows_available() or not _psapi:
        return 0
    handle = _kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0
    try:
        counters = _MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if _psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
        return 0
    finally:
        _kernel32.CloseHandle(handle)


def list_windows():
    """列出所有可见的顶层窗口。

    返回 ``[{hwnd, title, cls, pid, width, height, area}]``。
    只看**顶层且没有 owner** 的可见窗口 —— 游戏主窗口一定在里面，
    而各种隐藏/附属窗口会被滤掉。
    """
    if not _windows_available():
        return []

    found = []

    def _callback(hwnd, _lparam):
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            # GW_OWNER != 0 说明是附属窗口（对话框之类），跳过
            if _user32.GetWindow(hwnd, 4):
                return True
            title = _window_title(hwnd)
            cls = _window_class(hwnd)
            if not title and cls.lower() not in GAME_WINDOW_CLASSES:
                return True
            rect = _RECT()
            if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            w = max(0, rect.right - rect.left)
            h = max(0, rect.bottom - rect.top)
            found.append({
                "hwnd": int(hwnd),
                "title": title,
                "cls": cls,
                "pid": _window_pid(hwnd),
                "width": w,
                "height": h,
                "area": w * h,
            })
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(_WNDENUMPROC(_callback), 0)
    except Exception:
        return []
    return found


def _screen_area():
    if not _windows_available():
        return 0
    try:
        return (_user32.GetSystemMetrics(0) * _user32.GetSystemMetrics(1))
    except Exception:
        return 0


def _foreground_hwnd():
    if not _windows_available():
        return 0
    try:
        return int(_user32.GetForegroundWindow())
    except Exception:
        return 0


# --------------------------------------------------------------------------
#  字典：场景文件 / Steam / WeGame
# --------------------------------------------------------------------------

def _scene_files():
    """找出直播姬和 OBS 的场景文件。"""
    files = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = os.path.join(local, "bililive", "User Data")
        if os.path.isdir(base):
            for uid in os.listdir(base):
                uid_dir = os.path.join(base, uid)
                if not os.path.isdir(uid_dir):
                    continue
                # 直播姬：Scene Collection\*.json，另外还有 untitled*.json
                sc = os.path.join(uid_dir, "Scene Collection")
                if os.path.isdir(sc):
                    files.extend(glob.glob(os.path.join(sc, "*.json")))
                files.extend(glob.glob(os.path.join(uid_dir, "untitled*.json")))
    roaming = os.environ.get("APPDATA")
    if roaming:
        obs = os.path.join(roaming, "obs-studio", "basic", "scenes")
        if os.path.isdir(obs):
            files.extend(glob.glob(os.path.join(obs, "*.json")))
    return files


def _is_generic_source(name):
    """``游戏进程 3`` / ``窗口捕捉 1`` 这种是通用名，不是游戏名。"""
    s = (name or "").strip()
    if not s:
        return True
    for word in _GENERIC_WORDS:
        s = s.replace(word, "")
    s = re.sub(r"[\s\d\-_()（）\[\]]+", "", s)
    return s == ""


def _split_window_spec(spec):
    """``三角洲行动:UnrealWindow:DeltaForceClient-Win64-Shipping.exe``
    -> ``("三角洲行动", "UnrealWindow", "deltaforceclient-win64-shipping.exe")``
    """
    parts = str(spec or "").split(":")
    if len(parts) < 3:
        return None
    exe = parts[-1].strip().lower()
    cls = parts[-2].strip()
    title = ":".join(parts[:-2]).strip()
    if not exe or not exe.endswith(".exe"):
        return None
    return title, cls, exe


def scan_scene_collections():
    """从直播姬 / OBS 的场景文件里读「exe 名 → 游戏名」。

    每个游戏捕获源都记着它绑定的窗口，形如
    ``<窗口标题>:<窗口类>:<exe名>``。源的名字往往就是主播自己起的
    游戏名（"战争雷霆"），比窗口标题里的 "War Thunder" 更合口味。

    只认 ``game_capture``：``window_capture`` 是拿来抓浏览器、剪辑软件
    这类普通窗口的，索引进去会认出一堆不是游戏的东西。
    """
    # exe -> (优先级, 名字)。优先级 2 = 主播自己起的源名，1 = 窗口标题。
    # 谁优先级高谁赢，跟扫描顺序无关 —— 否则"游戏进程 2"会盖掉"《战舰世界》"。
    best = {}
    for path in _scene_files():
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        sources = data.get("sources")
        if not isinstance(sources, list):
            continue
        for src in sources:
            if not isinstance(src, dict):
                continue
            if str(src.get("id", "")) != "game_capture":
                continue
            spec = (src.get("settings") or {}).get("window")
            parsed = _split_window_spec(spec)
            if not parsed:
                continue
            title, _cls, exe = parsed
            source_name = clean_title(src.get("name"))
            if source_name and not _is_generic_source(source_name):
                priority, name = 2, source_name
            else:
                priority, name = 1, clean_title(title)
            if not name:
                continue
            if exe not in best or priority > best[exe][0]:
                best[exe] = (priority, name)
    return {exe: name for exe, (_p, name) in best.items()}


def _vdf_paths(text):
    """从 libraryfolders.vdf 里抠出所有库路径。"""
    return [m.replace("\\\\", "\\") for m in
            re.findall(r'"path"\s+"([^"]+)"', text or "")]


def scan_steam():
    """Steam 库：``appmanifest_*.acf`` 里有官方名和安装目录。"""
    by_exe, by_path = {}, []
    roots = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0]
            if steam:
                roots.append(os.path.normpath(steam))
    except (OSError, ImportError):
        pass

    # libraryfolders.vdf 里还有别的盘
    for root in list(roots):
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        if os.path.isfile(vdf):
            try:
                with open(vdf, "r", encoding="utf-8", errors="replace") as fh:
                    roots.extend(_vdf_paths(fh.read()))
            except OSError:
                pass

    seen = set()
    for root in roots:
        common = os.path.join(root, "steamapps", "common")
        apps = os.path.join(root, "steamapps")
        if not os.path.isdir(apps):
            continue
        for acf in glob.glob(os.path.join(apps, "appmanifest_*.acf")):
            try:
                with open(acf, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            m_name = re.search(r'"name"\s+"([^"]+)"', text)
            m_dir = re.search(r'"installdir"\s+"([^"]+)"', text)
            if not m_name or not m_dir:
                continue
            name = clean_title(m_name.group(1))
            install = os.path.join(common, m_dir.group(1))
            if name and install.lower() not in seen:
                seen.add(install.lower())
                by_path.append((os.path.normcase(install), name))
    return by_exe, by_path


_WEGAME_NOISE = re.compile(
    r"^(common_apps|download|downloading|rail_apps|rail_user_data|cache|"
    r"wegame|wegameinstaller)$", re.I)
_WEGAME_ID = re.compile(r"^(.*?)\s*\(\d+\)$")


def scan_wegame():
    """WeGame：``WeGameApps\\rail_apps\\三角洲(2001918)`` 目录名自带名字。"""
    by_path = []
    drives = []
    for letter in "CDEFGHIJ":
        drives.append("{}:\\".format(letter))
    for drive in drives:
        base = os.path.join(drive, "WeGameApps")
        if not os.path.isdir(base):
            continue
        try:
            children = os.listdir(base)
        except OSError:
            continue
        for child in children:
            if _WEGAME_NOISE.match(child):
                continue
            full = os.path.join(base, child)
            if not os.path.isdir(full):
                continue
            if child.lower() == "rail_apps":
                try:
                    for app in os.listdir(full):
                        m = _WEGAME_ID.match(app)
                        name = clean_title(m.group(1) if m else app)
                        if name:
                            by_path.append((os.path.normcase(os.path.join(full, app)), name))
                except OSError:
                    pass
            else:
                name = clean_title(child)
                if name:
                    by_path.append((os.path.normcase(full), name))
    return by_path


def build_index(force=False):
    """构建「游戏名索引」，五分钟内复用。

    返回 ``{"by_exe": {...}, "by_path": [(normcase(路径), 名字), ...]}``
    """
    now = time.time()
    if (not force and _GAME_CACHE["index"] is not None
            and now - _GAME_CACHE["at"] < _GAME_CACHE_TTL):
        return _GAME_CACHE["index"]

    by_exe = {}
    by_path = []
    for scanner in (scan_scene_collections,):
        try:
            found = scanner()
            if isinstance(found, dict):
                for k, v in found.items():
                    by_exe.setdefault(k, v)
        except Exception:
            pass
    for scanner in (scan_steam,):
        try:
            exe_map, path_map = scanner()
            for k, v in exe_map.items():
                by_exe.setdefault(k, v)
            by_path.extend(path_map)
        except Exception:
            pass
    try:
        by_path.extend(scan_wegame())
    except Exception:
        pass

    # 路径长的优先匹配（更具体的安装目录更可信）
    by_path.sort(key=lambda kv: len(kv[0]), reverse=True)
    index = {"by_exe": by_exe, "by_path": by_path}
    _GAME_CACHE["index"] = index
    _GAME_CACHE["at"] = now
    return index


_RULES_CACHE = {"at": 0.0, "names": {}, "ignore": set()}


def game_rules(cfg):
    """配置里的手工映射表和忽略名单，五分钟内复用。

    ``game.names``  形如 ``{"farcry6.exe": "孤岛惊魂6"}``，最高优先。
    ``game.ignore`` 形如 ``["vtube studio.exe"]`` —— 有些东西长得像游戏
                    但不是（虚拟形象软件、剪辑软件），列进来就永远不报。
    """
    now = time.time()
    if now - _RULES_CACHE["at"] < _GAME_CACHE_TTL:
        return _RULES_CACHE["names"], _RULES_CACHE["ignore"]
    names, ignore = {}, set()
    game_cfg = (cfg or {}).get("game") or {}
    for key, val in (game_cfg.get("names") or {}).items():
        k = clean_title(key).lower()
        v = clean_title(val)
        if k and v:
            names[k] = v
    for item in (game_cfg.get("ignore") or []):
        k = clean_title(item).lower()
        if k:
            ignore.add(k)
    _RULES_CACHE["at"] = now
    _RULES_CACHE["names"] = names
    _RULES_CACHE["ignore"] = ignore
    return names, ignore


# --------------------------------------------------------------------------
#  识别
# --------------------------------------------------------------------------

def _score_window(win, index, names, ignore, screen_area, fg_hwnd):
    """给一个窗口打分。分越高越像"正在玩的游戏"。"""
    cls = (win.get("cls") or "").lower()
    title = clean_title(win.get("title"))
    if cls in NON_GAME_WINDOW_CLASSES:
        return -1, None
    if not title:
        return -1, None

    score = 0
    name = None

    path = _process_path(win["pid"])
    exe = os.path.basename(path).lower() if path else ""
    proc = (exe[:-4] if exe.endswith(".exe") else "") or ""

    if proc in NON_GAME_PROCESSES:
        return -1, None
    if exe and exe in ignore:
        return -1, None

    # 手工映射表最高优先
    if exe and exe in names:
        score += 120
        name = names[exe]
    elif proc and proc in names:
        score += 120
        name = names[proc]

    # 窗口类像游戏 —— 这一条最有用：游戏窗口基本全屏，但全屏的不一定是游戏
    if cls in GAME_WINDOW_CLASSES:
        score += 60

    # 场景字典 / 平台库认识这个 exe
    if exe and exe in index["by_exe"]:
        score += 50
        name = name or index["by_exe"][exe]
    elif path:
        low = os.path.normcase(path)
        for prefix, known in index["by_path"]:
            if low.startswith(prefix):
                score += 50
                name = name or known
                break

    # 全屏
    if screen_area and win["area"] >= screen_area * 0.7:
        score += 25

    # 占内存大（游戏通常吃 1 GB 以上）
    if _process_memory(win["pid"]) > 800 * 1024 * 1024:
        score += 20

    # 前台窗口加分
    if fg_hwnd and win["hwnd"] == fg_hwnd:
        score += 15

    return score, (name or title)


def detect(cfg=None, min_score=55):
    """识别当前在玩什么游戏。

    返回 ``{"name": 游戏名, "source": 判定来源, "window": 窗口信息}``；
    认不出来时 ``name`` 为 None。

    阈值 55 的取舍：窗口类是游戏（60）或者被字典认识（50）+ 全屏（25）
    都能过；而一个全屏的浏览器只有 25 分，会被挡掉。
    """
    if not _windows_available():
        return {"name": None, "source": "unsupported", "window": None}

    try:
        index = build_index()
        names, ignore = game_rules(cfg)
    except Exception:
        index, names, ignore = {"by_exe": {}, "by_path": []}, {}, set()

    screen = _screen_area()
    fg = _foreground_hwnd()

    best = None
    for win in list_windows():
        try:
            score, name = _score_window(win, index, names, ignore, screen, fg)
        except Exception:
            continue
        if score < min_score or not name:
            continue
        if best is None or score > best[0]:
            best = (score, name, win)

    if best is None:
        return {"name": None, "source": "none", "window": None}
    score, name, win = best
    return {
        "name": clean_title(name),
        "source": "window",
        "score": score,
        "window": {
            "title": win["title"],
            "class": win["cls"],
            "pid": win["pid"],
            "size": "{}x{}".format(win["width"], win["height"]),
        },
    }


def describe_index():
    """给自检和界面看的：索引里都有什么。"""
    index = build_index(force=True)
    return {
        "scene_and_platform_exe": dict(sorted(index["by_exe"].items())),
        "install_paths": [{"path": p, "name": n} for p, n in index["by_path"][:40]],
        "scene_files": _scene_files(),
    }

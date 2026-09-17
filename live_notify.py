#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ 群开播自动 @全体成员 通知器  (qq-live-notify)
================================================

监控直播软件进程（OBS / 直播伴侣 / 直播姬 ...），一旦检测到开播，
就通过 NapCat（OneBot v11 HTTP 接口）向配置好的 QQ 群发送 @全体成员 通知。

特点：
  * 纯 Python 标准库实现，不需要 pip install 任何东西
  * 进程监控用 Win32 API，不依赖 psutil
  * 防抖 + 冷却，避免误触发和重复刷屏
  * 支持 at_all（@全体成员）和 at_list（@指定几个人）两种模式
  * 内置本地控制端口，浏览器点一下就能手动触发

用法：
    python live_notify.py watch     常驻监控（开播自动通知）——默认命令
    python live_notify.py send      手动立即发送一次
    python live_notify.py test      彩排：打印将要发送的内容，不真的发
    python live_notify.py check     自检：配置 / NapCat 连通性 / 群权限 / 进程名
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import triggers                # 触发源（OBS 事件 / 全局快捷键 / 冷却闸门）
except ImportError:                # 缺文件时降级为纯进程检测
    triggers = None

APP_NAME = "qq-live-notify"
VERSION = "1.0.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# 默认监控的直播软件进程名（小写比较；支持子串匹配）
#
# 注意：B站直播姬的真实进程名是 livehime.exe，不是 blivehime.exe ——
# 写成后者会导致子串匹配失败、永远检测不到（踩过这个坑）。
DEFAULT_PROCESSES = [
    "obs64.exe",              # OBS Studio 64 位
    "obs32.exe",              # OBS Studio 32 位
    "obs.exe",
    "streamlabs obs.exe",     # Streamlabs Desktop
    "直播伴侣.exe",            # 抖音 / 快手 直播伴侣
    "直播伴侣 launcher.exe",   # 抖音直播伴侣的启动器
    "livehime.exe",           # 哔哩哔哩直播姬（真实进程名）
    "blivehime.exe",          # 哔哩哔哩直播姬（部分旧版本）
    "直播姬.exe",
    "livecompanion.exe",
    "yylive.exe",             # YY 直播
    "huya.exe",               # 虎牙
    "douyulive.exe",
]

# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------

_log_lock = threading.Lock()
_log_file_path = None

# GUI 等外部消费者可以注册接收器，把日志实时拿到界面上显示
_log_sinks = []


def add_log_sink(sink):
    """注册一个日志接收器。sink 接收一行已格式化好的字符串。"""
    if sink not in _log_sinks:
        _log_sinks.append(sink)


def remove_log_sink(sink):
    try:
        _log_sinks.remove(sink)
    except ValueError:
        pass


def _open_log_file():
    global _log_file_path
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        _log_file_path = os.path.join(LOG_DIR, datetime.now().strftime("notify-%Y%m%d.log"))
    except OSError:
        _log_file_path = None


def log(msg, level="INFO"):
    """同时输出到控制台和日志文件。"""
    line = "[{}] [{}] {}".format(datetime.now().strftime("%H:%M:%S"), level, msg)
    with _log_lock:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)
        if _log_file_path:
            try:
                with open(_log_file_path, "a", encoding="utf-8") as fh:
                    fh.write("[{}] {}\n".format(
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), line))
            except OSError:
                pass
    # 在锁外分发，避免接收器内部再调用 log() 造成死锁
    for sink in list(_log_sinks):
        try:
            sink(line)
        except Exception:
            pass


if triggers is not None:
    # 让触发源模块的日志也走同一套输出（控制台 + 文件 + GUI 日志面板）
    triggers.set_logger(log)


def _setup_console():
    """Windows 控制台切到 UTF-8，保证中文和 emoji 正常显示。

    用 pythonw.exe（无控制台窗口）启动时，sys.stdout / sys.stderr 是 None，
    这里补一个黑洞流，否则后续所有 print() 都会抛异常。
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

class ConfigError(Exception):
    pass


def load_config(path):
    if not os.path.isfile(path):
        raise ConfigError(
            "找不到配置文件：{}\n"
            "请把 config.json 放在程序同目录，或用 --config 指定路径。".format(path)
        )
    try:
        # 用 utf-8-sig 读取：容忍记事本等编辑器写入的 BOM 头。
        # 否则配置文件第一个字符会变成 \ufeff，被当成 JSON 语法错误。
        with open(path, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "配置文件 JSON 格式错误：{}\n第 {} 行第 {} 列。\n"
            "常见原因：多了个逗号、用了中文引号 、或者写了 // 注释（JSON 不支持注释）。".format(
                path, exc.lineno, exc.colno
            )
        )
    except OSError as exc:
        raise ConfigError("读取配置文件失败：{}".format(exc))

    if not isinstance(raw, dict):
        raise ConfigError("配置文件最外层必须是一个 JSON 对象 {}。")

    # --- onebot ---
    onebot = raw.get("onebot") or {}
    if not isinstance(onebot, dict):
        raise ConfigError('onebot 必须是一个对象，例如 {"base_url": "http://127.0.0.1:3000"}')
    base_url = str(onebot.get("base_url") or "http://127.0.0.1:3000").rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError("onebot.base_url 必须以 http:// 或 https:// 开头，当前为：{}".format(base_url))

    # --- groups ---
    groups_raw = raw.get("groups")
    if not isinstance(groups_raw, list) or not groups_raw:
        raise ConfigError(
            "groups 必须是一个非空数组，每一项形如：\n"
            '  {"group_id": 123456789, "enabled": true, "at_all": true, "note": "主群"}'
        )
    groups = []
    for idx, item in enumerate(groups_raw):
        if not isinstance(item, dict):
            raise ConfigError("groups[{}] 必须是一个对象。".format(idx))
        gid = item.get("group_id")
        try:
            gid = int(gid)
        except (TypeError, ValueError):
            raise ConfigError(
                "groups[{}].group_id 必须是纯数字群号，当前为：{!r}\n"
                "群号在 QQ 群设置里能看到，不要填群名。".format(idx, gid)
            )
        at_list = item.get("at_list") or []
        if not isinstance(at_list, list):
            raise ConfigError("groups[{}].at_list 必须是数组，例如 [123456, 789012]".format(idx))
        clean_at = []
        for q in at_list:
            try:
                clean_at.append(int(q))
            except (TypeError, ValueError):
                raise ConfigError("groups[{}].at_list 里有非数字 QQ 号：{!r}".format(idx, q))
        groups.append({
            "group_id": gid,
            "enabled": bool(item.get("enabled", True)),
            "at_all": bool(item.get("at_all", True)),
            "at_list": clean_at,
            "note": str(item.get("note") or ""),
        })

    # --- watch ---
    watch = raw.get("watch") or {}
    if not isinstance(watch, dict):
        raise ConfigError("watch 必须是一个对象。")
    procs = watch.get("processes") or DEFAULT_PROCESSES
    if not isinstance(procs, list) or not procs:
        raise ConfigError('watch.processes 必须是非空数组，例如 ["obs64.exe", "直播伴侣.exe"]')

    def _num(key, default, lo, hi):
        val = watch.get(key, default)
        try:
            val = type(default)(val)
        except (TypeError, ValueError):
            raise ConfigError("watch.{} 必须是数字，当前为：{!r}".format(key, watch.get(key)))
        if not (lo <= val <= hi):
            raise ConfigError("watch.{} 取值应在 {} ~ {} 之间，当前为：{}".format(key, lo, hi, val))
        return val

    watch_cfg = {
        "enabled": bool(watch.get("enabled", True)),
        "interval_seconds": _num("interval_seconds", 5, 1, 3600),
        "processes": [str(p).lower() for p in procs],
        "confirm_checks": _num("confirm_checks", 2, 1, 100),
        "stop_grace_seconds": _num("stop_grace_seconds", 60, 0, 86400),
    }

    # --- message ---
    message = raw.get("message") or {}
    if not isinstance(message, dict):
        raise ConfigError("message 必须是一个对象。")
    msg_cfg = {
        "template": str(message.get("template") or "我开播啦！大家快来～"),
        "title": str(message.get("title") or ""),
        "link": str(message.get("link") or ""),
    }

    # --- behavior / control ---
    behavior = raw.get("behavior") or {}
    if not isinstance(behavior, dict):
        raise ConfigError("behavior 必须是一个对象。")
    control = raw.get("control") or {}
    if not isinstance(control, dict):
        raise ConfigError("control 必须是一个对象。")

    behavior_cfg = {
        "cooldown_minutes": float(behavior.get("cooldown_minutes", 30) or 0),
        "send_interval_seconds": float(behavior.get("send_interval_seconds", 3) or 0),
        "dry_run": bool(behavior.get("dry_run", False)),
    }
    try:
        control_port = int(control.get("port", 8899) or 8899)
    except (TypeError, ValueError):
        raise ConfigError("control.port 必须是数字端口，当前为：{!r}".format(control.get("port")))
    control_cfg = {
        "enabled": bool(control.get("enabled", True)),
        "port": control_port,
        "token": str(control.get("token") or ""),
    }

    # --- trigger：什么才算"开播了" ---
    trigger = raw.get("trigger") or {}
    if not isinstance(trigger, dict):
        raise ConfigError("trigger 必须是一个对象。")
    trigger_cfg = {
        # 进程检测的硬伤：软件一打开就触发，而人往往还要调设备、试麦。
        # 所以默认关闭，改用更精确的信号。
        "on_process_start": bool(trigger.get("on_process_start", False)),
        "on_obs_stream": bool(trigger.get("on_obs_stream", True)),
        "hotkey": str(trigger.get("hotkey") or "").strip().lower(),
    }

    return {
        "onebot": {
            "base_url": base_url,
            "access_token": str(onebot.get("access_token") or ""),
            "timeout": float(onebot.get("timeout", 10) or 10),
        },
        "groups": groups,
        "watch": watch_cfg,
        "message": msg_cfg,
        "behavior": behavior_cfg,
        "control": control_cfg,
        "trigger": trigger_cfg,
        "_path": os.path.abspath(path),
    }


# --------------------------------------------------------------------------
# 进程枚举
# --------------------------------------------------------------------------

class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


TH32CS_SNAPPROCESS = 0x00000002


def _list_processes_win32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snap or snap == invalid:
        raise OSError("CreateToolhelp32Snapshot 失败（err={}）".format(ctypes.get_last_error()))

    names = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            names.append(entry.szExeFile)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return names


def _list_processes_tasklist():
    """备用方案：解析 tasklist 输出。"""
    out = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="replace", timeout=20,
    ).stdout
    names = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        name = line.split('","')[0].strip('"')
        if name:
            names.append(name)
    return names


def _list_processes_posix():
    out = subprocess.run(["ps", "-Ao", "comm="], capture_output=True, text=True,
                         errors="replace", timeout=20).stdout
    return [os.path.basename(x.strip()) for x in out.splitlines() if x.strip()]


def list_processes():
    """返回当前所有进程名列表。失败时抛出异常，由调用方处理。"""
    if os.name == "nt":
        try:
            return _list_processes_win32()
        except Exception as exc:
            log("Win32 进程枚举失败（{}），改用 tasklist。".format(exc), "WARN")
            return _list_processes_tasklist()
    return _list_processes_posix()


def match_processes(process_names, watch_list):
    """返回被命中的进程名集合（不区分大小写，子串匹配）。"""
    hits = set()
    for proc in process_names:
        low = proc.lower()
        for want in watch_list:
            if want and (want == low or want in low):
                hits.add(proc)
                break
    return hits


# --------------------------------------------------------------------------
# OneBot 客户端
# --------------------------------------------------------------------------

class OneBot:
    def __init__(self, cfg):
        self.base_url = cfg["base_url"]
        self.token = cfg["access_token"]
        self.timeout = cfg["timeout"]
        # 关键：显式禁用代理。
        # urllib 默认会读取 Windows 注册表里的系统代理（Clash/v2ray 等），
        # 把发往 127.0.0.1 的请求也塞给代理，结果是代理返回 502，
        # 表现为"连不上 NapCat"却看不出原因。NapCat 永远在本机，直连即可。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, action, payload=None):
        """调用一个 OneBot 动作。返回 (ok, data_or_errmsg)。"""
        url = "{}/{}".format(self.base_url, action)
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            return False, "HTTP {} {} {}".format(exc.code, exc.reason, detail)
        except urllib.error.URLError as exc:
            return False, (
                "连不上 NapCat（{}）。请确认：\n"
                "  1) NapCat 正在运行并且已经登录 QQ\n"
                "  2) NapCat 网络配置里已开启 HTTP 服务器，端口与 config.json 的 base_url 一致\n"
                "  3) 若设置了 access_token，config.json 里也要填一样的值".format(exc.reason)
            )
        except Exception as exc:
            return False, "请求异常：{}".format(exc)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return False, "NapCat 返回的不是 JSON：{}".format(text[:300])

        status = data.get("status")
        retcode = data.get("retcode")
        if status == "ok" or retcode == 0:
            return True, data.get("data")
        msg = data.get("message") or data.get("wording") or data.get("msg") or text[:300]
        return False, "NapCat 返回失败（retcode={}）：{}".format(retcode, msg)

    # -- 常用动作 --
    def get_login_info(self):
        return self.call("get_login_info")

    def get_group_list(self):
        return self.call("get_group_list")

    def get_group_member_info(self, group_id, user_id):
        return self.call("get_group_member_info", {"group_id": group_id, "user_id": user_id})

    def get_group_member_list(self, group_id):
        return self.call("get_group_member_list", {"group_id": group_id})

    def send_group_msg(self, group_id, message):
        return self.call("send_group_msg", {"group_id": group_id, "message": message})


def resolve_role(onebot, group_id, self_id):
    """查询机器人在指定群里的身份（owner / admin / member）。

    为什么要这么绕：NapCat 刚登录、群成员缓存还没加载完的时候，
    get_group_member_info 会返回一个未初始化的默认值 "member"，
    让人误以为没有管理员权限。本程序就曾被这个假结果坑过，
    自动把新加群的 @全体成员 关掉了。

    策略：先问轻量的 get_group_member_info；只有当答案是 "member"
    （不利结果）时，才用 get_group_member_list 全量列表复核一次，
    那份数据才是权威的。有利结果直接采信，不浪费一次全量请求。
    """
    role = None
    ok, data = onebot.get_group_member_info(group_id, self_id)
    if ok and isinstance(data, dict):
        role = data.get("role")

    if role in ("owner", "admin"):
        return role

    ok2, members = onebot.get_group_member_list(group_id)
    if ok2 and isinstance(members, list):
        for m in members:
            if str(m.get("user_id")) == str(self_id):
                return m.get("role") or role or "member"

    return role or "member"


# --------------------------------------------------------------------------
# 消息构造
# --------------------------------------------------------------------------

def render_text(cfg):
    fields = {
        "title": cfg["message"]["title"],
        "link": cfg["message"]["link"],
        "time": datetime.now().strftime("%H:%M"),
        "date": datetime.now().strftime("%Y-%m-%d"),
    }
    template = cfg["message"]["template"]
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError):
        log("消息模板占位符有问题，已按原文发送。可用占位符：{}".format(
            "、".join("{" + k + "}" for k in fields)), "WARN")
        text = template
        for key, val in fields.items():
            text = text.replace("{" + key + "}", val)
        return text


def build_message(group, text):
    """按群配置生成 OneBot 消息段数组。

    每个 at 段后面都跟一个空格段：QQ 不会自动在 @ 后面补空格，
    不加的话会渲染成 "@全体成员我开播啦" 这种黏在一起的样子。
    """
    segments = []
    if group["at_all"]:
        segments.append({"type": "at", "data": {"qq": "all"}})
        segments.append({"type": "text", "data": {"text": " "}})
    elif group["at_list"]:
        for qq in group["at_list"]:
            segments.append({"type": "at", "data": {"qq": str(qq)}})
            segments.append({"type": "text", "data": {"text": " "}})
    segments.append({"type": "text", "data": {"text": text}})
    return segments


def describe_message(group, segments):
    """把消息段转成人类可读的预览文本。"""
    parts = []
    for seg in segments:
        if seg["type"] == "at":
            qq = str(seg["data"].get("qq"))
            parts.append("@全体成员" if qq == "all" else "@" + qq)
        elif seg["type"] == "text":
            parts.append(seg["data"].get("text", ""))
    return "".join(parts)


# --------------------------------------------------------------------------
# 发送
# --------------------------------------------------------------------------

def send_to_groups(cfg, onebot, reason, force_dry=False):
    """向所有启用的群发送通知。返回成功数。"""
    dry = cfg["behavior"]["dry_run"] or force_dry
    text = render_text(cfg)
    active = [g for g in cfg["groups"] if g["enabled"]]
    if not active:
        log("没有任何启用的群（enabled 全是 false），什么都没发。", "WARN")
        return 0

    log("触发原因：{}".format(reason))
    log("目标：{} 个启用的群（配置里共 {} 个）{}".format(
        len(active), len(cfg["groups"]), "  —— 彩排模式，不真的发" if dry else ""))

    ok_count = 0
    for idx, group in enumerate(active):
        segments = build_message(group, text)
        preview = describe_message(group, segments)
        label = "{}{}".format(group["group_id"],
                              "（{}）".format(group["note"]) if group["note"] else "")

        if dry:
            log("  [彩排] {} → {}".format(label, preview.replace("\n", " / ")))
            ok_count += 1
            continue

        ok, data = onebot.send_group_msg(group["group_id"], segments)
        if ok:
            mid = ""
            if isinstance(data, dict) and data.get("message_id") is not None:
                mid = " (message_id={})".format(data["message_id"])
            log("  [成功] {} -> 已发送{}".format(label, mid))
            ok_count += 1
        else:
            log("  [失败] {} -> 发送失败\n     {}".format(label, data), "ERROR")

        if idx < len(active) - 1 and cfg["behavior"]["send_interval_seconds"] > 0:
            time.sleep(cfg["behavior"]["send_interval_seconds"])

    log("完成：成功 {}/{}".format(ok_count, len(active)))
    return ok_count


# --------------------------------------------------------------------------
# 触发引擎（防抖 + 冷却 + 重新武装）
# --------------------------------------------------------------------------

class TriggerEngine:
    """进程检测触发源。

    注意：冷却不在这里处理，统一交给外层 CooldownGate —— 因为现在有多个
    触发源（OBS 事件、快捷键、进程检测）共用一次开播，必须一起限流，
    否则你在 OBS 里点开播的同时又按了快捷键，就会发两遍。
    """

    def __init__(self, cfg, fire_callback):
        self.cfg = cfg
        self.fire_callback = fire_callback
        self.watch = cfg["watch"]
        self.armed = True            # 是否可以再次触发
        self.seen_count = 0          # 连续命中次数（防抖）
        self.last_seen_ts = 0.0      # 最近一次看到进程的时间
        self.last_fire_ts = 0.0      # 最近一次触发的时间

    def tick(self):
        """执行一次检查。返回本次命中的进程集合。"""
        try:
            procs = list_processes()
        except Exception as exc:
            log("枚举进程失败：{}".format(exc), "ERROR")
            return set()

        hits = match_processes(procs, self.watch["processes"])
        now = time.time()

        if hits:
            self.last_seen_ts = now
            if not self.armed:
                return hits
            self.seen_count += 1
            if self.seen_count < self.watch["confirm_checks"]:
                return hits

            # 防抖通过 -> 本次开播只尝试触发一次
            self.armed = False
            self.seen_count = 0
            self.last_fire_ts = now
            try:
                self.fire_callback("检测到直播软件启动：{}".format("、".join(sorted(hits))))
            except Exception as exc:
                log("发送过程出错：{}".format(exc), "ERROR")
        else:
            self.seen_count = 0
            if not self.armed and (now - self.last_seen_ts) >= self.watch["stop_grace_seconds"]:
                self.armed = True
                log("直播软件已退出，重新武装，等待下次开播。")
        return hits


# --------------------------------------------------------------------------
# 本地控制端口
# --------------------------------------------------------------------------

_CONTROL_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{app}</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;background:#14161a;color:#e8eaed;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}}
.card{{background:#1e2126;padding:32px 36px;border-radius:16px;box-shadow:0 8px 40px #0008;
text-align:center;max-width:560px}}
h1{{font-size:20px;margin:0 0 6px}}small{{color:#9aa0a6;font-weight:400}}
p{{color:#9aa0a6;font-size:13px;margin:6px 0}}
button{{margin-top:20px;padding:14px 28px;font-size:16px;border:0;border-radius:10px;cursor:pointer;
background:#e5484d;color:#fff;font-weight:600}}
button:hover{{background:#f2555a}}
pre{{text-align:left;background:#0f1115;padding:12px;border-radius:8px;
font-size:12px;overflow:auto;max-height:240px;color:#9aa0a6}}
</style></head><body><div class="card">
<h1>{app} <small>v{version}</small></h1>
<p>开播自动通知正在后台监控中</p>
<button onclick="fetch('/trigger').then(r=>r.json()).then(d=>document.getElementById('o').textContent=JSON.stringify(d,null,2))">
立即发送通知</button>
<pre id="o">点上面的按钮手动触发一次（等同于检测到开播）</pre>
</div></body></html>"""


def start_control_server(cfg, on_trigger, get_status):
    """在 127.0.0.1 上开一个极简控制端口。仅本机可访问。"""
    port = cfg["control"]["port"]
    token = cfg["control"]["token"]

    class Handler(BaseHTTPRequestHandler):
        server_version = APP_NAME + "/" + VERSION

        def log_message(self, fmt, *args):
            pass  # 静音默认访问日志

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass

        def _authorized(self):
            if not token:
                return True
            if "?" not in self.path:
                return False
            from urllib.parse import unquote
            for pair in self.path.split("?", 1)[1].split("&"):
                if pair.startswith("token=") and unquote(pair[6:]) == token:
                    return True
            return False

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"

            if path == "/":
                return self._send(200, _CONTROL_PAGE.format(app=APP_NAME, version=VERSION),
                                  "text/html; charset=utf-8")

            if path in ("/status", "/health"):
                payload = {"ok": True, "app": APP_NAME, "version": VERSION}
                payload.update(get_status())
                return self._send(200, json.dumps(payload, ensure_ascii=False, indent=2))

            if path == "/trigger":
                if not self._authorized():
                    return self._send(403, json.dumps(
                        {"ok": False, "error": "token 不正确"}, ensure_ascii=False))
                try:
                    return self._send(200, json.dumps(on_trigger(), ensure_ascii=False, indent=2))
                except Exception as exc:
                    return self._send(500, json.dumps(
                        {"ok": False, "error": str(exc)}, ensure_ascii=False))

            return self._send(404, json.dumps({"ok": False, "error": "未知路径"}, ensure_ascii=False))

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        log("控制端口 {} 启动失败（{}）。不影响自动通知，继续运行。".format(port, exc), "WARN")
        return None

    threading.Thread(target=httpd.serve_forever, name="control-server", daemon=True).start()
    log("控制端口已开启：http://127.0.0.1:{}/   （浏览器打开可手动触发）".format(port))
    if token:
        log("已设 token，手动触发地址：http://127.0.0.1:{}/trigger?token={}".format(port, token))
    return httpd


# --------------------------------------------------------------------------
# 命令实现
# --------------------------------------------------------------------------

def cmd_watch(cfg, stop_event=None):
    onebot = OneBot(cfg["onebot"])

    ok, data = onebot.get_login_info()
    if ok and isinstance(data, dict):
        log("NapCat 已连接，登录账号：{}（{}）".format(data.get("nickname"), data.get("user_id")))
    else:
        log("警告：现在连不上 NapCat —— {}".format(data), "WARN")
        log("程序会继续运行，但开播时可能发不出去。请检查 NapCat 是否已启动并登录。", "WARN")

    tg = cfg.get("trigger") or {}
    gate = triggers.CooldownGate(cfg["behavior"]["cooldown_minutes"]) if triggers else None
    state = {"last_fire": 0.0}
    obs = None
    hotkey = None

    def fire(reason):
        """所有触发源的统一出口：先过冷却闸门，再真正发送。

        多个源（OBS 事件 / 快捷键 / 进程检测）共用一个冷却，
        这样同一次开播不会被发两遍。
        """
        if gate is not None:
            allowed, remain = gate.allow()
            if not allowed:
                log("触发（{}），但还在冷却期，还剩约 {} 秒，跳过本次。".format(reason, remain), "WARN")
                return False
            gate.mark()
        state["last_fire"] = time.time()
        send_to_groups(cfg, onebot, reason)
        return True

    engine = TriggerEngine(cfg, fire) if tg.get("on_process_start") else None

    def manual_trigger():
        fire("手动触发（控制端口）")
        return {"ok": True, "message": "已触发一次发送，详见控制台日志"}

    def status():
        return {
            "armed": engine.armed if engine else None,
            "trigger_sources": {
                "process_start": bool(engine),
                "obs_stream": bool(tg.get("on_obs_stream")),
                "hotkey": tg.get("hotkey") or None,
            },
            "obs": obs.status_text() if obs else None,
            "hotkey_ok": hotkey.registered if hotkey else None,
            "groups": [g["group_id"] for g in cfg["groups"] if g["enabled"]],
            "last_fire": (datetime.fromtimestamp(state["last_fire"]).strftime("%Y-%m-%d %H:%M:%S")
                          if state["last_fire"] else None),
        }

    httpd = None
    if cfg["control"]["enabled"]:
        httpd = start_control_server(cfg, manual_trigger, status)

    # ---------------- 启动各个触发源 ----------------
    if triggers is not None and tg.get("on_obs_stream"):
        obs = triggers.ObsWatcher(fire, stop_event,
                                  on_status=lambda t: log("OBS：{}".format(t)))
        obs.start()
    if triggers is not None and tg.get("hotkey"):
        hotkey = triggers.HotkeyListener(tg["hotkey"], fire, stop_event,
                                         on_status=lambda t: log("快捷键：{}".format(t)))
        hotkey.start()

    log("=" * 62)
    log("开始监控。按 Ctrl+C 退出。")
    log("目标群数：{}".format(len([g for g in cfg["groups"] if g["enabled"]])))
    log("触发方式：")
    if obs is not None:
        log("  · OBS 推流事件 —— 精确到「你按下开始推流」那一刻")
    if hotkey is not None:
        log("  · 全局快捷键 {}".format(tg["hotkey"].upper()))
    if engine is not None:
        log("  · 进程检测：{}".format("、".join(cfg["watch"]["processes"])))
        log("    （检查间隔 {} 秒，防抖 {} 次）".format(
            cfg["watch"]["interval_seconds"], cfg["watch"]["confirm_checks"]))
    if obs is None and hotkey is None and engine is None:
        log("  · 无 —— 只能通过控制端口手动触发", "WARN")
    log("冷却 {} 分钟".format(cfg["behavior"]["cooldown_minutes"]))
    if cfg["behavior"]["dry_run"]:
        log("当前是彩排模式（dry_run=true），不会真的发消息。", "WARN")
    log("=" * 62)

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                log("监控已停止。")
                break
            if engine is not None:
                engine.tick()
            # 分片 sleep：让"停止监控"能立刻生效，而不是干等满一个间隔
            interval = cfg["watch"]["interval_seconds"] if engine is not None else 1
            deadline = time.time() + interval
            while time.time() < deadline:
                if stop_event is not None and stop_event.is_set():
                    break
                time.sleep(0.2)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出。")
    finally:
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
    return 0


def cmd_send(cfg):
    send_to_groups(cfg, OneBot(cfg["onebot"]), "手动执行 send 命令")
    return 0


def cmd_test(cfg):
    log("彩排模式：下面展示将要发送的内容，不会真的发到群里。")
    send_to_groups(cfg, OneBot(cfg["onebot"]), "彩排（test 命令）", force_dry=True)
    log("内容没问题的话，执行  python live_notify.py send  真正发送一次。")
    return 0


def cmd_check(cfg):
    problems = []

    log("=" * 62)
    log("{} v{} 自检".format(APP_NAME, VERSION))
    log("=" * 62)
    log("Python：{}".format(sys.version.split()[0]))
    log("配置文件：{}".format(cfg["_path"]))
    log("NapCat 地址：{}".format(cfg["onebot"]["base_url"]))

    # 1. 群配置
    log("\n[1/4] 群配置")
    enabled = [g for g in cfg["groups"] if g["enabled"]]
    for g in cfg["groups"]:
        mode = "@全体成员" if g["at_all"] else (
            "@{}".format("、".join(str(x) for x in g["at_list"])) if g["at_list"] else "不@任何人")
        log("  {} [{}] {}{}".format(g["group_id"], "启用" if g["enabled"] else "已禁用", mode,
                                    "（{}）".format(g["note"]) if g["note"] else ""))
    if not enabled:
        problems.append("没有任何启用的群，不会发送任何消息。")

    # 2. 进程监控
    log("\n[2/4] 进程监控")
    try:
        procs = list_processes()
        hits = match_processes(procs, cfg["watch"]["processes"])
        log("  成功枚举到 {} 个进程。".format(len(procs)))
        if hits:
            log("  当前正在运行的目标进程：{}".format("、".join(sorted(hits))))
            log("  如果现在并没有在直播，请把误报的进程名从 watch.processes 里删掉。")
        else:
            log("  当前没有检测到目标进程（正常，说明现在没开播）。")
    except Exception as exc:
        problems.append("进程枚举失败：{}".format(exc))
        log("  进程枚举失败：{}".format(exc), "ERROR")

    # 3. NapCat 连通性
    log("\n[3/4] NapCat 连通性")
    onebot = OneBot(cfg["onebot"])
    ok, data = onebot.get_login_info()
    self_id = None
    if ok and isinstance(data, dict):
        self_id = data.get("user_id")
        log("  [OK] 连接成功，登录账号：{}（{}）".format(data.get("nickname"), self_id))
    else:
        problems.append("连不上 NapCat。")
        log("  [X] {}".format(data), "ERROR")

    # 4. 群权限（决定 @全体成员 能不能生效）
    log("\n[4/4] 群权限检查（@全体成员 只有群主/管理员才有效）")
    if ok:
        gok, glist = onebot.get_group_list()
        if not gok or not isinstance(glist, list):
            log("  无法获取群列表：{}".format(glist), "WARN")
        else:
            by_id = {}
            for item in glist:
                try:
                    by_id[int(item.get("group_id"))] = item
                except (TypeError, ValueError):
                    continue
            for g in cfg["groups"]:
                if not g["enabled"]:
                    continue
                info = by_id.get(g["group_id"])
                if not info:
                    problems.append("机器人不在群 {} 里。".format(g["group_id"]))
                    log("  [X] {} 机器人不在这个群里".format(g["group_id"]))
                    continue
                role = "member"
                if self_id is not None:
                    role = resolve_role(onebot, g["group_id"], self_id)
                name = info.get("group_name") or ""
                if g["at_all"] and role == "member":
                    problems.append(
                        "群 {} 里机器人只是普通成员，@全体成员 不会生效。".format(g["group_id"]))
                    log("  [X] {} {} —— 身份是【普通成员】，@全体成员 发出去也提醒不到全员。".format(
                        g["group_id"], name))
                    log("      解决办法：把机器人设成管理员/群主，或把该群 at_all 改成 false "
                        "并用 at_list 指定要@的人。")
                elif g["at_all"]:
                    log("  [OK] {} {} —— 身份【{}】，@全体成员 可用。".format(
                        g["group_id"], name, "群主" if role == "owner" else "管理员"))
                else:
                    log("  [-] {} {} —— 不使用 @全体成员，跳过检查。".format(g["group_id"], name))
    else:
        log("  跳过（NapCat 没连上）。", "WARN")

    # 汇总
    log("\n" + "=" * 62)
    if problems:
        log("发现 {} 个问题：".format(len(problems)), "WARN")
        for i, p in enumerate(problems, 1):
            log("  {}. {}".format(i, p), "WARN")
        log("修好后再跑一次 check。")
    else:
        log("全部检查通过，可以执行  python live_notify.py watch  开始监控了。")
    log("=" * 62)
    return 1 if problems else 0


def cmd_groups(cfg):
    """列出机器人所在的所有群，标明身份与能否 @全体成员，并生成可直接粘贴的配置。"""
    onebot = OneBot(cfg["onebot"])

    ok, me = onebot.get_login_info()
    if not ok or not isinstance(me, dict):
        log("连不上 NapCat：{}".format(me), "ERROR")
        log("请先双击 0-一键启动.bat 把 NapCat 启动起来，再执行本命令。", "ERROR")
        return 2
    self_id = me.get("user_id")
    log("当前机器人：{}（{}）".format(me.get("nickname"), self_id))

    gok, groups = onebot.get_group_list()
    if not gok or not isinstance(groups, list):
        log("获取群列表失败：{}".format(groups), "ERROR")
        return 2

    configured = {g["group_id"] for g in cfg["groups"]}
    role_cn = {"owner": "群主", "admin": "管理员", "member": "普通成员"}

    rows = []
    for item in groups:
        try:
            gid = int(item.get("group_id"))
        except (TypeError, ValueError):
            continue
        role = resolve_role(onebot, gid, self_id)
        rows.append({
            "group_id": gid,
            "group_name": item.get("group_name") or "",
            "member_count": int(item.get("member_count") or 0),
            "role": role,
        })

    # 有权限 @全体成员的排前面，同组内按人数从多到少
    rows.sort(key=lambda r: (r["role"] == "member", -r["member_count"]))

    log("")
    log("机器人一共在 {} 个群里：".format(len(rows)))
    log("")
    log("  {:<12} {:<8} {:<26} {}".format("群号", "身份", "群名", "能否@全体"))
    log("  " + "-" * 66)
    for r in rows:
        can = "可以" if r["role"] in ("owner", "admin") else "不行"
        mark = "  <= 已配置" if r["group_id"] in configured else ""
        name = r["group_name"]
        if len(name) > 24:
            name = name[:23] + "…"
        log("  {:<12} {:<8} {:<26} {}{}".format(
            r["group_id"], role_cn.get(r["role"], r["role"]), name, can, mark))

    log("")
    log("  说明：身份是【普通成员】的群，@全体成员 发出去也不会真正提醒到人。")

    pending = [r for r in rows if r["group_id"] not in configured]
    if not pending:
        log("")
        log("所有群都已经配置过了，不需要再加。")
        return 0

    log("")
    log("=" * 66)
    log("要添加群：把下面这段贴进 config.json 的 groups 数组里（注意逗号）")
    log("=" * 66)
    blocks = []
    for r in pending:
        use_all = r["role"] in ("owner", "admin")
        blocks.append(
            '    {{\n'
            '      "group_id": {gid},\n'
            '      "enabled": true,\n'
            '      "at_all": {at_all},\n'
            '      "at_list": [],\n'
            '      "note": "{name}"\n'
            '    }}'.format(gid=r["group_id"],
                           at_all="true" if use_all else "false",
                           name=r["group_name"].replace('"', "'").replace("\\", "/"))
        )
    # 用 print 而不是 log，避免每行被加上时间戳，方便直接复制
    print(",\n".join("  " + b for b in blocks), flush=True)
    log("")
    log("  已自动按各群身份填好 at_all（管理员/群主=true，普通成员=false）。")
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def main(argv=None):
    _setup_console()
    _open_log_file()

    parser = argparse.ArgumentParser(
        prog="live_notify.py",
        description="QQ 群开播自动 @全体成员 通知器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python live_notify.py check    先自检\n"
               "  python live_notify.py test     彩排看效果\n"
               "  python live_notify.py watch    正式开跑\n",
    )
    parser.add_argument("command", nargs="?", default="watch",
                        choices=["watch", "send", "test", "check", "groups"],
                        help="watch=常驻监控(默认)  send=立即发送  test=彩排  "
                             "check=自检  groups=列出所有群并生成配置")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="配置文件路径（默认同目录 config.json）")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        log(str(exc), "ERROR")
        return 2

    handlers = {"watch": cmd_watch, "send": cmd_send, "test": cmd_test,
                "check": cmd_check, "groups": cmd_groups}
    try:
        return handlers[args.command](cfg)
    except KeyboardInterrupt:
        log("已退出。")
        return 0


if __name__ == "__main__":
    sys.exit(main())

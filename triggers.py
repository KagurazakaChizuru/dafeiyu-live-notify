#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
触发源模块
==========

负责回答一个问题：**「用户到底开播了没有？」**

进程检测只能知道"直播软件被打开了"，而人在开播前通常要调设备、试麦、
试妆，这段时间不该打扰群友。所以这里提供三种更精确的触发方式：

  1. ObsWatcher     —— 连上 OBS 的 obs-websocket，精确捕获"开始推流"事件。
                       只有 OBS 支持；B站直播姬 / 抖音直播伴侣没开放接口。
  2. HotkeyListener —— 全局快捷键（Win32 RegisterHotKey）。任何软件下都有效，
                       开播时按一下就推送。
  3. CooldownGate   —— 冷却闸门，避免多种触发源叠加导致重复发。

本模块不依赖任何第三方库，也不 import live_notify（避免循环依赖），
日志通过 set_logger() 注入。
"""

import base64
import ctypes
import hashlib
import json
import os
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

try:
    from ctypes import wintypes
except ImportError:                                  # 非 Windows
    wintypes = None


# --------------------------------------------------------------------------
#  日志注入
# --------------------------------------------------------------------------

_log = lambda msg, level="INFO": None              # noqa: E731


def set_logger(fn):
    global _log
    _log = fn


# --------------------------------------------------------------------------
#  冷却闸门
# --------------------------------------------------------------------------

class CooldownGate:
    """多种触发源共用一个冷却，避免同一次开播被发两遍。"""

    def __init__(self, minutes):
        self.seconds = float(minutes or 0) * 60.0
        self.last = 0.0
        self._lock = threading.Lock()

    def allow(self):
        """返回 (是否放行, 剩余秒数)。"""
        with self._lock:
            if not self.seconds:
                return True, 0
            remain = self.seconds - (time.time() - self.last)
            if remain > 0:
                return False, int(remain)
            return True, 0

    def mark(self):
        with self._lock:
            self.last = time.time()

    def last_fire_time(self):
        return self.last


# --------------------------------------------------------------------------
#  极简 WebSocket 客户端（只支持文本帧，够连 obs-websocket 用）
# --------------------------------------------------------------------------

class WsIdle(Exception):
    """读超时（没有新消息）。"""


class WsClosed(Exception):
    """对端关闭。"""


class WsClient:
    def __init__(self, host, port, path="/", timeout=10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET {} HTTP/1.1\r\n"
               "Host: {}:{}\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               "Sec-WebSocket-Key: {}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n").format(path, host, port, key)
        self.sock.sendall(req.encode("ascii"))

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WsClosed("WebSocket 握手时连接被关闭")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
        if " 101 " not in status:
            raise WsClosed("WebSocket 握手失败：{}".format(status))
        self._buf = bytearray(rest)

    def _read_exact(self, n):
        while len(self._buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise WsIdle()
            if not chunk:
                raise WsClosed("连接已关闭")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _send_frame(self, opcode, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        head = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        self.sock.sendall(bytes(head) + bytes(data[i] ^ mask[i % 4] for i in range(n)))

    def send_text(self, text):
        self._send_frame(0x1, text)

    def recv_text(self):
        """读一条文本消息。超时抛 WsIdle，关闭抛 WsClosed。"""
        while True:
            b1, b2 = self._read_exact(2)
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(length) if length else b""
            if mask:
                payload = bytes(payload[i] ^ mask[i % 4] for i in range(length))
            if opcode == 0x8:
                raise WsClosed("对端关闭连接")
            if opcode == 0x9:                       # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:                       # pong
                continue
            if opcode in (0x1, 0x2):
                return payload.decode("utf-8", "replace")

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
#  OBS 配置读取
# --------------------------------------------------------------------------

OBS_WS_CONFIG = os.path.join(os.environ.get("APPDATA", ""), "obs-studio",
                             "plugin_config", "obs-websocket", "config.json")


def find_obs_websocket(config_path=None):
    """从 OBS 自己的配置文件里读出 WebSocket 端口 / 密码。

    OBS 28 以后内置 obs-websocket，第一次运行 OBS 时会自动生成这个文件并
    随机一个密码 —— 所以我们直接读它，用户不需要手工填任何东西。

    返回 dict 或 None（None 表示未启用/还没跑过 OBS）。
    """
    path = config_path or OBS_WS_CONFIG
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    if not raw.get("server_enabled", True):
        return None
    return {
        "port": int(raw.get("server_port") or 4455),
        "password": raw.get("server_password") or "",
        "auth_required": bool(raw.get("auth_required", True)),
        "alerts": bool(raw.get("alerts_enabled", True)),
    }


# --------------------------------------------------------------------------
#  OBS 推流监听
# --------------------------------------------------------------------------

EVENT_SUB_OUTPUTS = 1 << 6          # StreamStateChanged 属于 Outputs
EVENT_SUB_GENERAL = 1 << 0


class ObsWatcher(threading.Thread):
    """连上 obs-websocket，只在「开始推流」时触发。

    这是唯一能精确知道"你按下了开始推流"的信号源。
    断线会自动重连；OBS 没开时安静等待。
    """

    def __init__(self, fire, stop_event, on_status=None, config_path=None):
        super().__init__(daemon=True, name="obs-watcher")
        self.fire = fire
        self.stop_event = stop_event
        self.on_status = on_status or (lambda text: None)
        self.config_path = config_path
        self.streaming = False
        self.connected = False

    # -- 对外状态 --
    def status_text(self):
        if self.connected:
            return "OBS 已连接" + ("（正在推流）" if self.streaming else "（未推流）")
        return "OBS 未连接"

    def run(self):
        while not self.stop_event.is_set():
            info = find_obs_websocket(self.config_path)
            if info is None:
                self.connected = False
                self.on_status("OBS 未启用或还没运行过")
                self._sleep(20)
                continue
            try:
                self.on_status("正在连接 OBS …")
                self._session(info)
            except Exception as exc:
                self.connected = False
                self.on_status("OBS 连接中断：{}".format(exc))
                _log("OBS 连接中断：{}".format(exc), "WARN")
            self._sleep(8)

    def _sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self.stop_event.is_set():
                return
            time.sleep(0.2)

    def _session(self, info):
        ws = WsClient("127.0.0.1", info["port"], timeout=5.0)
        try:
            hello = json.loads(ws.recv_text())
            if hello.get("op") != 0:
                raise WsClosed("OBS 没有返回 Hello")
            d = hello.get("d") or {}
            payload = {
                "rpcVersion": int(d.get("rpcVersion") or 1),
                "eventSubscriptions": EVENT_SUB_OUTPUTS | EVENT_SUB_GENERAL,
            }
            auth = d.get("authentication")
            if auth:
                pw = info.get("password") or ""
                secret = base64.b64encode(
                    hashlib.sha256((pw + auth["salt"]).encode("utf-8")).digest()).decode()
                payload["authentication"] = base64.b64encode(
                    hashlib.sha256((secret + auth["challenge"]).encode("utf-8")).digest()
                ).decode()
            ws.send_text(json.dumps({"op": 1, "d": payload}))

            # 等 Identified
            while True:
                msg = json.loads(ws.recv_text())
                if msg.get("op") == 2:
                    break
                if msg.get("op") == 9:              # Identify 被拒
                    raise WsClosed("OBS 拒绝了认证（密码不对？）")

            self.connected = True
            self.on_status("OBS 已连接（未推流）")
            _log("已连上 OBS WebSocket（端口 {}），之后由 OBS 的推流事件触发".format(info["port"]))

            # 问一次当前状态：可能连上时已经在推流了
            ws.send_text(json.dumps({"op": 6, "d": {
                "requestType": "GetStreamStatus", "requestId": "init"}}))

            while not self.stop_event.is_set():
                try:
                    msg = json.loads(ws.recv_text())
                except WsIdle:
                    continue
                op = msg.get("op")
                d = msg.get("d") or {}
                if op == 5:                          # Event
                    if d.get("eventType") == "StreamStateChanged":
                        active = bool((d.get("eventData") or {}).get("outputActive"))
                        self._on_stream(active)
                elif op == 7:                        # RequestResponse
                    if d.get("requestId") == "init":
                        rd = d.get("responseData") or {}
                        if rd.get("outputActive"):
                            self._on_stream(True)
        finally:
            self.connected = False
            ws.close()

    def _on_stream(self, active):
        if active == self.streaming:
            # 首次连接时可能重复报一次相同的状态
            if active:
                return
        self.streaming = active
        if active:
            self.on_status("OBS 正在推流")
            _log("OBS 开始推流")
            try:
                self.fire("OBS 开始推流")
            except Exception as exc:
                _log("OBS 触发后发送出错：{}".format(exc), "ERROR")
        else:
            self.on_status("OBS 已停止推流")
            _log("OBS 停止推流")


# --------------------------------------------------------------------------
#  全局快捷键
# --------------------------------------------------------------------------

MOD_KEYS = {
    "ctrl": 0x0002, "control": 0x0002,
    "alt": 0x0001,
    "shift": 0x0004,
    "win": 0x0008, "super": 0x0008,
}

WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001


def parse_hotkey(spec):
    """把 'ctrl+alt+l' 解析成 (mods, vk)。无效返回 None。"""
    if not spec:
        return None
    mods = 0
    key = None
    for part in spec.lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in MOD_KEYS:
            mods |= MOD_KEYS[part]
        elif len(part) == 1 and (part.isalnum()):
            key = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            key = 0x70 + int(part[1:]) - 1
        else:
            return None
    if not key or not mods:
        return None
    return mods, key


class HotkeyListener(threading.Thread):
    """全局快捷键。Win32 RegisterHotKey，在任何程序里按下都生效。"""

    def __init__(self, spec, fire, stop_event, on_status=None, hotkey_id=0xB17E):
        super().__init__(daemon=True, name="hotkey")
        self.spec = spec
        self.fire = fire
        self.stop_event = stop_event
        self.on_status = on_status or (lambda text: None)
        self.hotkey_id = hotkey_id
        self.registered = False

    def run(self):
        if os.name != "nt" or wintypes is None:
            self.on_status("快捷键仅支持 Windows")
            return
        parsed = parse_hotkey(self.spec)
        if not parsed:
            self.on_status("快捷键格式无效：{}".format(self.spec))
            _log("快捷键格式无效：{}（示例：ctrl+alt+l）".format(self.spec), "WARN")
            return
        mods, vk = parsed
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, self.hotkey_id, mods, vk):
            msg = ("快捷键 {} 已被其它程序占用，注册失败。"
                   "请到「通知内容与设置」页换一个 —— 实测空闲的有 "
                   "ctrl+alt+k、ctrl+shift+l、alt+f9".format(self.spec.upper()))
            self.on_status(msg)
            _log(msg, "WARN")
            return

        self.registered = True
        pretty = "+".join(p.upper() if len(p) == 1 else p.capitalize()
                          for p in self.spec.replace(" ", "").split("+"))
        self.on_status("快捷键 {} 已就绪".format(pretty))
        _log("全局快捷键 {} 已注册，按下即推送".format(pretty))

        msg = wintypes.MSG()
        try:
            while not self.stop_event.is_set():
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_HOTKEY and msg.wParam == self.hotkey_id:
                        _log("收到快捷键 {}".format(pretty))
                        try:
                            self.fire("按下快捷键 {}".format(pretty))
                        except Exception as exc:
                            _log("快捷键触发后发送出错：{}".format(exc), "ERROR")
                else:
                    time.sleep(0.06)
        finally:
            user32.UnregisterHotKey(None, self.hotkey_id)
            self.registered = False


# --------------------------------------------------------------------------
#  直播平台轮询
# --------------------------------------------------------------------------

STATUS_OFFLINE = 0
STATUS_LIVE = 1
STATUS_ROUND = 2          # 轮播（自动重播），不算开播


def _http_json(url, timeout=10, proxy=""):
    """取 JSON。

    proxy 为空表示**直连**，这是本项目的默认选择。原因：这类机器通常
    挂着 Clash 之类的全局代理，而直播平台是境内服务，走境外节点只会更慢；
    更糟的是代理关掉之后注册表设置仍然在，请求会打到死端口上直接失败。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
    })
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def guess_bilibili_room_id(link):
    """从直播间链接里抠出房间号，省得用户再填一遍。

    https://live.bilibili.com/12345678?spm=xxx   ->   12345678
    """
    m = re.search(r"live\.bilibili\.com/(\d+)", link or "")
    return int(m.group(1)) if m else None


def format_duration(seconds):
    """把秒数说成人话。"""
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "一小会儿"
    if seconds < 60:
        return "不到 1 分钟"
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return "{} 小时 {} 分钟".format(hours, minutes)
    if hours:
        return "{} 小时".format(hours)
    return "{} 分钟".format(minutes)


class BilibiliRoom:
    """B站直播间状态查询。

    用的是公开接口，**不需要登录、不需要签名、不需要 cookie**：

        GET https://api.live.bilibili.com/room/v1/Room/get_info?room_id=<id>
        ->  data.live_status : 0=未开播  1=直播中  2=轮播
    """

    API = "https://api.live.bilibili.com/room/v1/Room/get_info"

    def __init__(self, room_id, timeout=10, proxy=""):
        self.room_id = int(room_id)
        self.timeout = timeout
        self.proxy = proxy

    def fetch(self):
        """返回 (live_status, room_info)。失败时抛异常。"""
        url = "{}?room_id={}".format(self.API, self.room_id)
        raw = _http_json(url, timeout=self.timeout, proxy=self.proxy)
        if raw.get("code") != 0:
            raise RuntimeError("B站接口返回 code={} {}".format(
                raw.get("code"), raw.get("message")))
        data = raw.get("data") or {}
        return int(data.get("live_status", 0)), data


class PlatformWatcher(threading.Thread):
    """轮询直播间状态，检测「开播」与「下播」两个事件。

    这是唯一一个**与开播软件无关**的触发源：不管用 OBS、直播姬、
    直播伴侣，还是干脆用手机开播，只要房间状态变了就能检测到。
    而且它读到的是平台判定的真值 —— 群友要知道的正是「房间开了」。

    语义要点（这几条决定了它不会打扰人）：

    * **只在状态跳变时触发**，直播期间不会反复发。
    * **启动时若已在直播，不补发开播通知**，否则每重启一次程序，
      群友就多收一条。要补发用界面上的「立即发送」。
    * **下播有宽限期**：状态转为离线后先等 `offline_grace` 秒再复核，
      期间若恢复直播则取消。用来过滤断流重连造成的假下播。
    * `live_status == 2`（轮播）按离线处理，但**不触发下播通知** ——
      轮播本来就不是你在播。
    """

    def __init__(self, room_id, on_live, on_offline, stop_event,
                 poll_seconds=30, offline_grace=60, proxy="",
                 on_status=None, room=None):
        super().__init__(daemon=True, name="platform-watcher")
        self.room = room or BilibiliRoom(room_id, proxy=proxy)
        self.on_live = on_live
        self.on_offline = on_offline
        self.stop_event = stop_event
        self.poll_seconds = max(5.0, float(poll_seconds or 30))
        self.offline_grace = max(0.0, float(offline_grace or 0))
        self.on_status = on_status or (lambda text: None)

        self.state = "unknown"          # unknown / live / offline
        self.live_since = None
        self._offline_since = None
        self.last_error = None
        self.last_status = None

    # ---- 对外状态 ----
    def status_text(self):
        if self.last_error:
            return "直播间查询失败：{}".format(self.last_error)
        if self.state == "live":
            return "直播间正在直播"
        if self.state == "offline":
            return "直播间未开播"
        return "等待首次查询"

    def run(self):
        while not self.stop_event.is_set():
            try:
                status, data = self.room.fetch()
                self.last_error = None
                self._tick(status, data)
            except Exception as exc:
                self.last_error = str(exc)
                self.on_status("直播间查询失败：{}".format(exc))
                _log("直播间状态查询失败：{}".format(exc), "WARN")
            self._sleep(self.poll_seconds)

    def _sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self.stop_event.is_set():
                return
            time.sleep(0.2)

    def _tick(self, status, data):
        now = time.time()
        self.last_status = status

        if status == STATUS_LIVE:
            self._offline_since = None
            if self.state == "offline":
                # 只有从「已确认离线」跳到直播，才算一次真的开播
                self._enter_live(now, data, fire=True)
            elif self.state == "unknown":
                self._enter_live(now, data, fire=False)
            else:
                self.state = "live"
            return

        # ---- 离线(0) 或 轮播(2) ----
        if self.state == "live" and status == STATUS_OFFLINE:
            if self._offline_since is None:
                self._offline_since = now
                if self.offline_grace > 0:
                    self.on_status("疑似下播，{} 秒后确认 …".format(int(self.offline_grace)))
                    return
            if (self.offline_grace > 0
                    and now - self._offline_since >= self.offline_grace):
                self._declare_offline(now)
            elif self.offline_grace == 0:
                self._declare_offline(now)
            return

        # 轮播、或本来就是离线：归为离线，且不发下播通知
        if self.state != "offline":
            self.state = "offline"
            self._offline_since = None
            self.live_since = None
            self.on_status("直播间未开播")

    def _enter_live(self, now, data, fire):
        self.state = "live"
        self.live_since = self._parse_live_time(data) or now
        if fire:
            self.on_status("直播间已开播")
            _log("直播间已开播")
            try:
                self.on_live()
            except Exception as exc:
                _log("开播通知发送出错：{}".format(exc), "ERROR")
        else:
            self.on_status("直播间已在直播中（不补发通知）")
            _log("启动时直播间已在直播中，不补发开播通知")

    def _declare_offline(self, now):
        duration = format_duration(now - self.live_since) if self.live_since else ""
        self.state = "offline"
        self._offline_since = None
        self.on_status("直播间已下播")
        _log("直播间已下播，时长 {}".format(duration or "未知"))
        try:
            self.on_offline(duration)
        except Exception as exc:
            _log("下播通知发送出错：{}".format(exc), "ERROR")
        self.live_since = None

    @staticmethod
    def _parse_live_time(data):
        """从接口的 live_time 字段取开播时刻；取不到返回 None。"""
        raw = (data or {}).get("live_time")
        if not raw or str(raw).startswith("0000"):
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt).timestamp()
            except ValueError:
                continue
        return None

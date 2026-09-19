#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
#  写给以后翻到这个文件的人
# ---------------------------------------------------------------------------
#  这个程序没有用任何第三方库：四千多行，全是 Python 标准库加 tkinter。
#  手写的部分包括 WebSocket 客户端、PNG 编码器、ICO 组装、圆角抗锯齿。
#
#  这不是为了炫技。是因为**没有依赖的东西才能活得久** —— 今天用某个框架写的
#  代码，十年后大概已经装不上了；而 tkinter 从 1994 年随 CPython 发布至今
#  没断过。它不需要有人维护，也能自己站着。
#
#  如果你是照着某个仓库 clone 下来的陌生人：
#  这东西确实存在过，也确实替人省过事。那也就够了。
#
#                                                    —— KagurazakaChizuru
# ---------------------------------------------------------------------------

"""
大肥鱼直播姬 —— 开播自动往 QQ 群发 @全体成员 通知
====================================================

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
import hmac
import json
import os
import random
import re
import subprocess
import sys

try:
    import msvcrt                  # Windows 专用的文件锁，单实例就靠它
except ImportError:                # 非 Windows：不做单实例限制，别把人拦在门外
    msvcrt = None
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

try:
    import games                   # 游戏名识别（窗口标题 / 场景文件 / 平台库）
except ImportError:                # 缺文件时通知里就不带游戏名
    games = None

APP_NAME = "dafeiyu-live-notify"        # 技术标识：控制端口、日志、JSON 字段用
DISPLAY_NAME = "大肥鱼直播姬"             # 界面与文档里显示的名字
VERSION = "1.7.5"

def _resolve_base_dir():
    """确定**数据目录**（config.json / logs / napcat 所在处）。

    打包成 exe 之后，`__file__` 指向的是 PyInstaller 解包出来的**临时目录**，
    程序一退出就删掉了。日志要是写在那里，等于从来没写过 —— 用户点
    「打开日志文件夹」会发现里面只有源码运行时的旧记录，出了问题无从查证。

    所以必须相对 **exe 自身** 解析。gui.py 里有一份同样的逻辑
    （`_resolve_data_dir`），两边算出来必须是同一个目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "app")
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _resolve_base_dir()
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# --------------------------------------------------------------------------
#  内置文案池
# --------------------------------------------------------------------------
#  开箱即用、**自动轮换**：每次开播从池子里随机挑一条，群友不会每次都看到
#  同一句。用户什么都不用设；想自己改就直接改配置里的 template / templates。
#
#  写这些文案时守三条：
#    · 每条都短 —— 群友扫一眼就完了，没人读长文
#    · 风格刻意拉开（直白 / 卖萌 / 中二 / 自嘲 / 简洁），不然挑来挑去一个味
#    · 留 {link}，那是通知里唯一有用的信息
TEMPLATE_POOLS = {
    # ------------------------------------------------------------------
    #  **池子里不许写死时段。**
    #
    #  原来 offline 里写着「今晚播了 {duration}」，live 里写着「深夜档开了」
    #  「周末到了」—— 早上八点下播就成了笑话，周三凌晨也不会是周末。
    #  时段的判断做不了「写的时候看一眼」，只能做在**挑文案的那一刻**。
    #
    #  所以：基础池只留任何时刻都成立的句子；需要时段的挪进 *_night /
    #  live_weekend，由 pick_from(kind=...) 按当前时间并入。
    # ------------------------------------------------------------------
    # 开播 —— **{hook} 顶格放**。
    #
    # 群消息扫过去只有一秒，第一行决定停不停。所以开场白放最上面，
    # 标题和游戏名跟在后面当"信息"。{room_title} 是直播间真实标题
    # （{title} 是配置里那句固定的），有真的就用真的。
    "live": [
        # ---- emoji 放开头，一眼扫过去先看到它。一条里最多两个，多了像广告 ----
        "🔴 {hook}\n\n{room_title}\n{room_desc}\n正在玩《{game}》\n{link}",
        "🔴 开播了\n\n{hook}\n{room_title}\n{room_desc}\n正在玩《{game}》\n{link}",
        "🔴 开播了 —— {hook}\n\n{room_title}\n{room_desc}\n{link}",
        "🔴 {hook}\n\n{room_title}\n正在玩《{game}》\n{link}",
        "🔴 {hook}\n\n{room_desc}\n正在玩《{game}》\n{link}",
        "🔴 开播了\n{link}",
        "🔴 开播了！\n\n{room_title}\n正在玩《{game}》\n{link}",
        "📢 已开播\n\n{room_title}\n{room_desc}\n正在玩《{game}》\n{link}",
        "📢 开播通知\n\n{room_title}\n正在玩《{game}》\n{link}",
        "▶ 直播已开始\n{room_title}\n《{game}》\n{link}",
        "🎮 上号了，正在玩《{game}》\n\n{room_title}\n{link}",
        "🎮 {hook}\n\n正在玩《{game}》\n{link}",
        "🍿 备好零食，开播了\n\n{room_title}\n正在玩《{game}》\n{link}",
        "🍿 {hook}\n\n{room_title}\n{room_desc}\n{link}",
        "🥺 播了半小时，房间还是空的\n\n{room_title}\n{link}\n来个人陪陪我",
        "🥺 人好少啊，来个活人\n\n{room_title}\n正在玩《{game}》\n{link}",
        "⚔️ 战场的门已经开了\n\n{room_title}\n正在玩《{game}》\n{link}",
        "⚔️ {hook}\n\n《{game}》 · {room_title}\n{link}",
        "🔴 又到了丢人现眼的时间\n\n{room_title}\n正在玩《{game}》\n{link}",
        "💗 想你们了，所以我开播了\n\n{room_title}\n正在玩《{game}》\n{link}",
        "✨ {hook}\n\n{room_title}\n{room_desc}\n正在玩《{game}》\n{link}",
        "🐟 摸鱼的可以来看了\n\n{room_title}\n{room_desc}\n正在玩《{game}》\n{link}",
        "🔔 你关注的直播间亮了\n\n{room_title}\n正在玩《{game}》\n{link}",
        "🌊 {hook}\n\n{room_title}\n正在玩《{game}》\n{link}",
    ],
    # 只在 23:00 ~ 05:00 并入
    "live_night": [
        "🌙 深夜档开了\n\n{room_title}\n正在玩《{game}》\n{link}\n睡不着就来聊两句",
        "🌙 这个点还醒着的，来看我\n\n{room_title}\n正在玩《{game}》\n{link}",
        "🌙 凌晨了，还有人在吗\n\n{room_title}\n正在玩《{game}》\n{link}",
        "🌙 陪你熬一会儿\n\n{hook}\n{room_title}\n{link}",
    ],
    # 只在周六周日并入
    "live_weekend": [
        "🎉 周末了，开播！\n\n{room_title}\n正在玩《{game}》\n{link}",
        "🎉 休息日就该这么过\n\n{room_title}\n正在玩《{game}》\n{link}",
        "🎉 周末不睡懒觉，来打游戏\n\n{hook}\n{room_title}\n{link}",
    ],
    # 下播 —— 跟开播一样，**不许写死时段**（早上八点下播写成「今晚」是笑话），
    # 也**别写死人数**。语气比开播松：散场了，可以自嘲、可以催睡。
    "offline": [
        "🌙 下播了，谢谢陪播\n今天播了 {duration}",
        "🌙 下播啦\n\n今天播了 {duration}\n人气最高 {peak}\n谢谢大家",
        "🌙 今天就到这\n\n播了 {duration}，峰值 {peak}\n明天见",
        "🥺 播了 {duration}，人还是不多\n谢谢留下来的各位",
        "🌙 战场暂时关闭\n\n本次 {duration}\n最后在玩《{game}》",
        "💗 谢谢陪我的每一个人\n\n播了 {duration}，峰值 {peak}",
        "🛌 关机睡觉，明天见\n\n今天播了 {duration}",
        "🍜 下播吃饭去了\n\n播了 {duration}，峰值 {peak}",
        "🐟 鱼塘关门\n\n今天游了 {duration}\n水下安静了",
        "🎬 今天的节目到此结束\n\n{duration}，峰值 {peak}\n谢谢收看",
        "😴 主播电量耗尽\n\n硬撑了 {duration}\n充电去了，明天见",
        "🌙 收工\n\n{duration}\n明天同一时间，不见不散",
        "👋 走了走了\n\n今天 {duration}，峰值 {peak}\n晚安",
        "🎮 手柄放下\n\n播了 {duration}\n最后在玩《{game}》",
    ],
    # 只在 23:00 ~ 05:00 并入
    "offline_night": [
        "💗 谢谢今晚陪我的每一个人\n\n播了 {duration}，峰值 {peak}\n晚安",
        "🌙 下播啦，去睡了\n\n今晚播了 {duration}\n大家也早点休息",
        "🌙 这个点下播，该睡了\n\n播了 {duration}\n你也早点休息",
    ],
    "reminder": [
        "还在播～\n{link}",
        "还在播，正在玩《{game}》\n{link}",
        "都播了一阵了，还不来看看？\n\n{title}\n正在玩《{game}》\n{link}",
        "还没下播，人少得可怜\n\n{title}\n{link}",
        "直播仍在继续\n\n{title}\n正在玩《{game}》\n{link}",
        "这个点还开着的应该不多了\n\n正在玩《{game}》\n{link}",
    ],
    # 换游戏 —— 要说清楚"从什么换成什么"。
    #
    # 原来只有「换游戏了，现在打《X》」，看的人不知道之前是什么，也就感觉不到
    # "换了"。加上 {prev_game} 之后信息才完整。
    #
    # {prev_game} 认不出来时是「刚才那个」，**不会是空串** —— 空串会让
    # 「不玩《》了」很难看；而 DROP_LINE_WHEN_EMPTY 是按整行删的，
    # 会把整句一起干掉，那就什么都不剩了。
    "change": [
        "🔄 换游戏了\n\n《{prev_game}》 → 《{game}》",
        "🔄 换战场：《{prev_game}》 → 《{game}》",
        "🔄 不玩《{prev_game}》了，改打《{game}》",
        "🎮 换个口味，现在打《{game}》\n（刚才在玩《{prev_game}》）",
        "🔄 《{prev_game}》打腻了，开《{game}》",
        "🎯 目标已切换：《{prev_game}》 → 《{game}》\n{link}",
        # 下面这几条**不含 {prev_game}**。认不出上一个游戏时，上面六条会被
        # 整条排除；要是这儿也只剩一条，就变成每次都发同一句了 ——
        # 比不写还单调。两种情况下都要有得挑。
        "🔄 换游戏了，现在打《{game}》\n{link}",
        "🎮 续上，现在打《{game}》\n{link}",
        "🔄 换个战场，现在打《{game}》\n{link}",
        "🎯 现在打《{game}》\n{link}",
    ],
}


#: 群文案（开场白）。跟模板一样，每次发送随机挑一句。
#:
#: 为什么单独成池：**这跟模板是两件事**。
#:   模板   —— 消息整体的骨架，决定有哪几行、什么顺序
#:   群文案 —— 顶端那句勾人的话，决定扫过去的人愿不愿意停一下
#: 混在一起写，改一句开场白就得重排整个模板。
#:
#: 写这些的规矩：短、口语、像群里的人在说话。**别堆形容词** ——
#: 「精彩绝伦的直播即将开始」那种一看就是机器人。
HOOK_POOL = [
    "刚开，人还不多，来占个前排",
    "别刷了，来看点会动的",
    "三分钟，看完不满意再走",
    "这个点还醒着的，进来坐坐",
    "开播了，第一波进来的都是元老",
    "人已经在了，就差你",
    "摸鱼的可以名正言顺了",
    "来看看今天能翻几次车",
    "刚坐下，热乎的",
    "进来聊两句也行，不一定非得看",
    "🎣 鱼已经上钩了，就差你这条",
    "🔔 你关注的鸽子开播了",
    "☕ 泡好茶了，来陪我坐会儿",
    "👀 在线等，挺急的，等一个观众",
    "🎬 今天的节目开始了，前排还有座",
    "🌊 开闸了，来看看今天的水花",
    "🍜 边吃边播，来看我翻车",
    "🚀 刚点火，别错过起飞",
]


def _own_first(own, pool):
    """用户自己写的那句排在最前面，后面跟内置池子。

    不这么做的话，一旦用上内置池子，用户原来那句就永远发不出去了 ——
    那是他自己敲的，不该被我的默认值挤掉。
    """
    out = list(pool)
    own = str(own or "").strip()
    if own and own not in out:
        out.insert(0, own)
    return out


def time_words(now=None):
    """给模板用的时段词，返回 (tod, weekday)。

    为什么要有这个：写死时段一定出错 —— 早上八点下播写成「今晚」、
    周三写成「周末」。要么别写时段，要么用占位符让程序填。
    """
    now = now or datetime.now()
    h = now.hour
    if h < 5:
        tod = "凌晨"
    elif h < 9:
        tod = "早上"
    elif h < 12:
        tod = "上午"
    elif h < 14:
        tod = "中午"
    elif h < 18:
        tod = "下午"
    elif h < 23:
        tod = "晚上"
    else:
        tod = "深夜"
    return tod, "星期" + "一二三四五六日"[now.weekday()]


def _is_night(now=None):
    """深夜档的界定：23:00 ~ 05:00。"""
    now = now or datetime.now()
    return now.hour >= 23 or now.hour < 5


def _time_pools(kind, now=None):
    """当前时间该额外并入哪些池子。

    **必须在每次挑文案时算，不能在上层加载配置时算。** 配置只读一次，
    而程序会一直挂着跨过 23 点、跨过周末。
    """
    if not kind:
        return []
    now = now or datetime.now()
    names = []
    if _is_night(now):
        names.append(kind + "_night")
    if kind == "live" and now.weekday() >= 5:
        names.append("live_weekend")
    return [TEMPLATE_POOLS[x] for x in names if x in TEMPLATE_POOLS]


def pick_from(pool, fallback="", kind="", avoid=()):
    """从文案池里随机挑一条。池子空了才退回 fallback。

    每次调用都重新挑 —— 所以同一场直播里的开播、二次提醒、下播会各挑各的，
    不会整场都用同一句。

    avoid 里的占位符**没有值**，含它们的模板会被整条排除。
    实测踩到过：用一个假名字（「刚才那个」）去填 {prev_game}，渲染出
    「（刚才在玩《刚才那个》）」这种废话。值没有，就别挑需要它的句子。
    """
    pool = [p for p in (pool or []) if p and p.strip()]
    # 按当前时间并入时段池。「深夜档」「周末」这类句子只在该出现的时候出现。
    for extra in _time_pools(kind):
        pool = pool + [p for p in extra if p and p.strip()]
    # 缺值的占位符 -> 排除含它的模板
    for token in (avoid or ()):
        marker = "{" + token + "}"
        kept = [p for p in pool if marker not in p]
        if kept:                 # 全被排掉就宁可留着，也不能没得发
            pool = kept
    if pool:
        return random.choice(pool)
    return fallback



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

# 日志文件里要打码的东西。
#
# 为什么只打码**文件**、不动控制台和界面面板：日志文件是拿来外传的
# （贴给群友、发给别人看），而里面躺着 NapCat 的 WebUi token 和我们自己的
# 控制端口 token —— 那等价于「谁拿到谁就能操作」。界面面板是本机的，
# 用户需要在那里看到完整地址以便复制，所以保持原样。
_SECRET_PATTERNS = (
    re.compile(r"((?:token|Token|TOKEN)\s*[=:：]\s*)([A-Za-z0-9_\-\.]{12,})"),
    re.compile(r"((?:access_token)\"?\s*[=:]\s*\"?)([A-Za-z0-9_\-\.]{12,})"),
)


def redact(msg):
    """把疑似密钥打码，只留前 6 位。够核对是哪一份，不够拿去用。"""
    if not isinstance(msg, str):
        return msg
    out = msg
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: m.group(1) + m.group(2)[:6] + "…（已打码）", out)
    return out


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
    """同时输出到控制台和日志文件。

    **时间戳只取一次。** 取两次的话，两次调用正好跨过秒边界时，文件里的
    「外层时间」会比消息自带的「内层时间」晚一秒，看起来像日志错乱 ——
    实际只是白多调了一次 now()。实测出现过：
        [2026-09-19 13:09:35] [13:09:34] [INFO] 界面已启动
    """
    now = datetime.now()
    line = "[{}] [{}] {}".format(now.strftime("%H:%M:%S"), level, msg)
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
                        now.strftime("%Y-%m-%d %H:%M:%S"), redact(line)))
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
        # 两种情况要给两种说法。**从源码目录直接运行**是最常见的一种 ——
        # 用户多半只是想打开程序，却点到了仓库里的 gui.py。那时候跟他说
        # 「请把 config.json 放在程序同目录」是答非所问：他压根不该在这儿跑。
        here = os.path.dirname(os.path.abspath(path))
        if os.path.isfile(os.path.join(here, "config.example.json")):
            raise ConfigError(
                "找不到配置文件：{}\n\n"
                "看起来你是**直接从源码目录运行**的。\n"
                "  · 只是想用程序：关掉这个窗口，双击 大肥鱼直播姬.exe\n"
                "  · 确实要开发：先执行  copy config.example.json config.json".format(path))
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
    templates = []
    for item in (message.get("templates") or []):
        text = str(item or "")
        if text.strip():
            templates.append(text)
    cover_size = message.get("cover_size") or [200, 112]
    try:
        cover_size = [int(cover_size[0]), int(cover_size[1])]
    except (TypeError, ValueError, IndexError):
        raise ConfigError("message.cover_size 必须是两个数字，例如 [200, 112]")
    msg_cfg = {
        "template": str(message.get("template") or TEMPLATE_POOLS["live"][0]),
        # 多套文案轮换。用户没填就用**内置池子** —— 默认就该是轮换的：
        # 每场直播发一模一样的话，群里刷到第三遍就自动忽略了。
        #
        # 用户自己写的那句要**插在池子最前面**，不能被内置池子盖掉：
        # 那是他一个字一个字敲的，是这套文案里最有个性的一条。
        "templates": templates or _own_first(message.get("template"),
                                             TEMPLATE_POOLS["live"]),
        "title": str(message.get("title") or ""),
        # 群文案池。空的话回落到内置那十条 —— 用户不填也得有得挑。
        "hooks": [str(x) for x in (message.get("hooks") or []) if str(x).strip()]
                 or list(HOOK_POOL),
        "link": str(message.get("link") or ""),
        # 开播通知里带一张小封面。B站图床直接给缩好的图，很便宜。
        "cover": bool(message.get("cover", True)),
        "cover_size": cover_size,
    }

    # --- game：通知里那句"正在玩《XXX》" ---
    game = raw.get("game") or {}
    if not isinstance(game, dict):
        raise ConfigError("game 必须是一个对象。")
    names_raw = game.get("names") or {}
    if not isinstance(names_raw, dict):
        raise ConfigError(
            'game.names 必须是一个对象，例如 {"farcry6.exe": "孤岛惊魂6"}')
    ignore_raw = game.get("ignore") or []
    if not isinstance(ignore_raw, list):
        raise ConfigError('game.ignore 必须是数组，例如 ["vtube studio.exe"]')
    game_cfg = {
        "enabled": bool(game.get("enabled", True)),
        # 手工映射：exe 名（小写）-> 想显示的名字。优先级最高。
        "names": {str(k): str(v) for k, v in names_raw.items()},
        # 长得像游戏但不是的东西（虚拟形象、剪辑软件），列进来永不播报
        "ignore": [str(x) for x in ignore_raw],
        # 中途换游戏时补发一条
        "announce_change": bool(game.get("announce_change", True)),
        "change_template": str(game.get("change_template")
                               or TEMPLATE_POOLS["change"][0]),
        "change_templates": [str(x) for x in (game.get("change_templates") or [])]
                            or _own_first(game.get("change_template"),
                                          TEMPLATE_POOLS["change"]),
        # 默认 0：**不再用"距上次播报多久"去压正常换游戏**。
        # 实测的坏场景：玩 A 几分钟换 B，被 5 分钟冷却吃掉，群里还以为在玩 A。
        "change_cooldown_minutes": float(game.get("change_cooldown_minutes", 0) or 0),
        # 新游戏要稳定这么多秒才播报 —— 这才是防抖该干的事（A→B→A→B 来回跳）。
        # 默认 20 秒：短到不会漏掉真换游戏，长到能滤掉切窗口时的抖动。
        "change_settle_seconds": float(game.get("change_settle_seconds", 20) or 0),
    }

    # --- reminder：开播后隔一段时间再喊一次 ---
    reminder = raw.get("reminder") or {}
    if not isinstance(reminder, dict):
        raise ConfigError("reminder 必须是一个对象。")
    raw_minutes = reminder.get("after_minutes")
    if raw_minutes is None:
        raw_minutes = [30, 60]
    if not isinstance(raw_minutes, list):
        raise ConfigError("reminder.after_minutes 必须是数组，例如 [30, 60]")
    minutes = []
    for item in raw_minutes:
        try:
            val = float(item)
        except (TypeError, ValueError):
            raise ConfigError("reminder.after_minutes 里有非数字：{!r}".format(item))
        if val > 0:
            minutes.append(val)
    minutes.sort()
    reminder_cfg = {
        "enabled": bool(reminder.get("enabled", True)),
        "after_minutes": minutes,
        # 一场直播最多发几条（含开播那条）。断流重连最容易把提醒刷爆，
        # 所以这个上限是硬性的。
        "max_total": max(1, int(reminder.get("max_total", 3) or 3)),
        "template": str(reminder.get("template")
                        or TEMPLATE_POOLS["reminder"][0]),
        "templates": [str(x) for x in (reminder.get("templates") or [])]
                     or _own_first(reminder.get("template"),
                                   TEMPLATE_POOLS["reminder"]),
        "at_all": bool(reminder.get("at_all", False)),
    }

    # --- offline_message：下播提示 ---
    offline = raw.get("offline_message") or {}
    if not isinstance(offline, dict):
        raise ConfigError("offline_message 必须是一个对象。")
    try:
        offline_grace = float(offline.get("grace_seconds", 60) or 0)
    except (TypeError, ValueError):
        raise ConfigError("offline_message.grace_seconds 必须是数字。")
    offline_cfg = {
        "enabled": bool(offline.get("enabled", True)),
        "template": str(offline.get("template")
                        or TEMPLATE_POOLS["offline"][0]),
        "templates": [str(x) for x in (offline.get("templates") or [])]
                     or _own_first(offline.get("template"),
                                   TEMPLATE_POOLS["offline"]),
        # 下播默认不 @ 任何人：没在看直播的人不会关心你几点停
        "at_all": bool(offline.get("at_all", False)),
        # 状态转离线后先等这么久再确认，用来过滤断流重连造成的假下播
        "grace_seconds": offline_grace,
    }

    # --- 模板占位符校验（不报错，只记下来给 check 看）---
    # 这里刻意**不抛异常**：一个拼错的占位符不该让整个程序起不来。
    # 它会在 check 里被明确列出来，同时渲染时会被剔除。
    cfg_view = {
        "message": msg_cfg,
        "offline_message": offline_cfg,
        "reminder": reminder_cfg,
        "game": game_cfg,
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
        # 打开程序就自动开始监控，不必再点大按钮。
        # 默认关闭：多数人打开界面只是想改设置，不该顺手把 NapCat 也拉起来。
        "auto_start": bool(behavior.get("auto_start", False)),
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

    try:
        poll_seconds = float(trigger.get("poll_seconds", 30) or 30)
    except (TypeError, ValueError):
        raise ConfigError("trigger.poll_seconds 必须是数字。")
    if not (5 <= poll_seconds <= 3600):
        raise ConfigError("trigger.poll_seconds 取值应在 5 ~ 3600 秒之间。")

    room_raw = trigger.get("room_id")
    room_id = None
    if room_raw not in (None, "", 0, "0"):
        try:
            room_id = int(room_raw)
        except (TypeError, ValueError):
            raise ConfigError(
                "trigger.room_id 必须是数字，或留空以从直播间链接自动识别。")
    if room_id is None and triggers is not None:
        # 房间号没填就从他已有的直播间链接里抠，省一次手工配置
        room_id = triggers.guess_bilibili_room_id(msg_cfg["link"])

    trigger_cfg = {
        # 进程检测的硬伤：软件一打开就触发，而人往往还要调设备、试麦。
        # 所以默认关闭，改用更精确的信号。
        "on_process_start": bool(trigger.get("on_process_start", False)),
        "on_obs_stream": bool(trigger.get("on_obs_stream", True)),
        # 轮询直播间状态。唯一与开播软件无关的触发源，含手机开播。
        # 注意：开启后程序会定期访问直播平台的公开接口（默认直连，不走代理）。
        "on_platform_live": bool(trigger.get("on_platform_live", False)),
        "hotkey": str(trigger.get("hotkey") or "").strip().lower(),
        "room_id": room_id,
        "poll_seconds": poll_seconds,
        # 留空 = 直连。境内平台走境外代理只会更慢，代理关掉还会直接失败。
        "platform_proxy": str(trigger.get("platform_proxy") or "").strip(),
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
        "game": game_cfg,
        "reminder": reminder_cfg,
        "behavior": behavior_cfg,
        "control": control_cfg,
        "trigger": trigger_cfg,
        "offline_message": offline_cfg,
        "_path": os.path.abspath(path),
        # 模板里拼错的占位符。这里查出来给 check 用，**不拦启动** ——
        # 一个错别字不该让程序起不来，渲染时还会再削一道。
        "_template_problems": validate_templates(cfg_view),
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

    def send_private_msg(self, user_id, message):
        """私聊发一条。**测试用** —— 想看看消息长什么样又不想打扰群友时走这条。

        注意这是发给「测试接收 QQ」，不是发给机器人自己：QQ 一般不允许
        给自己发私聊，填机器人自己的号多半会失败。
        """
        return self.call("send_private_msg",
                         {"user_id": int(user_id), "message": message})

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

def pick_template(cfg, template=None):
    """挑一条开播文案。配了多套就随机挑一条。

    每次都发一模一样的话，群里刷到第三遍就自动忽略了 —— 人是这样，
    平台的风控也更喜欢有变化的文本。
    """
    if template is not None:
        return template
    pool = cfg["message"].get("templates") or []
    if pool:
        return random.choice(pool)
    return cfg["message"]["template"]


#: 这些占位符为空时，把它所在的**整行**删掉。
#: 比如模板写「正在玩《{game}》」，没识别出游戏时那一行整个消失，
#: 而不是渲染成「正在玩《》」这种残缺的句子。
# room_title / room_desc 也要列进来：接口偶尔抽风取不到时，模板里那一行
# 应该整个消失，而不是留一个空行 —— 上一版漏了它们，实测就是这样。
DROP_LINE_WHEN_EMPTY = ("game", "peak", "room_title", "room_desc", "title")


#: 模板里允许出现的占位符。
#:
#: **必须跟 render_text 里 fields 的键保持一致。** 两边一旦对不上，用户就会
#: 看到一个「写着不报错、发出去却是花括号」的占位符。
ALLOWED_PLACEHOLDERS = frozenset({
    "title", "link", "game", "time", "date", "peak", "duration",
    # 时段词。**写死时段一定出错**（早上八点下播写成「今晚」），
    # 想让文案带时间感就用这两个，让程序在发送那一刻填。
    "tod", "weekday",
    # 换游戏时的"上一个游戏"。认不出来时是「刚才那个」，不会是空串。
    "prev_game",
    # 群文案：从 message.hooks 里随机挑的一句开场白
    "hook",
    # B站上**真实的**直播间标题。跟 {title} 不是一回事 ——
    # {title} 是配置里写死的那句，{room_title} 是你此刻在直播间的标题。
    "room_title",
    # 直播公告 / 简介（已剥掉 HTML）
    "room_desc",
})

#: 匹配一对花括号里的内容。不要求里面合法 —— 畸形的也要能抓出来。
_BRACE_RE = re.compile(r"\{([^{}]*)\}")
#: 落单的花括号。_BRACE_RE 要求成对，抓不到它们，得单独来一下。
_STRAY_BRACE_RE = re.compile(r"[{}]")
_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _split_placeholder(body):
    """把 `{title:>8}` 拆成字段名 title。畸形的返回 None。"""
    m = _FIELD_RE.match(body)
    return m.group(0) if m else None


def unknown_placeholders(text):
    """挑出模板里**不认识**的占位符，返回排好序的名字列表。

    为什么要这个：模板里打错一个字母（{titel}），format() 抛 KeyError，
    被兜底逻辑吞掉，最后把 "{titel}" **原样发进 QQ 群** —— 日志里只有一行
    WARN，群友已经看见了。这是这个程序唯一会「对外出丑」的失败。
    """
    text = str(text or "")
    bad = set()
    for m in _BRACE_RE.finditer(text):
        body = m.group(1)
        name = _split_placeholder(body)
        if name is None:
            bad.add(body.strip() or "(空)")
        elif name not in ALLOWED_PLACEHOLDERS:
            bad.add(name)
    # 成对的都摘掉之后还有花括号残留，说明有落单的
    if _STRAY_BRACE_RE.search(_BRACE_RE.sub("", text)):
        bad.add("(括号不成对)")
    return sorted(bad)


def scrub_template(text):
    """把不认识、写坏了的占位符统统删掉，认识的留着给 format 用。

    **渲染前必须过这一道。** 少一句话群友看不出来，发一串花括号就丢人了。
    """
    # **单遍扫描，不能分两步。** 先替换再全文清花括号的话，第二步会把
    # `{title}` 自己的括号也削掉（实测踩到过，断言当场红）。所以：
    # 配对区间**之外**的落单花括号才清，区间之内按白名单决定留还是丢。
    text = str(text or "")
    out = []
    pos = 0
    for m in _BRACE_RE.finditer(text):
        out.append(_STRAY_BRACE_RE.sub("", text[pos:m.start()]))
        name = _split_placeholder(m.group(1))
        if name and name in ALLOWED_PLACEHOLDERS:
            out.append(m.group(0))          # 认识的，原样留给 format 用
        pos = m.end()                       # 不认识的整对丢掉
    out.append(_STRAY_BRACE_RE.sub("", text[pos:]))
    return "".join(out)


#: 配置里每一处能写模板的地方 —— 校验要全覆盖，漏一处就等于没校验。
TEMPLATE_SLOTS = (
    ("message", "template", "开播文案"),
    ("message", "templates", "开播文案"),
    ("offline_message", "template", "下播文案"),
    ("offline_message", "templates", "下播文案"),
    ("reminder", "template", "二次提醒"),
    ("reminder", "templates", "二次提醒"),
    ("game", "change_template", "换游戏文案"),
    ("game", "change_templates", "换游戏文案"),
)


def validate_templates(cfg):
    """把所有模板扫一遍。返回 [(位置, 错误占位符), ...]，已去重。"""
    bad = []
    seen = set()
    for section, key, label in TEMPLATE_SLOTS:
        raw = (cfg.get(section) or {}).get(key)
        items = raw if isinstance(raw, list) else [raw]
        for text in items:
            for token in unknown_placeholders(text):
                mark = (label, token)
                if mark in seen:
                    continue
                seen.add(mark)
                bad.append(mark)
    return bad


def _drop_empty_lines(template, fields):
    kept = []
    for line in str(template).split("\n"):
        for key in DROP_LINE_WHEN_EMPTY:
            if "{" + key + "}" in line and not str(fields.get(key) or "").strip():
                line = None
                break
        if line is not None:
            kept.append(line)
    text = "\n".join(kept)
    text = re.sub(r"\n{3,}", "\n\n", text)      # 压掉删行留下的连续空行
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip("\n")


def render_text(cfg, template=None, extra=None):
    """渲染消息文本。

    template 为 None 时按配置挑一条开播文案；extra 用于补充额外占位符
    （游戏名 {game}、下播时长 {duration}、人气峰值 {peak}）。
    """
    fields = {
        "title": cfg["message"]["title"],
        "link": cfg["message"]["link"],
        "time": datetime.now().strftime("%H:%M"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        # 先占成空串：模板里写了但这次没值，也不该报错
        "game": "",
        "peak": "",
        "duration": "",
    }
    # 时段词在**这一刻**取，不是配置加载时 —— 程序会挂着跨过深夜。
    _tod, _wd = time_words()
    fields["tod"] = _tod
    fields["weekday"] = _wd
    # 群文案：每次发送随机挑一句。跟挑模板一个道理 —— 同一个开场白连发
    # 三遍，群里就自动忽略了。
    _hooks = [h for h in (cfg["message"].get("hooks") or []) if h and h.strip()]
    fields["hook"] = random.choice(_hooks) if _hooks else ""
    fields["room_title"] = ""
    fields["room_desc"] = ""
    # 空串会让「不玩《》了」很难看，给个读得通的兜底
    fields.setdefault("prev_game", "")
    if extra:
        for key, val in extra.items():
            fields[key] = "" if val is None else str(val)

    chosen = pick_template(cfg, template)
    # 先削掉不认识的占位符。**必须在下划线处理之前做** —— 否则
    # "{titel}" 里的 title 会被误判成"有值"，空行判断跟着出错。
    chosen = scrub_template(chosen)
    tpl = _drop_empty_lines(chosen, fields)
    if not tpl.strip():
        tpl = chosen        # 别把整条消息删成空的了
    try:
        return tpl.format(**fields)
    except (KeyError, IndexError, ValueError):
        # 走到这儿说明花括号本身畸形（`{` 或 `{}` 之类），scrub 没拦住。
        # 再兜一次：把剩下所有花括号段删干净，**绝不原样发出去**。
        log("模板里有畸形占位符，已剔除。可用占位符：{}".format(
            "、".join("{" + k + "}" for k in sorted(fields))), "WARN")
        text = _BRACE_RE.sub("", tpl)
        for key, val in fields.items():
            text = text.replace("{" + key + "}", val)
        return text.strip() or str(chosen)


def preview_messages(cfg, samples=None):
    """把四类消息各渲染一条，给界面预览。**只渲染，绝不发送。**

    样例值（时长、峰值、游戏名）是编的 —— 预览的意义是看**格式和文案**，
    不是看真实数据。想看真实数据就开一场播，或者用私聊测试。
    """
    s = samples or {}
    game = s.get("game") or "艾尔登法环"
    prev = s.get("prev_game") or "只狼"
    # 编得像真的：预览要能一眼看出"标题和公告到底进来了没有"。
    room_t = s.get("room_title") or "【空洞骑士】今天打完螳螂领主"
    room_d = s.get("room_desc") or "晚上八点开播，打到哪算哪，欢迎来聊"
    game_cfg = cfg.get("game") or {}
    off_cfg = cfg.get("offline_message") or {}
    rem_cfg = cfg.get("reminder") or {}

    items = [("开播通知", render_text(cfg, extra={
        "game": game, "room_title": room_t, "room_desc": room_d}))]

    if off_cfg.get("enabled", True):
        items.append(("下播提示", render_text(
            cfg,
            template=pick_from(off_cfg.get("templates"), off_cfg.get("template"),
                               kind="offline"),
            extra={"duration": "2 小时 15 分", "peak": 42, "game": game,
                   "room_title": room_t, "room_desc": room_d})))

    if rem_cfg.get("enabled", True):
        items.append(("二次提醒", render_text(
            cfg,
            template=pick_from(rem_cfg.get("templates"), rem_cfg.get("template"),
                               kind="reminder"),
            extra={"game": game, "room_title": room_t, "room_desc": room_d})))

    if game_cfg.get("enabled", True) and game_cfg.get("announce_change", True):
        # 两种都列出来：认得出上一个游戏是什么样、认不出又是什么样。
        # 后者会走 avoid 分支，把提到 {prev_game} 的句子整条排除。
        items.append(("换游戏（知道上一个）", render_text(
            cfg,
            template=pick_from(game_cfg.get("change_templates"),
                               game_cfg.get("change_template"),
                               kind="change"),
            extra={"game": game, "prev_game": prev})))
        items.append(("换游戏（认不出上一个）", render_text(
            cfg,
            template=pick_from(game_cfg.get("change_templates"),
                               game_cfg.get("change_template"),
                               kind="change", avoid=("prev_game",)),
            extra={"game": game, "prev_game": ""})))

    # 再列一条"标题没取到"的 —— 用来验证那一行是**整个消失**，
    # 而不是留一个空行。取不到标题是常事（接口偶尔抽风）。
    items.append(("开播通知（没取到标题时）", render_text(
        cfg, extra={"game": game, "room_title": "", "room_desc": ""})))

    return items


def build_message(group, text, image=""):
    """按群配置生成 OneBot 消息段数组。

    每个 at 段后面都跟一个空格段：QQ 不会自动在 @ 后面补空格，
    不加的话会渲染成 "@全体成员我开播啦" 这种黏在一起的样子。

    image 非空时在**文字后面**补一张图。放最后是有意的：文字和链接先入眼，
    封面跟在后面把整条消息撑大、更显眼。
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
    if image:
        segments.append({"type": "image", "data": {"file": image}})
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
        elif seg["type"] == "image":
            parts.append("［封面图］")
    return "".join(parts)


# --------------------------------------------------------------------------
# 发送
# --------------------------------------------------------------------------

# 群发失败后的重试间隔（秒）。
#
# 为什么需要：NapCat 偶尔会掉线，健康检查通常十几秒就把它拉回来了 —— 但
# **已经失败的那条通知不会重发**。实测踩到过一次：开播通知三条全军覆没，
# 日志里只留了一行「完成：成功 0/3」，群里什么都没收到，界面也不吭声。
# 那正好是这个工具最不能出错的一刻。
#
# 5 / 15 / 45 是照着「NapCat 重启要几秒到几十秒」定的：第一轮 5 秒正好落在
# 健康检查（8 秒一轮）之后不久，多数掉线一次就能补上。
SEND_RETRY_DELAYS = (5, 15, 45)

# 最近一次群发的结果，给界面用来弹提示 —— 重试用尽还有剩的，才值得打扰用户。
LAST_SEND = {"ok": 0, "total": 0, "failed": [], "when": "", "reason": ""}


class SendResult(object):
    """一次群发的完整结果。

    **为什么要单独一个对象：「触发成功」和「送达成功」是两件事。**

    检测到开播了（事件发生了）不等于群友看到了（消息送达了）。原来
    send_to_groups 只返回一个成功数字，而 fire() 连这个数字都丢掉了 ——
    结果「一条都没发出去」和「全部发成功」在调用方看来长得一模一样。

    这个对象把三件事分清楚：
        total    本该发给几个群
        ok       真正送达了几个
        failed   哪些群最终没发出去
        rounds   补发了几轮才成（0 = 一次就成了）
        reason   这次发送的缘由
    """

    __slots__ = ("total", "ok", "failed", "rounds", "reason", "dry")

    def __init__(self, total=0, ok=0, failed=None, rounds=0, reason="", dry=False):
        self.total = int(total)
        self.ok = int(ok)
        self.failed = list(failed or [])
        self.rounds = int(rounds)
        self.reason = str(reason or "")
        self.dry = bool(dry)

    @property
    def delivered(self):
        """全部送达。"""
        return self.total > 0 and self.ok == self.total

    @property
    def partial(self):
        """发出去了一部分。"""
        return 0 < self.ok < self.total

    @property
    def lost(self):
        """一条都没送出去 —— 这是最要命的那种，必须让用户看见。"""
        return self.total > 0 and self.ok == 0

    def summary(self):
        if self.total == 0:
            return "没有启用的群，什么都没发"
        if self.delivered:
            return "已送达 {}/{}".format(self.ok, self.total)
        if self.lost:
            return "一条都没发出去（0/{}）".format(self.total)
        return "部分送达 {}/{}，失败的群：{}".format(
            self.ok, self.total, "、".join(str(g) for g in self.failed))

    def __repr__(self):
        return "<SendResult {}/{} failed={} rounds={}>".format(
            self.ok, self.total, self.failed, self.rounds)


def note_lost(result, what):
    """一条都没送出去的时候额外喊一声。

    开播那条由 send_to_groups 自己喊；二次提醒、下播、换游戏这几类消息
    是**过了这村没这店**的，全丢也必须让用户知道，不能只有日志里一行 WARN。
    """
    if result is not None and result.lost:
        log("{}没能送达任何人（{}）。".format(what, result.summary()), "ERROR")


def send_to_groups(cfg, onebot, reason, force_dry=False,
                   template=None, extra_fields=None, at_all=None, image=""):
    """向所有启用的群发送通知。**返回 SendResult**，不是成功数。

    template / extra_fields / at_all / image 用于发送另一类消息
    （下播提示、二次提醒、换游戏播报）：

      · template     —— 换一套模板
      · extra_fields —— 补充额外占位符（{game} / {duration} / {peak}）
      · at_all       —— 覆盖各群自己的 @ 设置；None 表示沿用群配置
      · image        —— 图片地址，非空时附在文字后面

    发失败的群会按 SEND_RETRY_DELAYS 重试，**发成功的不重发** —— 否则群友
    会连着收到好几条一样的。全都试完还是不行的，记进 LAST_SEND["failed"]。
    """
    dry = cfg["behavior"]["dry_run"] or force_dry
    rounds_used = 0
    text = render_text(cfg, template=template, extra=extra_fields)
    active = [g for g in cfg["groups"] if g["enabled"]]
    if not active:
        log("没有任何启用的群（enabled 全是 false），什么都没发。", "WARN")
        LAST_SEND.update(ok=0, total=0, failed=[], when="", reason=reason)
        return SendResult(total=0, ok=0, reason=reason, dry=dry)

    log("触发原因：{}".format(reason))
    log("目标：{} 个启用的群（配置里共 {} 个）{}".format(
        len(active), len(cfg["groups"]), "  —— 彩排模式，不真的发" if dry else ""))

    gap = cfg["behavior"]["send_interval_seconds"]

    if dry:
        for group in active:
            target = group
            if at_all is not None:
                target = dict(group, at_all=bool(at_all),
                              at_list=[] if at_all else [])
            preview = describe_message(target, build_message(target, text,
                                                             image=image))
            label = "{}{}".format(group["group_id"],
                                  "（{}）".format(group["note"]) if group["note"] else "")
            log("  [彩排] {} → {}".format(label, preview.replace("\n", " / ")))
        log("完成：成功 {}/{}".format(len(active), len(active)))
        LAST_SEND.update(ok=len(active), total=len(active), failed=[],
                         when=datetime.now().strftime("%H:%M:%S"),
                         reason=reason)
        return SendResult(total=len(active), ok=len(active), reason=reason, dry=True)

    ok_count = 0
    pending = list(active)          # 还没发成功的群
    rounds = len(SEND_RETRY_DELAYS) + 1

    for attempt in range(rounds):
        if attempt:
            delay = SEND_RETRY_DELAYS[attempt - 1]
            log("还有 {} 个群没发出去，{} 秒后重试（第 {}/{} 轮）…".format(
                len(pending), delay, attempt, rounds - 1), "WARN")
            time.sleep(delay)

        still = []
        for idx, group in enumerate(pending):
            target = group
            if at_all is not None:
                # 覆盖 @ 行为：下播提示默认谁都不 @
                target = dict(group, at_all=bool(at_all),
                              at_list=[] if at_all else [])
            segments = build_message(target, text, image=image)
            label = "{}{}".format(group["group_id"],
                                  "（{}）".format(group["note"]) if group["note"] else "")

            ok, data = onebot.send_group_msg(group["group_id"], segments)
            if ok:
                mid = ""
                if isinstance(data, dict) and data.get("message_id") is not None:
                    mid = " (message_id={})".format(data["message_id"])
                log("  [成功] {} -> 已发送{}{}".format(
                    label, mid, "（第 {} 轮补发）".format(attempt) if attempt else ""))
                ok_count += 1
            else:
                still.append(group)
                if attempt == 0:
                    log("  [失败] {} -> {}\n     会在稍后自动重试".format(label, data),
                        "WARN")
                elif attempt == rounds - 1:
                    log("  [失败] {} -> 重试 {} 次仍然发不出去".format(
                        label, len(SEND_RETRY_DELAYS)), "ERROR")

            if idx < len(pending) - 1 and gap > 0:
                time.sleep(gap)

        pending = still
        if not pending:
            rounds_used = attempt
            break
        rounds_used = attempt

    result = SendResult(total=len(active), ok=ok_count,
                        failed=[g["group_id"] for g in pending],
                        rounds=rounds_used, reason=reason)
    LAST_SEND.update(ok=ok_count, total=len(active),
                     failed=result.failed,
                     when=datetime.now().strftime("%H:%M:%S"),
                     reason=reason)
    if result.lost:
        # **一条都没出去。** 这跟"部分失败"不是一回事：群友一个都没看到，
        # 而开播这件事已经发生了，不会再重来一次。必须显眼。
        log("完成：{} —— 没有任何群收到通知！".format(result.summary()), "ERROR")
        log("  开播提示已经错过，不会自动重发。请检查 NapCat 是否在线，"
            "必要时用「手动推一次」补发。", "ERROR")
    else:
        log("完成：{}".format(result.summary()),
            "WARN" if result.partial else "INFO")
    return result


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
<div id="auth" style="display:{need_token}">
<input id="tk" type="password" placeholder="控制端口 token"
 style="padding:10px;border-radius:8px;border:1px solid #3a3f46;background:#0f1115;color:#e8eaed;width:60%">
<button onclick="saveToken()" style="margin-top:0;padding:10px 16px;font-size:14px">记住</button>
</div>
<pre id="o">点上面的按钮手动触发一次（等同于检测到开播）</pre>
<script>
var TOKEN = sessionStorage.getItem('dsh_token') || '';
function hdr() {{ return TOKEN ? {{'Authorization': 'Bearer ' + TOKEN}} : {{}}; }}
function saveToken() {{
  TOKEN = document.getElementById('tk').value.trim();
  sessionStorage.setItem('dsh_token', TOKEN);
  document.getElementById('auth').style.display = 'none';
  fire();
}}
function fire() {{
  fetch('/trigger', {{headers: hdr()}})
    .then(function(r) {{ return r.json(); }})
    .then(function(d) {{
      if (d && d.error && String(d.error).indexOf('token') >= 0) {{
        document.getElementById('auth').style.display = 'block';
      }}
      document.getElementById('o').textContent = JSON.stringify(d, null, 2);
    }});
}}
</script>
</div></body></html>"""


def start_control_server(cfg, on_trigger, get_status):
    """在 127.0.0.1 上开一个极简控制端口。仅本机可访问。"""
    port = cfg["control"]["port"]
    token = cfg["control"]["token"]

    # 兼容模式的提示只打一次，别把日志刷屏
    warned = {"query": False}

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
            """控制端口鉴权。

            优先认 Authorization 头。URL 里的 token 会进浏览器历史、进各种日志、
            进 shell 历史 —— 只当**兼容模式**留着，用一次提醒一次。

            一律用 hmac.compare_digest：`==` 是短路比较，早退出的位置会泄露
            前缀信息。本机端口风险不高，但这条改起来几乎不要钱。
            """
            if not token:
                return True

            supplied, mode = "", ""
            auth = self.headers.get("Authorization") or ""
            if auth:
                supplied = auth[7:].strip() if auth[:7].lower() == "bearer " else auth.strip()
                mode = "header"
            if not supplied and "?" in self.path:
                from urllib.parse import unquote
                for pair in self.path.split("?", 1)[1].split("&"):
                    if pair.startswith("token="):
                        supplied = unquote(pair[6:])
                        mode = "query"
                        break
            if not supplied:
                return False
            if not hmac.compare_digest(supplied, token):
                return False
            if mode == "query" and not warned["query"]:
                warned["query"] = True
                # 提示里**不带 token 本身**
                log("控制端口收到的是带 token 的旧链接。为安全建议改用 "
                    "Authorization 头，或直接在浏览器里打开 "
                    "http://127.0.0.1:{}/ 填一次 token。".format(port), "WARN")
            return True

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"

            if path == "/":
                # 设了 token 就把输入框露出来 —— 否则那个按钮点下去必然 403。
                # 输入框只在浏览器里存（sessionStorage），token 不会进 URL、
                # 不会进历史记录。
                return self._send(200, _CONTROL_PAGE.format(
                    app=DISPLAY_NAME, version=VERSION,
                    need_token="block" if token else "none"),
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
        # **不要把 token 打进日志。** 原来这里直接 format 了完整 token ——
        # 日志文件虽然会打码，控制台和任何接走的日志都会看到明文。
        # 要触发就开 http://127.0.0.1:{port}/ ，页面上填一次就行。
        log("控制端口已设 token。打开 http://127.0.0.1:{}/ 后填一次即可，"
            "或改用 Authorization: Bearer 头。".format(port))
    return httpd


# --------------------------------------------------------------------------
# 命令实现
# --------------------------------------------------------------------------

#: 持有者信息写在这里，**跟锁分开两个文件**。
#:
#: 为什么不写进锁文件里：实测过 —— Windows 的 _locking 锁的是**整个文件**，
#: 不是那一个字节区间。
#:     [持锁时] 读同一文件 -> PermissionError: [Errno 13]
#:     [释放后] 读同一文件 -> 正常
#: 所以「锁加在偏移 4096、信息写在偏移 0」这条设计从根上就不成立，把偏移算准
#: 也没用。分成两个文件，各干各的。
LOCK_PATH = os.path.join(LOG_DIR, "app.lock")
LOCK_INFO_PATH = os.path.join(LOG_DIR, "app.lock.info")


class SingleInstance(object):
    """单实例锁。

    同时在跑两个 watch，两边都会监听、都会发送 —— 群里收到双份通知，而用户
    完全不知道为什么。这个类就是拦这件事的。

    用 msvcrt.locking 而不是 pid 文件：pid 文件在崩溃或被任务管理器强杀之后
    会留下来，下次启动就被永久锁死，只能让用户手动删。msvcrt 的锁由**内核**
    持有，进程一死立刻释放，不会有残留。
    """

    def __init__(self, path=None):
        self.path = path or LOCK_PATH
        self.info_path = self.path + ".info"
        self._fh = None

    def acquire(self):
        """拿到锁返回 True；已有实例在跑返回 False。"""
        if msvcrt is None:              # 非 Windows 不拦
            return True
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            try:
                # **不能用 "a+b"**：追加模式下 seek 无效，写永远落在文件末尾。
                self._fh = open(self.path, "r+b")
            except OSError:
                self._fh = open(self.path, "w+b")
        except OSError as exc:
            # 打不开锁文件（权限、只读盘）就放行 —— 不能因为锁坏了让程序起不来
            log("单实例锁文件打不开（{}），本次不做单实例限制。".format(exc), "WARN")
            return True
        try:
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        # 持有者信息写到**另一个文件**。锁文件一旦被锁，读它就是 PermissionError，
        # 所以两者不能共用。
        try:
            with open(self.info_path, "w", encoding="utf-8") as fh:
                fh.write("pid={} 启动于 {}\n".format(
                    os.getpid(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        except OSError:
            pass
        return True

    def holder(self):
        """读一眼持有者信息（仅用于提示，读失败就算了）。

        读的是 info 文件而不是锁文件 —— 锁文件被锁住之后是读不了的。
        """
        try:
            with open(self.info_path, "r", encoding="utf-8") as fh:
                return fh.read(200).strip()
        except OSError:
            return ""

    def release(self):
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None
        try:
            os.remove(self.info_path)      # 走了就把名片收走
        except OSError:
            pass


def cmd_watch(cfg, stop_event=None):
    # 单实例锁。GUI 和 CLI 都走这个函数，所以挂在这里两条路径一起覆盖。
    lock = SingleInstance()
    if not lock.acquire():
        who = lock.holder()
        log("已经有一个直播姬在监控了，这次不重复启动。", "ERROR")
        if who:
            log("  正在运行的那个：{}".format(who), "ERROR")
        log("  同时跑两个会让群里收到双份通知。要换一个，先把上一个停掉。", "ERROR")
        return 3

    onebot = OneBot(cfg["onebot"])

    ok, data = onebot.get_login_info()
    if ok and isinstance(data, dict):
        log("NapCat 已连接，登录账号：{}（{}）".format(data.get("nickname"), data.get("user_id")))
    else:
        log("警告：现在连不上 NapCat —— {}".format(data), "WARN")
        log("程序会继续运行，但开播时可能发不出去。请检查 NapCat 是否已启动并登录。", "WARN")

    tg = cfg.get("trigger") or {}
    offline_cfg = cfg.get("offline_message") or {}
    gate = triggers.CooldownGate(cfg["behavior"]["cooldown_minutes"]) if triggers else None
    # 下播用**独立**闸门：它和开播是两件事，绝不能被开播那份 30 分钟冷却吃掉。
    # 否则「播了 10 分钟就下播」时，下播提示会被静默丢弃。
    offline_gate = triggers.CooldownGate(0) if triggers else None
    game_cfg = cfg.get("game") or {}
    reminder_cfg = cfg.get("reminder") or {}
    state = {
        "last_fire": 0.0,
        "last_offline": 0.0,
        "live_started": 0.0,        # 本场直播开始的时刻
        "game": "",                 # 上次识别到的游戏
        "reminders_done": set(),    # 已经发过的提醒下标
        "reminders_sent": 0,        # 本场已发条数（含开播那条）
        "last_game_change": 0.0,
        "pending_game": "",         # 已识别到、但还没稳定够时间的新游戏
        "pending_since": 0.0,       # 它是从什么时候开始稳定的
        # 最近一次群发的结果（SendResult）。**和 live_started 分开记** ——
        # 「开播了」和「通知送到了」是两件事，混在一起就分不出全丢的情况。
        "last_send": None,
        "last_send_lost": False,
    }
    obs = None
    hotkey = None
    platform = None

    def detect_game():
        """认一下现在在玩什么。认不出来返回空串，绝不影响发消息。"""
        if not game_cfg.get("enabled", True) or games is None:
            return ""
        try:
            result = games.detect(cfg)
        except Exception as exc:
            log("游戏识别失败：{}".format(exc), "WARN")
            return ""
        name = (result or {}).get("name") or ""
        if name:
            win = (result or {}).get("window") or {}
            log("识别到当前游戏：{}（{} {}）".format(
                name, win.get("class") or "?", win.get("size") or ""))
        return name

    def room_title_now():
        """此刻直播间在用的标题。取不到返回空串，绝不影响发消息。"""
        if platform is None:
            return ""
        try:
            return platform.room_title()
        except Exception:
            return ""

    def room_desc_now():
        """此刻的直播公告（已剥 HTML）。取不到返回空串。"""
        if platform is None:
            return ""
        try:
            return platform.room_desc()
        except Exception:
            return ""

    def cover_image():
        """开播通知带的封面小图。拿不到就返回空串，消息照发。"""
        if not cfg["message"].get("cover", True) or platform is None:
            return ""
        try:
            size = cfg["message"].get("cover_size") or [200, 112]
            return platform.cover_url(size[0], size[1])
        except Exception:
            return ""

    def mark_live_start():
        """记下本场开播时刻 —— 优先用平台给的真实开播时间。"""
        started = getattr(platform, "live_since", None) if platform else None
        state["live_started"] = float(started or time.time())
        state["game"] = ""
        state["reminders_done"] = set()
        state["reminders_sent"] = 0
        state["last_game_change"] = 0.0
        state["pending_game"] = ""
        state["pending_since"] = 0.0

    def fire(reason):
        """所有触发源的统一出口：先过冷却闸门，再真正发送。

        多个源（OBS 事件 / 快捷键 / 进程检测）共用一个冷却，
        这样同一次开播不会被发两遍。

        **返回值分两种含义，别再混在一起：**
            没触发（冷却期）  -> None
            触发了            -> SendResult，里面写清楚送达了几个群
        原来一律 return True，于是「一条都没发出去」和「全部发成功」在调用方
        看来一模一样。
        """
        if gate is not None:
            allowed, remain = gate.allow()
            if not allowed:
                log("触发（{}），但还在冷却期，还剩约 {} 秒，跳过本次。".format(reason, remain), "WARN")
                return None
            gate.mark()
        state["last_fire"] = time.time()
        # 开播这件事**确实发生了**，所以状态照记 —— 事件和送达是两回事，
        # 不能因为没发出去就说没开播。
        mark_live_start()
        name = detect_game()
        state["game"] = name
        result = send_to_groups(cfg, onebot, reason,
                                extra_fields={"game": name,
                                              "room_title": room_title_now(),
                                              "room_desc": room_desc_now()},
                                image=cover_image())
        state["last_send"] = result
        state["last_send_lost"] = bool(result.lost)
        return result

    def check_reminders():
        """开播后隔一阵补一条：第一波没看到的人还有机会。"""
        pool = reminder_cfg.get("after_minutes") or []
        if not reminder_cfg.get("enabled") or not pool:
            return
        if platform is None or platform.state != "live":
            return          # 只有在真的还播着的时候才提醒
        started = state["live_started"]
        if not started:
            return
        # 开播那条也算一条，所以这里留一格给它的余量
        if state["reminders_sent"] + 1 >= int(reminder_cfg.get("max_total", 3)):
            return
        elapsed = (time.time() - started) / 60.0
        for idx, minutes in enumerate(pool):
            if idx in state["reminders_done"] or elapsed < minutes:
                continue
            state["reminders_done"].add(idx)
            state["reminders_sent"] += 1
            name = detect_game() or state["game"]
            state["game"] = name
            log("已开播 {} 分钟，发送二次提醒。".format(int(minutes)))
            send_to_groups(cfg, onebot,
                           "开播 {} 分钟后的二次提醒".format(int(minutes)),
                           template=pick_from(reminder_cfg.get("templates"),
                                   reminder_cfg.get("template")),
                           extra_fields={"game": name,
                                         "room_title": room_title_now(),
                                         "room_desc": room_desc_now()},
                           at_all=bool(reminder_cfg.get("at_all", False)))
            return

    def check_game_change():
        """中途换了游戏就补一条。默认不 @ 任何人。"""
        if not game_cfg.get("enabled") or not game_cfg.get("announce_change", True):
            return
        if platform is None or platform.state != "live":
            return
        name = detect_game()
        if not name or name == state["game"]:
            state["pending_game"] = ""
            return
        now = time.time()

        # ---- 先过"确认时间"（防抖）----
        #
        # 为什么要这一步：切窗口、加载地图、开个网页，都会让窗口标题瞬间
        # 变成别的东西。直接播报的话群里会看到一串莫名其妙的游戏名。
        #
        # 这跟下面那个冷却**不是一回事**：
        #   settle   —— 这游戏是不是真的换了（管真假）
        #   cooldown —— 两条播报隔得太近没有（管密度）
        settle = float(game_cfg.get("change_settle_seconds", 20) or 0)
        if settle > 0:
            if state["pending_game"] != name:
                state["pending_game"] = name
                state["pending_since"] = now
                log("识别到《{}》，先观察 {} 秒再决定要不要播报。".format(
                    name, int(settle)))
                return
            if now - state["pending_since"] < settle:
                return

        cooldown = float(game_cfg.get("change_cooldown_minutes", 0) or 0)
        if cooldown and state["last_game_change"] and \
                now - state["last_game_change"] < cooldown * 60:
            log("换游戏了（{}），但还在播报冷却期，只记下不发送。".format(name))
            state["game"] = name
            state["pending_game"] = ""
            return
        # **先把上一个游戏存下来再覆盖。** 原来是先 `state["game"] = name`
        # 再发送，旧名字当场就没了 —— 文案里想写「从 A 换到 B」也拿不到 A。
        prev = state["game"] or ""
        log("游戏从「{}」变成「{}」，补发一条。".format(prev or "未知", name))
        state["game"] = name
        state["last_game_change"] = now
        state["pending_game"] = ""
        send_to_groups(cfg, onebot,
                       "换游戏：{} → {}".format(prev or "未知", name),
                       template=pick_from(
                           game_cfg.get("change_templates"),
                           game_cfg.get("change_template"),
                           kind="change",
                           # 认不出上一个游戏时，把提到它的句子整条排除 ——
                           # 塞个假名字会渲染出「（刚才在玩《刚才那个》）」这种废话
                           avoid=() if prev else ("prev_game",)),
                       extra_fields={"game": name,
                                     "prev_game": prev or ""},
                       at_all=False)

    engine = TriggerEngine(cfg, fire) if tg.get("on_process_start") else None

    def fire_offline(duration):
        """下播提示。走独立闸门，且默认谁都不 @。"""
        if not offline_cfg.get("enabled", True):
            log("下播提示已关闭，跳过。")
            return False
        if offline_gate is not None:
            allowed, _ = offline_gate.allow()
            if not allowed:
                log("下播提示还在冷却期，跳过。", "WARN")
                return False
            offline_gate.mark()
        state["last_offline"] = time.time()

        peak = 0
        if platform is not None:
            try:
                peak = int(getattr(platform, "peak_online", 0) or 0)
            except (TypeError, ValueError):
                peak = 0
        # peak 传空串时，模板里含 {peak} 的那一行会被自动删掉，
        # 而不是渲染成「人气最高 0」
        extra = {
            "duration": duration or "一会儿",
            "game": state.get("game") or "",
            "peak": peak if peak > 0 else "",
        }
        send_to_groups(
            cfg, onebot,
            "直播间已下播（时长 {}，人气峰值 {}）".format(
                duration or "未知", peak or "未知"),
            template=pick_from(offline_cfg.get("templates"),
                              offline_cfg.get("template"), kind="offline"),
            extra_fields=extra,
            at_all=bool(offline_cfg.get("at_all", False)))

        # 本场结束，把提醒额度收回，等下一场重新算
        state["live_started"] = 0.0
        state["reminders_done"] = set()
        state["reminders_sent"] = 0
        state["last_game_change"] = 0.0
        return True

    def manual_trigger():
        fire("手动触发（控制端口）")
        return {"ok": True, "message": "已触发一次发送，详见控制台日志"}

    def status():
        return {
            "armed": engine.armed if engine else None,
            "trigger_sources": {
                "process_start": bool(engine),
                "obs_stream": bool(tg.get("on_obs_stream")),
                "platform_live": bool(platform),
                "hotkey": tg.get("hotkey") or None,
            },
            "obs": obs.status_text() if obs else None,
            "platform": platform.status_text() if platform else None,
            "offline_message": bool(offline_cfg.get("enabled", True)),
            "hotkey_ok": hotkey.registered if hotkey else None,
            "game": state.get("game") or None,
            "peak_online": int(getattr(platform, "peak_online", 0) or 0) if platform else None,
            "reminder": {
                "enabled": bool(reminder_cfg.get("enabled")),
                "after_minutes": reminder_cfg.get("after_minutes") or [],
                "sent": state["reminders_sent"],
                "max_total": reminder_cfg.get("max_total"),
            },
            "groups": [g["group_id"] for g in cfg["groups"] if g["enabled"]],
            "last_fire": (datetime.fromtimestamp(state["last_fire"]).strftime("%Y-%m-%d %H:%M:%S")
                          if state["last_fire"] else None),
            "last_offline": (datetime.fromtimestamp(state["last_offline"]).strftime("%Y-%m-%d %H:%M:%S")
                             if state["last_offline"] else None),
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
    if triggers is not None and tg.get("on_platform_live"):
        room_id = tg.get("room_id")
        if not room_id:
            log("直播间轮询已启用，但没能确定房间号。请在 message.link 里写完整的"
                "直播间链接（如 https://live.bilibili.com/12345），或直接填 "
                "trigger.room_id。", "WARN")
        else:
            platform = triggers.PlatformWatcher(
                room_id=room_id,
                on_live=lambda: fire("直播间已开播"),
                on_offline=fire_offline,
                stop_event=stop_event,
                poll_seconds=tg.get("poll_seconds", 30),
                offline_grace=offline_cfg.get("grace_seconds", 60),
                proxy=tg.get("platform_proxy", ""),
                on_status=lambda t: log("直播间：{}".format(t)))
            platform.start()

    log("=" * 62)
    log("开始监控。按 Ctrl+C 退出。")
    log("目标群数：{}".format(len([g for g in cfg["groups"] if g["enabled"]])))
    log("触发方式：")
    if platform is not None:
        log("  · 直播间轮询 —— 房间 {}，每 {} 秒查一次（与开播软件无关）".format(
            tg.get("room_id"), int(tg.get("poll_seconds", 30))))
    if obs is not None:
        log("  · OBS 推流事件 —— 精确到「你按下开始推流」那一刻")
    if hotkey is not None:
        log("  · 全局快捷键 {}".format(tg["hotkey"].upper()))
    if engine is not None:
        log("  · 进程检测：{}".format("、".join(cfg["watch"]["processes"])))
        log("    （检查间隔 {} 秒，防抖 {} 次）".format(
            cfg["watch"]["interval_seconds"], cfg["watch"]["confirm_checks"]))
    if platform is None and obs is None and hotkey is None and engine is None:
        log("  · 无 —— 只能通过控制端口手动触发", "WARN")
    log("开播冷却 {} 分钟".format(cfg["behavior"]["cooldown_minutes"]))
    if offline_cfg.get("enabled", True):
        log("下播提示：已开启（离线确认宽限 {} 秒，{}）".format(
            int(offline_cfg.get("grace_seconds", 60)),
            "@全体成员" if offline_cfg.get("at_all") else "不 @ 任何人"))
    else:
        log("下播提示：已关闭")
    if game_cfg.get("enabled", True) and games is not None:
        idx = games.build_index()
        log("游戏识别：已开启（场景/平台里认识 {} 个 exe{}）".format(
            len(idx["by_exe"]),
            "，换游戏会补发" if game_cfg.get("announce_change", True) else ""))
    elif games is None:
        log("游戏识别：未启用（缺 games.py）", "WARN")
    else:
        log("游戏识别：已关闭")
    if cfg["message"].get("templates"):
        log("开播文案：{} 套随机轮换".format(len(cfg["message"]["templates"])))
    if cfg["message"].get("cover", True):
        log("封面小图：已开启")
    if reminder_cfg.get("enabled") and reminder_cfg.get("after_minutes"):
        log("二次提醒：开播后 {} 分钟各一次（本场最多 {} 条，含开播这条）".format(
            "、".join(str(int(m)) for m in reminder_cfg["after_minutes"]),
            reminder_cfg.get("max_total")))
    if cfg["behavior"]["dry_run"]:
        log("当前是彩排模式（dry_run=true），不会真的发消息。", "WARN")
    log("=" * 62)

    try:
        next_aux = 0.0
        while True:
            if stop_event is not None and stop_event.is_set():
                log("监控已停止。")
                break
            if engine is not None:
                engine.tick()
            # 二次提醒 / 换游戏播报：15 秒看一次就够了，不必跟着
            # 进程检测那个 5 秒的节奏跑。
            now = time.time()
            if now >= next_aux:
                next_aux = now + 15.0
                try:
                    check_reminders()
                except Exception as exc:
                    log("二次提醒出错：{}".format(exc), "ERROR")
                try:
                    check_game_change()
                except Exception as exc:
                    log("换游戏播报出错：{}".format(exc), "ERROR")
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
        # 主动放锁。不写这句其实也行（进程退出内核会释放），但显式放掉能让
        # 「停止监控之后立刻再点开始」不需要任何等待。
        lock.release()
    return 0


def cmd_send(cfg):
    send_to_groups(cfg, OneBot(cfg["onebot"]), "手动执行 send 命令")
    return 0


def _preview_extras(cfg):
    """给彩排用：把 {game} 和封面也一并算出来，看到的就是真要发的东西。"""
    extra = {}
    game_cfg = cfg.get("game") or {}
    if game_cfg.get("enabled", True) and games is not None:
        try:
            name = (games.detect(cfg) or {}).get("name") or ""
        except Exception as exc:
            name = ""
            log("游戏识别失败：{}".format(exc), "WARN")
        extra["game"] = name
        if name:
            log("识别到当前游戏：{}".format(name))
        else:
            log("没识别出当前游戏（通知里就不写这一行）")

    image = ""
    room_id = (cfg.get("trigger") or {}).get("room_id")
    if cfg["message"].get("cover", True) and room_id and triggers is not None:
        try:
            size = cfg["message"].get("cover_size") or [200, 112]
            room = triggers.BilibiliRoom(
                room_id, proxy=(cfg.get("trigger") or {}).get("platform_proxy", ""))
            _status, data = room.fetch()
            image = triggers.BilibiliRoom.cover_url(data, size[0], size[1])
            log("封面：{}".format(image or "（接口没给封面）"))
        except Exception as exc:
            log("取封面失败（不影响彩排）：{}".format(exc), "WARN")
    return extra, image


def cmd_test(cfg):
    """彩排：把四类消息各渲染一条出来看。**一条都不发。**

    以前只预览了开播那条和二次提醒 —— **下播那条一次都没露过面**，
    等于只能等真的下播才知道长什么样。这里补齐。
    """
    log("彩排模式：下面展示将要发送的内容，不会真的发到群里。")
    extra, image = _preview_extras(cfg)

    # 走跟界面预览同一个函数，两边看到的东西必须一致
    for name, text in preview_messages(cfg):
        log("")
        log("【{}】".format(name))
        for line in text.split("\n"):
            log("    " + line)

    # 开播那条还带了封面图，单独说一句
    if image:
        log("")
        log("开播通知还会附一张直播间封面：{}".format(image))
    else:
        log("")
        log("（这次没拿到直播间封面，开播那条就只发文字。）")

    log("")
    log("内容没问题的话，执行  python live_notify.py send  真正发送一次开播通知。")
    log("想只发给自己看：界面「消息与设置」里的私聊测试。")
    return 0


def cmd_check(cfg):
    problems = []

    log("=" * 62)
    log("{} v{} 自检".format(DISPLAY_NAME, VERSION))
    log("=" * 62)
    log("Python：{}".format(sys.version.split()[0]))
    log("配置文件：{}".format(cfg["_path"]))
    log("NapCat 地址：{}".format(cfg["onebot"]["base_url"]))

    # 1. 群配置
    log("\n[1/6] 群配置")
    enabled = [g for g in cfg["groups"] if g["enabled"]]
    for g in cfg["groups"]:
        mode = "@全体成员" if g["at_all"] else (
            "@{}".format("、".join(str(x) for x in g["at_list"])) if g["at_list"] else "不@任何人")
        log("  {} [{}] {}{}".format(g["group_id"], "启用" if g["enabled"] else "已禁用", mode,
                                    "（{}）".format(g["note"]) if g["note"] else ""))
    if not enabled:
        problems.append("没有任何启用的群，不会发送任何消息。")

    # 2. 进程监控
    log("\n[2/6] 进程监控")
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
    log("\n[3/6] NapCat 连通性")
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
    log("\n[4/6] 群权限检查（@全体成员 只有群主/管理员才有效）")
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

    # 5. 模板占位符
    #
    # 这条排在这里不是凑数：模板里拼错一个字母，format() 会失败、被兜底吞掉，
    # 最后把 "{titel}" **原样发进 QQ 群**。群友看得见，日志里却只有一行 WARN。
    # 这是整个程序唯一会「对外出丑」的失败，所以必须让用户在开播前就看到。
    log("\n[5/6] 模板占位符")
    tpl_problems = cfg.get("_template_problems") or []
    if tpl_problems:
        for label, token in tpl_problems:
            problems.append("{}里的 {{{}}} 不是可用占位符，发出去会变成花括号。".format(
                label, token))
            log("  [X] {}里的 {{{}}} 不认识".format(label, token), "WARN")
        log("  可用占位符只有：{}".format(
            "、".join("{" + k + "}" for k in sorted(ALLOWED_PLACEHOLDERS))))
        log("  这些花括号在发送前会被剔除，但那句话也就没了 —— 建议改掉。")
    else:
        log("  [OK] 所有模板里的占位符都认识。")

    # 6. 配置安全检查
    #
    # 专门抓「配置看着没问题、实际会出事」的那些值。最典型的是 message.link
    # 还留着示例里的 YOUR_ROOM_ID —— 那玩意儿会被原样发进每一个群。
    #
    # **这里绝不打印任何密钥的值**，只说有没有问题、怎么改。
    log("\n[6/6] 配置安全检查")
    msg_cfg = cfg.get("message") or {}
    link = str(msg_cfg.get("link") or "").strip()
    if not link:
        problems.append("message.link 是空的，通知里会缺一条链接。")
        log("  [X] message.link 没填", "WARN")
    elif ("YOUR_ROOM_ID" in link.upper()
          or "example.com" in link.lower() or "example.org" in link.lower()):
        problems.append("message.link 还是示例值，发出去群友点不开。")
        log("  [X] message.link 还是示例值 —— 发出去群友点不开", "WARN")
        log("      改成你自己的直播间地址，例如 https://live.bilibili.com/123456")
    else:
        log("  [OK] message.link 已填。")

    ctrl = cfg.get("control") or {}
    if ctrl.get("enabled") and not ctrl.get("token"):
        problems.append("控制端口开着但没设 token，本机任何程序都能触发发送。")
        log("  [X] 控制端口开着却没设 token", "WARN")
        log("      本机任何程序都能调 /trigger 让群里收到通知。"
            "要么设一个 token，要么把 control.enabled 关掉。")
    elif ctrl.get("enabled") and len(str(ctrl.get("token") or "")) < 16:
        problems.append("控制端口的 token 太短，容易被猜。")
        log("  [X] 控制端口 token 太短（少于 16 位）", "WARN")
    elif ctrl.get("enabled"):
        log("  [OK] 控制端口已设 token。")
    else:
        log("  [-] 控制端口未启用。")

    if (cfg.get("behavior") or {}).get("dry_run"):
        problems.append("dry_run 开着，程序只会打印、不会真的发出去。")
        log("  [X] dry_run 开着 —— 你以为在发，其实一条都没发", "WARN")

    tg = cfg.get("trigger") or {}
    if tg.get("on_platform_live") and not tg.get("room_id"):
        problems.append("勾了「直播间轮询」但没填房间号，这一路不会生效。")
        log("  [X] 勾了直播间轮询却没填 room_id", "WARN")
    elif tg.get("on_platform_live"):
        log("  [OK] 直播间轮询配好了。")

    if not any(g.get("enabled") for g in cfg["groups"]):
        log("  [X] 一个启用的群都没有。", "WARN")
    else:
        log("  [OK] 群配置没问题。")

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
        description="大肥鱼直播姬 —— 开播自动往 QQ 群发 @全体成员 通知",
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

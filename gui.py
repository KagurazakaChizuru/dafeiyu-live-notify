#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大肥鱼直播姬 —— 图形界面
==========================

设计目标：傻瓜化。整个程序只有一个按钮。

    点「开始直播通知」 -> 自动启动 NapCat、等它就绪、开始监控
    点「停止」         -> 停止监控、关闭 NapCat、把你的 QQ 还给你

NapCat 的启动和关闭完全由本程序负责，不需要手动开任何东西。
其余细节（通知群、消息内容、运行日志）放在下方标签页里，不点开就不用管。

只依赖 Python 自带的 tkinter。
"""

import json
import math
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime

import ctypes
import tempfile
from ctypes import wintypes

import tkinter as tk
from tkinter import ttk, messagebox
from tkinter import font as tkfont

try:
    import qr as core_qr             # 二维码（纯标准库）：扫码登录要画一张码
except ImportError:                  # 缺文件时登录按钮会说清楚，不影响别的
    core_qr = None

try:
    import groupchat as core_chat     # 群聊：@机器人 说话 / 记提醒
except ImportError:
    core_chat = None


def _resolve_data_dir():
    """确定「程序数据目录」（config.json / napcat / qq-napcat / logs 所在处）。

    打包成 exe 之后两者不是一回事：
      · 代码（gui.py、live_notify.py）被打进 exe，运行时解包到临时目录，
        __file__ 指向的是那里；
      · 而配置、NapCat、QQ 副本这些大件仍然放在 exe 旁边的 app\\ 下。
    所以数据目录必须相对 **exe 自身** 解析，绝不能用 __file__。
    """
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "app")
    return os.path.dirname(os.path.abspath(__file__))


HERE = _resolve_data_dir()

# 未打包时从磁盘导入 live_notify；打包后它已在 exe 内，无需再改 sys.path
if not getattr(sys, "frozen", False) and HERE not in sys.path:
    sys.path.insert(0, HERE)

import live_notify as core                                    # noqa: E402
import tray                     # 右下角托盘图标（纯 ctypes，零依赖）  # noqa: E402

try:
    import games as core_games                                 # noqa: E402
except ImportError:                # 缺文件时界面只是没有"测试识别"按钮
    core_games = None

CONFIG_PATH = os.path.join(HERE, "config.json")
ACCOUNT_FILE = os.path.join(HERE, "_account.txt")
NAPCAT_DIR = os.path.join(HERE, "napcat")
ERROR_LOG = os.path.join(HERE, "gui-error.log")

MAX_LOG_LINES = 1500
#: 界面字体降级链。**按顺序取第一个真实存在的。**
#:
#: 系统里没有中文圆体（这是 Windows 的常态），实测比对下来 OPPOSans R 最接近
#: 「可爱但不花哨」：笔画末端更圆、字面更亲和。微软雅黑偏公文，Noto Light
#: 是清冷不是可爱。
#:
#: 但它不是 Windows 自带的 —— 所以必须有后路。**字体缺失不会报错，只会把
#: 界面变成一堆方块**，这条链就是防这个的。
FONT_CANDIDATES = ("OPPOSans R", "Noto Sans SC", "Microsoft YaHei UI")
FONT = FONT_CANDIDATES[0]
FONT_FALLBACK = FONT_CANDIDATES[-1]

# 日志用的字体。**绝不能用 Consolas 这类纯西文字体**：Tk 遇到字体里没有的
# 字形会走系统回退，而回退出来的中文是画在一小块浅色底上的——深色日志框里
# 会一条条冒白补丁，看起来像乱码。优先中文等宽，没有就退回界面字体。
LOG_FONT_CANDIDATES = ("Sarasa Mono SC", "Sarasa Mono HC", "NSimSun",
                       "SimSun", "Microsoft YaHei UI", FONT)


def pick_log_font():
    try:
        available = set(tkfont.families())
    except tk.TclError:
        return FONT
    for name in LOG_FONT_CANDIDATES:
        if name in available:
            return name
    return FONT

# --------------------------------------------------------------------------
#  配色 —— 用 Apple 的系统语义色
# --------------------------------------------------------------------------
#  之前几版的毛病是「拿背景和按钮抢戏」：冷蓝灰的工程软件味，或者一整块
#  高饱和紫渐变加一个 92px 的巨大按钮。参考 Apple Music 之后落到三条：
#
#    1. 大面积是**系统灰**（systemGroupedBackground），不带任何色相
#    2. 主色只出现在真正要引导视线的地方，而且用 systemBlue 这个系统本色，
#       不自己调一个"更有设计感"的蓝
#    3. 层次靠 systemBackground 的**亮度差**表达：窗口底 → 卡片 → 浮层，
#       越靠上的层越亮。深色模式就照 Apple 的做法：底是纯黑，卡片 #1C1C1E
#
#  语义色一律用 Apple 原值（systemBlue / Green / Orange / Red），
#  深色模式用它们的 dark 变体 —— 这也是 Apple 自己的规矩：
#  同一个语义色在深浅两套里是两个不同的值，不是一个值加透明度。
  # --------------------------------------------------------------------------
  #  配色 —— Fluent 的分层骨架 + 降对比
  # --------------------------------------------------------------------------
  #  这是第四版配色了，前几版的教训：
  #    · 冷蓝灰 → 像工程软件
  #    · 高饱和紫渐变 + 巨大按钮 → 吵
  #    · Apple Music 那一版结构对了，但**颜色照搬了 Apple**
  #
  #  最后一条是问题所在：Apple 深色模式的底是**纯黑 #000000**、浅色卡片是
  #  **纯白 #FFFFFF**，纯黑配纯白对比过强 —— 那正是「刺眼」的来源。而 Windows
  #  走的是另一条路（官方叫 Fluent）：底色 #202020、卡片 #2B2B2B，靠**亮度差**
  #  分层，不用极值。
  #
  #  所以这一版：**结构照 Fluent，颜色在 Fluent 基础上再降一档对比** ——
  #  正文不用纯黑纯白，次要文字统一压到中灰，分隔线淡到几乎看不见。
  #
  #  动效的时长和缓动也不是我编的，见 MOTION_* 和 ease_* 的注释。
# --------------------------------------------------------------------------
#  配色 —— Fluent 的分层骨架 + 降对比
# --------------------------------------------------------------------------
#  这是第四版配色了，前几版的教训：
#    · 冷蓝灰 → 像工程软件
#    · 高饱和紫渐变 + 巨大按钮 → 吵
#    · Apple Music 那一版结构对了，但**颜色照搬了 Apple**
#
#  最后一条是问题所在：Apple 深色模式的底是**纯黑 #000000**、浅色卡片是
#  **纯白 #FFFFFF**，纯黑配纯白对比过强 —— 那正是「刺眼」的来源。而 Windows
#  走的是另一条路（官方叫 Fluent）：底色 #202020、卡片 #2B2B2B，靠**亮度差**
#  分层，不用极值。
#
#  所以这一版：**结构照 Fluent，颜色在 Fluent 基础上再降一档对比** ——
#  正文不用纯黑纯白，次要文字统一压到中灰，分隔线淡到几乎看不见。
#
#  动效的时长和缓动也不是我编的，见 MOTION_* 和 ease_* 的注释。
# --------------------------------------------------------------------------

THEMES = {
    "light": {
        "BG":        "#EDF1F6",   # 页面底色：带一点冷调，让白卡片浮起来
        "HEADER_TOP": "#E3F1FB",  # 顶部那层极淡的天依蓝，往 BG 渐隐
        "CARD":      "#FFFFFF",   # 卡片：比页面浮一档
        "SUNKEN":    "#F2F2F2",   # 次级面：比底暗一档
        "BORDER":    "#E5E5E5",   # 分隔线，约 6% 黑
        "TEXT":      "#1A1A1A",   # 不用纯黑，纯黑在白底上太硬
        "MUTED":     "#5F5F5F",   # Fluent 的 secondary text 档位
        "PRIMARY":   "#2B8FC7",   # 天依蓝加深版：压白字 3.59:1，够大字用   # 系统蓝降饱和一档
        "PRIMARY_D": "#2477A8",
        "PRIMARY_S": "#E6F4FC",
        "ACCENT":    "#66CCFF",   # 天依蓝本体，只做装饰：压深字 9.65:1   # 图标的琥珀色，压暗到能在白底读
        "OK":        "#0F7B0F",
        "WARN":      "#9D5D00",
        "BAD":       "#C42B1C",   # Fluent light 的 system red
        "OK_S":      "#E6F4EA",
        "WARN_S":    "#FBF1E0",
        "BAD_S":     "#FBEAE8",
          "SURFACE":   "#FFFFFF",   # 抬升面：底栏、按钮、表格
        "LOG_BG":    "#1F1F1F",
        "LOG_FG":    "#D6D6D6",
        "LOG_BAR":   "#3A3A3A",
    },
    "dark": {
        "BG":        "#181818",   # 页面底色
        "HEADER_TOP": "#15262F",  # 深色下的蓝调，比浅色那层更暗更闷
        "CARD":      "#242424",   # 卡片：比页面浮一档
        "SUNKEN":    "#191919",   # 次级面暗一档
        "BORDER":    "#363636",
        "TEXT":      "#EDEDED",   # 不用纯白，纯白在深底上发炫
        "MUTED":     "#A0A0A0",
        "PRIMARY":   "#66CCFF",   # 天依蓝本体：在深底上有 9.14:1，非常好看   # 深色下提亮，否则发闷
        "PRIMARY_D": "#4FB8E8",
        "PRIMARY_S": "#16303F",
        "ACCENT":    "#66CCFF",
        "OK":        "#6CCB8F",
        "WARN":      "#E0A33D",
        "BAD":       "#FF99A4",   # Fluent dark 的 system red
        "OK_S":      "#1B2A21",
        "WARN_S":    "#2A2418",
        "BAD_S":     "#2E1D1E",
          "SURFACE":   "#2A2A2A",   # 抬升面（深色）
        "LOG_BG":    "#191919",
        "LOG_FG":    "#CFCFCF",
        "LOG_BAR":   "#333333",
    },
}

# --------------------------------------------------------------------------
#  动效
# --------------------------------------------------------------------------
#  时长和缓动抄自微软官方：
#      https://learn.microsoft.com/windows/apps/design/motion/timing-and-easing
#
#      ControlNormalAnimationDuration   250ms   常规控件
#      ControlFastAnimationDuration     167ms   悬停、聚焦
#      ControlFasterAnimationDuration    83ms   按下等微反馈
#
#  缓动只有两条，而且**方向不能反**：
#      进入用 cubic-bezier(0, 0, 0, 1) —— 快进慢停
#      退出用 cubic-bezier(1, 0, 1, 1) —— 慢起快走
#
#  tkinter 没有 CSS transition，只能「每帧算一个新值 + after() 排下一帧」，
#  所以下面用幂函数逼近那两条贝塞尔曲线。真正的三次贝塞尔要解方程，而这个
#  逼近在 16ms 一帧的粒度下肉眼分不出来。
MOTION_NORMAL = 250
MOTION_FAST = 167
MOTION_FASTER = 83
FRAME_MS = 16                  # ≈60fps


def ease_out(t):
    """快进慢停，对应 cubic-bezier(0, 0, 0, 1)。用于**进入**。"""
    return 1 - (1 - t) ** 3


def ease_in(t):
    """慢起快走，对应 cubic-bezier(1, 0, 1, 1)。用于**退出**。"""
    return t ** 3

THEME_NAMES = ("light", "dark")
THEME = "light"


def apply_theme(name):
    """把选中的主题写进模块全局，之后创建的控件就用这些颜色。

    颜色是在**创建控件时**写死进去的，所以换主题必须重建界面，没有
    「重新着色」这条路 —— 见 App.rebuild_ui()。启动时则在建界面**之前**
    先调用这里，否则会先按默认主题闪一下。
    """
    global THEME, BG, CARD, SUNKEN, BORDER, SURFACE, HEADER_TOP, TEXT, MUTED
    global PRIMARY, PRIMARY_D, PRIMARY_S, ACCENT
    global OK_COLOR, WARN, BAD_COLOR, OK_S, WARN_S, BAD_S
    global LOG_BG, LOG_FG, LOG_BAR
    global HEAD_TEXT, HEAD_DIM, HEAD_OK, HEAD_BAD, HEAD_WARN
    global BLUE, BLUE_DARK, RED, RED_DARK

    THEME = name if name in THEMES else "light"
    t = THEMES[THEME]

    BG, CARD, SUNKEN, BORDER = t["BG"], t["CARD"], t["SUNKEN"], t["BORDER"]
    SURFACE = t["SURFACE"]
    HEADER_TOP = t["HEADER_TOP"]
    TEXT, MUTED = t["TEXT"], t["MUTED"]
    PRIMARY, PRIMARY_D, PRIMARY_S = t["PRIMARY"], t["PRIMARY_D"], t["PRIMARY_S"]
    ACCENT = t["ACCENT"]
    OK_COLOR, WARN, BAD_COLOR = t["OK"], t["WARN"], t["BAD"]
    OK_S, WARN_S, BAD_S = t["OK_S"], t["WARN_S"], t["BAD_S"]
    LOG_BG, LOG_FG, LOG_BAR = t["LOG_BG"], t["LOG_FG"], t["LOG_BAR"]

    # 头部不再是深色块了，直接用主题自身的文字色
    HEAD_TEXT = TEXT
    HEAD_DIM = MUTED
    HEAD_OK, HEAD_BAD, HEAD_WARN = t["OK"], t["BAD"], t["WARN"]

    # 兼容旧名字（别的地方还在用）
    BLUE, BLUE_DARK = PRIMARY, PRIMARY_D
    RED, RED_DARK = BAD_COLOR, t["BAD"]
    return t


apply_theme("light")

ROLE_CN = {"owner": "群主", "admin": "管理员", "member": "普通成员"}

STATE_IDLE = "idle"
STATE_WORKING = "working"
STATE_RUNNING = "running"


# --------------------------------------------------------------------------
#  文案库
# --------------------------------------------------------------------------
#  用途：消息与设置页里点一下就把某套文案追加进「开播文案」框，攒够几套之后
#  程序会**每次开播随机挑一套**（用单独一行 --- 分隔，见 split_templates）。
#
#  写这些文案时守三条：
#    · 每条都短，群友扫一眼就完了，没人读长文
#    · 风格刻意拉开（直白 / 卖萌 / 中二 / 自嘲 / 简洁），不然挑来挑去都一个味
#    · 必须留 {link}，那是通知里唯一有用的信息
TEMPLATE_LIBRARY = {
    "live": [
        ("群文案+标题+公告", "🔴 {hook}\n\n{room_title}\n{room_desc}\n正在玩《{game}》\n{link}"),
        ("群文案标题公告", "🔴 开播了\n\n{hook}\n{room_title}\n{room_desc}\n正在玩《{game}》\n{link}"),
        ("破折号版", "🔴 开播了 —— {hook}\n\n{room_title}\n{room_desc}\n{link}"),
        ("群文案+标题", "🔴 {hook}\n\n{room_title}\n正在玩《{game}》\n{link}"),
        ("群文案+公告", "🔴 {hook}\n\n{room_desc}\n正在玩《{game}》\n{link}"),
        ("最简", "🔴 开播了\n{link}"),
        ("带标题和游戏", "🔴 开播了！\n\n{room_title}\n正在玩《{game}》\n{link}"),
        ("正经通知", "📢 已开播\n\n{room_title}\n{room_desc}\n正在玩《{game}》\n{link}"),
        ("通知+标题", "📢 开播通知\n\n{room_title}\n正在玩《{game}》\n{link}"),
        ("硬核", "▶ 直播已开始\n{room_title}\n《{game}》\n{link}"),
        ("游戏开场", "🎮 上号了，正在玩《{game}》\n\n{room_title}\n{link}"),
        ("游戏+群文案", "🎮 {hook}\n\n正在玩《{game}》\n{link}"),
        ("零食", "🍿 备好零食，开播了\n\n{room_title}\n正在玩《{game}》\n{link}"),
        ("零食+群文案", "🍿 {hook}\n\n{room_title}\n{room_desc}\n{link}"),
        ("卖惨", "🥺 播了半小时，房间还是空的\n\n{room_title}\n{link}\n来个人陪陪我"),
        ("卖惨+游戏", "🥺 人好少啊，来个活人\n\n{room_title}\n正在玩《{game}》\n{link}"),
        ("中二", "⚔️ 战场的门已经开了\n\n{room_title}\n正在玩《{game}》\n{link}"),
        ("中二+群文案", "⚔️ {hook}\n\n《{game}》 · {room_title}\n{link}"),
        ("自嘲", "🔴 又到了丢人现眼的时间\n\n{room_title}\n正在玩《{game}》\n{link}"),
        ("宠粉", "💗 想你们了，所以我开播了\n\n{room_title}\n正在玩《{game}》\n{link}"),
        ("闪亮", "✨ {hook}\n\n{room_title}\n{room_desc}\n正在玩《{game}》\n{link}"),
        ("摸鱼", "🐟 摸鱼的可以来看了\n\n{room_title}\n{room_desc}\n正在玩《{game}》\n{link}"),
        ("提醒式", "🔔 你关注的直播间亮了\n\n{room_title}\n正在玩《{game}》\n{link}"),
        ("水花", "🌊 {hook}\n\n{room_title}\n正在玩《{game}》\n{link}"),
    ],
    "offline": [
        ("简短", "🌙 下播了，谢谢陪播\n今天播了 {duration}"),
        ("带峰值", "🌙 下播啦\n\n今天播了 {duration}\n人气最高 {peak}\n谢谢大家"),
        ("预告下次", "🌙 今天就到这\n\n播了 {duration}，峰值 {peak}\n明天见"),
        ("卖惨", "🥺 播了 {duration}，人还是不多\n谢谢留下来的各位"),
        ("中二", "🌙 战场暂时关闭\n\n本次 {duration}\n最后在玩《{game}》"),
        ("感恩", "💗 谢谢陪我的每一个人\n\n播了 {duration}，峰值 {peak}"),
        ("关机", "🛌 关机睡觉，明天见\n\n今天播了 {duration}"),
        ("吃饭", "🍜 下播吃饭去了\n\n播了 {duration}，峰值 {peak}"),
        ("鱼塘", "🐟 鱼塘关门\n\n今天游了 {duration}\n水下安静了"),
        ("节目结束", "🎬 今天的节目到此结束\n\n{duration}，峰值 {peak}\n谢谢收看"),
        ("电量耗尽", "😴 主播电量耗尽\n\n硬撑了 {duration}\n充电去了，明天见"),
        ("收工", "🌙 收工\n\n{duration}\n明天同一时间，不见不散"),
        ("走了走了", "👋 走了走了\n\n今天 {duration}，峰值 {peak}\n晚安"),
        ("手柄放下", "🎮 手柄放下\n\n播了 {duration}\n最后在玩《{game}》"),
    ],
    "reminder": [
        ("简短", "还在播～\n{link}"),
        ("带游戏", "还在播，正在玩《{game}》\n{link}"),
        ("催人", "都播了一阵了，还不来看看？\n\n{title}\n正在玩《{game}》\n{link}"),
        ("自嘲", "还没下播，人少得可怜\n\n{title}\n{link}"),
        ("正经", "直播仍在继续\n\n{title}\n正在玩《{game}》\n{link}"),
        ("深夜", "这个点还开着的应该不多了\n\n正在玩《{game}》\n{link}"),
    ],
    "change": [
        ("前后对照", "🔄 换游戏了\n\n《{prev_game}》 → 《{game}》"),
        ("直白", "🔄 不玩《{prev_game}》了，改打《{game}》"),
        ("中二", "🎯 目标已切换：《{prev_game}》 → 《{game}》\n{link}"),
        ("随性", "🎮 换个口味，现在打《{game}》\n（刚才在玩《{prev_game}》）"),
        ("简短", "🔄 换游戏了，现在打《{game}》\n{link}"),
        ("无上一局", "🎮 续上，现在打《{game}》\n{link}"),
    ],
    "video": [
        ("更新了", "🔔 {up} 更新了，去看看\n\n{title}\n{link}"),
        ("新片出锅", "🆕 新片出锅\n\n{up} · {title}\n{link}"),
        ("进来坐会儿", "🍿 有新的了，进来坐会儿\n\n{title}\n{link}"),
        ("别刷了", "👀 别刷了，看这个\n\n{title}\n{link}"),
        ("刚更新", "🔥 {up} 刚更新\n\n{title}\n{link}"),
        ("新的一期", "📼 新的一期上了\n\n{title}\n{up}\n{link}"),
        ("趁热看", "🎞 更新了，趁热看\n\n{title}\n{link}"),
        ("最简", "📢 投稿提醒\n\n{up} · {title}\n{link}"),
    ],
    "dynamic": [
        ("发了条动态", "💬 {up} 发了条动态\n\n{text}\n{link}"),
        ("冒泡了", "📣 {up} 冒泡了\n\n{text}\n{link}"),
        ("刚说话", "👀 {up} 刚说话\n\n{text}\n{link}"),
        ("有新动静", "🫧 {up} 那边有新动静\n\n{text}\n{link}"),
        ("冒号式", "📝 {up}：\n\n{text}\n{link}"),
        ("催一下", "💬 快看，{up} 更新动态了\n\n{text}\n{link}"),
    ],
}


# --------------------------------------------------------------------------
#  界面小工具
# --------------------------------------------------------------------------

class RoundedCard(tk.Frame):
    """圆角卡片：内容摆在一张抗锯齿的圆角底图上。

    为什么自绘而不用 ttk.Labelframe：clam 主题下它自带立体边和虚线焦点框，
    改不干净 —— 跟当初弃用 ttk.Notebook 是同一个理由。

    底图复用按钮那套抗锯齿圆角（覆盖率烘焙进图片），所以边缘没有台阶。
    尺寸变了要重画，但卡片极少改变尺寸，代价可以忽略。
    """

    RADIUS = 16

    def __init__(self, parent, fill=None, background=None, padx=18, pady=16):
        bg = background or BG
        super().__init__(parent, background=bg)
        self._fill = fill or CARD
        self._bg = bg
        self._padx = padx
        self._pady = pady
        self._img = None
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0,
                                background=bg, takefocus=0)
        self.canvas.pack(fill="both", expand=True)
        # 内容区底色必须跟卡片一致，否则会露出一块方块
        self.body = tk.Frame(self.canvas, background=self._fill)
        self._win = self.canvas.create_window(padx, pady, anchor="nw",
                                              window=self.body)
        # 绑防抖版：拖动时别每像素重画
        self.bind("<Configure>", self._schedule_redraw)
        self.body.bind("<Configure>", self._schedule_redraw)
        self._redraw_job = None

    #: 拖动窗口时，停下来多久才真的重画卡片。
    #: 每像素都重画的话，十几张卡片加起来一帧就是几百毫秒 —— 拖起来像卡住。
    REDRAW_DELAY_MS = 80

    def _schedule_redraw(self, _event=None):
        """防抖：拖窗口时不要每像素都重画。

        跟头部渐变用的是同一个思路（_head_resized）。拖动过程中旧的圆角图
        先撑着，停下 80ms 再画一次 —— 视觉上完全看不出来，手感差别巨大。
        """
        job = getattr(self, "_redraw_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        self._redraw_job = self.after(self.REDRAW_DELAY_MS, self._redraw)

    def _redraw(self, _event=None):
        self._redraw_job = None
        w = self.winfo_width()
        h = self.body.winfo_reqheight() + self._pady * 2
        if w < 8 or h < 8:
            return
        self.canvas.configure(height=h)
        try:
            img = button_image(w, h, self.RADIUS, self._fill, self._bg)
        except Exception:
            return
        self._img = img                      # 引用要留住，被 GC 就白画了
        self.canvas.delete("bg")
        self.canvas.create_image(0, 0, anchor="nw", image=img, tags="bg")
        self.canvas.tag_lower("bg")


def make_card(parent, title=None, padx=18, pady=16, surface=False):
    """一个圆角卡片分组。返回 (卡片, 内容区) —— 兼容旧调用写法。

    早期这里是无边框的纯留白分组（学 FluentTerminal 的设置页）；二次元化之后
    改回卡片：**页面底色沉一档、卡片浮一档**，分组靠层次而不是靠线。
    """
    card = RoundedCard(parent, padx=padx, pady=pady)
    if title:
        row = tk.Frame(card.body, background=CARD)
        row.pack(anchor="w", fill="x", pady=(0, 14))
        # 标题前一小段天依蓝，跟页头那道短线呼应。装饰只占 4px，不抢字。
        bar = tk.Canvas(row, width=4, height=15, background=CARD,
                        highlightthickness=0, bd=0, takefocus=0)
        bar.pack(side="left", pady=(2, 0))
        bar.create_rectangle(0, 0, 4, 15, fill=ACCENT, outline="")
        tk.Label(row, text=title, background=CARD, foreground=TEXT,
                 font=(FONT, 11, "bold")).pack(side="left", padx=(9, 0))
    return card, card.body


def page_title(parent, text, hint=""):
    """标签页大标题。参考 FluentTerminal：常规字重，左对齐，下面留白。"""
    box = tk.Frame(parent, background=BG)
    box.pack(fill="x", pady=(0, 20))
    tk.Label(box, text=text, background=BG, foreground=TEXT,
             font=(FONT, 20)).pack(anchor="w")
    if hint:
        tk.Label(box, text=hint, background=BG, foreground=MUTED,
                 font=(FONT, 9), anchor="w", justify="left").pack(
            anchor="w", pady=(4, 0))
    return box


def toggle_row(parent, variable, text, pady=(0, 0), grid=None,
               background=None, font=None, command=None):
    """一行「胶囊开关 + 说明文字」。

    布局跟以前 tk.Checkbutton 那会儿一样（左开关、右文字），但控件换成了
    Windows 11 那种胶囊滑块 —— 那是整个界面里最一眼可辨的 Windows 元素，
    方框打勾会立刻显得像十年前的软件。
    """
    # 默认 CARD 不是 BG：这个函数**全都用在卡片里**，默认给页面色的话，
    # 每一行开关都会在白卡片上拖一条灰带。
    bg = background or CARD
    box = tk.Frame(parent, background=bg)
    if grid:
        box.grid(row=grid[0], column=grid[1], sticky="w", pady=pady)
    else:
        box.pack(anchor="w", fill="x", pady=pady)
    ToggleSwitch(box, variable, background=bg, command=command).pack(
        side="left", pady=(1, 0))
    tk.Label(box, text=text, background=bg, foreground=TEXT,
             font=font or (FONT, 9), anchor="w", justify="left",
             wraplength=690).pack(side="left", padx=(12, 0))
    return box


def card_hint(parent, text, wraplength=790, indent=0, pady=(6, 0)):
    """卡片里的灰色说明文字。"""
    lbl = tk.Label(parent, text=text, background=CARD, foreground=MUTED,
                   justify="left", anchor="w", wraplength=wraplength,
                   font=(FONT, 9))
    lbl.pack(anchor="w", padx=(indent, 0), pady=pady)
    return lbl


#: 文案框里用单独一行 ``---`` 分隔多套文案。
_TEMPLATE_SEP = re.compile(r"^\s*-{3,}\s*$", re.M)


def split_templates(text):
    """把「多套文案」文本框切成列表。

    一套文案可以有好几行，所以分隔符必须独占一行，不能按换行切。
    """
    blocks = [b.strip("\n") for b in _TEMPLATE_SEP.split(text or "")]
    return [b for b in blocks if b.strip()]


def join_templates(cfg_message):
    """反向：把配置里的模板拼回文本框内容。"""
    pool = cfg_message.get("templates") or []
    if pool:
        return "\n---\n".join(pool)
    return cfg_message.get("template") or ""


class ScrollFrame(tk.Frame):
    """可以竖向滚动的页面容器。

    设置项天生就比一屏高。窗口只有 760 高，内容再多就会被切掉——
    而被切掉的部分**没有任何提示**，用户只会觉得「这软件怎么少了几项」。
    所以每个设置页都套一层这个，内容矮的时候滚动条自动隐形。
    """

    def __init__(self, parent, background=None, padx=12, pady=12):
        # 默认值**不能**写 background=BG：那是在模块导入那一刻求值的，会被
        # 永久烤成当时那套主题的颜色，apply_theme() 之后再改全局量也追不回来。
        background = background or BG
        super().__init__(parent, background=background)
        self.canvas = tk.Canvas(self, background=background, bd=0,
                                highlightthickness=0, takefocus=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical",
                                  command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.body = tk.Frame(self.canvas, background=background,
                             padx=padx, pady=pady)
        self._win = self.canvas.create_window((0, 0), window=self.body,
                                              anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)
        # 滚轮仍然靠命中判断而不是 Enter/Leave（指针滑到卡片上时父容器会收到
        # Leave，那种写法滚一半就断），但**不再用 bind_all**：
        # bind_all 挂的是解释器级的 "all" 绑签，控件销毁**不会**解除。换一次
        # 主题（rebuild_ui）就多留一条死回调，下一次滚动撞 `bad window path
        # name`，经 on_error 弹一个模态框 —— 实测每重建一次泄漏四条，越积越多。
        # 绑在顶层窗口上等效（bindtags 会把事件往上传），却能随控件一起解绑。
        self._top = self.winfo_toplevel()
        self._wheel_id = self._top.bind("<MouseWheel>", self._on_wheel, add="+")

    def destroy(self):
        """先解绑滚轮，再销毁。

        bind_all 时代那条死绑定就是这么来的：控件没了，绑签还在。现在绑在
        顶层窗口上，必须**显式**撤，否则一样会留下悬空回调。
        """
        try:
            self._top.unbind("<MouseWheel>", self._wheel_id)
        except tk.TclError:
            pass
        super().destroy()

    def _on_body(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._sync_bar()

    def _on_canvas(self, event):
        # 让内容跟着窗口一起变宽，否则拉大窗口右边会留白
        self.canvas.itemconfigure(self._win, width=event.width)
        self._sync_bar()

    def _sync_bar(self):
        """内容装得下就把滚动条收起来。"""
        try:
            need = self.body.winfo_reqheight() > self.canvas.winfo_height()
        except tk.TclError:
            return
        shown = bool(self.vbar.winfo_ismapped())
        if need and not shown:
            self.vbar.pack(side="right", fill="y", before=self.canvas)
        elif not need and shown:
            self.vbar.pack_forget()

    def _on_wheel(self, event):
        try:
            if self.body.winfo_reqheight() <= self.canvas.winfo_height():
                return
        except tk.TclError:
            # 控件已销毁。解绑是显式做的，正常不该走到这里 —— 兜一手，
            # 免得一个滚轮事件把整条 bind 链断掉。
            return
        # 只有指针真的停在本页上才滚，否则切到别的标签页也会跟着动
        node = self.winfo_containing(event.x_root, event.y_root)
        while node is not None:
            if node is self:
                break
            node = getattr(node, "master", None)
        else:
            return
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")


def mix(c1, c2, t):
    """两个 #rrggbb 之间取插值。t=0 返回 c1，t=1 返回 c2。"""
    a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
    return "#{:02x}{:02x}{:02x}".format(
        *[int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3)])


class _BMIH(ctypes.Structure):
    """BITMAPINFOHEADER。只想用它的前 40 字节，字段照规格抄。"""
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BMI(ctypes.Structure):
    _fields_ = [("bmiHeader", _BMIH), ("bmiColors", wintypes.DWORD * 3)]


def _swap_rb(data):
    """R 与 B 对调。切片赋值走 C，17 万像素也是瞬间。"""
    out = bytearray(len(data))
    out[0::3] = data[2::3]
    out[1::3] = data[1::3]
    out[2::3] = data[0::3]
    return bytes(out)


def _ppm_rgb(raw):
    """极简 P6 解析：只认我们自己刚写出来的那种文件。"""
    if not raw.startswith(b"P6"):
        raise ValueError("不是 P6")
    parts, pos = [], 2
    while len(parts) < 3:
        while pos < len(raw) and raw[pos:pos + 1].isspace():
            pos += 1
        if raw[pos:pos + 1] == b"#":
            while pos < len(raw) and raw[pos:pos + 1] != b"\n":
                pos += 1
            continue
        start = pos
        while pos < len(raw) and not raw[pos:pos + 1].isspace():
            pos += 1
        parts.append(int(raw[start:pos]))
    pos += 1
    w, h, _max = parts
    return w, h, raw[pos:pos + w * h * 3]


def _dib(gdi32, w, h, pixels=None):
    """建一个 24 位、自上而下的 DIB。返回 (句柄, 位地址, 行距)。"""
    bmi = _BMI()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # 负数 = 自上而下，省得再翻
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 24
    bmi.bmiHeader.biCompression = 0      # BI_RGB
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(None, ctypes.byref(bmi), 0,
                                  ctypes.byref(bits), None, 0)
    if not hbmp:
        raise OSError("CreateDIBSection 失败")
    stride = (w * 3 + 3) & ~3            # 24 位 DIB 每行按 4 字节对齐
    if pixels is not None:
        for y in range(h):
            ctypes.memmove(bits.value + y * stride,
                           pixels[y * w * 3:(y + 1) * w * 3], w * 3)
    return hbmp, bits, stride


def gdi_stretch(bgr, sw, sh, dw, dh):
    """把 BGR 像素拉伸成 dw×dh（HALFTONE 质量），返回 BGR。

    传进来/交出去的都按 **BGR**：24 位 DIB 的内存布局就是这个，调用方负责
    转进转出。别在中间偷偷换 —— 靠两次错误抵消的写法看着像对的，改一处就崩
    （实测颜色反过一次，蓝头发变金褐色）。
    """
    gdi32 = ctypes.windll.gdi32
    hsrc, _sbits, _sstride = _dib(gdi32, sw, sh, bgr)
    hdst, dbits, dstride = _dib(gdi32, dw, dh)
    hdc_s = gdi32.CreateCompatibleDC(None)
    hdc_d = gdi32.CreateCompatibleDC(None)
    try:
        old_s = gdi32.SelectObject(hdc_s, hsrc)
        old_d = gdi32.SelectObject(hdc_d, hdst)
        gdi32.SetStretchBltMode(hdc_d, 4)            # HALFTONE
        gdi32.SetBrushOrgEx(hdc_d, 0, 0, None)
        if not gdi32.StretchBlt(hdc_d, 0, 0, dw, dh, hdc_s, 0, 0, sw, sh,
                                0x00CC0020):          # SRCCOPY
            raise OSError("StretchBlt 失败")
        raw = ctypes.string_at(dbits.value, dstride * dh)
        gdi32.SelectObject(hdc_s, old_s)
        gdi32.SelectObject(hdc_d, old_d)
    finally:
        for h in (hdc_s, hdc_d):
            gdi32.DeleteDC(h)
        for h in (hsrc, hdst):
            gdi32.DeleteObject(h)
    out = bytearray(dw * dh * 3)
    for y in range(dh):                               # 去掉行末的对齐填充
        out[y * dw * 3:(y + 1) * dw * 3] = raw[y * dstride:y * dstride + dw * 3]
    return bytes(out)


def paint_gradient(canvas, width, height, c1, c2, tag="bg"):
    """在 Canvas 上刷一层竖向渐变。

    一行画一条线就够了 —— 一百多条，比造 PhotoImage 简单得多，也不慢。
    纯色块的横幅看着像控制面板，渐变更像正经软件，这是「不那么工科」
    最省力的一笔。
    """
    canvas.delete(tag)
    span = max(1, height - 1)
    for y in range(height):
        canvas.create_line(0, y, width, y, fill=mix(c1, c2, y / span), tags=tag)


class CanvasLabel:
    """把 Canvas 上的文字项包装成 tk.Label 的样子。

    这样 `self.lbl_xxx.config(text=..., foreground=...)` 这类调用一行都不用改 ——
    状态刷新逻辑有好几处，全改一遍既啰嗦又容易漏。
    """

    def __init__(self, canvas, item):
        self.canvas = canvas
        self.item = item

    def config(self, **kw):
        if "text" in kw:
            self.canvas.itemconfig(self.item, text=kw["text"])
        if "foreground" in kw:
            self.canvas.itemconfig(self.item, fill=kw["foreground"])

    configure = config

    def cget(self, key):
        return self.canvas.itemcget(self.item, "text") if key == "text" else ""


def rgb(hex_color):
    """'#rrggbb' -> (r, g, b)。"""
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


_CORNER_ALPHA = {}          # (r, corner) -> 覆盖率矩阵，只跟半径和角有关
_CORNER_IMG = {}            # (r, corner, fill, bg) -> PhotoImage
_CORNER_IMG_CAP = 240       # 动画每帧一个颜色，得封顶不然会一直涨


def _corner_alpha(r, corner):
    """r×r 的圆角块里，每个像素被圆盖住的比例（0..1）。

    用 4×4 超采样：每个像素取 16 个样本点，数有多少个落在圆内。
    一次性算好缓存起来，跟颜色无关 —— 换颜色时直接拿这份覆盖率去混色。
    """
    key = (r, corner)
    hit = _CORNER_ALPHA.get(key)
    if hit is not None:
        return hit
    # 四个角，圆心在各自小方块的哪个位置
    cx, cy = {"tl": (r, r), "tr": (0.0, r),
              "bl": (r, 0.0), "br": (0.0, 0.0)}[corner]
    rr = float(r) * r
    out = []
    for y in range(r):
        row = []
        for x in range(r):
            n = 0
            for sy in range(4):
                for sx in range(4):
                    px = x + (sx + 0.5) / 4.0
                    py = y + (sy + 0.5) / 4.0
                    if (px - cx) ** 2 + (py - cy) ** 2 <= rr:
                        n += 1
            row.append(n / 16.0)
        out.append(row)
    _CORNER_ALPHA[key] = out
    return out


_BTN_MASK = {}          # (w, h, r) -> 每像素覆盖率
_BTN_IMG = {}           # (w, h, r, fill, background) -> PhotoImage


def _button_alpha(w, h, r, x, y):
    """圆角矩形在 (x, y) 这一点的覆盖率。

    换算成"到最近圆角圆心的位移"，再按 4x4 超采样算覆盖率。
    角落图、直边、整张 mask 三处都走这一个函数，保证不会各算各的。
    """
    if not r:
        return 1.0
    cx = min(max(x, r), w - 1 - r)
    cy = min(max(y, r), h - 1 - r)
    dx = x - cx
    dy = y - cy
    if dx * dx + dy * dy <= (r - 0.5) ** 2:
        return 1.0
    hit = 0
    for sy in range(4):
        for sx in range(4):
            px = x + (sx + 0.5) / 4.0
            py = y + (sy + 0.5) / 4.0
            qx = min(max(px, r), w - 1 - r)
            qy = min(max(py, r), h - 1 - r)
            if (px - qx) ** 2 + (py - qy) ** 2 <= r * r:
                hit += 1
    return hit / 16.0


def _button_mask(w, h, r):
    """整块圆角矩形的逐像素覆盖率。r 为 0 时就是纯矩形。

    内部一大片是 1.0、外面是 0.0，只有边缘一圈是中间值 —— 生成时对这两种
    走快路径，所以真正的浮点计算只发生在周长那一圈上。
    """
    key = (w, h, r)
    got = _BTN_MASK.get(key)
    if got is not None:
        return got
    rows = []
    for y in range(h):
        row = []
        for x in range(w):
            # 走跟角落图同一个算法，避免两套实现跑偏
            row.append(_button_alpha(w, h, r, x, y))
        rows.append(row)
    _BTN_MASK[key] = rows
    return rows


def button_image(w, h, r, fill, background, cap=160):
    """整块圆角矩形，抗锯齿烘焙进图片。

    ## 为什么不是"逐像素画整张"

    分段实测过（780x420）：
        _button_mask        72 ms   逐像素算覆盖率
        PhotoImage + put   131 ms   往图片里塞 262 万字符
        --------------------------------
        合计               208 ms   一张卡片

    拖窗口时宽度每变 1px、界面里十几张卡片全部重算 —— **846 ms 一帧**。
    这就是"运行起来有点卡和迟钝"的来源。

    而中间那一大片**本来就是纯色**，根本不需要逐像素：
        img.put(fill, to=(0,0,w,h))   1.15 ms
        一个 16x16 的角               0.45 ms

    所以只把四个圆角逐像素算出来贴上去，中间一次填满 —— 约 3 ms，快 70 倍。
    """
    key = (w, h, r, fill, background)
    got = _BTN_IMG.get(key)
    if got is not None:
        return got

    r = max(1, min(r, w // 2, h // 2))
    fc, bc = rgb(fill), rgb(background)
    img = tk.PhotoImage(width=w, height=h)

    def blend(a):
        if a <= 0.001:
            return background
        if a >= 0.999:
            return fill
        return "#%02x%02x%02x" % tuple(
            int(round(bc[i] + (fc[i] - bc[i]) * a)) for i in range(3))

    # 1) 内核：内缩 r 的那块必然全不透明，一次填满
    img.put(fill, to=(0, 0, w, h))

    # 2) **整个外圈逐像素。**
    #
    # 试过两种"更快"的写法，都不行，记在这儿免得以后有人再走一遍：
    #   · "直边算一次铺一整条" —— 边界上相邻像素的覆盖率并不相同，错 1141 点
    #   · "按 r 缓存角块和边廓" —— 四个角的镜像映射对不上，错 2777 点
    #
    # 外圈只有 2rw + 2r(h-2r) 个像素，约是整张 w*h 的十分之一，够用了。
    # 拖动时的流畅靠 RoundedCard 的防抖，不靠这里再抠。
    for y in range(h):
        if r <= y < h - r:
            xs = list(range(r)) + list(range(max(r, w - r), w))
        else:
            xs = range(w)
        for x in xs:
            a = _button_alpha(w, h, r, x, y)
            if a >= 0.999:
                continue                     # 已经是 fill，跳过
            img.put(blend(a), to=(x, y))

    if len(_BTN_IMG) >= cap:
        _BTN_IMG.clear()
    _BTN_IMG[key] = img
    return img


def _corner_image(r, corner, fill, background):
    """一个抗锯齿的圆角块。

    为什么要这么麻烦：**tkinter 的 Canvas 不做抗锯齿**。用「矩形 + 椭圆」拼
    出来的圆角是硬边的，半径一大，四个角就是肉眼可见的台阶 —— 看着像马赛克。

    这里改成逐像素算覆盖率，再把边缘像素跟背景色按比例混一下，就等于把
    抗锯齿烘焙进了图片。只画四个 r×r 的小角，中间照旧用矩形填，
    所以每次重绘只有几百个像素，动画也扛得住。
    """
    key = (r, corner, fill, background)
    img = _CORNER_IMG.get(key)
    if img is not None:
        return img

    alpha = _corner_alpha(r, corner)
    fc, bc = rgb(fill), rgb(background)
    img = tk.PhotoImage(width=r, height=r)
    rows = []
    for y in range(r):
        cells = []
        for x in range(r):
            a = alpha[y][x]
            if a >= 0.999:
                cells.append(fill)
            elif a <= 0.001:
                cells.append(background)
            else:
                cells.append("#%02x%02x%02x" % tuple(
                    int(round(bc[i] + (fc[i] - bc[i]) * a)) for i in range(3)))
        rows.append("{" + " ".join(cells) + "}")
    img.put(" ".join(rows))

    if len(_CORNER_IMG) >= _CORNER_IMG_CAP:
        _CORNER_IMG.clear()
    _CORNER_IMG[key] = img
    return img


class RoundedButton(tk.Canvas):
    """圆角按钮。

    tk.Button 画不出圆角，而圆角是现代感最直接的一笔。这里用两个矩形 +
    四个圆角拼出来 —— Tk 的经典做法，不需要图片、不需要第三方库。

    对外接口刻意做成跟 tk.Button 一样（`config(text=..., background=...)`），
    这样调用方不用改。
    """

    def __init__(self, parent, text, command, font_spec, height=88,
                 radius=20, fill=PRIMARY, fill_active=PRIMARY_D,
                 background=BG, width=None, text_fill="#FFFFFF"):
        super().__init__(parent, height=height, background=background,
                         highlightthickness=0, bd=0, cursor="hand2")
        if width:
            tk.Canvas.config(self, width=width)
        self._text = text
        self._command = command
        self._font = font_spec
        self._text_fill = text_fill
        self._radius = radius
        self._base = fill
        self._active = fill_active
        self._cur = fill
        self._bg = background          # 四角抗锯齿要拿它跟底色混
        self._radius_used = radius
        self._corner_refs = {}         # PhotoImage 引用，被 GC 就白画了
        self._corner_spots = {}
        self._label = None
        self._enabled = True
        self._anim = Animator(self)

        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", self._on_click)
        # 悬停和移开都走 83ms 过渡，不是硬切
        self.bind("<Enter>", lambda e: self._paint(self._active))
        self.bind("<Leave>", lambda e: self._paint(self._base))

    def _on_click(self, _event=None):
        if not click_ready():
            return
        if self._enabled and self._command:
            self._command()

    def _redraw(self):
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 4 or h < 4:
            return
        r = max(0, min(self._radius, h // 2 - 1))
        x1, y1, x2, y2 = 1, 1, w - 1, h - 1
        self.delete("all")
        self._corner_refs = {}          # 必须留引用，PhotoImage 被 GC 就白画了
        self._draw_body(r, x1, y1, x2, y2, self._cur)
        # 居中就按几何中心，**不要再挪**。
        #
        # 我试过往上挪 1px（想着中文字形的墨迹在行盒里偏低），但那是照着
        # 一张不同尺寸的截图调的：改之前 上15/下14，改之后 上10/下11 ——
        # 两个方向各差 1px，说明几何居中本来就是对的，是我调反了。
        self._label = self.create_text((x1 + x2) // 2, (y1 + y2) // 2,
                                       text=self._text, fill=self._text_fill,
                                       font=self._font)

    def _draw_body(self, r, x1, y1, x2, y2, color):
        """整块按钮贴一张抗锯齿图片。

        **不要退回「四个角 + 中间矩形」的拼法。** 那是我踩过的坑：Tk 的矩形是
        硬边整数像素，角落图片是按覆盖率混过色的，两种边缘模型凑不到一起，
        接缝处必然出现一条肉眼可见的竖线。
        """
        self.delete("body")
        w, h = (x2 - x1 + 1), (y2 - y1 + 1)
        if w < 4 or h < 4:
            return
        img = button_image(w, h, r, color, self._bg)
        self._corner_refs["all"] = img
        self.create_image(x1, y1, image=img, anchor="nw", tags="body")
        self.tag_lower("body")          # 文字要压在底色上面

    def _paint(self, color, duration=MOTION_FASTER):
        """把按钮底色过渡到 color。

        Fluent 的两档时长在这里分工：
          · 悬停 / 按下 —— MOTION_FASTER（83ms），要跟手
          · 状态切换（开始 ↔ 停止）—— MOTION_NORMAL（250ms），要看得见

        注意 _base 是「静止色」，_cur 是「当前显示色」：动画只改 _cur，
        否则鼠标移开时会把目标色当成静止色，越点越偏。
        """
        if not self._enabled:
            return
        start = self._cur or color

        def frame(t):
            c = mix(start, color, t)
            self._cur = c
            # 四角是图片，换色得重画 —— 只有 r×r 那么大，重画代价可以忽略
            self._draw_body(self._radius_used, 1, 1,
                            self.winfo_width() - 1, self.winfo_height() - 1, c)

        self._anim.run("color", duration, frame)

    def config(self, **kw):
        if "text" in kw:
            self._text = kw["text"]
            if self._label:
                self.itemconfig(self._label, text=kw["text"])
        if "background" in kw:
            self._base = kw["background"]
            self._active = kw.get("activebackground") or kw["background"]
            # 状态切换用常规时长 —— 开始/停止是件"有分量"的事，83ms 一闪而过
            self._paint(self._base, MOTION_NORMAL)
        if "state" in kw:
            self._enabled = kw["state"] != "disabled"
            self.config_cursor("hand2" if self._enabled else "arrow")

    configure = config

    def config_cursor(self, cursor):
        tk.Canvas.config(self, cursor=cursor)

    def dispose(self):
        """控件要没了：把没跑完的动画取消掉。"""
        self._anim.cancel()


#: 启动豁免期（秒）。
#:
#: 实测踩到过两次（主题开关、标签栏）：程序启动时如果鼠标恰好停在某个控件上，
#: Windows 会把光标位置上那一次点击投递进来，那个控件就被"点"了一下 ——
#: 用户看到的是"程序自己动了一下"，极难自行诊断。
#:
#: 这段时间内不认点击。代价是启动后 0.6 秒内点不动东西，可以忽略。
CLICK_GRACE_SECONDS = 0.8
_boot_time = None


def start_click_guard():
    """记下启动时刻。App 构造时调用一次。"""
    global _boot_time
    _boot_time = time.time()


def click_ready():
    """是否已经过了启动豁免期。

    注意这个判断只该放在**点击入口**上。不要放进 App.toggle_main /
    select_tab 这类方法里 —— 它们在构建界面时也会被程序自己调用，
    放进去会把初始化一起拦掉。
    """
    if _boot_time is None:
        return True
    return (time.time() - _boot_time) > CLICK_GRACE_SECONDS


class TabStrip(tk.Frame):
    """自绘标签栏，替掉 ttk.Notebook 自带的那一条。

    为什么不用 ttk.Notebook 的标签页：clam 主题的 tab 自带**竖向分隔线**和
    一圈**虚线焦点框**，而且那是画在 tab 元素本身上的 —— 我把 bordercolor /
    lightcolor / darkcolor 全设成背景色也去不掉，只改颜色没用。

    自绘的另一个好处是能做滑块动效：选中态用一条会**滑过去**的指示条，
    这是 Fluent 里很典型的一个小动效，Notebook 给不了。
    """

    HEIGHT = 38
    BAR_H = 2

    def __init__(self, parent, labels, command, background=None):
        # 同 ScrollFrame：默认参数会在导入时被烤死，必须用 None 占位。
        background = background or BG
        super().__init__(parent, background=background, height=self.HEIGHT)
        self.pack_propagate(False)
        self.command = command
        self._index = 0
        self._anim = Animator(self)

        # 指示条先建：tkinter 里同层控件按创建顺序叠，先建的在下面
        self._bar = tk.Frame(self, background=PRIMARY)
        self._bar.place(x=0, y=self.HEIGHT - self.BAR_H, width=0, height=self.BAR_H)

        self._row = tk.Frame(self, background=background)
        self._row.pack(fill="both", expand=True)

        self._tabs = []
        for i, text in enumerate(labels):
            lbl = tk.Label(self._row, text=text, font=(FONT, 10),
                           background=background, foreground=MUTED,
                           padx=20, cursor="hand2")
            lbl.pack(side="left", fill="y")
            lbl.bind("<Button-1>",
                     lambda e, i=i: click_ready() and self.command(i))
            lbl.bind("<Enter>", lambda e, i=i: self._hover(i, True))
            lbl.bind("<Leave>", lambda e, i=i: self._hover(i, False))
            self._tabs.append(lbl)

        self.after(30, lambda: self.set_active(0, animate=False))

    def _hover(self, i, on):
        if i == self._index:
            return
        self._tabs[i].config(foreground=TEXT if on else MUTED)

    def set_active(self, index, animate=True):
        if not (0 <= index < len(self._tabs)):
            return
        self._index = index
        for i, lbl in enumerate(self._tabs):
            lbl.config(foreground=PRIMARY if i == index else MUTED,
                       font=(FONT, 10, "bold") if i == index else (FONT, 10))

        target = self._tabs[index]
        x = target.winfo_x()
        w = target.winfo_width()
        if w < 2:                       # 还没布局完，等一帧再来
            self.after(30, lambda: self.set_active(index, animate=False))
            return
        if not animate:
            self._bar.place_configure(x=x, width=w)
            return
        start = self._bar.winfo_x()
        start_w = max(self._bar.winfo_width(), 1)
        # 指示条滑过去也用弹簧，收尾带一点回弹 —— 这就是 Expressive 的味道
        Spring(SPRING_SPATIAL).drive(
            self, lambda t: self._bar.place_configure(
                x=int(start + (x - start) * t),
                width=int(start_w + (w - start_w) * t)))

    def dispose(self):
        self._anim.cancel()


class ToggleSwitch(tk.Canvas):
    """Windows 11 那种胶囊开关。

    为什么不用 tk.Checkbutton：Windows 11 的开关是**胶囊滑块**，不是方框打勾。
    这是整个界面里最容易被一眼认出来的 Windows 元素 —— 用方框打勾会立刻
    显得像十年前的软件。

    对外接口跟 Checkbutton 一样收一个 BooleanVar，所以换控件不用动逻辑。
    """

    W, H, KNOB = 40, 20, 14

    def __init__(self, parent, variable, background=None, command=None):
        super().__init__(parent, width=self.W, height=self.H,
                         background=background or BG, highlightthickness=0,
                         bd=0, cursor="hand2")
        self.var = variable
        self.command = command
        self._t = 1.0 if variable.get() else 0.0        # 0=关 1=开
        self._anim = Animator(self)
        self.bind("<Button-1>", self._click)
        # 外部改了变量（比如 _cfg_to_ui 回填配置）也要跟着动
        self._trace = variable.trace_add("write", lambda *a: self._sync())
        self._draw()

    def _click(self, _event=None):
        if not click_ready():
            return
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _sync(self):
        target = 1.0 if self.var.get() else 0.0
        if abs(target - self._t) < 0.01:
            return
        start = self._t
        # 位移用 spatial 弹簧：会冲过目标再弹回来，那个回弹就是 M3 Expressive
        # 要的生命力。缓动函数做不出这个，因为它只保证「停在终点」。
        Spring(SPRING_SPATIAL).drive(
            self, lambda t: self._set(start + (target - start) * t))

    def _set(self, t):
        self._t = t
        self._draw()

    def _draw(self):
        self.delete("all")
        t = self._t
        r = self.H // 2
        track = mix(MUTED, PRIMARY, t)
        # 胶囊轨道 = 左右两个半圆 + 中间一个矩形
        self.create_oval(0, 0, self.H, self.H, fill=track, outline="")
        self.create_oval(self.W - self.H, 0, self.W, self.H,
                         fill=track, outline="")
        self.create_rectangle(r, 0, self.W - r, self.H, fill=track, outline="")
        pad = (self.H - self.KNOB) / 2.0
        x = pad + (self.W - self.H) * t
        self.create_oval(x, pad, x + self.KNOB, pad + self.KNOB,
                         fill="#FFFFFF", outline="")

    def dispose(self):
        self._anim.cancel()
        try:
            self.var.trace_remove("write", self._trace)
        except Exception:
            pass


# M3 Expressive 的弹簧 token，参数抄自官方（不是我调的）：
#     expressiveSpatialFast     damping 0.6  stiffness 800   ← 最弹的一个
#     expressiveSpatialDefault  damping 0.8  stiffness 380
#     expressiveEffectsFast     damping 1.0  stiffness 3800  ← 不弹，只求快
#
# 官方还有一句话很关键：**效果类动画两套方案的参数完全一样** ——
# 「表现力属于移动，不属于颜色」。所以：
#     位置、尺寸、滑块  → 用 spatial（会弹，有生命力）
#     颜色、透明度      → 用 effects（不弹，弹了反而眼花）
# damping 0.6 是 Material 官方最弹的一档（过冲 +8.9%）；二次元要更弹一点，
# 取 0.5（+15.5%）。实测 0.45 就到 +19.8%，开始像故障而不是活泼了 ——
# 这条线是算出来的，不是凭手感调的。
SPRING_SPATIAL = (0.5, 800.0)
SPRING_SPATIAL_SOFT = (0.8, 380.0)
SPRING_EFFECTS = (1.0, 3800.0)


class Spring:
    """M3 的弹簧模拟。

    这是 Material 3 Expressive 最核心的东西 —— 它的动效不是贝塞尔曲线，
    是**物理弹簧**。damping 小于 1 时会**冲过目标再弹回来**，那个"回弹"
    就是 Expressive 想要的"有生命力"的感觉，缓动函数给不了。

    数值积分用最朴素的显式欧拉，60fps 下看不出误差。
    """

    def __init__(self, token):
        damping, stiffness = token
        self.m = 1.0
        self.k = float(stiffness)
        self.c = 2.0 * damping * math.sqrt(self.k * self.m)

    def drive(self, widget, on_frame, on_done=None, velocity=0.0):
        """从 0 跑到 1。on_frame 收到的进度**可能大于 1**（过冲），也可能小于 0。

        调用方得自己决定怎么处理过冲：滑块位置过冲很好看，颜色过冲会算出
        非法颜色值，所以颜色那条路要用 SPRING_EFFECTS（damping=1，不过冲）。
        """
        state = {"x": 0.0, "v": velocity}
        frame_ms = 16
        # **子步长不能省。** 显式积分在高刚度下会发散：stiffness 3800 时阻尼系数
        # c = 2*sqrt(k) ≈ 123，按 1/60 秒积分的话 c*dt ≈ 2.06 > 2，一步就翻号，
        # 数值直接爆到 1e20（实测踩到过）。固定用 1/600 秒积分、每帧跑若干子步，
        # c*dt 降到 0.2 左右，稳稳收敛。
        sub_dt = 1.0 / 600.0
        per_frame = max(1, int(round((frame_ms / 1000.0) / sub_dt)))

        def step():
            x, v = state["x"], state["v"]
            for _ in range(per_frame):
                a = (-self.k * (x - 1.0) - self.c * v) / self.m
                v += a * sub_dt
                x += v * sub_dt
            state["x"], state["v"] = x, v
            if not (abs(x) < 10.0 and abs(v) < 1e4):      # 兜底：真炸了就直接落位
                try:
                    on_frame(1.0)
                except tk.TclError:
                    pass
                if on_done:
                    on_done()
                return
            try:
                on_frame(x)
            except tk.TclError:
                return
            if abs(x - 1.0) < 0.002 and abs(v) < 0.02:
                try:
                    on_frame(1.0)
                except tk.TclError:
                    pass
                if on_done:
                    on_done()
                return
            widget.after(frame_ms, step)

        step()


class ThemeToggle(tk.Canvas):
    """圆形的深浅色开关，图标用**字形**。

    实测过一轮（把候选字形 × 候选字体全渲染出来比对），结论：

        字体必须是 **Segoe UI Symbol**
            ☀ U+2600     实心太阳，正常
            🌙 U+1F319    月牙，粗细正好
        换成 Segoe UI Emoji 就废了：☀ 变成一个空心圆圈，🌞 变成一个笑脸。

    之前 `☾` 显示成怪模怪样的 C，不是字形的问题，是**9pt 太小**，月牙糊成一团。

    图标语义按**点下去会变成什么**来放 —— 开关类控件的通行做法：
        当前浅色 → 显示月亮（点了变深色）
        当前深色 → 显示太阳（点了变浅色）
    """

    D = 30                      # 直径（同时也是点击热区）
    GLYPH_FONT = "Segoe UI Symbol"

    def __init__(self, parent, command, background=None, dark=False):
        bg = background or BG
        super().__init__(parent, width=self.D, height=self.D, background=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self._dark = dark
        self._hover = False
        self._face = SURFACE
        self._ink = TEXT
        self._label = None
        self.bind("<Button-1>",
                  lambda e: click_ready() and self.command())
        self.bind("<Enter>", lambda e: self._hover_set(True))
        self.bind("<Leave>", lambda e: self._hover_set(False))
        self._draw()

    def _hover_set(self, on):
        self._hover = on
        self._draw()

    def set_dark(self, dark):
        if dark != self._dark:
            self._dark = dark
            self._draw()

    def _draw(self):
        self.delete("all")
        self._face = PRIMARY_S if self._hover else SURFACE
        self._ink = PRIMARY if self._hover else TEXT
        self.create_oval(1, 1, self.D - 1, self.D - 1, fill=self._face,
                         outline=BORDER, width=1)
        self._label = self.create_text(
            self.D / 2.0, self.D / 2.0 + 1,
            text="\u2600" if self._dark else "\U0001f319",
            fill=self._ink, font=(self.GLYPH_FONT, 15))

    def dispose(self):
        pass


class Animator:
    """按 Fluent 的时长和缓动驱动一个逐帧回调。

    tkinter 没有 CSS transition，也没有透明度，动画只能是「每帧算一个新值，
    用 after() 排下一帧」。全部动画都走这里，好处有两个：

      · 时长和缓动统一，不会每处各写一套魔数
      · 换主题要重建界面，能一把取消干净 —— 否则旧控件的回调会打到已经
        销毁的 widget 上，报一堆 TclError
    """

    def __init__(self, widget):
        self.widget = widget
        self._jobs = {}

    def run(self, key, duration, on_frame, ease=None, on_done=None):
        self.cancel(key)
        ease = ease or ease_out
        steps = max(1, int(round(float(duration) / FRAME_MS)))

        def tick(step):
            if step > steps:
                self._jobs.pop(key, None)
                if on_done:
                    try:
                        on_done()
                    except tk.TclError:
                        pass
                return
            try:
                on_frame(ease(step / steps))
            except tk.TclError:
                self._jobs.pop(key, None)      # 控件没了，别再排下一帧
                return
            self._jobs[key] = self.widget.after(FRAME_MS, tick, step + 1)

        tick(1)

    def cancel(self, key=None):
        if key is None:
            jobs, self._jobs = self._jobs, {}
        else:
            job = self._jobs.pop(key, None)
            jobs = {key: job} if job else {}
        for job in jobs.values():
            try:
                self.widget.after_cancel(job)
            except Exception:
                pass

    def dispose(self):
        """和 RoundedButton.dispose 同名 —— 退出清理时能一视同仁地调。"""
        self.cancel()


def png_has_alpha(path):
    """PNG 有没有透明通道 —— 读 IHDR 的颜色类型字节。

    实测过：丢一张白底图进去，程序照单全收，头部就糊一块白方块。
    **tkinter 做不到运行时抠图**，所以只能提前提醒，不能默默接受。
    返回 None 表示"说不好"（不是 PNG，或者读不了）。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(26)
    except OSError:
        return None
    if len(head) < 26 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    # IHDR 第 25 字节是颜色类型：4 = 灰度+alpha，6 = RGBA
    return head[25] in (4, 6)


def load_header_image():
    """读整条头部的背景图，返回 (PhotoImage 或 None, 来源路径)。

    **分主题**：先找 header-<主题>.png，再找通用的 header.png。

    为什么非得两张：文字颜色在两套主题里是相反的（浅色深字、深色白字），
    所以需要**两个方向的遮罩** —— 一张图靠调透明度盖不住两个方向。

    **不做缩放**：tkinter 只能整数倍缩，硬缩反而糟。图按原始尺寸贴左上角，
    窗口比图宽时右边自然露出渐变。
    """
    names = ["header-{}.png".format(THEME), "header.png"]
    roots = [HERE]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(meipass)
    roots.append(os.path.join(HERE, "_build"))
    for root in roots:
        for name in names:
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            try:
                return tk.PhotoImage(file=path), path
            except Exception:
                continue
    return None, None


#: 花体英文的候选。Gabriola 最像"花体"（Win7+ 自带，带连笔装饰），
#: 次选 Segoe Script。两个都没有就退回界面字体 —— 字体缺失不报错，
#: 只会把字变成方块，所以必须有后路。
SCRIPT_CANDIDATES = ("Gabriola", "Segoe Script", "Segoe Print")


def pick_script_font():
    """挑一个真实存在的花体字体。跟 pick_ui_font 一样，要有 Tk 根窗口。"""
    try:
        import tkinter.font as _tkfont
        families = set(_tkfont.families())
    except Exception:
        return FONT_FALLBACK
    for name in SCRIPT_CANDIDATES:
        if name in families:
            return name
    return FONT_FALLBACK


def pick_ui_font():
    """挑一个真实存在的界面字体。

    **字体缺失不会报错，只会把界面变成一堆方块** —— 这种事故必须在运行时挡掉。
    查系统字体要有 Tk 根窗口，所以这个函数只能在 root 建好之后调，
    不能做成模块级常量。
    """
    try:
        import tkinter.font as _tkfont
        families = set(_tkfont.families())
    except Exception:
        return FONT_FALLBACK
    for name in FONT_CANDIDATES:
        if name in families:
            return name
    return FONT_FALLBACK


def load_asset(name):
    """找一个随程序发布的资源文件。

    跟 set_window_icon 走同一条路：打包后在 sys._MEIPASS，开发时在 _build/。
    单独抽出来是因为头图也要走这里 —— 否则打好的 exe 里找不到它。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        path = os.path.join(meipass, name)
        if os.path.isfile(path):
            return path
    path = os.path.join(HERE, "_build", name)
    return path if os.path.isfile(path) else None


def ensure_config():
    """首次运行：没有 config.json 就从 config.example.json 生成一份。

    网盘分享的场景下，用户解压出来是没有 config.json 的 —— 直接报「找不到
    配置文件」会让人以为包坏了。这里自动生成一份，剩下的交给界面上的
    「配置安全检查」去告诉他该填什么。

    返回 True 表示这次是新生成的。
    """
    if os.path.isfile(CONFIG_PATH):
        return False
    example = os.path.join(HERE, "config.example.json")
    if not os.path.isfile(example):
        return False
    try:
        import shutil
        shutil.copyfile(example, CONFIG_PATH)
        return True
    except OSError:
        return False


def set_window_icon(root):
    """设置标题栏 / 任务栏图标。

    exe 的图标是嵌在可执行文件里的，但 **tkinter 窗口默认用 Tk 自带的羽毛**，
    不显式设一次，标题栏左上角就还是那个羽毛。
    打包后图标在解包目录（sys._MEIPASS），开发时在 app/_build/ 下。
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "app.ico"))
    candidates.append(os.path.join(HERE, "_build", "app.ico"))
    for path in candidates:
        if os.path.isfile(path):
            try:
                root.iconbitmap(default=path)
                return path
            except Exception:
                continue
    return None



# --------------------------------------------------------------------------
#  NapCat 生命周期控制
# --------------------------------------------------------------------------

def read_account():
    try:
        with open(ACCOUNT_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def port_open(port, host="127.0.0.1", timeout=0.5):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0
    except OSError:
        return False


def _no_window_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


_NAPCAT_PROC = None          # 后台运行的 NapCat 进程句柄


def start_napcat():
    """在**后台无窗口**启动 NapCat，输出直接送进界面日志。

    优先 launcher-hidden.bat —— 它是 launcher-second.bat 的无 pause 版本，
    配合 CREATE_NO_WINDOW 使用：NapCat 完全不弹黑窗口，它的输出由本函数
    接管并转发到「运行日志」标签页，便于排错。
    """
    hidden = os.path.join(NAPCAT_DIR, "launcher-hidden.bat")
    private = os.path.join(NAPCAT_DIR, "launcher-second.bat")
    fallback = os.path.join(NAPCAT_DIR, "launcher-user.bat")

    if os.path.isfile(hidden):
        launcher = "launcher-hidden.bat"
    elif os.path.isfile(private):
        launcher = "launcher-second.bat"
    elif os.path.isfile(fallback):
        launcher = "launcher-user.bat"
    else:
        return False, "找不到 NapCat 启动脚本：{}".format(NAPCAT_DIR)

    if port_open(3000):
        return True, "NapCat 已经在运行"

    account = read_account()
    args = ["cmd", "/c", launcher]
    if account:
        args.append(account)

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    global _NAPCAT_PROC
    try:
        _NAPCAT_PROC = subprocess.Popen(
            args, cwd=NAPCAT_DIR, creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, universal_newlines=True,
            encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, "启动 NapCat 失败：{}".format(exc)

    # 把 NapCat 的控制台输出实时转发到界面日志（替代那个黑窗口）
    proc = _NAPCAT_PROC

    def pump():
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    core.log("[NapCat] " + line)
        except Exception:
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    threading.Thread(target=pump, daemon=True, name="napcat-log").start()
    return True, "已启动"


def stop_napcat():
    """关闭 NapCat。

    关键：NapCat 跑在私有 QQ 副本里时，只关副本中的那个 QQ 实例，
    绝不碰用户自己正在用的 QQ。只有在退回官方启动器（没有副本）时，
    才退化为关闭全部 QQ。
    """
    try:
        subprocess.run(["taskkill", "/IM", "NapCatWinBootMain.exe", "/F"],
                       capture_output=True, creationflags=_no_window_flags())
    except OSError:
        pass

    if os.path.isfile(os.path.join(NAPCAT_DIR, "launcher-second.bat")):
        # 私有副本模式：只终止可执行文件路径里带 qq-napcat 的 QQ 进程
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='QQ.exe'\" | "
              "Where-Object { $_.ExecutablePath -like '*qq-napcat*' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
              "-ErrorAction SilentlyContinue }")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, creationflags=_no_window_flags(),
                           timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            subprocess.run(["taskkill", "/IM", "QQ.exe", "/F"],
                           capture_output=True, creationflags=_no_window_flags())
        except OSError:
            pass


def wait_for_port(port, seconds, should_cancel=None):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if should_cancel is not None and should_cancel():
            return False
        if port_open(port):
            return True
        time.sleep(0.5)
    return port_open(port)


# --------------------------------------------------------------------------
#  主程序
# --------------------------------------------------------------------------

class App:
    @staticmethod
    def _peek_ui():
        """建界面**之前**单独读一次界面偏好（主题、窗口位置）。

        配置本来要等 _build_ui 之后才读（界面才是它的消费者），但控件颜色
        和窗口尺寸都得在**创建时**定下来 —— 晚一步就得整个重建/挪动一次，
        启动时会明显闪一下。读不到或读坏了都退回默认值。
        """
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
                return json.load(fh).get("ui") or {}
        except Exception:
            return {}

    def __init__(self, root):
        self.root = root
        self.root.title("大肥鱼直播姬")

        ui = self._peek_ui()
        # 字体必须在 root 建好之后、_build_ui 之前定下来
        global FONT
        FONT = pick_ui_font()

        apply_theme(ui.get("theme") or "light")

        # 窗口尺寸：没存过就按屏幕大小挑一个合适的初值。
        # 硬编码 980x760 在 1366x768 的笔记本上会顶到任务栏。
        geo = str(ui.get("geometry") or "")
        if re.match(r"^\d+x\d+\d*[+-]\d+[+-]\d+$", geo):
            self.root.geometry(geo)
        else:
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            # 默认 780x915 —— 比原来窄而高。窄是因为窗口铺太开时每行字都拉得
            # 很长，反而不好读；高是因为下面那张日志面板需要纵向空间。
            # 仍然跟屏幕尺寸取 min：小屏笔记本上要能缩得下。
            self.root.geometry("{}x{}".format(min(780, sw - 80),
                                              min(915, sh - 120)))
        self.root.minsize(740, 560)
        self.root.configure(background=BG)
        set_window_icon(self.root)

        self.cfg = None
        self.state = STATE_IDLE
        self.stop_event = None
        self.monitor_thread = None
        self.log_queue = queue.Queue()
        self.log_tail = []           # 换主题要重建界面，用它把日志接回来
        self.available_groups = []
        self.role_of = {}
        self._recovering = False     # 正在自动重启 NapCat？
        self._theming = False        # 正在重建界面换主题？（防重入）
        self._pulsing = False        # 状态点呼吸动画在跑？（防止起出多条循环）
        self._last_send_sig = None   # 上一次「上次通知」的内容指纹，变了才闪一下
        # 彩蛋：版本号点五下的计数。停手超过 1.2 秒就重新数。
        self._ver_hits = 0
        self._ver_last = 0.0

        self._build_ui()

        core.add_log_sink(self.log_queue.put)
        core._setup_console()
        # 关键：GUI 启动时也要打开日志文件，否则通过界面运行时
        # app\logs\ 下不会有任何记录，出了问题无从查证。
        core._open_log_file()
        core.log("=" * 54)
        core.log("界面已启动")

        self.reload_config()
        self._set_state(STATE_IDLE)

        self.root.after(120, self._drain_log)
        self.root.after(400, self.refresh_status)
        self.root.after(1200, self._poll_processes)
        self.root.after(6000, self._poll_health)
        self.root.after(3000, self._poll_trigger)
        self.root.after(900, self._maybe_autostart)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        # 托盘延后起：它的回调会碰 self.btn_main / self.state，得等界面就绪。
        # 也放在 start_click_guard 之前 —— 托盘跟"误触豁免"没关系。
        self._tray = None
        self.root.after(300, self._start_tray)

        # 豁免期计时**必须在这里起步**，不能放在 __init__ 开头 ——
        # 构建界面本身要花几百毫秒，等窗口真正出现时豁免期早就过去了，等于没装。
        # 误触是"窗口出现的那一刻"发生的，计时就得从那一刻算。
        start_click_guard()

    # ==================================================================
    #  界面
    # ==================================================================

    def _setup_style(self):
        """全局主题。

        必须用 clam：Windows 默认的 vista 主题**会忽略 background 配置**，
        颜色一个都设不上去，界面对比度极差。clam 牺牲一点原生感，
        换来做得到统一的设计。
        """
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=TEXT, font=(FONT, 9))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED)

        # 按钮
        style.configure("TButton", background=SUNKEN, foreground=TEXT,
                        borderwidth=0, focusthickness=0, padding=(12, 7),
                        font=(FONT, 9), relief="flat")
        style.map("TButton",
                  background=[("pressed", BORDER), ("active", PRIMARY_S),
                              ("disabled", BG)],
                  foreground=[("disabled", MUTED)])
        style.configure("Primary.TButton", background=PRIMARY, foreground="white",
                        borderwidth=0, padding=(14, 8), font=(FONT, 9, "bold"),
                        relief="flat")
        style.map("Primary.TButton",
                  background=[("pressed", PRIMARY_D), ("active", PRIMARY_D)])

        # 输入类控件
        style.configure("TEntry", fieldbackground=CARD, foreground=TEXT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        borderwidth=1, padding=5)
        style.configure("TSpinbox", fieldbackground=CARD, foreground=TEXT,
                        bordercolor=BORDER, arrowcolor=PRIMARY, borderwidth=1,
                        padding=3)
        style.configure("TCombobox", fieldbackground=CARD, background=CARD,
                        bordercolor=BORDER, arrowcolor=PRIMARY, padding=4)

        # 复选框
        style.configure("TCheckbutton", background=CARD, foreground=TEXT,
                        focusthickness=0, font=(FONT, 9))
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure("Bg.TCheckbutton", background=BG)
        style.map("Bg.TCheckbutton", background=[("active", BG)])

        # 标签页
        style.configure("TNotebook", background=BG, borderwidth=0,
                        tabmargins=(8, 8, 8, 0),
                        bordercolor=BG, lightcolor=BG, darkcolor=BG)
        # Tab 也要显式关掉 clam 的立体描边 —— 只设 TNotebook 不够，
        # 那个浅色边框是画在 tab 自己身上的，深色主题下会露出来。
        style.configure("TNotebook.Tab", background=BG, foreground=MUTED,
                        padding=(24, 12), borderwidth=0, font=(FONT, 10),
                        bordercolor=BG, lightcolor=BG, darkcolor=BG)
        style.map("TNotebook.Tab",
                  background=[("selected", CARD), ("active", PRIMARY_S)],
                  foreground=[("selected", PRIMARY)])

        # 表格
        style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE,
                        foreground=TEXT, rowheight=28, borderwidth=0,
                        font=(FONT, 9))
        style.configure("Treeview.Heading", background=SUNKEN,
                        foreground=MUTED, font=(FONT, 9, "bold"),
                        borderwidth=0, padding=(6, 7))
        style.map("Treeview",
                  background=[("selected", PRIMARY_S)],
                  foreground=[("selected", TEXT)])
        style.map("Treeview.Heading", background=[("active", BORDER)])

        # ---- 滚动条 ----
        #
        # clam 的默认滚动条是「上箭头 + 轨道 + 握把 + 下箭头」四段拼的，握把上
        # 还有几道横纹 —— 加上立体描边，凑齐了九十年代的样子。
        #
        # **光设颜色治不了它**：箭头和横纹是**布局里的独立元素**，不是颜色。
        # 必须把布局整个换掉，只留轨道和握把。现代滚动条本来也不靠箭头，
        # 滚轮和拖拽就够了。
        def _flat_scrollbar(name, thumb, trough, active=None):
            try:
                style.layout(name, [
                    ("Vertical.Scrollbar.trough", {
                        "sticky": "ns",
                        "children": [("Vertical.Scrollbar.thumb",
                                      {"expand": "1", "sticky": "nswe"})],
                    })
                ])
            except tk.TclError:
                pass
            style.configure(name, background=thumb, troughcolor=trough,
                            # 描边全部跟轨道同色 = 视觉上没有描边
                            bordercolor=trough, lightcolor=trough,
                            darkcolor=trough, borderwidth=0,
                            gripcount=0, arrowsize=0)
            if active:
                style.map(name, background=[("active", active),
                                            ("pressed", active)])

        _flat_scrollbar("Vertical.TScrollbar", BORDER, BG, PRIMARY)
        # 日志区是深色的，滚动条得跟着一起深，不然整块黑里插一条白杠
        _flat_scrollbar("Log.Vertical.TScrollbar", LOG_BAR, LOG_BG, PRIMARY)

    def _head_resized(self, _event=None):
        """窗口宽度变了就重刷横幅。

        渐变是一行画一条线刷出来的，拖动窗口边框时 Configure 会疯狂触发，
        所以 debounce 一下 —— 不然拖起来会卡。
        """
        if self._head_job:
            try:
                self.root.after_cancel(self._head_job)
            except Exception:
                pass
        self._head_job = self.root.after(60, self._paint_header)

    def _on_version_click(self, _event=None):
        """版本号点五下 —— 彩蛋。

        计数要防抖：五下如果算错次数，就等于没有彩蛋。所以要求点与点之间
        不超过 1.2 秒，停手久了就重新数。
        """
        if not click_ready():
            return
        now = time.time()
        if now - self._ver_last > 1.2:
            self._ver_hits = 0
        self._ver_last = now
        self._ver_hits += 1
        if self._ver_hits >= 5:
            self._ver_hits = 0
            self._show_about()

    def _show_about(self):
        """关于窗口，兼彩蛋。

        **完全静态**：不读配置、不发网络请求、不改任何状态。
        删掉这个方法不影响程序任何功能 —— 这是当初给自己定的验收标准。
        """
        dlg = tk.Toplevel(self.root)
        dlg.title("关于")
        dlg.configure(background=BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)

        box = tk.Frame(dlg, background=BG, padx=28, pady=24)
        box.pack(fill="both", expand=True)

        tk.Label(box, text="大肥鱼直播姬", background=BG, foreground=TEXT,
                 font=(FONT, 16, "bold")).pack(anchor="w")
        tk.Label(box, text="v" + core.VERSION, background=BG,
                 foreground=MUTED, font=(FONT, 9)).pack(anchor="w",
                                                        pady=(2, 16))

        body = (
            "开播时自动往 QQ 群发通知，省得每回手动去喊人。\n\n"
            "它没有用任何第三方库，四千多行全是 Python 标准库和 tkinter。\n"
            "不是因为这样优雅 —— 是没有依赖的东西才能活得久。\n\n"
            "如果哪天没人维护了，希望它还能自己站着。"
        )
        tk.Label(box, text=body, background=BG, foreground=TEXT,
                 font=(FONT, 9), justify="left", anchor="w",
                 wraplength=360).pack(anchor="w")

        tk.Frame(box, background=BORDER, height=1).pack(fill="x", pady=18)

        # 花体英文。Gabriola 有没有装不影响别的 —— 挑不到就退回界面字体。
        # 破折号在前、「」包住、一点点斜体。
        tk.Label(box, text="「I love you three thousand」——", background=BG,
                 foreground=ACCENT,
                 font=(pick_script_font(), 17, "italic")).pack(
                     anchor="w", pady=(0, 12))

        tk.Label(box, text="林千鹤", background=BG, foreground=TEXT,
                 font=(FONT, 10)).pack(anchor="e")
        tk.Label(box, text="KagurazakaChizuru", background=BG,
                 foreground=MUTED, font=(FONT, 9)).pack(anchor="e", pady=(2, 0))

        dlg.update_idletasks()
        # 居中到主窗口，而不是屏幕 —— 它属于那个窗口
        x = self.root.winfo_rootx() + (self.root.winfo_width()
                                       - dlg.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height()
                                       - dlg.winfo_height()) // 3
        dlg.geometry("+{}+{}".format(max(0, x), max(0, y)))
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.focus_set()

    def _header_source(self):
        """头图像素（RGB，无行距）。走 Tk 的 PPM 导出。

        为什么不用 `PhotoImage.get`：那是**逐像素一次 Tcl 调用**，780×138 就是
        十万次。导出成 PPM 再解析是 C 侧一把过，实测几毫秒。
        """
        if getattr(self, "_src_pixels", None) is not None:
            return self._src_pixels
        img = getattr(self, "_header_img", None)
        if img is None:
            self._src_pixels = ()
            return ()
        path = os.path.join(tempfile.gettempdir(), "dafeiyu-hdr.ppm")
        try:
            img.write(path, format="ppm")
            with open(path, "rb") as fh:
                raw = fh.read()
        except Exception:
            self._src_pixels = ()
            return ()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            self._src_pixels = _ppm_rgb(raw)
        except Exception:
            self._src_pixels = ()
        return self._src_pixels

    def _header_cover(self, w, h):
        """把当前主题的头图等比缩到**刚好盖住** w×h（贴的时候居中裁切）。

        动的是 GDI，不是 Python 循环：纯 Python 双线性对 17 万像素要一两秒，
        拖窗口会卡成幻灯片；GDI HALFTONE 实测 3 毫秒。
        拿不到缩放结果就返回 None —— 调用方退回"原尺寸 + 右缘补色"，
        宁可难看一点，也不能让头图整个不见了。
        """
        img = getattr(self, "_header_img", None)
        if img is None or w < 8 or h < 8:
            return None
        key = (int(w), int(h))
        cache = getattr(self, "_cover_cache", None)
        if cache is None:
            cache = self._cover_cache = {}
        if key in cache:
            return cache[key]
        sw, sh, rgb = self._header_source()
        photo = None
        if rgb:
            k = max(w / float(sw), h / float(sh))
            dw, dh = max(1, int(round(sw * k))), max(1, int(round(sh * k)))
            try:
                got = gdi_stretch(_swap_rb(rgb), sw, sh, dw, dh)
                photo = tk.PhotoImage(
                    data=b"P6\n%d %d\n255\n" % (dw, dh) + _swap_rb(got),
                    format="ppm")
            except Exception:
                photo = None
        if len(cache) >= 3:
            cache.clear()
        cache[key] = photo
        return photo

    def _bg_under(self, x, y, w, h):
        """头部某一点下面的底色是什么。

        主题按钮是独立控件，它那块**方形**底色必须跟背后的东西对上，
        否则头图上会浮出一个方块。背后是整条头图就采图，是渐变就算渐变。

        采样点取控件中心：渐变更淡，图片在 30px 内的变化也很小，够用。
        """
        def fmt(rgb):
            try:
                r, g, b = rgb[0], rgb[1], rgb[2]
                return "#{:02x}{:02x}{:02x}".format(int(r), int(g), int(b))
            except (TypeError, ValueError, IndexError):
                return None

        # 取**此刻真正贴在头部的**那张图（窗口宽时是缩放过的），
        # 并且把贴图偏移补回去 —— 不然主题按钮那块底色会跟背后差一点，
        # 于是浮出一个方块（1.7.x 踩过）。
        shown, off_x, off_y = getattr(self, "_head_shown", (None, 0, 0))
        if shown is not None:
            try:
                ix, iy = int(x) + off_x, int(y) + off_y
                if 0 <= ix < shown.width() and 0 <= iy < shown.height():
                    # Tk 的 PhotoImage.get 对 RGB 图返回 (r,g,b)，对调色板图返回
                    # 颜色名。两种都兜住。
                    got = fmt(shown.get(ix, iy))
                    if got:
                        return got
            except Exception:
                pass
        # 没有头图（或在图外）：退回渐变那一行的颜色
        return mix(HEADER_TOP, BG, min(1.0, max(0.0, y / max(1, h))))

    def _paint_header(self):
        self._head_job = None
        w = self.head.winfo_width()
        h = self.head.winfo_height()
        if w < 4 or h < 4:
            return
        # 顶部一层极淡的天依蓝，往下渐隐到背景色。**只是"一层"** ——
        # 这里原来是一整块高饱和紫渐变，典型的「设计抢内容」，被拆掉过一次。
        # 二次元的分寸就在这儿：有颜色，但颜色不参与阅读。
        paint_gradient(self.head, w, h, HEADER_TOP, BG, tag="bg")
        self.head.tag_lower("bg")

        # 背景图夹在渐变和其它元素之间。
        #
        # **顺序不能想当然**：文字是在 _build_ui 里先建好的，而
        # _paint_header 是之后才跑的 —— 这时新建的图会跑到所有东西**上面**，
        # 把头部的字全糊掉。所以画完必须把它压回渐变正上方。
        self.head.delete("hdrpic")
        img, off_x, off_y = self._header_img, 0, 0
        cover = self._header_cover(w, h)
        if cover is not None:
            img = cover
            off_x = max(0, (img.width() - w) // 2)
            off_y = max(0, (img.height() - h) // 2)
        self._head_shown = (img, off_x, off_y)
        if img is not None:
            self.head.create_image(-off_x, -off_y, anchor="nw",
                                   image=img, tags="hdrpic")
            self.head.tag_raise("hdrpic", "bg")
        self.head.delete("hair")
        self.head.create_line(0, h - 1, w, h - 1, fill=BORDER, tags="hair")
        # 主题按钮靠右上角
        self.head.coords(self._theme_win, w - 30, 34)

        # 主题按钮那块方形底色要跟它背后的东西对上 —— 背后是头图就采图，
        # 是渐变就算渐变。不然头图上会浮出一个方块（实测踩到过）。
        self.btn_theme.configure(
            background=self._bg_under(self.head.coords(self._theme_win)[0],
                                      34, w, h))
        self.btn_theme.set_dark(THEME == "dark")

        # 可能换行的文字限制宽度，别顶出画布
        # 状态文字限宽。**不能给到整个窗口宽** —— 头图左边那块浅色区是留给
        # 文字的，写太长就压到人物脸上，看着"乱"（用户报过）。400 是那段
        # 浅色区的宽度，超出就换行，宁可两行也别糊在画上。
        for lbl in (self.lbl_conn, self.lbl_sources, self.lbl_alert):
            self.head.itemconfig(lbl.item, width=min(w - 52, 400))

    def _build_ui(self):
        self._setup_style()

        # ---------------- 顶部标题区 ----------------
        # 用 Canvas 只为一件事：刷一层几乎看不出的明暗过渡 + 一条发丝分隔线，
        # Frame 做不到。文字包一层 CanvasLabel，底下那些
        # .config(text=..., foreground=...) 一行都不用改。
        self.head = tk.Canvas(self.root, height=138, highlightthickness=0,
                              bd=0, background=SUNKEN)
        self.head.pack(fill="x")
        self._head_job = None
        self.head.bind("<Configure>", self._head_resized)

        _title = "大肥鱼直播姬"
        self.head.create_text(26, 34, anchor="w", text=_title,
                              fill=TEXT, font=(FONT, 23, "bold"))
        # 版本号：小一号、灰一点，贴着标题右侧的基线放。
        # 用真正的 Label 而不是画布文字项 —— 文字项上的 tag_bind 靠不住
        # （换主题重建界面后，鼠标停在原位置会被反复命中，这个坑踩过一次）。
        self._title_w = tkfont.Font(family=FONT, size=23,
                                    weight="bold").measure(_title)
        self.lbl_ver = tk.Label(self.head, text="v" + core.VERSION,
                                background=HEADER_TOP, foreground=MUTED,
                                font=(FONT, 9), cursor="hand2")
        self._ver_win = self.head.create_window(
            26 + self._title_w + 12, 40, anchor="w", window=self.lbl_ver)
        self.lbl_ver.bind("<Button-1>", self._on_version_click)
        # 标题下面一小段天依蓝，是整块头部唯一的彩色
        self.head.create_line(27, 62, 62, 62, fill=ACCENT, width=3,
                              capstyle="round")
        # 状态文字跟 QQ 连接状态**同一行**（右对齐），不跟主题按钮挤一起。
        # 「未开启」说的是监控开没开，主题按钮是界面偏好 —— 两者没关系，
        # 并排放会让人以为「未开启」修饰的是旁边的按钮。分组要按语义，不是按
        # 好不好对齐。
        self.lbl_conn = CanvasLabel(self.head, self.head.create_text(
            26, 82, anchor="w", text="● 正在检查 …", fill=TEXT,
            font=(FONT, 10)))
        self.lbl_sources = CanvasLabel(self.head, self.head.create_text(
            26, 106, anchor="w", text="", fill=MUTED, font=(FONT, 9)))
        self.lbl_alert = CanvasLabel(self.head, self.head.create_text(
            26, 128, anchor="w", text="", fill=WARN, font=(FONT, 10, "bold")))

        # 整条头部的背景图。**这一句不能少** —— 少了它 _header_img 就没被赋值，
        # _paint_header 里访问会抛 AttributeError，而那是 Tk 的回调，
        # 异常会被吞掉：界面照常启动，只是头图静默消失。
        self._header_img, self._header_src = load_header_image()

        # 右上角：深浅色开关。
        #
        # 两点讲究：
        #  1. 用真正的 Label 而不是 Canvas 文字项 + tag_bind —— 文字项上的
        #     tag_bind 靠不住：rebuild_ui 之后鼠标还停在原位置时会被反复命中，
        #     实测出现过主题自我横跳（一秒切一次）。
        #  2. **必须有边框**。最早它是没有边框的一行字，而 Label 的 padx 会把
        #     文字往里推，于是「浅色」比下一行的「未开启」缩进了 8px —— 看着
        #     就是没对齐。加了边框之后，按钮的**右边缘**跟「未开启」的右边缘
        #     对齐；同时也一眼看得出这是个能点的按钮，而不是一行说明文字。
        # 圆形图标按钮。图标是画出来的，不用字体符号（☾ 在微软雅黑下渲染成
        # 了一个怪模怪样的 C，emoji 又不受控）。
        self.btn_theme = ThemeToggle(
            self.head, command=self.toggle_theme,
            background=HEADER_TOP, dark=(THEME == "dark"))
        self._theme_win = self.head.create_window(0, 34, anchor="e",
                                                  window=self.btn_theme)

        # ---------------- 底部常驻操作栏 ----------------
        # 整个界面最「Apple Music」的一处：主操作不放顶部那个巨大的色块里，
        # 而是收进一条**常驻底栏** —— 左边是当前状态，右边是唯一的主按钮。
        # 两个好处：按钮不再抢戏（Apple Music 从不用大按钮），而且切到任何
        # 标签页它都还在那儿，不用滚回去找。
        #
        # pack 顺序有意为之：先占住底边，中间那块再用 expand 填满剩下的空间。
        bar = tk.Frame(self.root, background=SURFACE)
        bar.pack(side="bottom", fill="x")
        tk.Frame(bar, background=BORDER, height=1).pack(fill="x")   # 发丝分隔线

        inner = tk.Frame(bar, background=SURFACE)
        inner.pack(fill="x", padx=24, pady=16)

        left = tk.Frame(inner, background=SURFACE)
        left.pack(side="left", fill="both", expand=True)

        # 状态行：一个圆点 + 一行字。圆点在监控中会呼吸（见 _pulse_dot），
        # 不监控时是静止的中灰 —— 一眼就能看出「它到底在不在干活」。
        row = tk.Frame(left, background=SURFACE)
        row.pack(anchor="w", fill="x")
        self.dot = tk.Canvas(row, width=14, height=14, background=SURFACE,
                             highlightthickness=0, bd=0)
        self.dot.pack(side="left", padx=(0, 8), pady=(2, 0))
        self._dot = self.dot.create_oval(3, 3, 11, 11, fill=MUTED, outline="")

        self.lbl_hotkey_hint = tk.Label(
            row, justify="left", anchor="w", font=(FONT, 11, "bold"),
            background=SURFACE, foreground=OK_COLOR, bd=0, wraplength=580)
        self.lbl_hotkey_hint.pack(side="left", anchor="w")

        self.lbl_tip = tk.Label(
            left, justify="left", anchor="w", font=(FONT, 9), background=SURFACE,
            foreground=MUTED, wraplength=600,
            text="点一下就开始，之后一直挂着。不影响你自己聊天的 QQ。")
        self.lbl_tip.pack(anchor="w", pady=(3, 0))

        # 右侧一列：按钮在上，状态在它正下方。
        # 状态文字描述的就是这个按钮控制的东西，挂在一起才读得通。
        right = tk.Frame(inner, background=SURFACE)
        right.pack(side="right", padx=(24, 0))
        self.btn_main = RoundedButton(
            right, text="开始直播通知", command=self.toggle_main,
            font_spec=(FONT, 14, "bold"), height=48, radius=24, width=210,
            fill=PRIMARY, fill_active=PRIMARY_D, background=SURFACE)
        self.btn_main.pack()
        self.lbl_state = tk.Label(right, text="", background=SURFACE,
                                  foreground=MUTED, font=(FONT, 9))
        self.lbl_state.pack(pady=(6, 0))

        # ---------------- 标签栏 + 内容区 ----------------
        self.tabbar = TabStrip(
            self.root, ["运行日志", "通知群", "触发方式", "消息与设置"],
            command=self.select_tab)
        self.tabbar.pack(fill="x", padx=16, pady=(6, 0))
        tk.Frame(self.root, background=BORDER, height=1).pack(fill="x")

        body = tk.Frame(self.root, background=BG)
        body.pack(fill="both", expand=True)

        self.tab_log = tk.Frame(body, background=BG)
        self.tab_groups = tk.Frame(body, background=BG)
        self.tab_trigger = tk.Frame(body, background=BG)
        self.tab_message = tk.Frame(body, background=BG)
        self._pages = [self.tab_log, self.tab_groups,
                       self.tab_trigger, self.tab_message]

        self._build_log_tab()
        self._build_groups_tab()
        self._build_trigger_tab()
        self._build_message_tab()

        self._tab_anim = Animator(body)
        self._tab_index = 0
        self.select_tab(0, animate=False)

    def select_tab(self, index, animate=True):
        """切换标签页。

        内容用 pack / pack_forget 换，不用 ttk.Notebook —— 那条标签栏在 clam
        主题下自带分隔线和虚线焦点框，去不掉。

        Fluent 的进入动画是 167ms 快进慢停。tkinter 没有透明度，做不了真正的
        淡入，所以改用位移：内容从上方一点点落到位。位移比透明度更容易被眼睛
        读到，也更好实现 —— 关键是不假装能做做不到的事。
        """
        if not (0 <= index < len(self._pages)):
            return
        changed = (index != getattr(self, "_tab_index", -1))
        self._tab_index = index
        for i, page in enumerate(self._pages):
            if i == index:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        if hasattr(self, "tabbar"):
            self.tabbar.set_active(index, animate=animate)

        if not (animate and changed):
            return
        page = self._pages[index]
        kids = page.winfo_children()
        inner = kids[0] if kids else None
        if inner is None:
            return
        # cget 有时回的是 Tcl_Obj 而不是数字，直接 int() 会炸 —— 先转字符串再取
        try:
            base = int(str(inner.cget("pady")).strip().split()[0])
        except (ValueError, IndexError, tk.TclError):
            base = 16

        def frame(t):
            try:
                inner.config(pady=base + int(round(14 * (1 - t))))
            except tk.TclError:
                pass

        self._tab_anim.run("tab", MOTION_FAST, frame)

    def _build_log_tab(self):
        page = tk.Frame(self.tab_log, background=BG, padx=16, pady=16)
        page.pack(fill="both", expand=True)

        bar = tk.Frame(page, background=BG)
        bar.pack(fill="x", pady=(0, 8))
        tk.Label(bar, text="程序运行记录", background=BG,
                 foreground=MUTED, font=(FONT, 9)).pack(side="left")
        ttk.Button(bar, text="清空", width=8,
                   command=self.clear_log).pack(side="right")
        ttk.Button(bar, text="打开日志文件夹", width=15,
                   command=self.open_log_dir).pack(side="right", padx=(0, 8))

        # 日志框：**固定高度，不再 expand**。
        #
        # 原来它是 fill="both", expand=True，把中间全吃掉 —— 只有两行内容却
        # 撑满一屏，空旷得很难看。改成固定 18 行 + 圆角卡片，整页立刻有了呼吸。
        #
        # 圆角要看得出来，靠的是"文字区底色跟卡片一致"：卡片是 LOG_BG 的圆角块，
        # 里面的 Text 也是 LOG_BG，它自己的直角就藏在圆角里了。
        # **padx/pady 不能给 0。** 给了 0 的话内部 Text 会铺满整张卡片，
        # 把它自己的直角压在圆角上面 —— 圆角就白做了。
        # 注意区分：这是"卡片的内边距"，跟 Text 自己的 padx（文字缩进）不是一回事。
        log_card = RoundedCard(page, fill=LOG_BG, padx=7, pady=7)
        log_card.pack(fill="x")
        self._log_lines = 16
        outer = log_card.body
        # 不用 ScrolledText：它内部挂的是 tk.Scrollbar，在 Windows 上由系统
        # 主题直接绘制，background/troughcolor 一律被忽略，深色日志框右边
        # 会永远吊着一条惨白的系统滚动条。改用 ttk.Scrollbar 手动拼。
        self.sb_log = ttk.Scrollbar(outer, orient="vertical",
                                    style="Log.Vertical.TScrollbar")
        self.txt_log = tk.Text(
            outer, wrap="word", state="disabled", yscrollcommand=self.sb_log.set,
            height=self._log_lines, background=LOG_BG, foreground=LOG_FG,
            insertbackground=LOG_FG, relief="flat", padx=10, pady=8,
            selectbackground=PRIMARY, borderwidth=0, highlightthickness=0,
            font=(pick_log_font(), 9))
        self.sb_log.config(command=self.txt_log.yview)
        # 文字区先占满，滚动条按需再插进来 —— 常驻一条滚动条是噪音
        self.txt_log.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        self._log_bar_shown = False

        # 「上次通知」跟在日志下面，**不再钉死在底边** —— 那样中间会空一大块。
        self._build_last_send_card(page)

        # 页面尺寸一变就重算日志该多高。上面两个约束（内容量、窗口高度）
        # 只有在这里才拿得到真实数值。
        self.tab_log.bind("<Configure>", self._fit_log_height)

    def _sync_log_bar(self):
        """日志装得下就把滚动条收起来。

        内容只有三行却常驻一条滚动条，是纯噪音 —— 现代做法都是按需出现。
        装不下时才插进去，注意用 before= 保证它在文字区右边而不是跑到下面去。
        """
        try:
            _first, last = self.txt_log.yview()
        except tk.TclError:
            return
        need = last < 0.999
        if need and not self._log_bar_shown:
            try:
                self.sb_log.pack(side="right", fill="y", padx=(0, 6), pady=6,
                                 before=self.txt_log)
                self._log_bar_shown = True
            except tk.TclError:
                pass
        elif not need and self._log_bar_shown:
            try:
                self.sb_log.pack_forget()
                self._log_bar_shown = False
            except tk.TclError:
                pass

    def _fit_log_height(self, _event=None):
        """按"内容量"和"窗口高度"一起决定日志框多高。

        固定高度试过，两头不讨好：内容两行时它照样占一大块；窗口矮的时候
        它又把下面的「上次通知」挤出画面（实测 780x611 就撞上了）。

        所以取两个上限里更小的那个：
            · 内容量      —— 够放下就行，上限 16 行
            · 窗口可用高度 —— 减掉标题栏和下面那张卡片，剩下的才是它的
        下限 5 行，再少就看不见上下文了。
        """
        if _event is not None and _event.widget is not self.tab_log:
            return
        try:
            if not self.txt_log.winfo_exists():
                return
        except tk.TclError:
            return

        # 内容行数
        try:
            lines = int(str(self.txt_log.index("end-1c")).split(".")[0])
        except (tk.TclError, ValueError):
            lines = 1
        want = max(5, min(16, lines + 1))

        # 可用高度换算成行数
        try:
            line_h = tkfont.Font(font=self.txt_log.cget("font")).metrics("linespace")
        except Exception:
            line_h = 18
        if line_h < 6:
            line_h = 18
        avail = self.tab_log.winfo_height()
        if avail > 120:
            # 扣掉：顶部那行按钮 ~30、圆角卡片内边距 14、
            #       下面那张卡片 ~92、卡片间距 14、页面上下留白 32、粗算余量 10
            room = int((avail - 192) // line_h)
            want = max(5, min(want, max(5, room)))

        if want != getattr(self, "_log_lines", None):
            self._log_lines = want
            try:
                self.txt_log.configure(height=want)
            except tk.TclError:
                pass
        # 高度一变，可滚动与否也跟着变
        self.root.after_idle(self._sync_log_bar)

    def _build_last_send_card(self, parent):
        """「上次通知」卡片 —— 摆在日志框下面，占住底边。

        这块地方原来是空的（日志框独占整页撑满一屏）。拿来放这个，是因为
        「通知到底发出去没有」恰恰是最该一眼看到、而原来只能去日志里翻的事。
        """
        outer, card = make_card(parent)
        outer.pack(fill="x", pady=(14, 0))
        # 留着给「结果变了闪一下」用 —— outer 的底色就是那圈 1px 描边，
        # 闪它等于闪一圈高亮环，只动一个控件，不用挨个改子控件
        self._last_send_outer = outer
        self._last_send_anim = Animator(outer)

        head = tk.Frame(card, background=CARD)
        head.pack(fill="x")
        tk.Label(head, text="上次通知", background=CARD, foreground=MUTED,
                 font=(FONT, 9)).pack(side="left")
        self.lbl_last_when = tk.Label(head, text="", background=CARD,
                                      foreground=MUTED, font=(FONT, 9))
        self.lbl_last_when.pack(side="right")

        self.lbl_last_send = tk.Label(
            card, text="", background=CARD, foreground=MUTED,
            font=(FONT, 11, "bold"), anchor="w", justify="left",
            wraplength=740)
        self.lbl_last_send.pack(fill="x", pady=(6, 0))

    def _append_from_library(self, kind, widget):
        """从文案库里点一条，追加到指定的文本框里。

        追加而不是替换 —— 用户可能已经写了自己那句，替换掉就没了。
        多条之间用单独一行 --- 分隔，跟读取那边同一套规矩。
        """
        items = TEMPLATE_LIBRARY.get(kind) or []
        if not items:
            return
        win = tk.Toplevel(self.root)
        win.title("文案库")
        win.configure(background=BG)
        win.transient(self.root)

        tk.Label(win, text="点一条加进去（不会覆盖你已经写的）",
                 background=BG, foreground=MUTED, font=(FONT, 9),
                 anchor="w", padx=14, pady=10).pack(fill="x")

        # padx/pady 的二元组只能给 pack/grid —— 控件自身的 -padx/-pady
        # 只吃单个距离，传元组会被拼成 "0 12"，Tk 直接抛 bad screen distance。
        # 这个坑真踩过：文案库一点开就崩，而代码看着跟 .pack(pady=(0, 12)) 一样。
        body = tk.Frame(win, background=BG, padx=14)
        body.pack(fill="both", expand=True, pady=(0, 12))
        for label, tpl in items:
            row = tk.Frame(body, background=CARD, highlightthickness=1,
                           highlightbackground=BORDER)
            row.pack(fill="x", pady=(0, 6))
            tk.Label(row, text=label, width=16, anchor="w", background=CARD,
                     foreground=PRIMARY, font=(FONT, 9, "bold"),
                     padx=8, pady=6).pack(side="left")
            tk.Label(row, text=tpl.replace("\n", " / "), anchor="w",
                     background=CARD, foreground=TEXT, font=(FONT, 9),
                     justify="left").pack(side="left", fill="x", expand=True)

            def pick(_e=None, t=tpl):
                cur = widget.get("1.0", "end-1c").rstrip("\n")
                sep = "\n---\n" if cur.strip() else ""
                widget.insert("end", sep + t)
                win.destroy()

            for w in (row,) + tuple(row.winfo_children()):
                w.bind("<Button-1>", pick)
                try:
                    w.configure(cursor="hand2")
                except tk.TclError:
                    pass
        win.bind("<Escape>", lambda e: win.destroy())

    def _refresh_pool_hint(self):
        """告诉用户「现在有几套、是不是在轮换」。"""
        if not hasattr(self, "lbl_pool"):
            return
        blocks = split_templates(self.txt_tpl.get("1.0", "end-1c"))
        if len(blocks) > 1:
            self.lbl_pool.config(
                text="共 {} 套，每次开播随机挑一套".format(len(blocks)),
                foreground=OK_COLOR)
        elif len(blocks) == 1:
            self.lbl_pool.config(
                text="只有 1 套，每次开播都是这一句。想换着发就在下面加 "
                     "--- 再写一套",
                foreground=MUTED)
        else:
            self.lbl_pool.config(text="", foreground=MUTED)

    def _refresh_last_send(self):
        """把 core.LAST_SEND 摊到界面上。

        和日志的区别：日志是流水，这里只回答一个问题 —— **上一次通知，
        群里到底收到没有**。发失败的时候这一行是红的，扫一眼就知道。
        """
        if not hasattr(self, "lbl_last_send"):
            return
        info = getattr(core, "LAST_SEND", None) or {}
        when = str(info.get("when") or "")
        total = int(info.get("total") or 0)
        if not when or not total:
            self.lbl_last_when.config(text="")
            self.lbl_last_send.config(
                text="还没发过通知。", foreground=MUTED)
            return

        ok = int(info.get("ok") or 0)
        failed = list(info.get("failed") or [])
        what = str(info.get("reason") or "通知")
        self.lbl_last_when.config(text="{}　{}".format(when, what))
        if failed:
            self.lbl_last_send.config(
                text="⚠ 成功 {}/{}，{} 个群没发出去".format(
                    ok, total, len(failed)),
                foreground=BAD_COLOR)
        elif ok == total:
            self.lbl_last_send.config(text="成功 {}/{}".format(ok, total),
                                      foreground=OK_COLOR)
        else:
            self.lbl_last_send.config(text="已发送 {}/{}".format(ok, total),
                                      foreground=WARN)

        # 结果变了就闪一圈高亮环（250ms，Fluent 的常规时长）。
        # 用指纹判断而不是「有没有值」—— 第二次通知来了也得闪。
        sig = (when, what, ok, total, len(failed))
        if sig != self._last_send_sig:
            self._last_send_sig = sig
            self._flash_last_send(PRIMARY if not failed else BAD_COLOR)

    def _flash_last_send(self, color):
        """描边从 color 淡回 BORDER。只动 outer 一个控件，代价极低。"""
        outer = getattr(self, "_last_send_outer", None)
        anim = getattr(self, "_last_send_anim", None)
        if outer is None or anim is None:
            return
        anim.run("flash", MOTION_NORMAL,
                 lambda t: outer.config(background=mix(color, BORDER, t)))

    def _build_groups_tab(self):
        self.sf_groups = ScrollFrame(self.tab_groups)
        self.sf_groups.pack(fill="both", expand=True)
        page = self.sf_groups.body

        outer, card = make_card(page, "已配置的群", padx=12, pady=12)
        outer.pack(fill="x")

        cols = ("on", "gid", "note", "role", "at")
        heads = (("on", "启用", 56), ("gid", "群号", 124), ("note", "备注", 280),
                 ("role", "我的身份", 96), ("at", "@方式", 110))
        self.tree = ttk.Treeview(card, columns=cols, show="headings", height=8)
        for key, title, width in heads:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w", stretch=(key == "note"))
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("odd", background=SUNKEN)

        btns = tk.Frame(card, background=CARD)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="启用 / 禁用", width=13,
                   command=self.toggle_enabled).pack(side="left")
        ttk.Button(btns, text="切换 @方式", width=13,
                   command=self.toggle_at_all).pack(side="left", padx=6)
        ttk.Button(btns, text="自定义 @名单", width=13,
                   command=self.edit_at_list).pack(side="left")
        ttk.Button(btns, text="删除选中", width=11,
                   command=self.remove_group).pack(side="left", padx=6)
        self.lbl_group_count = tk.Label(btns, text="", background=CARD,
                                        foreground=MUTED, font=(FONT, 9))
        self.lbl_group_count.pack(side="right")

        card_hint(card, "@全体成员 只有群主/管理员发了才生效。", pady=(10, 0))

        add_outer, add = make_card(page, "加群", padx=12, pady=12)
        add_outer.pack(fill="x", pady=(12, 0))
        card_hint(add, "先把机器人拉进群，这里才刷得出来。", pady=(0, 8))
        row = tk.Frame(add, background=CARD)
        row.pack(fill="x")
        self.cmb_groups = ttk.Combobox(row, state="readonly", font=(FONT, 9))
        self.cmb_groups.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="刷新", width=8,
                   command=self.fetch_groups).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="添加", width=8,
                   command=self.add_group).pack(side="left", padx=(6, 0))

    def _build_trigger_tab(self):
        """触发方式独立成页 —— 它决定「能不能用」，不该埋在设置列表底部。"""
        self.sf_trigger = ScrollFrame(self.tab_trigger)
        self.sf_trigger.pack(fill="both", expand=True)
        page = self.sf_trigger.body

        self.var_platform = tk.BooleanVar()
        self.var_obs = tk.BooleanVar()
        self.var_hotkey_on = tk.BooleanVar()
        self.var_hotkey = tk.StringVar()
        self.var_proc = tk.BooleanVar()
        self.var_hotkey.trace_add("write", lambda *a: self._refresh_hotkey_hint())
        self.var_hotkey_on.trace_add("write", lambda *a: self._refresh_hotkey_hint())
        # 开关一变，大按钮下面的提示语气也跟着变
        self.var_platform.trace_add("write", lambda *a: self._refresh_hotkey_hint())
        self.var_obs.trace_add("write", lambda *a: self._refresh_hotkey_hint())

        def check(parent, var, text, bold=True):
            """一行「胶囊开关 + 说明」—— 代替原来的 tk.Checkbutton。"""
            return toggle_row(parent, var, text, font=(
                FONT, 10 if bold else 9, "bold" if bold else "normal"))

        # ① 直播间轮询 —— 最通用
        outer, card = make_card(page, "开播时通知　勾选任意一种即可，可多选")
        outer.pack(fill="x")

        check(card, self.var_platform, "① 直播间开播时通知　推荐")
        card_hint(card, "用什么软件播都认，手机播也认。",
                  indent=24, pady=(2, 0))
        self.lbl_platform = tk.Label(card, text="直播间状态：—", background=CARD,
                                     foreground=MUTED, font=(FONT, 9, "bold"),
                                     anchor="w", justify="left")
        self.lbl_platform.pack(anchor="w", padx=(24, 0), pady=(3, 14))

        check(card, self.var_obs, "② OBS 开始推流时通知")
        card_hint(card, "精确到按下「开始推流」那一刻。需要 OBS 启动过一次。",
                  indent=24, pady=(2, 0))
        self.lbl_obs = tk.Label(card, text="OBS 状态：—", background=CARD,
                                foreground=MUTED, font=(FONT, 9, "bold"),
                                anchor="w", justify="left")
        self.lbl_obs.pack(anchor="w", padx=(24, 0), pady=(3, 14))

        check(card, self.var_hotkey_on, "③ 全局快捷键（兜底）")
        card_hint(card, "任何情况下按一下就推。", indent=24, pady=(2, 6))
        hkrow = tk.Frame(card, background=CARD)
        hkrow.pack(anchor="w", padx=(24, 0), pady=(0, 14))
        ttk.Entry(hkrow, textvariable=self.var_hotkey, width=16,
                  font=(FONT, 9)).pack(side="left")
        ttk.Button(hkrow, text="按下组合键设置…",
                   command=self.capture_hotkey).pack(side="left", padx=8)

        check(card, self.var_proc, "④ 直播软件一启动就通知　不推荐")
        card_hint(card, "打开软件 ≠ 开播，会白提醒一次。默认关。",
                  indent=24, pady=(2, 0))
        self.lbl_process = tk.Label(card, text="", background=CARD,
                                    foreground=MUTED, font=(FONT, 9), anchor="w")
        self.lbl_process.pack(anchor="w", padx=(24, 0))

        # ---------------- UP 主订阅 ----------------
        # 跟上面四种并列，因为它是第五种**触发**：上面四种都在回答
        # 「我的直播开始了没」，这一种是「我关注的人更新了没」。
        self.var_sub_on = tk.BooleanVar()
        self.var_sub_dyn = tk.BooleanVar()
        self.var_sub_sec = tk.StringVar()
        self.var_sub_mid = tk.StringVar()
        self.var_sub_note = tk.StringVar()

        s_outer, scard = make_card(page, "UP 主发新视频 / 新动态时通知")
        s_outer.pack(fill="x", pady=(14, 0))
        check(scard, self.var_sub_on, "⑤ UP 主发新视频时通知")
        card_hint(scard, "只播报**订阅之后**发的 —— 刚加上去的时候不会把人家"
                         "几年前的旧东西刷进群。UID 就是空间地址里那串数字。",
                  indent=24, pady=(2, 4))
        check(scard, self.var_sub_dyn, "也通知动态（转发、图文、说说那种）", bold=False)
        card_hint(scard, "只播报订阅之后发的；投稿类动态不重复播。",
                  indent=24, pady=(2, 4))

        self.var_sub_cover = tk.BooleanVar()
        check(scard, self.var_sub_cover, "播报带一张小封面", bold=False)
        card_hint(scard, "B站图床直接切小图（320×180），不占带宽。",
                  indent=24, pady=(2, 8))

        lrow = tk.Frame(scard, background=CARD)
        lrow.pack(fill="x", padx=(24, 0), pady=(0, 8))
        ttk.Button(lrow, text="登录 B站（扫码）", width=18,
                   command=self.login_bili).pack(side="left")
        self.lbl_sub_login = tk.Label(lrow, text="", background=CARD,
                                      foreground=MUTED, font=(FONT, 9))
        self.lbl_sub_login.pack(side="left", padx=8)

        srow = tk.Frame(scard, background=CARD)
        srow.pack(fill="x", padx=(24, 0))
        tk.Label(srow, text="每", background=CARD, foreground=MUTED,
                 font=(FONT, 9)).pack(side="left")
        ttk.Entry(srow, textvariable=self.var_sub_sec, width=6,
                  font=(FONT, 9)).pack(side="left", padx=4)
        tk.Label(srow, text="秒查一次（不低于 60；查太勤会被 B 站拒绝，建议 300）",
                 background=CARD, foreground=MUTED,
                 font=(FONT, 9)).pack(side="left")

        self.tree_sub = ttk.Treeview(scard, columns=("mid", "note"),
                                     show="headings", height=3)
        self.tree_sub.heading("mid", text="UID")
        self.tree_sub.heading("note", text="备注")
        self.tree_sub.column("mid", width=140, anchor="w")
        self.tree_sub.column("note", width=380, anchor="w", stretch=True)
        self.tree_sub.pack(fill="x", padx=(24, 0), pady=(10, 0))
        self.tree_sub.tag_configure("odd", background=SUNKEN)

        abtn = tk.Frame(scard, background=CARD)
        abtn.pack(fill="x", padx=(24, 0), pady=(8, 0))
        ttk.Entry(abtn, textvariable=self.var_sub_mid, width=14,
                  font=(FONT, 9)).pack(side="left")
        ttk.Entry(abtn, textvariable=self.var_sub_note, width=16,
                  font=(FONT, 9)).pack(side="left", padx=6)
        ttk.Button(abtn, text="添加", width=8,
                   command=self.add_sub).pack(side="left")
        ttk.Button(abtn, text="删除选中", width=11,
                   command=self.remove_sub).pack(side="left", padx=6)
        self.lbl_sub_count = tk.Label(abtn, text="", background=CARD,
                                      foreground=MUTED, font=(FONT, 9))
        self.lbl_sub_count.pack(side="right")

        # ---------------- 群聊 ----------------
        self.var_chat_on = tk.BooleanVar()
        self.var_chat_cool = tk.StringVar()

        c_outer, ccard = make_card(page, "群友 @机器人 可以和 AI 聊天")
        c_outer.pack(fill="x", pady=(14, 0))
        check(ccard, self.var_chat_on, "① 允许群友 @机器人 聊天")
        card_hint(ccard, "只聊天，没有工具权限；群友说「@我 提醒我 21:30 交作业」"
                         "也能记提醒。",
                  indent=24, pady=(2, 8))

        self.var_chat_backend = tk.StringVar()
        self.var_chat_model = tk.StringVar()
        brow = tk.Frame(ccard, background=CARD)
        brow.pack(fill="x", padx=(24, 0), pady=(2, 0))
        tk.Label(brow, text="用哪个模型", background=CARD, foreground=TEXT,
                 font=(FONT, 9)).pack(side="left")
        ttk.Combobox(brow, textvariable=self.var_chat_backend, width=10,
                     state="readonly",
                     values=("local", "dsh")).pack(side="left", padx=(8, 8))
        ttk.Entry(brow, textvariable=self.var_chat_model, width=18,
                  font=(FONT, 9)).pack(side="left")
        tk.Label(brow, text="local = 本机模型（不花 token）；dsh = DSH 的 groupchat 档案",
                 background=CARD, foreground=MUTED,
                 font=(FONT, 9)).pack(side="left", padx=8)

        crow = tk.Frame(ccard, background=CARD)
        crow.pack(fill="x", padx=(24, 0))
        tk.Label(crow, text="同一个人两次提问至少隔", background=CARD,
                 foreground=MUTED, font=(FONT, 9)).pack(side="left")
        ttk.Entry(crow, textvariable=self.var_chat_cool, width=6,
                  font=(FONT, 9)).pack(side="left", padx=4)
        tk.Label(crow, text="秒（防刷屏）", background=CARD, foreground=MUTED,
                 font=(FONT, 9)).pack(side="left")
        ttk.Button(crow, text="试一句", width=10,
                   command=self.test_chat).pack(side="left", padx=(12, 0))
        self.lbl_chat_test = tk.Label(ccard, text="", background=CARD,
                                      foreground=MUTED, font=(FONT, 9),
                                      anchor="w", justify="left", wraplength=700)
        self.lbl_chat_test.pack(anchor="w", padx=(24, 0), pady=(6, 0))

        # ---------------- 保存 ----------------
        # 「保存设置」原来只在「消息与设置」页最下面，而订阅开关在这一页 ——
        # 实测有人勾了开关找不到保存按钮，于是"开了却没推送"（2026-09-21）。
        # 两页各给一个，按的是同一个处理。
        tsv = tk.Frame(page, background=BG)
        tsv.pack(fill="x", pady=(16, 0))
        ttk.Button(tsv, text="保存设置", width=14, style="Primary.TButton",
                   command=self.save_config_clicked).pack(side="left")
        # **这一页也得有自己的"已保存"。** 反馈那行字原来只挂在「消息与设置」页，
        # 于是点这一页的保存按钮什么都不显示 —— 用户以为没反应（实测报过），
        # 其实配置已经写下去了。
        self.lbl_saved_trigger = tk.Label(tsv, text="", background=BG,
                                          foreground=OK_COLOR,
                                          font=(FONT, 9, "bold"))
        self.lbl_saved_trigger.pack(side="left", padx=8)
        tk.Label(tsv, text="这一页改了也要保存（订阅开关、轮询间隔都在这一页）",
                 background=BG, foreground=MUTED,
                 font=(FONT, 9)).pack(side="left")

        # ---------------- 下播提示 ----------------
        self.var_offline = tk.BooleanVar()
        self.var_offline_at = tk.BooleanVar()
        self.var_offline_tpl = tk.StringVar()

    def _build_message_tab(self):
        self.sf_message = ScrollFrame(self.tab_message)
        self.sf_message.pack(fill="both", expand=True)
        page = self.sf_message.body

        self.var_title = tk.StringVar()
        self.var_link = tk.StringVar()

        # ---------------- 开播通知文案 ----------------
        m_outer, msg = make_card(page, "开播通知文案")
        m_outer.pack(fill="x")
        # make_card 的标题是 pack 进去的，同一容器不能再混用 grid，
        # 所以下面所有 grid 布局都放在这个子框里。
        grid = tk.Frame(msg, background=CARD)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        # 标签列给个固定宽度，三行的输入框左边缘才会齐
        grid.columnconfigure(0, minsize=80)

        def row_label(row, text, top=False):
            """表单行标签。

            `sticky="w"` 而不是 `"nw"` 是关键：前者让标签在格子里**垂直居中**，
            跟旁边那个更高的输入框对得上；后者是顶对齐，「标题」两个字贴在
            输入框上沿，看着就是没对齐。多行输入框（开播文案）例外 —— 那种
            标签按惯例应该顶对齐，所以留了 top 开关。
            """
            tk.Label(grid, text=text, background=CARD, foreground=TEXT,
                     font=(FONT, 9), anchor="nw" if top else "w").grid(
                row=row, column=0, sticky="nw" if top else "w",
                padx=(0, 16), pady=(5, 0) if top else 0)

        row_label(0, "标题")
        ttk.Entry(grid, textvariable=self.var_title, font=(FONT, 9)).grid(
            row=0, column=1, sticky="we", pady=(0, 12))
        row_label(1, "直播间链接")
        ttk.Entry(grid, textvariable=self.var_link, font=(FONT, 9)).grid(
            row=1, column=1, sticky="we", pady=(0, 12))
        row_label(2, "开播文案", top=True)
        self.txt_tpl = tk.Text(grid, height=8, width=64, wrap="word", font=(FONT, 9),
                               background=SUNKEN, foreground=TEXT,
                               relief="flat", bd=0,
                               highlightthickness=1,
                               highlightbackground=BORDER,
                               highlightcolor=PRIMARY,
                               insertbackground=TEXT,
                               padx=8, pady=6)
        self.txt_tpl.grid(row=2, column=1, sticky="w")
        # 不再放"文案库 + 加进去"那种要用户动手的东西 —— 程序**自带一池文案
        # 自动轮换**，用户想改就直接改上面的框。
        self.lbl_pool = tk.Label(
            grid, text="", background=CARD, foreground=MUTED, font=(FONT, 8),
            anchor="w", justify="left", wraplength=560)
        self.lbl_pool.grid(row=4, column=1, sticky="w", pady=(8, 0))
        self.txt_tpl.bind("<KeyRelease>", lambda e: self._refresh_pool_hint())

        tk.Label(grid,
                 text="占位符：{hook} {room_title} {room_desc} {title} {link} {game} {time} {date} {tod} {weekday}",
                 background=CARD, foreground=MUTED, font=(FONT, 8),
                 anchor="w", justify="left", wraplength=560).grid(
            row=5, column=1, sticky="w", pady=(4, 0))

        self.var_cover = tk.BooleanVar()
        self.var_cover_size = tk.StringVar()
        toggle_row(grid, self.var_cover, "开播通知里带一张小封面图", pady=(16, 0), grid=(7, 1))
        tk.Label(grid, text="用直播间封面，压到几 KB",
                 background=CARD, foreground=MUTED, font=(FONT, 8),
                 anchor="w").grid(row=8, column=1, sticky="w", padx=(24, 0))

        # ---------------- 游戏识别 ----------------
        off_outer, off = make_card(page, "下播时通知")
        off_outer.pack(fill="x", pady=(12, 0))

        # 用 toggle_row 而不是 check —— check 是「触发方式」页里的局部函数，
        # 这块挪过来之后它不在作用域里（实测 NameError）。
        toggle_row(off, self.var_offline,
                   "下播时也发一条（依赖「触发方式」页里的「直播间开播时通知」）",
                   font=(FONT, 10, "bold"))
        card_hint(off, "占位符 {duration} 时长、{peak} 峰值、{game} 游戏、"
                       "{tod} 时段；别写死「今晚」。",
                  indent=24, pady=(3, 8))
        # **多行框，跟开播那边对等。** 原来这里是个单行 Entry ——
        # 连第二条文案都存不下，更别说挑着发。现在能存多条，
        # 用单独一行 --- 分隔，每次下播随机挑一条。
        tplrow = tk.Frame(off, background=CARD)
        tplrow.pack(fill="x", padx=(24, 0))
        self.txt_offline = tk.Text(tplrow, height=5, wrap="word", font=(FONT, 9),
                                   background=SUNKEN, foreground=TEXT,
                                   relief="flat", padx=8, pady=6,
                                   insertbackground=TEXT, borderwidth=0,
                                   highlightthickness=1,
                                   highlightbackground=BORDER,
                                   highlightcolor=PRIMARY)
        self.txt_offline.pack(fill="x")

        orow = tk.Frame(off, background=CARD)
        orow.pack(fill="x", padx=(24, 0), pady=(6, 0))
        ttk.Button(orow, text="从文案库里挑一条加进来", width=22,
                   command=lambda: self._append_from_library("offline",
                                                             self.txt_offline)
                   ).pack(side="left")
        tk.Label(orow, text="多条用单独一行 --- 分隔，每次随机挑一条",
                 background=CARD, foreground=MUTED, font=(FONT, 9)).pack(
                     side="left", padx=8)

        toggle_row(off, self.var_offline_at,
                   "@全体成员（默认不 @ —— 没看直播的人不会关心你几点停）",
                   pady=(11, 0))
        card_hint(off, "转离线后先等 60 秒复核，期间恢复直播就取消。", pady=(7, 0))

        # ---------------- UP 主新投稿的文案 ----------------
        v_outer, vcard = make_card(page, "UP 主发新视频时的文案")
        v_outer.pack(fill="x", pady=(12, 0))
        card_hint(vcard, "占位符 {up} UP 主名字，{title} 视频标题，{link} 视频地址。"
                         "多条用单独一行 --- 分隔，每次随机挑一条；"
                         "拿不到的占位符那一整行会自动消失。",
                  pady=(0, 8))
        self.txt_sub = tk.Text(vcard, height=4, wrap="word", font=(FONT, 9),
                               background=SUNKEN, foreground=TEXT,
                               relief="flat", padx=8, pady=6,
                               insertbackground=TEXT, borderwidth=0,
                               highlightthickness=1,
                               highlightbackground=BORDER,
                               highlightcolor=PRIMARY)
        self.txt_sub.pack(fill="x")
        vrow = tk.Frame(vcard, background=CARD)
        vrow.pack(fill="x", pady=(6, 0))
        ttk.Button(vrow, text="从文案库里挑一条加进来", width=22,
                   command=lambda: self._append_from_library("video",
                                                             self.txt_sub)
                   ).pack(side="left")
        tk.Label(vrow, text="UP 主名单在「触发方式」页里加",
                 background=CARD, foreground=MUTED,
                 font=(FONT, 9)).pack(side="left", padx=8)

        # ---------------- UP 主新动态的文案 ----------------
        # 跟投稿分开写：动态常常只有一句话，用「新片」「这期」那套措辞会
        # 驴唇不对马嘴。
        d_outer, dcard = make_card(page, "UP 主发新动态时的文案")
        d_outer.pack(fill="x", pady=(12, 0))
        card_hint(dcard, "占位符 {up} UP 主名字，{text} 动态正文（太长会截断在标点处），"
                         "{link} 动态地址。多条用单独一行 --- 分隔。",
                  pady=(0, 8))
        self.txt_dyn = tk.Text(dcard, height=4, wrap="word", font=(FONT, 9),
                               background=SUNKEN, foreground=TEXT,
                               relief="flat", padx=8, pady=6,
                               insertbackground=TEXT, borderwidth=0,
                               highlightthickness=1,
                               highlightbackground=BORDER,
                               highlightcolor=PRIMARY)
        self.txt_dyn.pack(fill="x")
        drow = tk.Frame(dcard, background=CARD)
        drow.pack(fill="x", pady=(6, 0))
        ttk.Button(drow, text="从文案库里挑一条加进来", width=22,
                   command=lambda: self._append_from_library("dynamic",
                                                             self.txt_dyn)
                   ).pack(side="left")
        tk.Label(drow, text="没开动态也不影响：这段文案先存着",
                 background=CARD, foreground=MUTED,
                 font=(FONT, 9)).pack(side="left", padx=8)

        g_outer, gcard = make_card(page, "游戏识别　在通知里写清楚「正在玩什么」")
        g_outer.pack(fill="x", pady=(12, 0))

        self.var_game_on = tk.BooleanVar()
        self.var_game_change = tk.BooleanVar()
        self.var_game_ignore = tk.StringVar()

        toggle_row(gcard, self.var_game_on, "自动识别当前在玩的游戏，写进通知里")
        card_hint(gcard, "看窗口和场景配置，认不出就不写。不截图、不上传。",
                  indent=24, pady=(3, 0))

        grow = tk.Frame(gcard, background=CARD)
        grow.pack(anchor="w", padx=(24, 0), pady=(7, 0))
        ttk.Button(grow, text="测试一下现在认成什么",
                   command=self.test_game_detect).pack(side="left")
        self.lbl_game_test = tk.Label(grow, text="", background=CARD,
                                      foreground=MUTED, font=(FONT, 9, "bold"))
        self.lbl_game_test.pack(side="left", padx=10)

        toggle_row(gcard, self.var_game_change, "中途换游戏时补一条（不 @ 任何人）", pady=(11, 0))

        tk.Label(gcard, text="不算游戏的　exe 名，逗号分隔",
                 background=CARD, foreground=TEXT, font=(FONT, 9),
                 anchor="w").pack(anchor="w", pady=(11, 4))
        ttk.Entry(gcard, textvariable=self.var_game_ignore,
                  font=(FONT, 9)).pack(fill="x")

        # ---------------- 开播后二次提醒 ----------------
        r_outer, rcard = make_card(page, "开播后二次提醒")
        r_outer.pack(fill="x", pady=(12, 0))

        self.var_reminder_on = tk.BooleanVar()
        self.var_reminder_minutes = tk.StringVar()
        self.var_reminder_max = tk.StringVar()
        self.var_reminder_tpl = tk.StringVar()

        toggle_row(rcard, self.var_reminder_on, "开播一段时间后再提醒一次")
        card_hint(rcard, "只在直播间确实还开着时才发。",
                  indent=24, pady=(3, 0))

        rrow = tk.Frame(rcard, background=CARD)
        rrow.pack(anchor="w", padx=(24, 0), pady=(8, 0))
        tk.Label(rrow, text="开播后", background=CARD, foreground=TEXT,
                 font=(FONT, 9)).pack(side="left")
        ttk.Entry(rrow, textvariable=self.var_reminder_minutes,
                  width=10, font=(FONT, 9)).pack(side="left", padx=5)
        tk.Label(rrow, text="分钟各一次　本场最多", background=CARD,
                 foreground=TEXT, font=(FONT, 9)).pack(side="left")
        ttk.Spinbox(rrow, from_=1, to=20, textvariable=self.var_reminder_max,
                    width=4).pack(side="left", padx=5)
        tk.Label(rrow, text="条（含开播那条）", background=CARD,
                 foreground=MUTED, font=(FONT, 9)).pack(side="left")

        tk.Label(rcard, text="提醒文案", background=CARD, foreground=TEXT,
                 font=(FONT, 9), anchor="w").pack(anchor="w", pady=(10, 4))
        ttk.Entry(rcard, textvariable=self.var_reminder_tpl,
                  font=(FONT, 9)).pack(fill="x")
        card_hint(rcard, "占位符 {game} {link} {title} {time}　默认不 @ 人",
                  pady=(4, 0))

        # ---------------- 触发与发送 ----------------
        b_outer, beh = make_card(page, "触发与发送")
        b_outer.pack(fill="x", pady=(12, 0))
        bgrid = tk.Frame(beh, background=CARD)
        bgrid.pack(fill="x")

        self.var_interval = tk.StringVar()
        self.var_confirm = tk.StringVar()
        self.var_cooldown = tk.StringVar()
        self.var_change_cd = tk.StringVar()
        self.var_settle = tk.StringVar()
        self.var_test_qq = tk.StringVar()
        self.var_sendgap = tk.StringVar()

        def num_field(c, col, label, var, lo, hi, unit, pad_right=24):
            tk.Label(c, text=label, background=CARD, foreground=TEXT,
                     font=(FONT, 9)).grid(row=0, column=col, sticky="w",
                                          padx=(0, 6))
            ttk.Spinbox(c, from_=lo, to=hi, textvariable=var,
                        width=6).grid(row=0, column=col + 1)
            tk.Label(c, text=unit, background=CARD, foreground=MUTED,
                     font=(FONT, 9)).grid(row=0, column=col + 2,
                                          padx=(4, pad_right))

        num_field(bgrid, 0, "检查间隔", self.var_interval, 1, 3600, "秒", 20)
        num_field(bgrid, 3, "防抖次数", self.var_confirm, 1, 100, "次", 20)
        num_field(bgrid, 6, "冷却时间", self.var_cooldown, 0, 1440, "分钟", 0)

        # 换游戏单独的冷却。原来只在配置里有、界面上没入口 —— 用户想调
        # 「换游戏太频繁」只能去手改 JSON。
        tk.Label(bgrid, text="换游戏冷却", background=CARD, foreground=TEXT,
                 font=(FONT, 9)).grid(row=1, column=3, sticky="w", pady=(12, 0))
        ttk.Spinbox(bgrid, from_=0, to=1440, textvariable=self.var_change_cd,
                    width=6).grid(row=1, column=4, pady=(12, 0))
        tk.Label(bgrid, text="分钟　换游戏后多久内不再播报", background=CARD,
                 foreground=MUTED, font=(FONT, 9)).grid(row=1, column=5,
                                                        columnspan=2, sticky="w",
                                                        padx=(4, 0), pady=(12, 0))

        # 确认时间：新游戏要稳定这么多秒才播报。**这跟冷却不是一回事** ——
        # 它管"是不是真的换了"（切窗口会瞬间改标题），冷却管"播得太密没有"。
        tk.Label(bgrid, text="换游戏确认", background=CARD, foreground=TEXT,
                 font=(FONT, 9)).grid(row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Spinbox(bgrid, from_=0, to=600, textvariable=self.var_settle,
                    width=6).grid(row=2, column=1, pady=(12, 0))
        tk.Label(bgrid, text="秒　新游戏稳定这么久才播报，防切窗口抖动",
                 background=CARD, foreground=MUTED, font=(FONT, 9)).grid(
                     row=2, column=2, columnspan=4, sticky="w", padx=(4, 0),
                     pady=(12, 0))

        tk.Label(bgrid, text="多群发送间隔", background=CARD, foreground=TEXT,
                 font=(FONT, 9)).grid(row=1, column=0, sticky="w", pady=(12, 0))
        ttk.Spinbox(bgrid, from_=0, to=600, textvariable=self.var_sendgap,
                    width=6).grid(row=1, column=1, pady=(12, 0))
        tk.Label(bgrid, text="秒　太快容易被风控", background=CARD,
                 foreground=MUTED, font=(FONT, 9)).grid(
            row=1, column=2, columnspan=6, sticky="w", padx=(4, 0), pady=(12, 0))

        # ---------------- 进程名单 ----------------
        p_outer, proc = make_card(
            page, "进程名单　仅当勾了「④ 直播软件一启动就通知」时生效")
        p_outer.pack(fill="x", pady=(12, 0))
        self.var_procs = tk.StringVar()
        ttk.Entry(proc, textvariable=self.var_procs,
                  font=(FONT, 9)).pack(fill="x")
        ttk.Button(proc, text="列出当前直播相关进程",
                   command=self.list_live_processes).pack(anchor="w", pady=(8, 0))

        # ---------------- 启动行为 ----------------
        boot_outer, boot = make_card(page, "启动行为")
        boot_outer.pack(fill="x", pady=(12, 0))
        self.var_autostart = tk.BooleanVar()
        toggle_row(boot, self.var_autostart, "打开程序后自动开始监控（不用再点大按钮）")
        card_hint(boot, "勾上之后，双击图标就等于直接把监控开起来了——背后会自动拉起 "
                        "NapCat，大约 10 秒后就绪。只想改设置时建议别勾，"
                        "否则每次都白起一遍 NapCat。", indent=24, pady=(4, 0))

        # ---------------- 群文案 ----------------
        #
        # 跟「开播文案」分开：那边是消息骨架（有哪几行、什么顺序），
        # 这边是顶端那句勾人的话。混在一起改一句开场白就得重排整个模板。
        hk_outer, hk = make_card(page, "群文案（开场白）")
        hk_outer.pack(fill="x", pady=(12, 0))
        card_hint(hk, "每次开播从里面**随机挑一句**放在消息最上面，用单独一行 "
                      "--- 分隔。留空就用内置的十条。\n"
                      "写在「开播通知文案」里用 {hook} 引用。",
                  indent=0, pady=(2, 6))
        self.txt_hooks = tk.Text(hk, height=6, wrap="word", font=(FONT, 10),
                                 background=SUNKEN, foreground=TEXT,
                                 relief="flat", padx=10, pady=8,
                                 insertbackground=TEXT, borderwidth=0,
                                 highlightthickness=1,
                                 highlightbackground=BORDER,
                                 highlightcolor=PRIMARY)
        self.txt_hooks.pack(fill="x")

        # ---------------- 测试 ----------------
        #
        # 为什么要有这块：想看一眼消息长什么样，原来只有两条路 ——
        # 去点 2-彩排.bat（命令行），或者真发一次让群友当小白鼠。
        tcard_outer, tcard = make_card(page, "测试")
        tcard_outer.pack(fill="x", pady=(12, 0))

        trow = tk.Frame(tcard, background=CARD)
        trow.pack(fill="x", pady=(2, 0))
        ttk.Button(trow, text="预览将发送的内容", width=20,
                   command=self.preview_messages).pack(side="left")
        card_hint(tcard, "只渲染给你看，**一条都不会发出去**。开了哪几类就列哪几类；"
                         "窗口里可以「换一批」，把池子里的文案多翻几句看看。",
                  indent=0, pady=(6, 0))

        trow2 = tk.Frame(tcard, background=CARD)
        trow2.pack(fill="x", pady=(12, 0))
        tk.Label(trow2, text="测试接收 QQ", background=CARD, foreground=TEXT,
                 font=(FONT, 9)).pack(side="left")
        ttk.Entry(trow2, textvariable=self.var_test_qq, width=16,
                  font=(FONT, 10)).pack(side="left", padx=(8, 8))
        # 开播和下播各一个按钮。原来只有一个"发一条给我"，发的是开播文案 ——
        # 下播那条只能等真的下播才知道长什么样。
        ttk.Button(trow2, text="私聊发开播", width=13,
                   command=lambda: self.send_test_private("live")).pack(side="left")
        ttk.Button(trow2, text="私聊发下播", width=13,
                   command=lambda: self.send_test_private("offline")).pack(
                       side="left", padx=(6, 0))
        ttk.Button(trow2, text="私聊发新投稿", width=15,
                   command=lambda: self.send_test_private("video")).pack(
                       side="left", padx=(6, 0))
        ttk.Button(trow2, text="私聊发新动态", width=15,
                   command=lambda: self.send_test_private("dynamic")).pack(
                       side="left", padx=(6, 0))
        card_hint(tcard,
                  "真的会发，但只发到这个 QQ，不进群；内容是编的。",
                  indent=0, pady=(6, 0))

        # ---------------- 保存 ----------------
        save = tk.Frame(page, background=BG)
        save.pack(fill="x", pady=(16, 0))
        self.btn_save = ttk.Button(save, text="保存设置", width=14,
                                   style="Primary.TButton",
                                   command=self.save_config_clicked)
        self.btn_save.pack(side="left")
        ttk.Button(save, text="放弃修改并重载", width=18,
                   command=self.reload_config).pack(side="left", padx=8)
        self.lbl_saved = tk.Label(save, text="", background=BG,
                                  foreground=OK_COLOR, font=(FONT, 9, "bold"))
        self.lbl_saved.pack(side="left", padx=10)

    # ==================================================================
    #  主按钮：一个按钮管全部
    # ==================================================================

    def toggle_main(self):
        if self.state == STATE_IDLE:
            self.start_all()
        elif self.state == STATE_RUNNING:
            self.stop_all()

    def _set_dot(self, color):
        try:
            self.dot.itemconfig(self._dot, fill=color)
        except tk.TclError:
            pass

    def _pulse_dot(self):
        """监控中让底栏那个状态点呼吸。

        用连续正弦而不是「亮-暗」两帧硬切：硬切看着像在闪，像报警；呼吸才是
        「我在正常工作」。停止监控后不再排下一帧，不会空转。
        """
        if self.state != STATE_RUNNING or not self._pulsing:
            self._pulsing = False
            self._set_dot(MUTED)
            return
        try:
            phase = (time.time() * 1.6) % (2 * math.pi)
            t = (math.sin(phase) + 1) / 2.0            # 0..1
            # 最多往背景退 45%，退到底也还是个看得见的绿，不会闪没
            self._set_dot(mix(OK_COLOR, CARD, 0.45 * (1 - t)))
        except tk.TclError:
            self._pulsing = False                      # 界面重建了，循环自然结束
            return
        self.root.after(60, self._pulse_dot)

    def _set_state(self, state, note=""):
        self.state = state
        # lbl_state 在头部，而头部是跟随主题的浅/深色，所以这里一律用主题里的
        # 语义色（MUTED / OK_COLOR），别再写死颜色 —— 写死的话换到深色主题
        # 就会有一行字看不见。
        if state == STATE_IDLE:
            self.btn_main.config(text="开始直播通知", background=BLUE,
                                 activebackground=BLUE_DARK, state="normal")
            self.lbl_state.config(text="未开启" + ("　" + note if note else ""),
                                  foreground=MUTED)
        elif state == STATE_WORKING:
            self.btn_main.config(text=note or "请稍候 …", background=MUTED,
                                 activebackground=MUTED, state="disabled")
            self.lbl_state.config(text=note or "请稍候 …", foreground=MUTED)
        elif state == STATE_RUNNING:
            self.btn_main.config(text="停止监控", background=RED,
                                 activebackground=RED_DARK, state="normal")
            self.lbl_state.config(text="正在监控" + ("　" + note if note else ""),
                                  foreground=OK_COLOR)

        # 状态点：只有监控中才呼吸。加 _pulsing 是为了防止重复调用 _set_state
        # 时起出好几条并行的动画循环。
        if state == STATE_RUNNING:
            if not self._pulsing:
                self._pulsing = True
                self._pulse_dot()
        else:
            self._pulsing = False
            self._set_dot(MUTED)

    # ==================================================================
    #  主题
    # ==================================================================

    def toggle_theme(self):
        """在浅色 / 深色之间切换，并记住选择。"""
        # 防重入：重建界面期间如果又收到一次点击，会递归拆建控件，
        # 表现成主题疯狂横跳。宁可丢掉一次点击，也不能让界面自己打自己。
        if self._theming:
            return
        self._theming = True
        try:
            new = "dark" if THEME == "light" else "light"
            apply_theme(new)
            self.root.configure(background=BG)

            # 顺手存进配置。_ui_to_cfg 是**原地改** self.cfg 的（只覆盖它认识的
            # 键），所以这个自定义键不会被冲掉。
            if self.cfg is not None:
                try:
                    self.cfg.setdefault("ui", {})["theme"] = new
                    self._write_config()
                except Exception as exc:
                    core.log("主题偏好没存下来：{}".format(exc), "WARN")

            self.rebuild_ui()
            core.log("已切到{}模式。".format("深色" if new == "dark" else "浅色"))
        finally:
            self._theming = False

    def rebuild_ui(self):
        """换主题：把界面整个拆了重建。

        没有「重新着色」这条路 —— 颜色是创建控件时写死的。与其维护一张
        控件清单逐个改色（漏一个就是一个诡异的色块），不如重建：界面本身
        很轻，重建是瞬时的。

        重建会丢两样东西，都得手动接回来：
          · 输入框里的内容 —— 绝大多数来自配置，_cfg_to_ui() 能填回去
          · 日志面板里的文字 —— 用 _restore_log() 从内存缓冲接回来
        当前状态也要重新贴一次，否则按钮会退回默认文案。
        """
        for child in self.root.winfo_children():
            child.destroy()
        self._build_ui()

        if self.cfg is not None:
            try:
                self._cfg_to_ui()
            except Exception as exc:
                core.log("重建界面后回填配置失败：{}".format(exc), "WARN")
        self._set_state(self.state)
        try:
            self._refresh_group_tree()
        except Exception:
            pass
        try:
            self._refresh_hotkey_hint()
        except Exception:
            pass
        self._restore_log()
        if self.state == STATE_RUNNING:
            core.log("配置已重载。**正在运行的监控仍按旧配置跑** —— "
                     "要让它用上新配置，请点「停止监控」再开始。", "WARN")

    def _maybe_autostart(self):
        """配置里开了 auto_start 时，界面一起来就直接进监控，不用点大按钮。"""
        if ((self.cfg or {}).get("behavior") or {}).get("auto_start"):
            core.log("配置里开了「打开就自动开始」，正在自动进入监控 …")
            self.start_all()

    def start_all(self):
        if self.cfg is None:
            messagebox.showerror("无法启动", "配置文件有问题，请先修好。")
            return
        err = self._ui_to_cfg()
        if err:
            messagebox.showerror("填写有误", err)
            return
        err = self._write_config()
        if err:
            messagebox.showerror("保存失败", err)
            return

        self._set_state(STATE_WORKING, "正在启动 NapCat …")
        core.log("=" * 54)
        core.log("开始启动流程")

        cfg = self.cfg
        stop_event = threading.Event()
        self.stop_event = stop_event

        def work():
            # --- 1. 确保 NapCat 在跑 ---
            if not port_open(3000):
                ok, msg = start_napcat()
                if not ok:
                    return "error", msg
                core.log("已拉起 NapCat，等待它登录并开放接口（最多 90 秒）…")
                if not wait_for_port(3000, 90, should_cancel=stop_event.is_set):
                    return "error", ("NapCat 没能在 90 秒内就绪。\n\n"
                                     "如果它需要扫码登录，请用浏览器打开：\n"
                                     "    http://127.0.0.1:6099/webui\n\n"
                                     "（也可以直接用看图软件打开\n"
                                     "  app\\napcat\\cache\\qrcode.png）\n\n"
                                     "扫码登录一次之后，以后就不用再扫了。\n"
                                     "详细过程见「运行日志」标签页。")
                core.log("NapCat 已就绪")
            else:
                core.log("NapCat 已经在运行")
            return "ok", None

        def done(res):
            kind, msg = res if isinstance(res, tuple) else ("error", res)
            if stop_event.is_set():
                self._set_state(STATE_IDLE)
                return
            if kind != "ok":
                self._set_state(STATE_IDLE)
                messagebox.showerror("启动失败", msg)
                return

            # --- 2. 起监控线程 ---
            def monitor():
                rc = 0
                try:
                    rc = core.cmd_watch(cfg, stop_event)
                except Exception:
                    core.log("监控线程异常：\n" + traceback.format_exc(), "ERROR")
                    rc = -1
                finally:
                    # 把返回码交回去。cmd_watch 返回 3 = 已经有别的实例在监控，
                    # 那种情况必须让用户看见，不能假装一切正常。
                    self.root.after(0, lambda code=rc: self._on_monitor_exit(code))

            self.monitor_thread = threading.Thread(target=monitor, daemon=True,
                                                   name="monitor")
            self.monitor_thread.start()
            self._set_state(STATE_RUNNING)
            # 起来之后立刻刷一次顶栏那句「QQ 已就绪」—— 原来它只在**停止监控**
            # 的时候才刷新，于是一直停在"未就绪"，跟日志对不上（用户报的就是这个）。
            self.refresh_status()
            core.log("监控已开始。关掉本窗口或点「停止」都会结束。")

        self.run_async(work, done)

    def stop_all(self):
        self._set_state(STATE_WORKING, "正在停止 …")
        if self.stop_event:
            self.stop_event.set()

        def work():
            # 等监控线程收尾
            deadline = time.time() + 6
            while time.time() < deadline:
                if self.monitor_thread and not self.monitor_thread.is_alive():
                    break
                time.sleep(0.2)
            core.log("正在关闭 NapCat 并释放 QQ …")
            stop_napcat()
            time.sleep(1.0)
            return port_open(3000)

        def done(still_up):
            if still_up:
                core.log("警告：NapCat 可能还没完全退出。", "WARN")
            core.log("已全部停止，QQ 可以正常打开了。")
            self._set_state(STATE_IDLE)
            self.refresh_status()

        self.run_async(work, done)

    def _on_monitor_exit(self, rc=0):
        """监控线程自己退了（异常、外部停止，或被单实例锁挡住）。"""
        if rc == 3:
            # 被锁挡住。这时候**不能**只是静默回到空闲 —— 用户点了开始，
            # 得让他知道为什么没起来。
            self._set_state(STATE_IDLE, "（已有实例在跑）")
            messagebox.showwarning(
                "没启动",
                "已经有一个直播姬在监控了，这次没有重复启动。\n\n"
                "同时跑两个会让群里收到双份通知。要换一个，先把上一个停掉。\n"
                "（详情见「运行日志」标签页）")
            return
        if self.state == STATE_RUNNING:
            self._set_state(STATE_IDLE, "（监控已结束）")

    # ==================================================================
    #  配置
    # ==================================================================

    def reload_config(self):
        """放弃修改、从磁盘重读。

        注意它**替换**了 self.cfg 对象，而监控线程手里攥着旧的子字典引用
        （cfg["subscribe"] 之类）—— 所以重载之后，正在跑的监控不会自动跟上，
        必须停一次再开始。保存设置那条路不同：它是就地 update，能实时生效。
        这个差别不说清楚，用户只会看到"改了没用"。
        """
        try:
            self.cfg = core.load_config(CONFIG_PATH)
        except core.ConfigError as exc:
            self.cfg = None
            messagebox.showerror("配置文件有问题", str(exc))
            return
        self._cfg_to_ui()

    def _cfg_to_ui(self):
        if not self.cfg:
            return
        m, b, w = self.cfg["message"], self.cfg["behavior"], self.cfg["watch"]
        self.var_title.set(m["title"])
        self.var_link.set(m["link"])
        self.txt_tpl.delete("1.0", "end")
        self.txt_tpl.insert("1.0", join_templates(m))
        self.var_cover.set(bool(m.get("cover", True)))
        self.var_interval.set(str(int(w["interval_seconds"])))
        self.var_confirm.set(str(int(w["confirm_checks"])))
        self.var_cooldown.set(str(int(b["cooldown_minutes"])))
        self.var_test_qq.set(str(b.get("test_target") or ""))
        self.txt_hooks.delete("1.0", "end")
        self.txt_hooks.insert("1.0", "\n---\n".join(
            self.cfg["message"].get("hooks") or []))
        # 默认 0，跟 load_config 保持一致（界面写 5 会把修复改回去）
        _gcd = (self.cfg.get("game") or {}).get("change_cooldown_minutes", 0)
        self.var_change_cd.set(str(int(float(_gcd or 0))))
        self.var_settle.set(str(int(float(
            (self.cfg.get("game") or {}).get("change_settle_seconds", 20) or 0))))
        self.var_sendgap.set(str(int(b["send_interval_seconds"])))
        self.var_autostart.set(bool(b.get("auto_start", False)))
        self.var_procs.set("，".join(w["processes"]))

        g = self.cfg.get("game") or {}
        self.var_game_on.set(bool(g.get("enabled", True)))
        self.var_game_change.set(bool(g.get("announce_change", True)))
        self.var_game_ignore.set("，".join(g.get("ignore") or []))
        self.lbl_game_test.config(text="")

        r = self.cfg.get("reminder") or {}
        self.var_reminder_on.set(bool(r.get("enabled", True)))
        self.var_reminder_minutes.set("，".join(
            str(int(x)) for x in (r.get("after_minutes") or [30, 60])))
        self.var_reminder_max.set(str(int(r.get("max_total", 3) or 3)))
        self.var_reminder_tpl.set(r.get("template") or "")

        t = self.cfg.get("trigger") or {}
        self.var_obs.set(bool(t.get("on_obs_stream", True)))
        self.var_proc.set(bool(t.get("on_process_start", False)))
        self.var_platform.set(bool(t.get("on_platform_live", False)))
        self.var_hotkey_on.set(bool(t.get("hotkey")))
        self.var_hotkey.set(t.get("hotkey") or "ctrl+alt+k")
        self._refresh_hotkey_hint()

        om = self.cfg.get("offline_message") or {}
        self.var_offline.set(bool(om.get("enabled", True)))
        self.var_offline_at.set(bool(om.get("at_all", False)))
        self.txt_offline.delete("1.0", "end")
        self.txt_offline.insert("1.0", join_templates(om))

        sc = self.cfg.get("subscribe") or {}
        self.var_sub_on.set(bool(sc.get("enabled", False)))
        self.var_sub_dyn.set(bool(sc.get("dynamics", False)))
        self.var_sub_cover.set(bool(sc.get("cover", True)))
        self.var_sub_sec.set(str(int(sc.get("poll_seconds", 300) or 300)))
        self.txt_sub.delete("1.0", "end")
        self.txt_sub.insert("1.0", join_templates(sc))
        self.txt_dyn.delete("1.0", "end")
        self.txt_dyn.insert("1.0", join_templates(
            {"templates": sc.get("dyn_templates") or [],
             "template": sc.get("dyn_template") or ""}))
        ch = self.cfg.get("chat") or {}
        self.var_chat_on.set(bool(ch.get("enabled", False)))
        self.var_chat_cool.set(str(int(ch.get("cooldown_seconds", 20) or 20)))
        self.var_chat_backend.set(str(ch.get("backend") or "local"))
        self.var_chat_model.set(str(ch.get("local_model") or "qwen2.5:3b"))
        self._refresh_sub_tree()
        self._refresh_sub_login()

        self._refresh_group_tree()

    def _ui_to_cfg(self):
        if not self.cfg:
            return "配置未载入"

        def num(var, name, lo, hi):
            raw = var.get().strip()
            try:
                val = int(raw)
            except ValueError:
                raise ValueError("「{}」必须是数字，当前是：{}".format(name, raw))
            if not (lo <= val <= hi):
                raise ValueError("「{}」应在 {} ~ {} 之间".format(name, lo, hi))
            return val

        try:
            self.cfg["message"]["title"] = self.var_title.get().strip()
            self.cfg["message"]["link"] = self.var_link.get().strip()
            blocks = split_templates(self.txt_tpl.get("1.0", "end-1c"))
            if not blocks:
                raise ValueError("开播文案不能是空的")
            # 只有一段就写回 template，多段才用 templates —— 这样存出来的
            # config.json 对只用一套文案的人来说还是原来的样子。
            self.cfg["message"]["template"] = blocks[0]
            self.cfg["message"]["templates"] = blocks if len(blocks) > 1 else []
            self.cfg["message"]["cover"] = bool(self.var_cover.get())

            old_g = self.cfg.get("game") or {}
            ignore = [x.strip().lower()
                      for x in re.split(r"[,，、]", self.var_game_ignore.get())
                      if x.strip()]
            # **就地 update，不要整块替换。** 这个字典里还有界面不暴露的键，
            # 而整块替换只写回它列出的那几个 —— change_templates（换游戏文案池）
            # 就是这么被吃掉的：用户自己写的换游戏文案，点一次「保存设置」
            # 就永久消失，而且没有任何提示。
            self.cfg.setdefault("game", {}).update({
                "enabled": bool(self.var_game_on.get()),
                # 手工映射表界面上不暴露，重写这一块时绝不能弄丢
                "names": old_g.get("names") or {},
                "ignore": ignore,
                "announce_change": bool(self.var_game_change.get()),
                "change_template": (old_g.get("change_template")
                                    or "换游戏了，现在打《{game}》"),
                # 默认值与 load_config 对齐（那边是 0：不再用冷却压正常换游戏）。
                # 这里写 5 的话，界面一保存就把 1.7.5 那个修复又改了回去。
                "change_cooldown_minutes": old_g.get("change_cooldown_minutes", 0),
            })

            minutes = []
            for part in re.split(r"[,，、]", self.var_reminder_minutes.get()):
                part = part.strip()
                if not part:
                    continue
                try:
                    minutes.append(int(float(part)))
                except ValueError:
                    raise ValueError("二次提醒的时间点必须是数字，用逗号分隔，"
                                     "例如 30,60")
            minutes = sorted(set(m for m in minutes if m > 0))
            old_r = self.cfg.get("reminder") or {}
            # 同上：就地 update。reminder.templates（二次提醒文案池）不在下面
            # 这份键清单里，整块替换会把它连同用户写的句子一起丢掉。
            self.cfg.setdefault("reminder", {}).update({
                "enabled": bool(self.var_reminder_on.get()),
                "after_minutes": minutes,
                "max_total": num(self.var_reminder_max, "二次提醒上限", 1, 20),
                "template": (self.var_reminder_tpl.get().strip()
                             or "还在播～ 现在打《{game}》\n{link}"),
                "at_all": bool(old_r.get("at_all", False)),
            })

            self.cfg["watch"]["interval_seconds"] = num(self.var_interval, "检查间隔", 1, 3600)
            self.cfg["watch"]["confirm_checks"] = num(self.var_confirm, "防抖次数", 1, 100)
            self.cfg["behavior"]["cooldown_minutes"] = num(self.var_cooldown, "冷却时间", 0, 1440)
            self.cfg["behavior"]["test_target"] = self.var_test_qq.get().strip()
            self.cfg["message"]["hooks"] = split_templates(
                self.txt_hooks.get("1.0", "end-1c"))
            # 换游戏的冷却。**就地改 self.cfg["game"]，不要整个替换** ——
            # 那个字典里还有用户在别处填的游戏名映射，换掉就丢了。
            self.cfg.setdefault("game", {})["change_cooldown_minutes"] = num(
                self.var_change_cd, "换游戏冷却", 0, 1440)
            self.cfg.setdefault("game", {})["change_settle_seconds"] = num(
                self.var_settle, "换游戏确认", 0, 600)
            self.cfg["behavior"]["send_interval_seconds"] = num(self.var_sendgap, "多群发送间隔", 0, 600)
            self.cfg["behavior"]["auto_start"] = bool(self.var_autostart.get())
            raw = self.var_procs.get().replace("，", ",").replace("、", ",")
            procs = [p.strip().lower() for p in raw.split(",") if p.strip()]
            if self.var_proc.get() and not procs:
                raise ValueError("勾了「直播软件一启动就通知」，进程名单不能为空")
            self.cfg["watch"]["processes"] = procs or ["obs64.exe"]

            hk = ""
            if self.var_hotkey_on.get():
                hk = self.var_hotkey.get().strip().lower()
                parts = [p for p in hk.split("+") if p]
                if len(parts) < 2:
                    raise ValueError("快捷键格式不对：至少一个修饰键 + 一个键，"
                                     "例如 ctrl+alt+l")
            # 保留 room_id / poll_seconds / platform_proxy —— 界面上不暴露，
            # 但重写 trigger 块时绝不能把它们弄丢
            old_t = self.cfg.get("trigger") or {}
            self.cfg.setdefault("trigger", {}).update({
                "on_obs_stream": bool(self.var_obs.get()),
                "on_process_start": bool(self.var_proc.get()),
                "on_platform_live": bool(self.var_platform.get()),
                "hotkey": hk,
                "room_id": old_t.get("room_id"),
                "poll_seconds": old_t.get("poll_seconds", 30),
                "platform_proxy": old_t.get("platform_proxy", ""),
            })
            # UP 主订阅。开关和名单是两个地方，所以这里要拦一下：
            # 勾了开关却没名单，load_config 会把 enabled 归一成 false，
            # 界面却还显示勾着 —— 用户以为在订阅，其实早关了。
            sub_old = self.cfg.get("subscribe") or {}
            _sub_err = self._sub_switch_error()
            if _sub_err:
                raise ValueError(_sub_err)

            _sblocks = split_templates(self.txt_sub.get("1.0", "end-1c"))
            # 同 reminder/offline：就地 update。at_all 与 sessdata 界面上不暴露，
            # 整块替换会把它们连用户的选择一起丢掉。
            self.cfg.setdefault("subscribe", {}).update({
                "enabled": bool(self.var_sub_on.get()),
                "dynamics": bool(self.var_sub_dyn.get()),
                "cover": bool(self.var_sub_cover.get()),
                "poll_seconds": num(self.var_sub_sec, "订阅轮询间隔", 60, 86400),
                "at_all": bool(sub_old.get("at_all", False)),
            })
            if _sblocks:
                self.cfg["subscribe"]["template"] = _sblocks[0]
                self.cfg["subscribe"]["templates"] = (
                    _sblocks if len(_sblocks) > 1 else [])
            _dblocks = split_templates(self.txt_dyn.get("1.0", "end-1c"))
            if _dblocks:
                self.cfg["subscribe"]["dyn_template"] = _dblocks[0]
                self.cfg["subscribe"]["dyn_templates"] = (
                    _dblocks if len(_dblocks) > 1 else [])

            # 群聊。就地 update —— 这里也有界面上不暴露的键
            # （dsh_profile / node / dsh_bin / at_only）。
            _chat = self.cfg.setdefault("chat", {})
            _chat.update({
                "enabled": bool(self.var_chat_on.get()),
                "cooldown_seconds": num(self.var_chat_cool, "群聊冷却", 0, 3600),
                "backend": (self.var_chat_backend.get() or "local").strip(),
                "local_model": self.var_chat_model.get().strip(),
            })
            if self.var_chat_on.get() and core_chat is not None:
                _n, _b = core_chat.find_dsh(_chat.get("node"), _chat.get("dsh_bin"))
                if not (_n and _b):
                    raise ValueError(
                        "开了群聊，但找不到 node 或 dsh。\n\n"
                        "程序会去找 node.exe 和 @deepseek-ai/dsh 的 bin.js；"
                        "找不到就在配置里手填 chat.node / chat.dsh_bin。\n"
                        "（也可以在命令行跑一次：dsh --profile groupchat \"你好\"）")

            # 下播文案跟开播一样是多条，用单独一行 --- 分隔。
            # 存的时候铺开成 templates 列表 + template（第一条）——
            # **两个都要写**：老版本读的是 template，新版本读的是 templates。
            _oblocks = split_templates(self.txt_offline.get("1.0", "end-1c"))
            old_o = self.cfg.get("offline_message") or {}
            # 同上。这里 template/templates 两个都写，看着没丢东西，
            # 但 grace_seconds 之类仍在映射里 —— 整块替换迟早出事。
            self.cfg.setdefault("offline_message", {}).update({
                "enabled": bool(self.var_offline.get()),
                "template": "🌙 下播了，谢谢陪播\n今天播了 {duration}",
                "templates": [],
                "at_all": bool(self.var_offline_at.get()),
                "grace_seconds": old_o.get("grace_seconds", 60),
            })
            if _oblocks:
                self.cfg["offline_message"]["templates"] = _oblocks
                self.cfg["offline_message"]["template"] = _oblocks[0]
        except ValueError as exc:
            return str(exc)
        return None

    def _write_config(self):
        payload = {k: v for k, v in self.cfg.items() if not k.startswith("_")}
        tmp = CONFIG_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            core.load_config(tmp)
            os.replace(tmp, CONFIG_PATH)
            return None
        except core.ConfigError as exc:
            return str(exc)
        except OSError as exc:
            return "写入失败：{}".format(exc)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def save_config_clicked(self):
        err = self._ui_to_cfg()
        if err:
            self._report_sub_error(err)
            return
        err = self._write_config()
        if err:
            messagebox.showerror("保存失败", err)
            return
        self._saved_hint("✔ 已保存")
        self.root.after(2500, self._clear_saved_hint)

    def _saved_hint(self, text):
        """把「已保存」写到**两页各自的**提示位上。

        两页都有保存按钮，反馈就得跟着按钮走 —— 只在另一页显示的话，
        用户点完看不见任何变化，只会以为按钮坏了（实测踩过）。
        """
        for name in ("lbl_saved", "lbl_saved_trigger"):
            lbl = getattr(self, name, None)
            try:
                if lbl is not None and lbl.winfo_exists():
                    lbl.config(text=text)
            except tk.TclError:
                pass

    def _clear_saved_hint(self):
        """几秒后把「已保存 / 已设为 …」那行提示清掉。

        查的是**此刻**的 lbl_saved，而不是注册时闭包抓到的那个：换主题会重建
        整个界面（rebuild_ui），旧标签那时已经销毁，lambda 一跑就是
        `invalid command name`，经 on_error 弹一个模态错误框 —— 实测
        「保存设置后 2.5 秒内切主题」「设完快捷键后 6 秒内切主题」都能撞到。
        """
        for name in ("lbl_saved", "lbl_saved_trigger"):
            lbl = getattr(self, name, None)
            try:
                if lbl is not None and lbl.winfo_exists():
                    lbl.config(text="")
            except tk.TclError:
                pass

    # ==================================================================
    #  群管理
    # ==================================================================

    def _refresh_group_tree(self):
        if not self.cfg:
            return
        self.tree.delete(*self.tree.get_children())
        # 表格高度跟着群的个数走：只有 2 个群却撑 8 行，底下空一大片很难看；
        # 超过 10 个就固定 10 行，由表格自己滚动。
        self.tree.config(height=max(3, min(10, len(self.cfg["groups"]))))
        for i, g in enumerate(self.cfg["groups"]):
            role_cn = ROLE_CN.get(self.role_of.get(g["group_id"]), "未知")
            at = "@全体成员" if g["at_all"] else (
                "@{}人".format(len(g["at_list"])) if g["at_list"] else "不 @")
            tags = ("odd",) if i % 2 else ()
            self.tree.insert("", "end", iid=str(g["group_id"]), tags=tags,
                             values=("✔" if g["enabled"] else "✘", g["group_id"],
                                     g["note"] or "（无备注）", role_cn, at))
        self.lbl_group_count.config(
            text="共 {} 个群，其中 {} 个已启用".format(
                len(self.cfg["groups"]),
                sum(1 for g in self.cfg["groups"] if g["enabled"])))

    def _selected_group(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在列表里选中一个群。")
            return None
        gid = int(sel[0])
        for g in self.cfg["groups"]:
            if g["group_id"] == gid:
                return g
        return None

    def _persist(self):
        """立刻落盘（加群、改订阅名单都走这条）。

        **必须把订阅卡的两个开关也读进来。** 不读的话：勾上开关、再点
        「添加」，写进文件的仍是旧的开关状态，界面却显示勾着 ——
        用户看到的现象就是"我开了，但它不推送"（2026-09-21 实测）。
        """
        if self.cfg is not None:
            _sub_err = self._sub_switch_error()
            if _sub_err:
                self._report_sub_error(_sub_err)
                return False
            sub = self.cfg.setdefault("subscribe", {})
            sub["enabled"] = bool(self.var_sub_on.get())
            sub["dynamics"] = bool(self.var_sub_dyn.get())
        err = self._write_config()
        if err:
            messagebox.showerror("保存失败", err)
            return False
        self._refresh_group_tree()
        return True

    def _sub_switch_error(self):
        """订阅卡的两个坑，返回错误文案；没问题返回 None。

        「保存设置」和名单的"立刻落盘"两条路共用它 —— 两条路都得拦，
        否则勾了开关再点「添加」，落盘的仍是旧开关状态（实测过）。
        """
        sub = (self.cfg or {}).get("subscribe") or {}
        if self.var_sub_on.get() and not (sub.get("ups") or []):
            return ("勾了「UP 主发新视频时通知」，但一个 UP 主都没加。\n\n"
                    "先在下面的名单里把 UID 加进去。")
        if self._sub_need_login():
            # 这里**不要再教用户去 F12 抄 cookie**：同一个卡片上就有扫码按钮。
            # 实测这行提示过过一次时 —— 界面已经有了扫码登录，提示还在教抄 cookie，
            # 用户照着做只会更糊。
            return ("勾了「也通知动态」，但还没登录 B站。\n\n"
                    "动态接口匿名读不到（实测，B站官方号也一样），"
                    "点上面的「登录 B站（扫码）」扫一下就行，不用抄 cookie。")
        return None

    def _sub_need_login(self):
        """勾了动态、却没有登录态 —— 唯一那种"该去登录"的错误。"""
        sub = (self.cfg or {}).get("subscribe") or {}
        return bool(self.var_sub_dyn.get()) and not str(
            sub.get("sessdata") or "").strip()

    def _report_sub_error(self, err):
        """把订阅卡的错误说清楚；如果是"还没登录"，**顺手把扫码窗口打开**。

        光弹一句"没有登录态"等于把活儿丢回给用户 —— 他刚点的那个保存按钮
        旁边就有登录入口，直接送过去。
        """
        if self._sub_need_login():
            if messagebox.askyesno("要先登录 B站", str(err) + "\n\n现在打开扫码登录？"):
                self.login_bili()
                return
        messagebox.showerror("填写有误", str(err))

    def _refresh_sub_login(self):
        """把"登录了没"写在那行小字上 —— 用户看不见凭据，只能看见这个。"""
        sess = str(((self.cfg or {}).get("subscribe")
                    or {}).get("sessdata") or "").strip()
        if not hasattr(self, "lbl_sub_login"):
            return
        self.lbl_sub_login.config(
            text="B站登录态：已保存" if sess else "B站登录态：未登录（动态要用）",
            foreground=OK_COLOR if sess else MUTED)

    def test_chat(self):
        """真的问一次 DSH，把大肥鱼的回话显示出来。

        为什么要这个按钮：群聊这东西不开张就不知道通不通 —— 配置对不对、
        node 找不找得到、档案在不在、模型答不答，全都要真跑一次才知道。
        这条路**只问模型、不发群**。
        """
        if core_chat is None:
            messagebox.showerror("缺 groupchat.py", "群聊功能需要 app\\groupchat.py。")
            return
        self.lbl_chat_test.config(text="正在问大肥鱼 …（一次要十几秒）",
                                  foreground=MUTED)

        cfg = dict(self.cfg.get("chat") or {})
        cfg["enabled"] = True
        bot = core_chat.ChatBot(cfg, None, log=core.log)

        def work():
            return bot._ask_backend("在群里打个招呼，顺便说说你是干什么的")

        def done(res):
            if isinstance(res, BaseException):
                self.lbl_chat_test.config(text="✘ 没答上来：{}".format(res),
                                          foreground=BAD_COLOR)
                return
            text = core_chat.clean_reply(res, 200)
            self.lbl_chat_test.config(text="✔ 大肥鱼说：" + text,
                                      foreground=OK_COLOR)
            core.log("群聊试一句：{}".format(text))

        self.run_async(work, done)

    def _sample_thumb(self):
        """私聊测试里那张示例图。

        用程序**自带**的一张图（本地文件，不联网）—— 真通知里那个位置是视频或
        动态的封面。这样点一下就能在 QQ 里看见图片段长什么样，而不用等真的
        有人发新视频。
        """
        for name in ("header-light.png", "header-dark.png", "app.ico"):
            for root in (HERE, os.path.join(HERE, "_build")):
                path = os.path.join(root, name)
                if os.path.isfile(path):
                    return "file:///" + path.replace("\\", "/")
        return ""

    def _save_sessdata(self, sess):
        """把登录态写进配置。**只动订阅这几个键。**"""
        if self.cfg is None:
            return
        sub = self.cfg.setdefault("subscribe", {})
        sub["sessdata"] = sess
        # 登录前那次保存是被"还没有登录态"拦下的，所以这里顺手把卡上的开关
        # 落实 —— 不然用户扫完码还得再点一次保存，而他刚才就是想开这个。
        if self.var_sub_on.get():
            sub["enabled"] = True
        if self.var_sub_dyn.get():
            sub["dynamics"] = True
        err = self._write_config()
        if err:
            messagebox.showerror("登录态没存上", err)
            return
        self._refresh_sub_login()
        if hasattr(self, "lbl_saved"):
            self.lbl_saved.config(text="✔ 已登录，设置也存好了")
        core.log("B站登录态已保存。勾上「也通知动态」就能用了。")

    def login_bili(self):
        """扫码登录 B站，拿动态接口要的登录态。

        为什么要做在界面里：动态接口匿名读不到，而让用户自己开 F12 抄 cookie
        不该叫"登录"。**不去读浏览器的 cookie 库** —— 那等于从别人的加密库里
        挖凭据，一个公开分发的工具不该长那样。

        登录态只写进本机 config.json：日志里打码、界面不回显、打包产物里排除。
        """
        if core.bili is None:
            messagebox.showerror("缺 bili.py", "订阅功能需要 app\\bili.py，"
                                               "当前目录里没有。")
            return
        if core_qr is None:
            messagebox.showerror("缺 qr.py", "扫码登录要画二维码，需要 app\\qr.py。")
            return

        win = tk.Toplevel(self.root)
        win.title("登录 B站（扫码）")
        win.configure(background=BG)
        win.geometry("360x470")
        win.transient(self.root)

        tk.Label(win, text="用手机 B站客户端扫这个码",
                 background=BG, foreground=TEXT, font=(FONT, 11, "bold"),
                 pady=10).pack()
        canvas = tk.Canvas(win, width=300, height=300, background="#FFFFFF",
                           highlightthickness=1, highlightbackground=BORDER)
        canvas.pack()
        lbl = tk.Label(win, text="正在生成二维码 …", background=BG,
                       foreground=MUTED, font=(FONT, 9), wraplength=320,
                       justify="center")
        lbl.pack(fill="x", pady=(10, 4))
        tk.Label(win, text="登录态只存在你自己的 config.json 里，"
                           "不会发到任何地方。",
                 background=BG, foreground=MUTED, font=(FONT, 8)).pack()

        login = core.bili.QrLogin()
        state = {"timer": None, "done": False}

        def stop():
            # 老坑：after_cancel 在界面重建后会报 can't delete Tcl command，
            # 所以直接调 Tcl。
            if state["timer"]:
                try:
                    self.root.tk.call("after", "cancel", state["timer"])
                except tk.TclError:
                    pass
                state["timer"] = None

        def draw(grid):
            n = len(grid)
            cell = max(1, 300 // n)
            off = (300 - cell * n) // 2
            canvas.delete("all")
            for r, row in enumerate(grid):
                for c, v in enumerate(row):
                    if v:
                        x, y = off + c * cell, off + r * cell
                        canvas.create_rectangle(x, y, x + cell, y + cell,
                                                fill="#000000", width=0)

        def tick():
            if state["done"] or not win.winfo_exists():
                return

            def work():
                return login.poll()

            def done(res):
                if state["done"] or not win.winfo_exists():
                    return
                if isinstance(res, BaseException):
                    lbl.config(text="查状态失败：{}".format(res), foreground=BAD_COLOR)
                    state["timer"] = self.root.after(4000, tick)
                    return
                kind, sess = res
                if kind == "ok":
                    state["done"] = True
                    stop()
                    self._save_sessdata(sess)
                    lbl.config(text="✔ 登录成功，登录态已保存", foreground=OK_COLOR)
                    win.after(1200, win.destroy)
                elif kind == "scanned":
                    lbl.config(text="扫到了 —— 在手机上点一下「确认登录」",
                               foreground=PRIMARY)
                    state["timer"] = self.root.after(1500, tick)
                elif kind == "expired":
                    lbl.config(text="二维码过期了，点下面的「换一张」",
                               foreground=BAD_COLOR)
                else:
                    state["timer"] = self.root.after(2000, tick)

            self.run_async(work, done)

        def refresh():
            stop()
            lbl.config(text="正在生成二维码 …", foreground=MUTED)
            canvas.delete("all")

            def work():
                return login.start()

            def done(res):
                if state["done"] or not win.winfo_exists():
                    return
                if isinstance(res, BaseException):
                    lbl.config(text="生成失败：{}".format(res), foreground=BAD_COLOR)
                    return
                draw(core_qr.encode(res, border=4))
                lbl.config(text="等扫码 …（手机 B站 → 右上角 → 扫一扫）",
                           foreground=TEXT)
                state["timer"] = self.root.after(2000, tick)

            self.run_async(work, done)

        def close_win():
            state["done"] = True
            stop()
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close_win)
        btns = tk.Frame(win, background=BG)
        btns.pack(pady=10)
        ttk.Button(btns, text="换一张", width=10,
                   command=refresh).pack(side="left", padx=6)
        ttk.Button(btns, text="关闭", width=10,
                   command=close_win).pack(side="left", padx=6)
        refresh()

    def _refresh_sub_tree(self):
        """重画 UP 主名单。只读 self.cfg —— 增删都是先改配置再重画。"""
        if not self.cfg:
            return
        ups = (self.cfg.get("subscribe") or {}).get("ups") or []
        self.tree_sub.delete(*self.tree_sub.get_children())
        self.tree_sub.config(height=max(2, min(8, len(ups))))
        for i, up in enumerate(ups):
            self.tree_sub.insert("", "end", iid=str(up["mid"]),
                                 tags=(("odd",) if i % 2 else ()),
                                 values=(up["mid"],
                                         up.get("note") or "（无备注）"))
        self.lbl_sub_count.config(text="共 {} 个 UP 主".format(len(ups)))

    def add_sub(self):
        if not self.cfg:
            return
        raw = self.var_sub_mid.get().strip()
        if not raw.isdigit():
            messagebox.showinfo(
                "提示", "UID 只能是数字。在 UP 主的空间地址里看：\n"
                        "space.bilibili.com/12345678  →  12345678")
            return
        mid = int(raw)
        sub = self.cfg.setdefault("subscribe", {})
        ups = sub.setdefault("ups", [])
        if any(int(u["mid"]) == mid for u in ups):
            messagebox.showinfo("提示", "这个 UID 已经在名单里了。")
            return
        ups.append({"mid": mid, "enabled": True,
                    "note": self.var_sub_note.get().strip()})
        self.var_sub_mid.set("")
        self.var_sub_note.set("")
        # 立即落盘（跟加群一样），不用再点「保存设置」。但**不替用户勾开关**：
        # 一个会往外发请求的功能，不该因为点了「添加」就自己跑起来。
        self._persist()
        self._refresh_sub_tree()

    def remove_sub(self):
        if not self.cfg:
            return
        sel = self.tree_sub.selection()
        if not sel:
            messagebox.showinfo("提示", "先在名单里选中一个 UP 主。")
            return
        mid = int(sel[0])
        sub = self.cfg.setdefault("subscribe", {})
        sub["ups"] = [u for u in (sub.get("ups") or [])
                      if int(u["mid"]) != mid]
        self._persist()
        self._refresh_sub_tree()

    def toggle_enabled(self):
        g = self._selected_group()
        if g:
            g["enabled"] = not g["enabled"]
            self._persist()

    def toggle_at_all(self):
        g = self._selected_group()
        if not g:
            return
        if g["at_all"]:
            g["at_all"] = False
            if not g["at_list"]:
                self._persist()
                messagebox.showinfo("已改为「@指定人」",
                                    "这个群现在不 @ 任何人。\n\n"
                                    "点「自定义 @名单」填要 @ 的 QQ 号。")
                return
        elif g["at_list"]:
            g["at_list"] = []
        else:
            if self.role_of.get(g["group_id"]) == "member":
                messagebox.showwarning("权限不足",
                                       "你在群 {} 里是普通成员，@全体成员 提醒不到人。\n\n"
                                       "建议继续用 @指定人。".format(g["group_id"]))
            g["at_all"] = True
        self._persist()

    def edit_at_list(self):
        g = self._selected_group()
        if not g:
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("自定义 @名单 —— 群 {}".format(g["group_id"]))
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        ttk.Label(dlg, text="要 @ 的 QQ 号（逗号分隔，留空=不@）",
                  font=(FONT, 9)).pack(anchor="w", padx=16, pady=(16, 6))
        var = tk.StringVar(value=", ".join(str(x) for x in g["at_list"]))
        e = ttk.Entry(dlg, textvariable=var, width=50, font=(FONT, 9))
        e.pack(padx=16)
        e.focus_set()

        def ok():
            raw = var.get().replace("，", ",").replace("、", ",").replace(" ", ",")
            nums = []
            for p in raw.split(","):
                p = p.strip()
                if not p:
                    continue
                if not p.isdigit():
                    messagebox.showerror("格式错误", "「{}」不是纯数字。".format(p), parent=dlg)
                    return
                nums.append(int(p))
            g["at_list"] = nums
            if nums:
                g["at_all"] = False
            dlg.destroy()
            self._persist()

        row = ttk.Frame(dlg)
        row.pack(pady=16)
        ttk.Button(row, text="确定", width=10, command=ok).pack(side="left")
        ttk.Button(row, text="取消", width=10, command=dlg.destroy).pack(side="left", padx=8)

    def remove_group(self):
        g = self._selected_group()
        if not g:
            return
        if not messagebox.askyesno("确认", "不再向群 {} 发通知？".format(g["group_id"])):
            return
        self.cfg["groups"] = [x for x in self.cfg["groups"] if x is not g]
        if not self.cfg["groups"]:
            messagebox.showwarning("注意", "已没有任何群。")
        self._persist()

    def fetch_groups(self):
        if not self.cfg:
            return

        def work():
            ob = core.OneBot(self.cfg["onebot"])
            ok, me = ob.get_login_info()
            if not ok or not isinstance(me, dict):
                return "err", str(me)
            gok, glist = ob.get_group_list()
            if not gok or not isinstance(glist, list):
                return "err", str(glist)
            out = []
            for item in glist:
                try:
                    gid = int(item.get("group_id"))
                except (TypeError, ValueError):
                    continue
                role = core.resolve_role(ob, gid, me.get("user_id"))
                out.append((gid, item.get("group_name") or "",
                            int(item.get("member_count") or 0), role))
            return "ok", out

        def done(res):
            kind, payload = res
            if kind == "err":
                messagebox.showwarning("获取失败",
                                       str(payload) + "\n\n请先点「开始直播通知」把 NapCat 拉起来。")
                return
            self.available_groups = payload
            self.role_of = {gid: r for gid, _, _, r in payload}
            configured = {g["group_id"] for g in self.cfg["groups"]}
            items = ["{}　{}　[{}]　{}人".format(gid, name, ROLE_CN.get(role, role), cnt)
                     for gid, name, cnt, role in
                     sorted(payload, key=lambda x: (x[3] == "member", -x[2]))
                     if gid not in configured]
            self.cmb_groups["values"] = items
            if items:
                self.cmb_groups.current(0)
            self._refresh_group_tree()
            if not items:
                messagebox.showinfo("没有可加的群",
                                    "机器人所在的所有群都已经配置过了。\n\n"
                                    "要加新群：先用你的主号把机器人拉进群，"
                                    "最好设为管理员，然后回来点「刷新」。")

        self.run_async(work, done)

    def add_group(self):
        if not self.cfg:
            return
        sel = self.cmb_groups.get()
        if not sel:
            messagebox.showinfo("提示", "先点「刷新」。")
            return
        try:
            gid = int(sel.split("　")[0].strip())
        except (ValueError, IndexError):
            messagebox.showerror("解析失败", sel)
            return
        if any(g["group_id"] == gid for g in self.cfg["groups"]):
            return
        role = self.role_of.get(gid, "member")
        name = sel.split("　")[1] if "　" in sel else ""
        self.cfg["groups"].append({
            "group_id": gid, "enabled": True,
            "at_all": role in ("owner", "admin"), "at_list": [],
            "note": name.strip(),
        })
        if self._persist() and role == "member":
            messagebox.showwarning("该群没有管理员权限",
                                   "你在群 {} 里是普通成员，已自动设为不 @。\n\n"
                                   "想提醒人请在列表里选中它 →「自定义 @名单」。".format(gid))

    # ==================================================================
    #  状态 / 工具
    # ==================================================================

    def _poll_processes(self):
        """只有启用了进程检测才显示它 —— 否则这行纯属噪音。"""
        if self.cfg and self.var_proc.get():
            watch = list(self.cfg["watch"]["processes"])

            def work():
                return sorted(core.match_processes(core.list_processes(), watch))

            def done(hits):
                if isinstance(hits, Exception):
                    self.lbl_process.config(text="进程检测出错：{}".format(hits),
                                            foreground=BAD_COLOR)
                elif hits:
                    self.lbl_process.config(
                        text="● 已检测到直播软件：{}".format("、".join(hits)),
                        foreground=OK_COLOR)
                else:
                    self.lbl_process.config(text="○ 暂未检测到名单里的进程",
                                            foreground=MUTED)

            self.run_async(work, done)
        elif hasattr(self, "lbl_process"):
            self.lbl_process.config(text="")

        self.root.after(3000, self._poll_processes)

    def _poll_health(self):
        """监控运行时定期检查 NapCat 是否还活着，掉了就自动拉起来。

        没有这个，NapCat 一旦掉线，监控会毫无察觉地继续跑，
        检测到开播也发不出去 —— 表现就是"开播没反应"。
        """
        if self.state == STATE_RUNNING and not self._recovering and not port_open(3000):
            self._recovering = True
            core.log("检测到 NapCat 掉线，正在自动重启 …", "WARN")
            self.lbl_alert.config(text="⚠ NapCat 掉线了，正在自动重连 …")

            def work():
                ok, _ = start_napcat()
                if not ok:
                    return False
                return wait_for_port(3000, 90)

            def done(ok):
                self._recovering = False
                if ok:
                    core.log("NapCat 已重新连上，通知功能恢复正常。")
                    self.lbl_alert.config(text="")
                else:
                    core.log("NapCat 自动重启失败。", "ERROR")
                    self.lbl_alert.config(text="⚠ NapCat 重启失败，通知发不出去")
                    messagebox.showwarning(
                        "NapCat 掉线了",
                        "通知暂时发不出去。\n\n"
                        "请点「停止监控」，再点「开始直播通知」重试；\n"
                        "如果反复失败，查看「运行日志」标签页。")

            self.run_async(work, done)
        elif self.state != STATE_RUNNING:
            self.lbl_alert.config(text="")

        # 顶栏那句「QQ 已就绪」也得跟着变：NapCat 掉线自动拉起之后、
        # 或者用户重开 QQ 之后，这里要能自己纠正过来。32 秒一次，
        # 打的是本机 NapCat，不心疼。
        self._health_ticks = getattr(self, "_health_ticks", 0) + 1
        if self._health_ticks % 4 == 1:
            self.refresh_status()
        self.root.after(8000, self._poll_health)

    def _poll_trigger(self):
        """监控运行时，从控制端口读触发源状态（OBS 连接情况等）显示出来。"""
        if self.state == STATE_RUNNING:
            # 端口和 token 先在主线程取好：work() 跑在后台线程上，
            # 不该让它去读可能正被主线程改写的 self.cfg。
            # 顺带修掉硬编码的 8899 —— control.port 是可配的，改了端口之后
            # 这里原来会一直读不到状态。
            _ctl = (self.cfg or {}).get("control") or {}
            _port = int(_ctl.get("port") or 8899)
            _token = str(_ctl.get("token") or "")

            def work():
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                req = urllib.request.Request(
                    "http://127.0.0.1:{}/status".format(_port))
                if _token:
                    # /status 现在也要鉴权（它会带出群号、当前游戏、直播间标题）
                    req.add_header("Authorization", "Bearer " + _token)
                with opener.open(req, timeout=4) as resp:
                    return json.loads(resp.read().decode("utf-8", "replace"))

            def done(res):
                if isinstance(res, Exception) or not isinstance(res, dict):
                    self.lbl_sources.config(text="触发源：状态读取失败", foreground=HEAD_DIM)
                    self.lbl_obs.config(text="OBS 状态：—", foreground=MUTED)
                    self.lbl_platform.config(text="直播间状态：—", foreground=MUTED)
                    return
                src = res.get("trigger_sources") or {}
                obs = res.get("obs") or "未启用"
                pf = res.get("platform") or "未启用"
                hk = src.get("hotkey")
                ok_hk = res.get("hotkey_ok")

                # ---- 顶部汇总行 ----
                bits = []
                if src.get("platform_live"):
                    if "正在直播" in pf:
                        bits.append("直播间 直播中")
                    elif "未开播" in pf:
                        bits.append("直播间 未开播")
                    else:
                        bits.append("直播间 " + pf)
                if src.get("obs_stream"):
                    if "正在推流" in obs:
                        bits.append("OBS 推流中")
                    elif "已连接" in obs:
                        bits.append("OBS 已连接")
                    else:
                        bits.append("OBS 未连接")
                if hk:
                    bits.append("快捷键 {}：{}".format(
                        hk.upper(), "就绪" if ok_hk else "注册失败"))
                if src.get("process_start"):
                    bits.append("进程检测")
                if bits:
                    self.lbl_sources.config(text="触发源　" + "　·　".join(bits),
                                            foreground=HEAD_DIM)
                else:
                    self.lbl_sources.config(text="触发源：一个都没启用，不会自动通知",
                                            foreground=HEAD_BAD)

                # ---- 触发方式页里的详细状态 ----
                off = "已开启" if res.get("offline_message") else "已关闭"
                self.lbl_platform.config(
                    text="直播间状态：{}　|　下播提示 {}".format(pf, off),
                    foreground=OK_COLOR if "正在直播" in pf else MUTED)
                self.lbl_obs.config(
                    text="OBS 状态：{}".format(obs),
                    foreground=OK_COLOR if "正在推流" in obs else MUTED)

            self.run_async(work, done)
        else:
            self.lbl_sources.config(text="触发源　监控未开启", foreground=HEAD_DIM)
            self.lbl_obs.config(text="OBS 状态：—", foreground=MUTED)
            self.lbl_platform.config(text="直播间状态：—", foreground=MUTED)

        self._refresh_last_send()
        self.root.after(4000, self._poll_trigger)

    def _refresh_hotkey_hint(self):
        """刷新大按钮下面那条提示。

        有了直播间轮询之后，快捷键从「必须记得按」降级成「备用手段」，
        语气也得跟着变 —— 否则用户会以为不按就不会通知。
        """
        if not hasattr(self, "lbl_hotkey_hint"):
            return
        t = (self.cfg or {}).get("trigger") or {}
        hk = (self.var_hotkey.get() if hasattr(self, "var_hotkey") else "") or t.get("hotkey") or ""
        hk_on = self.var_hotkey_on.get() if hasattr(self, "var_hotkey_on") else bool(hk)
        auto = self.var_platform.get() if hasattr(self, "var_platform") else bool(t.get("on_platform_live"))
        obs_on = self.var_obs.get() if hasattr(self, "var_obs") else bool(t.get("on_obs_stream"))

        if auto or obs_on:
            what = []
            if auto:
                what.append("直播间开播")
            if obs_on:
                what.append("OBS 推流")
            text = "已开启自动检测（{}）".format(" + ".join(what))
            if hk and hk_on:
                text += "\n快捷键 {} 可手动推一次".format(hk.upper())
            self.lbl_hotkey_hint.config(foreground=OK_COLOR, text=text)
        elif hk and hk_on:
            self.lbl_hotkey_hint.config(
                foreground=WARN,
                text="⚠ 没开自动检测，开播要手动按 {}".format(hk.upper()))
        else:
            self.lbl_hotkey_hint.config(
                foreground=BAD_COLOR,
                text="⚠ 自动检测和快捷键都没开，开播不会通知任何人")

    def capture_hotkey(self):
        """弹窗：让用户直接按下组合键来设置快捷键（比手打直观，也不会写错格式）。"""
        dlg = tk.Toplevel(self.root)
        dlg.title("设置快捷键")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        ttk.Label(dlg, text="请直接按下你想要的组合键",
                  font=(FONT, 13, "bold")).pack(padx=34, pady=(22, 4))
        ttk.Label(dlg, text="至少要带一个 Ctrl / Alt / Shift",
                  foreground=MUTED).pack()
        shown = tk.Label(dlg, text="等待按键 …", font=(FONT, 17, "bold"),
                         foreground=BLUE, width=22, pady=12)
        shown.pack(padx=34, pady=(10, 4))
        ttk.Label(dlg, text="按 Esc 取消", foreground=MUTED).pack(pady=(0, 18))

        mod_map = {
            "Control_L": "ctrl", "Control_R": "ctrl",
            "Alt_L": "alt", "Alt_R": "alt",
            "Shift_L": "shift", "Shift_R": "shift",
            "Super_L": "win", "Super_R": "win",
        }
        held = set()
        done = {"v": False}

        def pretty(mods, key):
            order = [m for m in ("ctrl", "alt", "shift", "win") if m in mods]
            return "+".join([m.capitalize() for m in order] + [key.upper()])

        def finish(combo):
            if done["v"]:
                return
            done["v"] = True
            self.var_hotkey.set(combo)
            self.var_hotkey_on.set(True)
            dlg.destroy()
            self._refresh_hotkey_hint()
            self.lbl_saved.config(
                text="✔ 已设为 {}，点「保存」并重新开始监控后生效".format(combo.upper()),
                foreground=OK_COLOR)
            self.root.after(6000, self._clear_saved_hint)

        def on_press(ev):
            ks = ev.keysym
            if ks in mod_map:
                held.add(mod_map[ks])
                shown.config(text="等待按键 …", foreground=BLUE)
                return "break"
            if ks == "Escape":
                dlg.destroy()
                return "break"

            mods = set(held)
            st = ev.state
            if st & 0x0004:
                mods.add("ctrl")
            if st & 0x0001:
                mods.add("shift")
            if (st & 0x0008) or (st & 0x20000):
                mods.add("alt")

            key = None
            if len(ks) == 1 and ks.isalnum():
                key = ks.lower()
            elif ks.startswith("F") and ks[1:].isdigit() and 1 <= int(ks[1:]) <= 24:
                key = ks.lower()

            if key is None:
                shown.config(text="这个键不支持", foreground=BAD_COLOR)
                return "break"
            if not mods:
                shown.config(text="要带上 Ctrl / Alt / Shift", foreground=BAD_COLOR)
                return "break"

            combo = "+".join([m for m in ("ctrl", "alt", "shift", "win") if m in mods]
                             + [key])
            shown.config(text=pretty(mods, key), foreground=OK_COLOR)
            dlg.after(450, lambda: finish(combo))
            return "break"

        def on_release(ev):
            held.discard(mod_map.get(ev.keysym, ""))
            return "break"

        dlg.bind("<KeyPress>", on_press)
        dlg.bind("<KeyRelease>", on_release)
        dlg.focus_force()

    def refresh_status(self):
        if self.cfg is None:
            self.lbl_conn.config(text="● 配置未载入", foreground=HEAD_BAD)
            return

        def work():
            ob = core.OneBot(self.cfg["onebot"])
            return ob.get_login_info()

        def done(res):
            ok, data = res if isinstance(res, tuple) else (False, res)
            if ok and isinstance(data, dict):
                self.lbl_conn.config(
                    text="● QQ 已就绪　{}（{}）".format(data.get("nickname"),
                                                        data.get("user_id")),
                    foreground=HEAD_OK)
            else:
                self.lbl_conn.config(text="● QQ 未就绪（点下面的按钮会自动启动）",
                                     foreground=HEAD_DIM)

        self.run_async(work, done)

    def preview_messages(self):
        """测试窗口：把各类消息渲染出来给用户看。**绝不发送。**

        为什么带「换一批」：池子里每类有十几句，随机挑一次只能看见一句 ——
        想挑一套自己喜欢的文案，得能连着翻。**文案好不好，只能靠眼睛看**，
        而原来的窗口关掉再打开才是另一条，翻十次就烦了。
        """
        win = tk.Toplevel(self.root)
        win.title("测试窗口 —— 将要发送的内容（不会真的发）")
        win.configure(background=BG)
        win.geometry("640x600")
        win.transient(self.root)

        head = tk.Label(win,
                        text="下面这些**只是渲染结果**，一条都没发出去。\n"
                             "点「换一批」重新随机挑文案 —— 池子里有十几句，"
                             "多翻几次就知道自己喜欢哪套。",
                        background=BG, foreground=MUTED, font=(FONT, 9),
                        justify="left", anchor="w", padx=16, pady=12)
        head.pack(fill="x")

        box = tk.Text(win, wrap="word", background=LOG_BG, foreground=LOG_FG,
                      relief="flat", padx=16, pady=12, borderwidth=0,
                      highlightthickness=0, font=(pick_log_font(), 10))
        box.pack(fill="both", expand=True, padx=16, pady=(0, 12))

        def fill():
            try:
                items = core.preview_messages(self.cfg)
            except Exception as exc:
                items = [("渲染失败", "{}\n\n（不是没救了：多数是配置里某个模板"
                                     "写坏了，跑一次 1-自检.bat 能看到是哪个）".format(exc))]
            box.config(state="normal")
            box.delete("1.0", "end")
            for title, text in items:
                box.insert("end", "【{}】\n".format(title))
                box.insert("end", text + "\n\n")
            box.config(state="disabled")

        btns = tk.Frame(win, background=BG)
        btns.pack(pady=(0, 14))
        ttk.Button(btns, text="换一批", width=12,
                   command=fill).pack(side="left", padx=6)
        ttk.Button(btns, text="关闭", width=10,
                   command=win.destroy).pack(side="left", padx=6)
        fill()

    def send_test_private(self, kind="live"):
        """私聊发一条测试。**真的会发，但只发到指定的 QQ，不进群。**

        kind 是 "live" 或 "offline"。下播那条要带上时长和峰值 ——
        它们是编的，只为让模板里那几行有东西可填。
        """
        target = (self.var_test_qq.get() or "").strip()
        if not target.isdigit():
            messagebox.showwarning(
                "要填一个 QQ 号", "「测试接收 QQ」得填数字，比如你自己的 QQ 号。\n\n"
                "别填机器人的号 —— QQ 一般不允许给自己发私聊。")
            return

        off_cfg = self.cfg.get("offline_message") or {}
        sub_cfg = self.cfg.get("subscribe") or {}
        if kind == "offline":
            if not off_cfg.get("enabled", True):
                messagebox.showinfo("下播提示是关着的",
                                    "配置里关掉了下播提示，发出来也不代表实际会发。")
            text = core.render_text(
                self.cfg,
                template=core.pick_from(off_cfg.get("templates"),
                                        off_cfg.get("template"), kind="offline"),
                extra={"game": "测试游戏", "duration": "2 小时 15 分", "peak": 42})
        elif kind == "video":
            if not sub_cfg.get("enabled"):
                messagebox.showinfo("订阅是关着的",
                                    "配置里没开 UP 主订阅，发出来也不代表实际会发。")
            text = core.render_text(
                self.cfg,
                template=core.pick_from(sub_cfg.get("templates"),
                                        sub_cfg.get("template"), kind="video"),
                extra={"up": "某位 UP 主", "title": "这条是测试视频的标题",
                       "link": "https://www.bilibili.com/video/BV1xx411c7mD"})
        elif kind == "dynamic":
            if not sub_cfg.get("dynamics"):
                messagebox.showinfo(
                    "动态是关着的",
                    "配置里没开「也通知动态」，或者还没填 subscribe.sessdata。\n"
                    "文案照样可以看，实际不会发。")
            text = core.render_text(
                self.cfg,
                template=core.pick_from(sub_cfg.get("dyn_templates"),
                                        sub_cfg.get("dyn_template"),
                                        kind="dynamic"),
                extra={"up": "某位 UP 主",
                       "text": "这条动态大概是这个长度，专门用来试文案排版。",
                       "link": "https://t.bilibili.com/1234567890123456789"})
        else:
            text = core.render_text(self.cfg, extra={"game": "测试游戏"})

        # 订阅那两类可以顺手带上示例图：图片段到底长什么样，看一眼就知道，
        # 不用等真的有人发新视频。别的类目不带（它们的图是直播封面，走另一条路）。
        message = text
        if kind in ("video", "dynamic") and self.var_sub_cover.get():
            message = core.build_message({"at_all": False, "at_list": []}, text,
                                         image=self._sample_thumb())

        def work():
            ob = core.OneBot(self.cfg["onebot"])
            ok, data = ob.send_private_msg(int(target), message)
            if not ok:
                raise RuntimeError(str(data))
            return text

        def done(result):
            if isinstance(result, BaseException):
                messagebox.showerror(
                    "没发出去",
                    "发到 {} 失败了：\n\n{}\n\n"
                    "常见的两个原因：\n"
                    "  · 填的是机器人自己的号（QQ 不许给自己发）\n"
                    "  · NapCat 没启动或没登录".format(target, result))
                return
            core.log("测试消息已私聊发到 {}。".format(target))
            messagebox.showinfo("发出去了",
                                "已经私聊发到 {}，去看看 QQ。\n\n"
                                "群里没有任何动静。".format(target))

        self.run_async(work, done)

    def test_game_detect(self):
        """点一下，看看现在会被认成在玩什么。"""
        if core_games is None:
            self.lbl_game_test.config(text="✘ 缺 games.py", foreground=BAD_COLOR)
            return
        self.lbl_game_test.config(text="识别中 …", foreground=MUTED)

        def work():
            return core_games.detect(self.cfg)

        def done(result):
            if isinstance(result, BaseException):
                self.lbl_game_test.config(text="✘ 出错：{}".format(result),
                                          foreground=BAD_COLOR)
                return
            name = (result or {}).get("name")
            if not name:
                self.lbl_game_test.config(
                    text="○ 没认出来（不写这一行）", foreground=MUTED)
                return
            win = (result or {}).get("window") or {}
            self.lbl_game_test.config(
                text="✔ {}　（{} {}）".format(
                    name, win.get("class") or "?", win.get("size") or ""),
                foreground=HEAD_OK)

        self.run_async(work, done)

    def list_live_processes(self):
        def work():
            procs = core.list_processes()
            kw = ("obs", "live", "stream", "直播", "bilibili", "douyu",
                  "huya", "douyin", "yylive", "capture", "camera")
            return sorted({p for p in procs if any(k in p.lower() for k in kw)})

        def done(hits):
            if not hits:
                messagebox.showinfo("没发现",
                                    "没找到名字像直播软件的进程。\n"
                                    "去「任务管理器 → 详细信息」看真实进程名，手动填进去。")
                return
            dlg = tk.Toplevel(self.root)
            dlg.title("当前相关进程（双击可填入）")
            dlg.transient(self.root)
            box = tk.Listbox(dlg, width=56, height=min(14, len(hits)), font=("Consolas", 9))
            box.pack(padx=14, pady=14)
            for h in hits:
                box.insert("end", h)

            def use(_e=None):
                s = box.curselection()
                if not s:
                    return
                name = box.get(s[0]).lower()
                cur = self.var_procs.get().strip()
                parts = [p.strip() for p in cur.replace("，", ",").split(",") if p.strip()]
                if name not in [p.lower() for p in parts]:
                    parts.append(name)
                self.var_procs.set("，".join(parts))
                dlg.destroy()

            box.bind("<Double-Button-1>", use)
            ttk.Button(dlg, text="使用选中项", command=use).pack(pady=(0, 14))

        self.run_async(work, done)

    #: 一次最多刷这么多行。日志是**成串**来的（启动、断流、重试、群发失败），
    #: 逐行 insert 每行要 4 次 Tcl 往返 + 一次行号查询 + 一次几何计算 ——
    #: 实测 300 行 2092 ms，界面卡两秒；批着插同样 300 行 13 ms，**差 166 倍**。
    LOG_BATCH = 500

    def _drain_log(self):
        lines = []
        try:
            while len(lines) < self.LOG_BATCH:
                lines.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self._append_log(lines)
        self.root.after(120, self._drain_log)

    def _append_log(self, lines):
        """lines 可以是单行，也可以是一批。**批着插，几何只算一次。**"""
        if isinstance(lines, str):
            lines = [lines]
        if not lines:
            return
        # 同时留一份在内存里：换主题要重建界面，日志面板是新建的空控件，
        # 得靠这个缓冲把内容接回来，否则一切主题日志就清空了。
        self.log_tail.extend(lines)
        if len(self.log_tail) > MAX_LOG_LINES:
            del self.log_tail[:len(self.log_tail) - MAX_LOG_LINES]
        # 用户往上翻着看的时候别硬把他拽到底部 —— 那比卡顿还烦
        try:
            at_bottom = self.txt_log.yview()[1] >= 0.999
        except tk.TclError:
            at_bottom = True
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", "\n".join(lines) + "\n")
        total = int(self.txt_log.index("end-1c").split(".")[0])
        if total > MAX_LOG_LINES:
            self.txt_log.delete("1.0", "{}.0".format(total - MAX_LOG_LINES))
        if at_bottom:
            self.txt_log.see("end")
        self.txt_log.config(state="disabled")
        # 内容变多了：滚动条可能要出现，高度也可能要长。**一批只做一次。**
        self._sync_log_bar()
        self._fit_log_height()

    def _restore_log(self):
        """把内存里那份日志写回新建的面板（换主题用）。"""
        if not self.log_tail:
            return
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", "\n".join(self.log_tail) + "\n")
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")
        self._sync_log_bar()
        self._fit_log_height()

    def clear_log(self):
        self.log_tail = []
        self.txt_log.config(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.config(state="disabled")

    def open_log_dir(self):
        """在资源管理器里打开日志目录 —— 界面上的日志关掉就没了，磁盘上的还在。"""
        path = getattr(core, "LOG_DIR", os.path.join(HERE, "logs"))
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
        if not os.path.isdir(path):
            messagebox.showinfo("找不到日志目录", "还没有生成过日志。")
            return
        try:
            os.startfile(path)
        except OSError as exc:
            messagebox.showerror("打不开", "打不开日志目录：{}".format(exc))

    def run_async(self, work, done=None):
        def runner():
            try:
                result = work()
            except Exception as exc:
                result = exc
            if done is not None:
                # 窗口可能已经销毁了（用户点了托盘里的退出，而这个后台活儿
                # 还在跑）。after 这时候会抛 RuntimeError: main thread is
                # not in main loop —— 是个噪音，不该让它冒到 stderr。
                try:
                    self.root.after(0, lambda: done(result))
                except (RuntimeError, tk.TclError):
                    pass
        threading.Thread(target=runner, daemon=True).start()

    def _start_tray(self):
        """起右下角托盘图标。

        失败也不能影响主程序 —— 没有托盘顶多是回到"必须占着任务栏"的
        老样子，不该让整个界面起不来。
        """
        try:
            icon = os.path.join(HERE, "app.ico")
            if not os.path.isfile(icon):
                icon = os.path.join(HERE, "_build", "app.ico")
            self._tray = tray.TrayIcon(
                tooltip="大肥鱼直播姬",
                icon=icon,
                on_toggle=self.toggle_window,
                on_main=self.toggle_main,
                on_quit=self.quit_app,
                post=lambda fn: self.root.after(0, fn),
                state_text=lambda: ("停止监控" if self.state == STATE_RUNNING
                                    else "开始监控"),
            )
            if not self._tray.start():
                core.log("托盘图标没能创建，程序照常运行。", "WARN")
                self._tray = None
        except Exception as exc:
            core.log("托盘图标起不来：{}".format(exc), "WARN")
            self._tray = None

    def toggle_window(self):
        """显示 / 隐藏主界面。托盘的左键点击和菜单第一项都走这儿。"""
        try:
            if self.root.state() == "withdrawn":
                self.show_window()
            else:
                self.hide_window()
        except tk.TclError:
            pass

    def hide_window(self):
        """收进托盘。**进程和监控都不动。**"""
        try:
            self.root.withdraw()
            core.log("已收进托盘。右键托盘图标可以退出。")
        except tk.TclError:
            pass

    def show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def quit_app(self):
        """**真正的退出。** 只有托盘菜单那条路会走到这儿。"""
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        if self.stop_event:
            self.stop_event.set()
        core.log("退出中，正在关闭 NapCat …")
        try:
            stop_napcat()
        except Exception:
            pass
        self._teardown_and_destroy()

    def _teardown_and_destroy(self):
        """退出前的收尾。关窗口和真退出都要走这儿，别抄两份。"""
        self._pulsing = False
        for obj in (getattr(self, "_tab_anim", None),
                    getattr(self, "btn_main", None),
                    getattr(self, "_last_send_anim", None)):
            if obj is not None and hasattr(obj, "dispose"):
                obj.dispose()
        self._save_geometry()
        try:
            core.remove_log_sink(self.log_queue.put)
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def on_close(self):
        """点 X —— **问一句**：收进托盘继续跑，还是真的退出。

        为什么要问：这程序是挂着才有用的（开着才收得到开播），点 X 的本意
        多半是"别占我任务栏"；但也确实有人是要退出。早先是不问就收托盘，
        用户找不到退出的路；中间简化成直接收托盘，现在按她的要求做回来。
        **不弹是/否那种按钮** —— "是"到底指哪个，每次都得想一下。
        """
        # **永远问，不搞"没在跑就直接收起来"那种捷径** ——
        # 加过一次，结果平时点 X 窗口直接消失、什么反馈都没有，
        # 用户要的"问一句"等于被绕过去了（她报的"关闭按钮的反馈没处理好"）。
        win = tk.Toplevel(self.root)
        win.title("要退出吗？")
        win.configure(background=BG)
        win.transient(self.root)
        win.resizable(False, False)
        tk.Label(win, text="监控还在运行", background=BG, foreground=TEXT,
                 font=(FONT, 11, "bold"), anchor="w").pack(
                     fill="x", padx=18, pady=(16, 4))
        tk.Label(win, text="收进托盘 = 界面关掉，它继续跑\n"
                           "完全退出 = 一起停掉，开播就没有通知了",
                 background=BG, foreground=MUTED, font=(FONT, 9),
                 justify="left", anchor="w").pack(fill="x", padx=18)
        row = tk.Frame(win, background=BG)
        row.pack(fill="x", padx=18, pady=(14, 16))

        def hide():
            win.destroy()
            if self._tray is not None:
                self.hide_window()
                # 窗口一消失就什么反馈都没有了。日志写在"运行日志"页里，
                # 而那一页此刻已经看不见了 —— 用托盘气泡说一句。
                try:
                    self._tray.say("大肥鱼直播姬",
                                   "已收进托盘，还在盯着开播。右键图标可以退出。")
                except Exception:
                    pass
            else:
                core.log("没有托盘图标，界面关掉后通知器仍在后台跑；"
                         "要停请用 app\\退出全部.bat。", "WARN")
                self._teardown_and_destroy()

        def quit_all():
            win.destroy()
            if self.state == STATE_RUNNING:
                if self.stop_event:
                    self.stop_event.set()
                core.log("退出中，正在关闭 NapCat …")
                stop_napcat()
            self._teardown_and_destroy()

        ttk.Button(row, text="收进托盘继续跑", width=16,
                   command=hide).pack(side="left")
        # **别在这里探测样式名。** 为了给这个按钮挑个红色样式，我写过
        # `self.root.tk.call("ttk::style", "names")` —— 它抛
        # `TclError: bad command "names"`，把"关闭"这条路整个炸掉。
        # 一个纯装饰的念头不该有这种权力，所以就用最普通的按钮。
        ttk.Button(row, text="完全退出", width=12,
                   command=quit_all).pack(side="left", padx=8)
        ttk.Button(row, text="取消", width=8,
                   command=win.destroy).pack(side="left")
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + 120
        win.geometry("+{}+{}".format(max(0, x), max(0, y)))

    def _save_geometry(self):
        """把窗口大小和位置记进配置。

        Windows 上的常规期待：下次打开还在原来的地方、还是原来那么大。
        窗口最大化时不存尺寸 —— 存了的话下次会以最大化尺寸开出来，
        而且再也缩不回去（用户是按最大化按钮，不是把窗口拉那么大）。
        """
        if self.cfg is None:
            return
        try:
            if self.root.state() != "normal":
                return
            geo = self.root.geometry()          # 形如 980x760+120+80
            if "x" not in geo or "+" not in geo:
                return
            ui = self.cfg.setdefault("ui", {})
            if ui.get("geometry") == geo:
                return
            ui["geometry"] = geo
            self._write_config()
        except Exception:
            pass        # 存不下就算了，不能因为这个拦住退出


def main():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        try:
            with open(ERROR_LOG, "a", encoding="utf-8") as fh:
                fh.write("无法创建窗口：{}\n".format(exc))
        except OSError:
            pass
        return 1

    # 首次运行（网盘分享的场景）：没有 config.json 就先生成一份。
    # 不这么做的话，用户解压出来直接看到「找不到配置文件」，会以为包坏了。
    if ensure_config():
        core.log("首次运行：已从 config.example.json 生成 config.json，"
                 "请在界面里填好直播间地址和要通知的群。", "WARN")

    app = App(root)          # 保持引用：mainloop 阻塞期间它一直存活

    def on_error(exc_type, exc_value, exc_tb):
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            with open(ERROR_LOG, "a", encoding="utf-8") as fh:
                fh.write("[{}]\n{}\n".format(datetime.now(), detail))
        except OSError:
            pass
        try:
            messagebox.showerror("出了点问题",
                                 "错误已记录到 gui-error.log\n\n" + str(exc_value))
        except Exception:
            pass

    root.report_callback_exception = on_error
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

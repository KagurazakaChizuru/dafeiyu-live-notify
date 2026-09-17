#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ 开播通知器 —— 图形界面
==========================

设计目标：傻瓜化。整个程序只有一个按钮。

    点「开始直播通知」 -> 自动启动 NapCat、等它就绪、开始监控
    点「停止」         -> 停止监控、关闭 NapCat、把你的 QQ 还给你

NapCat 的启动和关闭完全由本程序负责，不需要手动开任何东西。
其余细节（通知群、消息内容、运行日志）放在下方标签页里，不点开就不用管。

只依赖 Python 自带的 tkinter。
"""

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

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

CONFIG_PATH = os.path.join(HERE, "config.json")
ACCOUNT_FILE = os.path.join(HERE, "_account.txt")
NAPCAT_DIR = os.path.join(HERE, "napcat")
ERROR_LOG = os.path.join(HERE, "gui-error.log")

MAX_LOG_LINES = 1500
FONT = "Microsoft YaHei UI"
OK_COLOR = "#137333"
BAD_COLOR = "#c5221f"
MUTED = "#5f6368"
BLUE = "#1a73e8"
BLUE_DARK = "#1557b0"
RED = "#d93025"
RED_DARK = "#a50e0e"

ROLE_CN = {"owner": "群主", "admin": "管理员", "member": "普通成员"}

STATE_IDLE = "idle"
STATE_WORKING = "working"
STATE_RUNNING = "running"


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
    def __init__(self, root):
        self.root = root
        self.root.title("QQ 开播通知器")
        self.root.geometry("920x780")
        self.root.minsize(860, 700)

        self.cfg = None
        self.state = STATE_IDLE
        self.stop_event = None
        self.monitor_thread = None
        self.log_queue = queue.Queue()
        self.available_groups = []
        self.role_of = {}
        self._recovering = False     # 正在自动重启 NapCat？

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
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ==================================================================
    #  界面
    # ==================================================================

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26, font=(FONT, 9))
        style.configure("Treeview.Heading", font=(FONT, 9, "bold"))
        style.configure("TNotebook.Tab", padding=(18, 8), font=(FONT, 10))

        # ---------------- 状态条 ----------------
        head = tk.Frame(self.root, background="#f5f6f8")
        head.pack(fill="x")
        inner = tk.Frame(head, background="#f5f6f8", padx=18, pady=12)
        inner.pack(fill="x")

        self.lbl_conn = tk.Label(inner, text="● 正在检查 …", font=(FONT, 10),
                                 background="#f5f6f8", foreground=MUTED)
        self.lbl_conn.pack(anchor="w")
        self.lbl_state = tk.Label(inner, text="", font=(FONT, 11, "bold"),
                                  background="#f5f6f8", foreground=MUTED)
        self.lbl_state.pack(anchor="w", pady=(4, 0))

        # 实时显示"现在检测到哪些直播软件" —— 让你能亲眼看到检测是否生效
        self.lbl_watch = tk.Label(inner, text="", font=(FONT, 9),
                                  background="#f5f6f8", foreground=MUTED)
        self.lbl_watch.pack(anchor="w", pady=(5, 0))

        tk.Frame(self.root, height=1, background="#dcdde0").pack(fill="x")

        # ---------------- 大按钮区 ----------------
        main = tk.Frame(self.root, padx=28, pady=22)
        main.pack(fill="x")

        self.btn_main = tk.Button(
            main, text="开始直播通知", font=(FONT, 19, "bold"),
            background=BLUE, foreground="white",
            activebackground=BLUE_DARK, activeforeground="white",
            relief="flat", cursor="hand2", bd=0,
            command=self.toggle_main)
        self.btn_main.pack(fill="x", ipady=24)

        # 快捷键提醒：直接贴在大按钮正下方，免得开播时忘了按
        self.lbl_hotkey_hint = tk.Label(
            main, justify="center", font=(FONT, 12, "bold"),
            background="#fff8e1", foreground="#a35b00",
            padx=14, pady=8, wraplength=820)
        self.lbl_hotkey_hint.pack(fill="x", pady=(14, 0))

        self.lbl_tip = tk.Label(
            main, justify="center", font=(FONT, 10), foreground=MUTED,
            text="点一下就开始，之后可以一直挂着。\n"
                 "它跑在一份独立的 QQ 副本上，你自己聊天的 QQ 不受任何影响。")
        self.lbl_tip.pack(pady=(12, 0))

        # ---------------- 细节标签页 ----------------
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=14, pady=(6, 12))
        self.notebook = nb

        self.tab_groups = ttk.Frame(nb)
        self.tab_settings = ttk.Frame(nb)
        self.tab_log = ttk.Frame(nb)
        nb.add(self.tab_log, text="运行日志")
        nb.add(self.tab_groups, text="通知哪些群")
        nb.add(self.tab_settings, text="通知内容与设置")

        self._build_log_tab()
        self._build_groups_tab()
        self._build_settings_tab()

    def _build_log_tab(self):
        f = ttk.Frame(self.tab_log, padding=10)
        f.pack(fill="both", expand=True)
        self.txt_log = ScrolledText(f, wrap="word", state="disabled",
                                    font=("Consolas", 9),
                                    background="#1b1c1e", foreground="#d6d7d9",
                                    insertbackground="#d6d7d9", relief="flat")
        self.txt_log.pack(fill="both", expand=True)
        ttk.Button(f, text="清空日志", command=self.clear_log).pack(anchor="e", pady=(6, 0))

    def _build_groups_tab(self):
        wrap = ttk.Frame(self.tab_groups, padding=12)
        wrap.pack(fill="both", expand=True)

        cols = ("on", "gid", "note", "role", "at")
        heads = (("on", "启用", 60), ("gid", "群号", 130), ("note", "备注", 300),
                 ("role", "我的身份", 100), ("at", "@方式", 120))
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=7)
        for key, title, width in heads:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w", stretch=(key == "note"))
        self.tree.pack(fill="both", expand=True)

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="启用 / 禁用", width=13,
                   command=self.toggle_enabled).pack(side="left")
        ttk.Button(btns, text="切换 @方式", width=13,
                   command=self.toggle_at_all).pack(side="left", padx=6)
        ttk.Button(btns, text="自定义 @名单", width=13,
                   command=self.edit_at_list).pack(side="left")
        ttk.Button(btns, text="删除选中", width=11,
                   command=self.remove_group).pack(side="left", padx=6)

        ttk.Label(wrap, foreground=MUTED, wraplength=840, justify="left",
                  text="@全体成员 只有群主或管理员发出去才生效；普通成员请用"
                       "「切换 @方式」改成 @指定人，再点「自定义 @名单」填 QQ 号。").pack(
            anchor="w", pady=(10, 0))

        add = ttk.LabelFrame(wrap, text=" 加群（机器人要先被拉进那个群） ", padding=10)
        add.pack(fill="x", pady=(12, 0))
        self.cmb_groups = ttk.Combobox(add, state="readonly", font=(FONT, 9))
        self.cmb_groups.pack(side="left", fill="x", expand=True)
        ttk.Button(add, text="刷新", width=8,
                   command=self.fetch_groups).pack(side="left", padx=(8, 0))
        ttk.Button(add, text="添加", width=8,
                   command=self.add_group).pack(side="left", padx=(6, 0))

    def _build_settings_tab(self):
        outer = ttk.Frame(self.tab_settings, padding=14)
        outer.pack(fill="both", expand=True)

        msg = ttk.LabelFrame(outer, text=" 通知内容 ", padding=12)
        msg.pack(fill="x")
        msg.columnconfigure(1, weight=1)

        self.var_title = tk.StringVar()
        self.var_link = tk.StringVar()

        ttk.Label(msg, text="标题").grid(row=0, column=0, sticky="w", pady=5, padx=(0, 10))
        ttk.Entry(msg, textvariable=self.var_title, font=(FONT, 9)).grid(
            row=0, column=1, sticky="we", pady=5)
        ttk.Label(msg, text="直播间链接").grid(row=1, column=0, sticky="w", pady=5, padx=(0, 10))
        ttk.Entry(msg, textvariable=self.var_link, font=(FONT, 9)).grid(
            row=1, column=1, sticky="we", pady=5)
        ttk.Label(msg, text="消息模板").grid(row=2, column=0, sticky="nw", pady=5, padx=(0, 10))
        self.txt_tpl = tk.Text(msg, height=4, wrap="word", font=(FONT, 9))
        self.txt_tpl.grid(row=2, column=1, sticky="we", pady=5)
        ttk.Label(msg, foreground=MUTED,
                  text="可用占位符：{title} {link} {time} {date}　换行写 \\n").grid(
            row=3, column=1, sticky="w")

        beh = ttk.LabelFrame(outer, text=" 触发与发送 ", padding=12)
        beh.pack(fill="x", pady=(12, 0))

        self.var_interval = tk.StringVar()
        self.var_confirm = tk.StringVar()
        self.var_cooldown = tk.StringVar()
        self.var_sendgap = tk.StringVar()

        ttk.Label(beh, text="检查间隔").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Spinbox(beh, from_=1, to=3600, textvariable=self.var_interval, width=6).grid(row=0, column=1)
        ttk.Label(beh, text="秒", foreground=MUTED).grid(row=0, column=2, padx=(4, 20))

        ttk.Label(beh, text="防抖次数").grid(row=0, column=3, sticky="w", padx=(0, 6))
        ttk.Spinbox(beh, from_=1, to=100, textvariable=self.var_confirm, width=6).grid(row=0, column=4)
        ttk.Label(beh, text="次", foreground=MUTED).grid(row=0, column=5, padx=(4, 20))

        ttk.Label(beh, text="冷却时间").grid(row=0, column=6, sticky="w", padx=(0, 6))
        ttk.Spinbox(beh, from_=0, to=1440, textvariable=self.var_cooldown, width=6).grid(row=0, column=7)
        ttk.Label(beh, text="分钟", foreground=MUTED).grid(row=0, column=8, padx=(4, 0))

        ttk.Label(beh, text="多群发送间隔").grid(row=1, column=0, sticky="w", pady=(12, 0))
        ttk.Spinbox(beh, from_=0, to=600, textvariable=self.var_sendgap, width=6).grid(
            row=1, column=1, pady=(12, 0))
        ttk.Label(beh, text="秒（太快容易被风控）", foreground=MUTED).grid(
            row=1, column=2, columnspan=6, sticky="w", padx=(4, 0), pady=(12, 0))

        # ---------------- 触发方式 ----------------
        trig = ttk.LabelFrame(outer, text=" 什么才算「开播了」 ", padding=12)
        trig.pack(fill="x", pady=(12, 0))
        trig.columnconfigure(1, weight=1)

        self.var_obs = tk.BooleanVar()
        self.var_proc = tk.BooleanVar()
        self.var_hotkey = tk.StringVar()
        self.var_hotkey_on = tk.BooleanVar()
        # 输入框一改，大按钮下面的提醒立刻跟着变
        self.var_hotkey.trace_add("write", lambda *a: self._refresh_hotkey_hint())
        self.var_hotkey_on.trace_add("write", lambda *a: self._refresh_hotkey_hint())

        ttk.Checkbutton(trig, variable=self.var_obs,
                        text="OBS 开始推流时自动通知（推荐：精确到按下「开始推流」那一刻）"
                        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self.lbl_obs = ttk.Label(trig, text="OBS 状态：—", foreground=MUTED)
        self.lbl_obs.grid(row=1, column=0, columnspan=3, sticky="w", padx=(22, 0), pady=(2, 8))

        ttk.Checkbutton(trig, variable=self.var_hotkey_on,
                        text="全局快捷键（直播姬 / 直播伴侣 没有接口，用这个手动触发）"
                        ).grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Entry(trig, textvariable=self.var_hotkey, width=16,
                  font=(FONT, 9)).grid(row=3, column=0, sticky="w", padx=(22, 8), pady=(4, 8))
        ttk.Button(trig, text="按下组合键设置…",
                   command=self.capture_hotkey).grid(row=3, column=1, sticky="w", pady=(4, 8))
        ttk.Label(trig, text="点左边按钮直接按组合键，也可以在上面手动输入",
                  foreground=MUTED).grid(row=3, column=2, sticky="w", padx=(10, 0))

        ttk.Checkbutton(trig, variable=self.var_proc,
                        text="直播软件一启动就通知（不推荐 —— 你开软件后还要调设备、试麦，"
                             "这段时间会白提醒一次）"
                        ).grid(row=4, column=0, columnspan=3, sticky="w")
        ttk.Label(trig, text="注意：OBS 的 WebSocket 需要先启动过一次 OBS 才会生成配置；"
                             "它默认就是开启的，端口 4455，密码程序会自动读取。",
                  foreground=MUTED, wraplength=840, justify="left").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

        proc = ttk.LabelFrame(outer, text=" 进程名单（仅当上面勾了「软件一启动就通知」时生效） ", padding=12)
        proc.pack(fill="x", pady=(12, 0))
        self.var_procs = tk.StringVar()
        ttk.Entry(proc, textvariable=self.var_procs, font=(FONT, 9)).pack(fill="x")
        ttk.Button(proc, text="列出当前直播相关进程",
                   command=self.list_live_processes).pack(anchor="w", pady=(8, 0))

        save = ttk.Frame(outer)
        save.pack(fill="x", pady=(14, 0))
        ttk.Button(save, text="保存", width=12,
                   command=self.save_config_clicked).pack(side="left")
        ttk.Button(save, text="重新载入", width=12,
                   command=self.reload_config).pack(side="left", padx=8)
        self.lbl_saved = ttk.Label(save, text="", foreground=OK_COLOR)
        self.lbl_saved.pack(side="left", padx=10)

    # ==================================================================
    #  主按钮：一个按钮管全部
    # ==================================================================

    def toggle_main(self):
        if self.state == STATE_IDLE:
            self.start_all()
        elif self.state == STATE_RUNNING:
            self.stop_all()

    def _set_state(self, state, note=""):
        self.state = state
        if state == STATE_IDLE:
            self.btn_main.config(text="开始直播通知", background=BLUE,
                                 activebackground=BLUE_DARK, state="normal")
            self.lbl_state.config(text="未开启" + ("　" + note if note else ""),
                                  foreground=MUTED)
        elif state == STATE_WORKING:
            self.btn_main.config(text=note or "请稍候 …", background="#9aa0a6",
                                 activebackground="#9aa0a6", state="disabled")
            self.lbl_state.config(text=note or "请稍候 …", foreground=MUTED)
        elif state == STATE_RUNNING:
            self.btn_main.config(text="停止监控", background=RED,
                                 activebackground=RED_DARK, state="normal")
            self.lbl_state.config(text="正在监控，开播会自动通知" + ("　" + note if note else ""),
                                  foreground=OK_COLOR)

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
                try:
                    core.cmd_watch(cfg, stop_event)
                except Exception:
                    core.log("监控线程异常：\n" + traceback.format_exc(), "ERROR")
                finally:
                    self.root.after(0, self._on_monitor_exit)

            self.monitor_thread = threading.Thread(target=monitor, daemon=True,
                                                   name="monitor")
            self.monitor_thread.start()
            self._set_state(STATE_RUNNING)
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

    def _on_monitor_exit(self):
        # 监控线程自己退了（异常或外部停止）
        if self.state == STATE_RUNNING:
            self._set_state(STATE_IDLE, "（监控已结束）")

    # ==================================================================
    #  配置
    # ==================================================================

    def reload_config(self):
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
        self.txt_tpl.insert("1.0", m["template"])
        self.var_interval.set(str(int(w["interval_seconds"])))
        self.var_confirm.set(str(int(w["confirm_checks"])))
        self.var_cooldown.set(str(int(b["cooldown_minutes"])))
        self.var_sendgap.set(str(int(b["send_interval_seconds"])))
        self.var_procs.set("，".join(w["processes"]))

        t = self.cfg.get("trigger") or {}
        self.var_obs.set(bool(t.get("on_obs_stream", True)))
        self.var_proc.set(bool(t.get("on_process_start", False)))
        self.var_hotkey_on.set(bool(t.get("hotkey")))
        self.var_hotkey.set(t.get("hotkey") or "ctrl+alt+k")
        self._refresh_hotkey_hint()

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
            self.cfg["message"]["template"] = self.txt_tpl.get("1.0", "end-1c")
            self.cfg["watch"]["interval_seconds"] = num(self.var_interval, "检查间隔", 1, 3600)
            self.cfg["watch"]["confirm_checks"] = num(self.var_confirm, "防抖次数", 1, 100)
            self.cfg["behavior"]["cooldown_minutes"] = num(self.var_cooldown, "冷却时间", 0, 1440)
            self.cfg["behavior"]["send_interval_seconds"] = num(self.var_sendgap, "多群发送间隔", 0, 600)
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
            self.cfg["trigger"] = {
                "on_obs_stream": bool(self.var_obs.get()),
                "on_process_start": bool(self.var_proc.get()),
                "hotkey": hk,
            }
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
            messagebox.showerror("填写有误", err)
            return
        err = self._write_config()
        if err:
            messagebox.showerror("保存失败", err)
            return
        self.lbl_saved.config(text="✔ 已保存")
        self.root.after(2500, lambda: self.lbl_saved.config(text=""))

    # ==================================================================
    #  群管理
    # ==================================================================

    def _refresh_group_tree(self):
        if not self.cfg:
            return
        self.tree.delete(*self.tree.get_children())
        for g in self.cfg["groups"]:
            role_cn = ROLE_CN.get(self.role_of.get(g["group_id"]), "未知")
            at = "@全体成员" if g["at_all"] else (
                "@{}人".format(len(g["at_list"])) if g["at_list"] else "不 @")
            self.tree.insert("", "end", iid=str(g["group_id"]),
                             values=("✔" if g["enabled"] else "✘", g["group_id"],
                                     g["note"] or "（无备注）", role_cn, at))

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
        err = self._write_config()
        if err:
            messagebox.showerror("保存失败", err)
            return False
        self._refresh_group_tree()
        return True

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
        """每 3 秒把"当前检测到的直播软件"显示出来，让检测逻辑可见。"""
        if self.cfg:
            watch = list(self.cfg["watch"]["processes"])

            def work():
                return sorted(core.match_processes(core.list_processes(), watch))

            def done(hits):
                if isinstance(hits, Exception):
                    self.lbl_watch.config(text="○ 进程检测出错：{}".format(hits),
                                          foreground=BAD_COLOR)
                elif hits:
                    self.lbl_watch.config(
                        text="● 已检测到直播软件：{}".format("、".join(hits)),
                        foreground=OK_COLOR)
                elif self.state == STATE_RUNNING:
                    self.lbl_watch.config(
                        text="○ 监控中，暂未检测到直播软件"
                             "（打开 OBS / 直播伴侣 / 直播姬 就会自动通知）",
                        foreground=MUTED)
                else:
                    self.lbl_watch.config(
                        text="○ 监控未开启 —— 点上面的大按钮才会自动通知",
                        foreground=BAD_COLOR)

            self.run_async(work, done)

        self.root.after(3000, self._poll_processes)

    def _poll_health(self):
        """监控运行时定期检查 NapCat 是否还活着，掉了就自动拉起来。

        没有这个，NapCat 一旦掉线，监控会毫无察觉地继续跑，
        检测到开播也发不出去 —— 表现就是"开播没反应"。
        """
        if self.state == STATE_RUNNING and not self._recovering and not port_open(3000):
            self._recovering = True
            core.log("检测到 NapCat 掉线，正在自动重启 …", "WARN")
            self.lbl_watch.config(text="⚠ NapCat 掉线了，正在自动重连 …",
                                  foreground=BAD_COLOR)

            def work():
                ok, _ = start_napcat()
                if not ok:
                    return False
                return wait_for_port(3000, 90)

            def done(ok):
                self._recovering = False
                if ok:
                    core.log("NapCat 已重新连上，通知功能恢复正常。")
                else:
                    core.log("NapCat 自动重启失败。", "ERROR")
                    messagebox.showwarning(
                        "NapCat 掉线了",
                        "通知暂时发不出去。\n\n"
                        "请点「停止监控」，再点「开始直播通知」重试；\n"
                        "如果反复失败，查看「运行日志」标签页。")

            self.run_async(work, done)

        self.root.after(8000, self._poll_health)

    def _poll_trigger(self):
        """监控运行时，从控制端口读触发源状态（OBS 连接情况等）显示出来。"""
        if self.state == STATE_RUNNING:
            def work():
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open("http://127.0.0.1:8899/status", timeout=4) as resp:
                    return json.loads(resp.read().decode("utf-8", "replace"))

            def done(res):
                if isinstance(res, Exception) or not isinstance(res, dict):
                    self.lbl_obs.config(text="OBS 状态：读取失败", foreground=MUTED)
                    return
                obs = res.get("obs") or "未启用"
                hk = (res.get("trigger_sources") or {}).get("hotkey")
                txt = "OBS 状态：{}".format(obs)
                if hk:
                    txt += "　|　快捷键 {} 已就绪".format(hk.upper())
                color = OK_COLOR if "正在推流" in obs else MUTED
                self.lbl_obs.config(text=txt, foreground=color)

            self.run_async(work, done)
        else:
            self.lbl_obs.config(text="OBS 状态：监控未开启", foreground=MUTED)

        self.root.after(4000, self._poll_trigger)

    def _refresh_hotkey_hint(self):
        """刷新大按钮下面那条快捷键提醒。"""
        if not hasattr(self, "lbl_hotkey_hint"):
            return
        t = (self.cfg or {}).get("trigger") or {}
        hk = (self.var_hotkey.get() if hasattr(self, "var_hotkey") else "") or t.get("hotkey") or ""
        on = self.var_hotkey_on.get() if hasattr(self, "var_hotkey_on") else bool(hk)

        if hk and on:
            self.lbl_hotkey_hint.config(
                background="#fff8e1", foreground="#a35b00",
                text="⚠ 用 直播姬 / 直播伴侣 开播时，记得按一下  {}  \n"
                     "（用 OBS 开播不用按，它会自动通知）".format(hk.upper()))
        elif hk and not on:
            self.lbl_hotkey_hint.config(
                background="#f1f3f4", foreground=MUTED,
                text="快捷键已关闭 —— 用 直播姬 / 直播伴侣 开播时不会自动通知，\n"
                     "只能靠界面上的「立即发送」或控制端口手动触发")
        else:
            self.lbl_hotkey_hint.config(
                background="#fdecea", foreground=BAD_COLOR,
                text="⚠ 还没有设置快捷键 —— 用 直播姬 / 直播伴侣 开播时不会自动通知")

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
            self.root.after(6000, lambda: self.lbl_saved.config(text=""))

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
            self.lbl_conn.config(text="● 配置未载入", foreground=BAD_COLOR)
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
                    foreground=OK_COLOR)
            else:
                self.lbl_conn.config(text="● QQ 未就绪（点下面的按钮会自动启动）",
                                     foreground=MUTED)

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

    def _drain_log(self):
        try:
            while True:
                self._append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log)

    def _append_log(self, line):
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", line + "\n")
        total = int(self.txt_log.index("end-1c").split(".")[0])
        if total > MAX_LOG_LINES:
            self.txt_log.delete("1.0", "{}.0".format(total - MAX_LOG_LINES))
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")

    def clear_log(self):
        self.txt_log.config(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.config(state="disabled")

    def run_async(self, work, done=None):
        def runner():
            try:
                result = work()
            except Exception as exc:
                result = exc
            if done is not None:
                self.root.after(0, lambda: done(result))
        threading.Thread(target=runner, daemon=True).start()

    def on_close(self):
        if self.state == STATE_RUNNING or port_open(3000):
            ans = messagebox.askyesnocancel(
                "还在运行",
                "监控还在运行。\n\n"
                "是 —— 全部停止，然后退出\n"
                "否 —— 只关界面，通知器继续在后台跑\n"
                "取消 —— 什么都不做")
            if ans is None:
                return
            if ans:
                if self.stop_event:
                    self.stop_event.set()
                core.log("退出中，正在关闭 NapCat …")
                stop_napcat()
        core.remove_log_sink(self.log_queue.put)
        self.root.destroy()


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

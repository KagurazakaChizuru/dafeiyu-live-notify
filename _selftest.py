#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端自测 —— 不需要真 NapCat、不需要真 QQ。
=============================================

在同一個进程里：启动假 NapCat -> 造一份测试配置 -> 依次跑 check / test / send，
最后单独验证触发引擎的状态机逻辑。

验证内容：
  1. 代理绕过是否生效（本机 127.0.0.1 直连，不被系统代理劫持）
  2. 自检能否正确识别群权限（owner / member）
  3. at_all 和 at_list 两种消息格式是否拼装正确
  4. 禁用群是否被跳过
  5. 触发引擎：防抖、只触发一次、冷却拦截、退出后重新武装

用法：  python _selftest.py
"""

import gc
import io
import json
import os
import re
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import live_notify                                    # noqa: E402
from _mock_napcat import Handler, PORT, GROUPS         # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_CONFIG = os.path.join(HERE, "_test-config.json")


def make_config():
    cfg = {
        "onebot": {"base_url": "http://127.0.0.1:{}".format(PORT),
                   "access_token": "", "timeout": 5},
        "groups": [
            {"group_id": 123456789, "enabled": True, "at_all": True, "note": "群主群"},
            {"group_id": 222222222, "enabled": True, "at_all": False,
             "at_list": [10002, 10003], "note": "普通成员群-用 at_list"},
            {"group_id": 333333333, "enabled": True, "at_all": True, "note": "无权限群"},
            {"group_id": 444444444, "enabled": False, "at_all": True, "note": "禁用群"},
        ],
        "watch": {"enabled": True, "interval_seconds": 5,
                  "processes": ["obs64.exe"], "confirm_checks": 2,
                  "stop_grace_seconds": 60},
        "message": {"template": "🔴 我开播啦！\n\n{title}\n{link}\n\n大家快来捧场～",
                    "title": "今晚直播", "link": "https://example.com/live"},
        "behavior": {"cooldown_minutes": 30, "send_interval_seconds": 0, "dry_run": False},
        "control": {"enabled": False, "port": 8899, "token": ""},
    }
    with open(TEST_CONFIG, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return TEST_CONFIG


def run_engine_tests(path):
    """直接测试触发引擎状态机，不依赖网络。"""
    failures = []

    def check(name, cond, detail=""):
        if cond:
            print("  [PASS] {}".format(name))
        else:
            print("  [FAIL] {}  {}".format(name, detail))
            failures.append(name)

    # ---------- 用例 A：防抖 + 冷却 ----------
    # 注意分工：防抖（连续命中 N 次才算开播）是 TriggerEngine 的活；
    # 冷却（同一次开播不许发两遍）由外层 CooldownGate 管，TriggerEngine
    # 自己完全不看冷却。所以这里必须像 cmd_watch 那样把两者接起来测 ——
    # 直接把 fired.append 当回调，测的是一个根本不存在的行为。
    cfg = live_notify.load_config(path)
    cfg["watch"]["processes"] = ["obs64.exe"]
    cfg["watch"]["confirm_checks"] = 2
    cfg["watch"]["stop_grace_seconds"] = 0
    cfg["behavior"]["cooldown_minutes"] = 30

    fired = []
    gate = live_notify.triggers.CooldownGate(30)

    def fire(reason):
        allowed, _remain = gate.allow()
        if not allowed:
            return
        gate.mark()
        fired.append(reason)

    eng = live_notify.TriggerEngine(cfg, fire)
    state = {"procs": ["explorer.exe"]}
    live_notify.list_processes = lambda: list(state["procs"])

    eng.tick()
    check("没有目标进程时不触发", len(fired) == 0)

    state["procs"] = ["explorer.exe", "obs64.exe"]
    eng.tick()
    check("第 1 次命中被防抖拦住", len(fired) == 0, "fired={}".format(len(fired)))

    eng.tick()
    check("第 2 次命中触发一次", len(fired) == 1, "fired={}".format(len(fired)))

    eng.tick()
    eng.tick()
    check("持续命中不会重复触发", len(fired) == 1, "fired={}".format(len(fired)))

    check("触发原因里带上了进程名", bool(fired) and "obs64.exe" in fired[0], repr(fired))

    state["procs"] = ["explorer.exe"]
    eng.tick()
    state["procs"] = ["obs64.exe"]
    eng.tick()
    eng.tick()
    check("冷却期内重启软件不会重发", len(fired) == 1, "fired={}".format(len(fired)))

    # ---------- 用例 B：关闭冷却后能重新武装 ----------
    cfg2 = live_notify.load_config(path)
    cfg2["watch"]["processes"] = ["obs64.exe"]
    cfg2["watch"]["confirm_checks"] = 1
    cfg2["watch"]["stop_grace_seconds"] = 0
    cfg2["behavior"]["cooldown_minutes"] = 0

    fired2 = []
    eng2 = live_notify.TriggerEngine(cfg2, lambda r: fired2.append(r))
    state2 = {"procs": ["obs64.exe"]}
    live_notify.list_processes = lambda: list(state2["procs"])

    eng2.tick()
    check("confirm_checks=1 时立刻触发", len(fired2) == 1, "fired={}".format(len(fired2)))

    state2["procs"] = ["explorer.exe"]
    eng2.tick()
    state2["procs"] = ["obs64.exe"]
    eng2.tick()
    check("退出后再次开播会重新通知", len(fired2) == 2, "fired={}".format(len(fired2)))

    # ---------- 用例 C：进程名匹配 ----------
    hits = live_notify.match_processes(
        ["OBS64.EXE", "explorer.exe", "直播伴侣.exe", "notobs.exe"],
        ["obs64.exe", "直播伴侣.exe"])
    check("进程名匹配大小写不敏感", "OBS64.EXE" in hits and "直播伴侣.exe" in hits,
          repr(hits))
    check("子串匹配不会误伤 notobs.exe", "notobs.exe" not in hits, repr(hits))

    return failures


def run_games_tests():
    """games.py 的纯逻辑测试。

    不打桩、不造窗口、不要求有游戏在跑 —— 只测那些「输入确定、输出就该确定」
    的部分：标题清洗、通用源名判断、窗口串解析，以及认不出来时别抛异常。

    窗口打分那套依赖真实桌面，不在这里测（那是要人眼看的）。
    """
    failures = []

    def check(name, ok, detail=""):
        print("  [{}] {}{}".format("PASS" if ok else "FAIL", name,
                                   "  " + detail if detail and not ok else ""))
        if not ok:
            failures.append(name)

    try:
        import games
    except ImportError as exc:
        check("games.py 能否导入", False, str(exc))
        return failures

    # ---- 标题清洗 ----
    # 有些启动器把标题写成 C<ZWSP>a<ZWSP>l<ZWSP>l，不过滤的话群里会收到
    # 一串看不见的乱码
    zwsp = "\u200b"
    dirty = "C{}a{}l{}l of Duty".format(zwsp, zwsp, zwsp)
    check("清掉零宽字符", games.clean_title(dirty) == "Call of Duty",
          repr(games.clean_title(dirty)))
    check("去掉书名号", games.clean_title("《战舰世界》") == "战舰世界",
          repr(games.clean_title("《战舰世界》")))
    check("去掉商标符号", games.clean_title("FarCry\u00ae6") == "FarCry6",
          repr(games.clean_title("FarCry\u00ae6")))
    check("去首尾空白并压掉连续空格",
          games.clean_title("  三角洲行动  ") == "三角洲行动",
          repr(games.clean_title("  三角洲行动  ")))
    check("空输入不炸", games.clean_title(None) == "" and games.clean_title("") == "")

    # ---- 通用源名判断 ----
    # 直播姬里没改过名的源就叫「游戏进程 3」，遇到这类得改用窗口标题
    for generic in ("游戏进程 3", "窗口捕捉 1", "游戏源", "窗口采集"):
        check("认得出通用源名：{}".format(generic), games._is_generic_source(generic))
    for real in ("战争雷霆", "《战舰世界》", "Battlefield Labs"):
        check("不误判真名：{}".format(real), not games._is_generic_source(real))

    # ---- 场景文件里的窗口串 ----
    parsed = games._split_window_spec(
        "三角洲行动:UnrealWindow:DeltaForceClient-Win64-Shipping.exe")
    check("解析 <标题>:<类>:<exe>",
          parsed == ("三角洲行动", "UnrealWindow",
                     "deltaforceclient-win64-shipping.exe"), repr(parsed))
    two = games._split_window_spec("A:B:C:Cls:x.exe")
    check("标题里带冒号也能解析", bool(two) and two[0] == "A:B:C", repr(two))
    for bad in ("", "没有冒号", "只有两段:类", "标题:类:不是exe.txt", None):
        check("拒绝畸形输入：{!r}".format(bad),
              games._split_window_spec(bad) is None,
              repr(games._split_window_spec(bad)))

    # ---- 配置里的两张表 ----
    names, ignore = games.game_rules({
        "game": {"names": {"FarCry6.exe": "孤岛惊魂6"},
                 "ignore": ["VTube Studio.exe"]}})
    check("手工映射表名字统一小写", names.get("farcry6.exe") == "孤岛惊魂6",
          repr(names))
    check("忽略名单名字统一小写", "vtube studio.exe" in ignore, repr(ignore))
    # 换一份配置要立刻生效（缓存按内容判定，不是按时间）
    names2, _ = games.game_rules({"game": {"names": {"cod.exe": "使命召唤"}}})
    check("换了配置立刻生效", names2.get("cod.exe") == "使命召唤", repr(names2))

    # ---- 认不出来也不能抛 ----
    cfg = {"game": {"enabled": True, "names": {}, "ignore": []}}
    res = games.detect(cfg)
    check("detect 返回结构完整",
          isinstance(res, dict) and "name" in res and "window" in res, repr(res))
    check("detect 的 name 要么是字符串要么是 None",
          res.get("name") is None or isinstance(res.get("name"), str), repr(res))

    idx = games.build_index(force=True)
    check("build_index 结构完整",
          isinstance(idx.get("by_exe"), dict) and isinstance(idx.get("by_path"), list),
          repr(type(idx)))

    return failures


def run_send_retry_tests(path):
    """群发失败要自动重试，而且**成功过的群不能重发**。

    后者才是真正容易写错的地方：NapCat 掉线往往是「部分群发出去、部分没发出去」，
    如果重试时不区分，群友会连着收到两三条一样的通知。
    """
    failures = []

    def check(name, ok, detail=""):
        print("  [{}] {}{}".format("PASS" if ok else "FAIL", name,
                                   "  " + detail if detail and not ok else ""))
        if not ok:
            failures.append(name)

    base = live_notify.load_config(path)
    base["behavior"]["dry_run"] = False
    base["behavior"]["send_interval_seconds"] = 0
    base["groups"] = [
        {"group_id": 1, "enabled": True, "at_all": False, "at_list": [], "note": "甲"},
        {"group_id": 2, "enabled": True, "at_all": False, "at_list": [], "note": "乙"},
    ]

    class FakeBot:
        """send_group_msg 按次数失败，用来模拟 NapCat 掉线又恢复。"""

        def __init__(self, fail_times):
            self.fail_times = dict(fail_times)
            self.calls = []

        def send_group_msg(self, gid, segments):
            self.calls.append(gid)
            if self.fail_times.get(gid, 0) > 0:
                self.fail_times[gid] -= 1
                return False, "模拟：连不上 NapCat"
            return True, {"message_id": 1}

    old_delays = live_notify.SEND_RETRY_DELAYS
    live_notify.SEND_RETRY_DELAYS = (0, 0, 0)       # 测试里不真等
    try:
        # ---- 用例 A：掉线一轮就恢复 ----
        bot = FakeBot({1: 1, 2: 1})
        res = live_notify.send_to_groups(base, bot, "测试")
        ok = res.ok
        check("掉线一轮后重试成功", ok == 2, "ok={}".format(ok))
        check("每个群恰好两次调用（失败 + 补发）",
              sorted(bot.calls) == [1, 1, 2, 2], repr(bot.calls))
        check("全部成功后 failed 为空",
              live_notify.LAST_SEND["failed"] == [], repr(live_notify.LAST_SEND))

        # ---- 用例 B：甲群一直失败，乙群一次成功 ----
        bot = FakeBot({1: 99})
        res = live_notify.send_to_groups(base, bot, "测试")
        ok = res.ok
        check("部分失败时成功数正确", ok == 1, "ok={}".format(ok))
        check("成功的群**没有**被重发", bot.calls.count(2) == 1, repr(bot.calls))
        check("一直失败的群试满了所有轮次",
              bot.calls.count(1) == len(live_notify.SEND_RETRY_DELAYS) + 1,
              repr(bot.calls))
        check("彻底失败的群记进 LAST_SEND",
              live_notify.LAST_SEND["failed"] == [1], repr(live_notify.LAST_SEND))
        check("LAST_SEND 的总数是启用群数",
              live_notify.LAST_SEND["total"] == 2, repr(live_notify.LAST_SEND))

        # ---- 用例 B2：SendResult 的语义（P0-2）----
        # 「触发成功」和「送达成功」必须分得开 —— 这几条就是钉这件事的。
        # 原来 send_to_groups 只返回一个成功数字，而 fire() 连数字都丢了，
        # 于是「一条都没发出去」和「全部发成功」在调用方看来一模一样。
        check("SendResult 认出这是部分送达", res.partial is True, repr(res))
        check("SendResult 没把它当成全丢", res.lost is False, repr(res))
        check("SendResult 记下了失败群", res.failed == [1], repr(res))
        check("SendResult 记下了重试轮次",
              res.rounds == len(live_notify.SEND_RETRY_DELAYS), repr(res))

        # 全军覆没：一个群都没成功
        all_dead = FakeBot({1: 99, 2: 99})
        res2 = live_notify.send_to_groups(base, all_dead, "测试")
        check("全军覆没时 lost 为真", res2.lost is True, repr(res2))
        check("全军覆没时 delivered 为假", res2.delivered is False, repr(res2))
        check("全军覆没时 ok 是 0", res2.ok == 0, repr(res2))
        check("summary 明说「一条都没发出去」",
              "一条都没发出去" in res2.summary(), res2.summary())

        # ---- 用例 C：一个启用的群都没有 ----
        empty = live_notify.load_config(path)
        empty["behavior"]["dry_run"] = False
        empty["groups"] = []
        res = live_notify.send_to_groups(empty, FakeBot({}), "测试")
        ok = res.ok
        check("没有启用的群时返回 0 且不炸", ok == 0, "ok={}".format(ok))
    finally:
        live_notify.SEND_RETRY_DELAYS = old_delays

    return failures


def run_redact_tests():
    """日志落盘前的密钥打码。

    日志文件是拿来外传的（贴群、发给别人看），而里面躺着 NapCat 的 WebUi token
    和控制端口 token —— 那个等价于「谁拿到谁就能操作」。所以文件那份要打码，
    控制台和界面面板保持完整（用户要在那儿复制触发地址）。
    """
    failures = []

    def check(name, ok, detail=""):
        print("  [{}] {}{}".format("PASS" if ok else "FAIL", name,
                                   "  " + detail if detail and not ok else ""))
        if not ok:
            failures.append(name)

    cases = [
        ("手动触发地址 http://127.0.0.1:8899/trigger?token="
         "deadbeef00112233445566778899aabbccddeeff00112233445566778899aabb", True),
        ("[WebUi] WebUi Token: feedface00ff11ee22dd33cc44bb55aa66ff77ee88dd", True),
        ('[Config] 加载 {"access_token":"fake0000111122223333"}', True),
        ("目标群数：3", False),
        # 误伤检查：路径里有 token 这个词，但它不是密钥
        (r"日志已保存到 C:\token\abc.txt", False),
    ]
    for text, should_mask in cases:
        out = live_notify.redact(text)
        masked = "已打码" in out
        check("{}：{}".format("要打码" if should_mask else "不能误伤", text[:34]),
              masked == should_mask, repr(out))

    # 打码之后不能再出现完整的原始密钥
    raw = "deadbeef00112233445566778899aabbccddeeff00112233445566778899aabb"
    check("原始密钥不会完整留在打码结果里",
          raw not in live_notify.redact("token=" + raw))
    return failures


def run_corner_tests():
    """圆角抗锯齿。

    为什么值得一条常驻测试：**tkinter 的 Canvas 不做抗锯齿**。最省事的画法是
    「两个矩形 + 四个椭圆」，但那样拼出来的圆角是硬边的，半径一大四个角就是
    肉眼可见的台阶，像马赛克。

    正确做法是逐像素算覆盖率再跟背景色混。这个测试断言「边缘存在半覆盖的
    像素」—— 走回椭圆画法的话，覆盖率只会有 0 和 1，立刻红。
    """
    failures = []

    def check(name, ok, detail=""):
        print("  [{}] {}{}".format("PASS" if ok else "FAIL", name,
                                   "  " + detail if detail and not ok else ""))
        if not ok:
            failures.append(name)

    try:
        import gui
    except Exception as exc:
        check("gui.py 能否导入", False, str(exc))
        return failures

    for r in (8, 12, 16):
        alpha = gui._corner_alpha(r, "tl")
        vals = [v for row in alpha for v in row]
        partial = [v for v in vals if 0.001 < v < 0.999]
        check("半径 {}：边缘有半覆盖像素（抗锯齿生效）".format(r),
              len(partial) >= r // 2, "半覆盖像素只有 {} 个".format(len(partial)))
        check("半径 {}：角落外面是全透明".format(r),
              alpha[0][0] < 0.5, "左上角应该是空的，实际 {}".format(alpha[0][0]))
        check("半径 {}：角落里面是全不透明".format(r),
              alpha[r - 1][r - 1] > 0.99,
              "右下角应该在圆内，实际 {}".format(alpha[r - 1][r - 1]))

    # 覆盖率矩阵跟颜色无关，改颜色不该让它重算
    before = gui._corner_alpha(12, "tl")
    check("覆盖率矩阵被缓存复用", gui._corner_alpha(12, "tl") is before)

    # 四个角形状不同（TR 应该是右上角贴边）
    tl, tr = gui._corner_alpha(12, "tl"), gui._corner_alpha(12, "tr")
    check("四个角不是同一个形状", tl[0][0] < 0.5 and tr[0][11] < 0.5,
          "tl[0][0]={} tr[0][11]={}".format(tl[0][0], tr[0][11]))
    return failures


def run_click_guard_tests():
    """启动豁免期：启动后一小段时间内的点击要吞掉。

    为什么要这个 —— 实测踩到过两次（主题开关、标签栏）：程序启动时如果鼠标
    恰好停在某个控件上，Windows 会把光标位置上那一次点击投递进来，那个控件
    就被"点"了一下。用户看到的是"程序自己动了一下"，极难自行诊断。
    """
    failures = []

    def check(name, ok, detail=""):
        print("  [{}] {}{}".format("PASS" if ok else "FAIL", name,
                                   "  " + detail if detail and not ok else ""))
        if not ok:
            failures.append(name)

    try:
        import gui                      # 只用纯函数，不需要建窗口
    except Exception as exc:
        check("gui.py 能否导入", False, str(exc))
        return failures

    gui.start_click_guard()
    check("刚启动时处于豁免期", gui.click_ready() is False)
    time.sleep(gui.CLICK_GRACE_SECONDS + 0.1)
    check("过了豁免期就放行", gui.click_ready() is True)
    return failures


def run_theme_bake_tests():
    """主题色不许被烤死在默认参数里。

    这个坑长这样：

        def __init__(self, parent, background=BG):

    `BG` 是在**模块导入那一刻**求值的 —— 之后 `apply_theme()` 再改全局量也
    追不回来，那个控件就永远停在初始主题上。表现出来就是：深色模式下标签栏
    发白、滚动区底下一整片白。实测踩到过（ScrollFrame 和 TabStrip 各一处）。

    两层检查：
      静态层 —— 扫源码里的签名，一网打尽这一类写法
      动态层 —— 真建一个控件，看它跟不跟随主题
    """
    failures = []

    def check(name, ok, detail=""):
        print("  [{}] {}{}".format("PASS" if ok else "FAIL", name,
                                   "  " + detail if detail and not ok else ""))
        if not ok:
            failures.append(name)

    # ---- 静态层 ----
    try:
        import gui
    except Exception as exc:
        check("gui.py 能否导入", False, str(exc))
        return failures

    here = os.path.dirname(os.path.abspath(gui.__file__))
    source = io.open(os.path.join(here, "gui.py"), encoding="utf-8").read()
    theme_names = ("BG", "CARD", "SUNKEN", "SURFACE", "BORDER", "TEXT",
                   "MUTED", "PRIMARY", "PRIMARY_D", "PRIMARY_S", "ACCENT",
                   "LOG_BG", "LOG_FG", "LOG_BAR", "OK_COLOR", "WARN",
                   "BAD_COLOR")
    # 形如  background=BG  或  background=CARD,  或  =SURFACE)
    pat = re.compile(r"=\s*(" + "|".join(theme_names) + r")\s*[,)]")
    hits = []
    for i, line in enumerate(source.split("\n"), 1):
        stripped = line.strip()
        if not stripped.startswith("def "):
            continue
        if pat.search(line):
            hits.append("第 {} 行：{}".format(i, stripped[:70]))
    check("源码里没有把主题色当默认参数的地方", not hits,
          "；".join(hits) if hits else "")

    # ---- 动态层 ----
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
    except Exception as exc:
        check("能否建 Tk 根窗口", False, str(exc))
        return failures

    try:
        for theme in ("dark", "light"):
            gui.apply_theme(theme)
            want_bg = gui.BG.lower()
            want_surface = gui.SURFACE.lower()
            sf = gui.ScrollFrame(root)
            ts = gui.TabStrip(root, ["甲", "乙", "丙"], lambda i: None)
            got_sf = str(sf.cget("background")).lower()
            got_ts = str(ts.cget("background")).lower()
            got_body = str(sf.body.cget("background")).lower()
            check("{}：ScrollFrame 底色跟随主题".format(theme),
                  got_sf == want_bg, "期望 {} 实际 {}".format(want_bg, got_sf))
            check("{}：ScrollFrame 内容区跟随主题".format(theme),
                  got_body == want_bg, "期望 {} 实际 {}".format(want_bg, got_body))
            check("{}：TabStrip 底色跟随主题".format(theme),
                  got_ts == want_bg, "期望 {} 实际 {}".format(want_bg, got_ts))
            for w in (sf, ts):
                w.destroy()
            check("{}：SURFACE 与 BG 不同（分层还在）".format(theme),
                  want_surface != want_bg)
        gui.apply_theme("light")
    finally:
        # **必须在这里彻底释放，不能留给 GC。**
        # destroy() 只销毁窗口，不释放 Tcl 解释器；对象要是活到后面某个
        # 多线程小节才被回收，就会撞上
        #     Tcl_AsyncDelete: async handler deleted by the wrong thread
        # 整个进程直接挂掉（实测退出码 -2147483645）。所以要显式 del + collect，
        # 而且就在创建它的这个线程里做。
        root.destroy()
        del root
        gc.collect()

    return failures


def run_template_tests(path):
    """P0-4 模板占位符校验。

    要防的失败长这样：用户模板里拼错一个字母（{titel}），format() 抛 KeyError，
    被兜底逻辑吞掉，最后把 "{titel}" **原样发进 QQ 群**。群友看见了，日志里
    只有一行 WARN。这是整个程序唯一会「对外出丑」的失败。

    两道防线各测各的：
      校验 —— 加载时抓出来，check 里列给用户看
      清理 —— 渲染时兜底，**花括号绝不能活到消息里**
    """
    failures = []

    def check(name, ok, detail=""):
        print("  [{}] {}{}".format("PASS" if ok else "FAIL", name,
                                   "  " + detail if detail and not ok else ""))
        if not ok:
            failures.append(name)

    # ---- 校验认得准 ----
    cases = [
        ("{link}", []),
        ("{title} 正在玩《{game}》", []),
        ("{title:>10} 带格式说明", []),
        ("{game!r} 带转换", []),
        ("{titel} 开播了", ["titel"]),
        ("{Title} 大小写算错", ["Title"]),
        ("{a} 和 {b}", ["a", "b"]),
        ("空的 {} 也算错", ["(空)"]),
        ("没有占位符", []),
    ]
    wrong = [(t, live_notify.unknown_placeholders(t), w)
             for t, w in cases if live_notify.unknown_placeholders(t) != sorted(w)]
    check("认得准该抓和不该抓的", not wrong,
          "；".join("{!r}->{}期望{}".format(*x) for x in wrong[:3]))

    # ---- 清理绝不漏花括号 ----
    leaks = []
    for text in ("{titel} 开播了", "{} 空的", "{Title} 大小写", "{ 畸形"):
        out = live_notify.scrub_template(text)
        if "{" in out or "}" in out:
            leaks.append("{!r}->{!r}".format(text, out))
    check("清理后不留花括号", not leaks, "；".join(leaks))

    # ---- 认得的不能被误删 ----
    kept = live_notify.scrub_template("{title} / {game} / {link}")
    check("认得的占位符原样留着", kept == "{title} / {game} / {link}", repr(kept))

    # ---- 白名单必须和 render_text 支持的字段一致 ----
    # 两边一旦对不上，用户会看到「写着不报错、发出去却是花括号」的占位符。
    try:
        cfg = live_notify.load_config(path)
        rendered = live_notify.render_text(cfg, template="{title}|{link}|{game}|{time}|{date}")
        check("白名单覆盖 render_text 实际支持的字段",
              "{" not in rendered and "}" not in rendered, repr(rendered))
    except Exception as exc:
        check("render_text 用全白名单不炸", False, str(exc))

    # ---- 坏模板不该拦启动，但必须被记录 ----
    import json
    base = os.path.dirname(os.path.abspath(__file__))
    bad_path = os.path.join(base, "_test-badtpl.json")
    try:
        raw = json.load(io.open(path, encoding="utf-8-sig"))
        # 用 setdefault：make_config 只造必填段，offline_message 这些靠
        # load_config 兜默认值。测试要覆盖「用户配置缺省段」这条路径。
        raw.setdefault("message", {})["templates"] = ["{titel} 开播了"]
        raw.setdefault("offline_message", {})["templates"] = ["播了 {duraton}"]
        io.open(bad_path, "w", encoding="utf-8").write(
            json.dumps(raw, ensure_ascii=False, indent=2))
        bad = live_notify.load_config(bad_path)
        found = dict(bad.get("_template_problems") or [])
        check("坏模板不拦启动（配置照样加载出来）", bool(bad.get("message")))
        check("坏模板被记进 _template_problems",
              found.get("开播文案") == "titel" or ("开播文案", "titel") in (bad.get("_template_problems") or []),
              repr(bad.get("_template_problems")))
        check("下播文案的错也抓到了",
              any(x[0] == "下播文案" for x in (bad.get("_template_problems") or [])),
              repr(bad.get("_template_problems")))

        # 渲染任意多次都不许出现花括号
        leaked = []
        for _ in range(30):
            out = live_notify.render_text(bad)
            if "{" in out or "}" in out:
                leaked.append(out)
        check("坏模板渲染 30 次都没有花括号漏出去", not leaked,
              repr(leaked[:2]))
    finally:
        try:
            os.remove(bad_path)
        except OSError:
            pass

    return failures


def run_single_instance_tests():
    """P0-1 单实例锁。

    同时跑两个 watch，两边都在监听、都在发送 —— 群里收双份，用户不知道为什么。

    三条要求都要测到，**尤其第三条**：
        · 第二个实例进不去
        · 退出后能重开
        · 异常退出后不能永久锁死 —— 这条只有把子进程强杀才验得出来，
          也是我选 msvcrt.locking 而不是「写个 pid 文件」的全部理由
    """
    failures = []

    def check(name, ok, detail=""):
        print("  [{}] {}{}".format("PASS" if ok else "FAIL", name,
                                   "  " + detail if detail and not ok else ""))
        if not ok:
            failures.append(name)

    if live_notify.msvcrt is None:
        check("非 Windows 平台，跳过单实例检查", True)
        return failures

    import subprocess as sp

    try:
        os.remove(live_notify.LOCK_PATH)
    except OSError:
        pass

    # ---- 同进程：第二次抢不到 ----
    a = live_notify.SingleInstance()
    b = live_notify.SingleInstance()
    check("第一次抢锁成功", a.acquire() is True)
    check("第二次抢锁被拦", b.acquire() is False)
    check("被拦时能读出持有者", "pid=" in (b.holder() or ""), repr(b.holder()))
    a.release()
    check("释放后可以重新抢", b.acquire() is True)
    b.release()

    # ---- 跨进程 + 强杀：这才是「异常退出不锁死」的真正验收 ----
    here = os.path.dirname(os.path.abspath(__file__))
    child = os.path.join(here, "_test-lock-child.py")
    io.open(child, "w", encoding="utf-8").write(
        "import os, sys, time\n"
        "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
        "import live_notify as L\n"
        "s = L.SingleInstance()\n"
        "print('OK' if s.acquire() else 'BUSY')\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n")
    try:
        proc = sp.Popen([sys.executable, child], stdout=sp.PIPE,
                        stderr=sp.STDOUT, cwd=here)
        first = proc.stdout.readline().decode("utf-8", "replace").strip()
        check("子进程抢锁成功", first == "OK", repr(first))

        # 子进程还活着的时候，本进程应该抢不到
        c = live_notify.SingleInstance()
        check("子进程持有期间，本进程抢不到", c.acquire() is False)

        # **强杀**，模拟崩溃 / 任务管理器结束进程
        proc.kill()
        proc.wait(timeout=30)
        time.sleep(0.5)

        d = live_notify.SingleInstance()
        got = d.acquire()
        check("子进程被强杀后，锁自动释放（不会永久锁死）", got is True)
        d.release()
    except Exception as exc:
        check("子进程强杀后释放锁", False, "{}: {}".format(type(exc).__name__, exc))
    finally:
        try:
            os.remove(child)
        except OSError:
            pass
        try:
            os.remove(live_notify.LOCK_PATH)
        except OSError:
            pass
        try:
            os.remove(live_notify.LOCK_INFO_PATH)
        except OSError:
            pass

    return failures


def run_control_auth_tests():
    """P0-3 控制端口认证。

    **真的起端口、真的发 HTTP 请求。** 鉴权看代码看不出漏，必须打真请求。

    为什么 URL 里的 token 只算兼容模式：它会进浏览器历史、进各种日志、进
    shell 历史 —— 一条被复制来复制去的链接就等于把钥匙一起传出去了。
    """
    failures = []
    import time as _time
    import urllib.error
    import urllib.request

    def check(name, ok, detail=""):
        print("  [{}] {}{}".format("PASS" if ok else "FAIL", name,
                                   "  " + detail if detail and not ok else ""))
        if not ok:
            failures.append(name)

    port = 18899
    token = "test-token-abcdef123456"
    fired = []
    hints = []

    orig_log = live_notify.log

    def spy(msg, level="INFO"):
        if level == "WARN" and "旧链接" in msg:
            hints.append(msg)
        orig_log(msg, level)

    live_notify.log = spy
    srv = None
    try:
        srv = live_notify.start_control_server(
            {"control": {"port": port, "token": token}},
            lambda: (fired.append(1), {"ok": True})[1],
            lambda: {"state": "test"})
        if srv is None:
            check("控制端口能否启动", False, "端口 {} 起不来".format(port))
            return failures
        _time.sleep(0.6)
        base = "http://127.0.0.1:{}".format(port)

        def call(path, headers=None):
            req = urllib.request.Request(base + path, headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode("utf-8", "replace")
            except Exception as exc:
                return 0, str(exc)

        check("不带 token 打 /trigger 被拒", call("/trigger")[0] == 403)
        check("错误的 token（头）被拒",
              call("/trigger", {"Authorization": "Bearer wrong-token-zzzzzz"})[0] == 403)
        check("正确的 token（Authorization 头）放行",
              call("/trigger", {"Authorization": "Bearer " + token})[0] == 200)
        check("正确的 token（URL 兼容模式）放行",
              call("/trigger?token=" + token)[0] == 200)
        check("错误的 token（URL）被拒",
              call("/trigger?token=wrong-token-zzzzzz")[0] == 403)

        # 只有两次成功，所以只该触发两次。上一版我把这里写成 3，
        # 是自己数错了 —— 断言写错比代码写错更隐蔽。
        check("成功的请求恰好触发两次", len(fired) == 2, "fired={}".format(len(fired)))

        check("用旧链接时给出了提示", len(hints) >= 1)
        if hints:
            check("提示里不含 token 明文", token not in hints[0])

        status, body = call("/")
        check("控制页能打开", status == 200)
        check("页面准备了 token 输入框", 'id="auth"' in body)
        check("页面的请求带 Authorization 头", "Authorization" in body)
        check("页面里没有明文 token", token not in body)
        check("/status 免鉴权可访问", call("/status")[0] == 200)
    except Exception as exc:
        check("控制端口测试整体跑通", False, "{}: {}".format(type(exc).__name__, exc))
    finally:
        live_notify.log = orig_log
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass

    return failures


def run_config_safety_tests(path):
    """P0-5 配置安全检查。

    要抓的是「配置看着没问题、实际会出事」的那些值。最典型的是
    message.link 还留着示例里的 YOUR_ROOM_ID —— **它会被原样发进每一个群**，
    而 check 原来一个字都不说。

    做法：把日志抓下来，看看该报的有没有报、不该报的有没有误报。
    """
    failures = []
    import json as _json

    def check(name, ok, detail=""):
        print("  [{}] {}{}".format("PASS" if ok else "FAIL", name,
                                   "  " + detail if detail and not ok else ""))
        if not ok:
            failures.append(name)

    here = os.path.dirname(os.path.abspath(__file__))
    bad_path = os.path.join(here, "_test-badcfg.json")
    raw = _json.load(io.open(path, encoding="utf-8-sig"))
    raw["message"]["link"] = "https://live.bilibili.com/YOUR_ROOM_ID"
    raw["control"] = {"enabled": True, "port": 18897, "token": ""}
    raw["behavior"]["dry_run"] = True
    raw["trigger"] = dict(raw.get("trigger") or {},
                          on_platform_live=True, room_id=None)
    io.open(bad_path, "w", encoding="utf-8").write(
        _json.dumps(raw, ensure_ascii=False, indent=2))

    lines = []
    orig_log = live_notify.log

    def spy(msg, level="INFO"):
        lines.append(str(msg))
        orig_log(msg, level)

    live_notify.log = spy
    try:
        live_notify.main(["check", "--config", bad_path])
    except SystemExit:
        pass
    except Exception as exc:
        check("check 命令跑完不炸", False, "{}: {}".format(type(exc).__name__, exc))
    finally:
        live_notify.log = orig_log
        try:
            os.remove(bad_path)
        except OSError:
            pass

    blob = "\n".join(lines)
    check("抓到 message.link 还是示例值", "message.link 还是示例值" in blob)
    check("抓到控制端口没设 token", "控制端口开着却没设 token" in blob)
    check("抓到 dry_run 开着", "dry_run 开着" in blob)
    check("抓到勾了轮询没填房间号", "却没填 room_id" in blob)
    check("有 [6/6] 这一节", "[6/6] 配置安全检查" in blob)

    # 不该误报的：跑一遍正常配置，这几条都不该出现
    lines2 = []
    live_notify.log = spy
    try:
        live_notify.main(["check", "--config", path])
    except SystemExit:
        pass
    except Exception:
        pass
    finally:
        live_notify.log = orig_log
    blob2 = "\n".join(
        x for x in lines2 if "message.link" in x or "dry_run" in x or "room_id" in x)
    check("正常配置不误报 dry_run", "dry_run 开着" not in blob2, blob2[:100])

    return failures


def run_widget_presence_tests():
    """控件的「引用」与「创建」必须对得上。

    实测踩到过：删代码时按起止标记整段切，把三个头部标签的**创建**切掉了，
    引用还在。后果不是崩溃 —— `_build_ui` 照常跑完，只是那三个属性从未存在，
    于是 `_paint_header` / `_poll_trigger` / `_poll_health` / `done` 全撞
    AttributeError，用户看到四个错误弹窗，而程序不崩，只是一直报。

    静态层扫源码，动态层真建一个界面挨个 hasattr —— 都要，因为属性也可能是
    条件创建的，静态看不出来。
    """
    failures = []

    def check(name, ok, detail=""):
        print("  [{}] {}{}".format("PASS" if ok else "FAIL", name,
                                   "  " + detail if detail and not ok else ""))
        if not ok:
            failures.append(name)

    try:
        import gui
    except Exception as exc:
        check("gui.py 能否导入", False, str(exc))
        return failures

    here = os.path.dirname(os.path.abspath(gui.__file__))
    source = io.open(os.path.join(here, "gui.py"), encoding="utf-8").read()

    # ---- 静态层 ----
    # **先剥掉注释和字符串再扫。** 文档里写着 `self.lbl_xxx` 这种示例，
    # 直接正则会把示例当成"引用了却没创建"，报一个永远修不掉的假警。
    try:
        import io as _io
        import tokenize
        pieces = []
        for tok in tokenize.tokenize(_io.BytesIO(source.encode("utf-8")).readline):
            if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                pieces.append(tok.string)
        code_only = " ".join(pieces)
    except Exception:
        code_only = source
    used = set(re.findall(r"self\.((?:lbl|btn|txt|cmb|tree|sf|var)_\w+)", code_only))
    created = set(re.findall(r"self\.((?:lbl|btn|txt|cmb|tree|sf|var)_\w+)\s*=", code_only))
    missing = sorted(used - created)
    check("源码里没有「引用了却没创建」的控件", not missing,
          "、".join("self." + m for m in missing[:6]))

    # ---- 动态层 ----
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
    except Exception as exc:
        check("能否建 Tk 根窗口", False, str(exc))
        return failures

    try:
        app = gui.App(root)
        root.update()
        absent = sorted(a for a in used if not hasattr(app, a))
        check("界面建好之后，这些控件属性都真的在", not absent,
              "、".join("self." + a for a in absent[:6]))

        # _paint_header 是延迟调度的，单独把它跑一次 —— 它踩过两次坑
        try:
            app._paint_header()
            ok = True
            detail = ""
        except Exception as exc:
            ok = False
            detail = "{}: {}".format(type(exc).__name__, exc)
        check("_paint_header 能独立跑通", ok, detail)
    except Exception as exc:
        check("界面能否建成", False, "{}: {}".format(type(exc).__name__, exc))
    finally:
        root.destroy()
        del root
        gc.collect()

    return failures


def main():
    live_notify._setup_console()          # 先切 UTF-8，否则中文输出会乱码
    path = make_config()

    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("### 假 NapCat 已启动，群列表 {}\n".format([g["group_id"] for g in GROUPS]))

    results = {}

    for idx, (title, argv) in enumerate([
        ("1/15  自检 check", ["check", "--config", path]),
        ("2/15  彩排 test（不应真的发出去）", ["test", "--config", path]),
        ("3/15  真实发送 send", ["send", "--config", path]),
    ], 1):
        print("\n" + "#" * 70)
        print("# " + title)
        print("#" * 70)
        results[argv[0]] = live_notify.main(argv)

    httpd.shutdown()

    print("\n" + "#" * 70)
    print("# 4/15  触发引擎状态机")
    print("#" * 70)
    failures = run_engine_tests(path)

    print("\n" + "#" * 70)
    print("# 5/15  游戏识别（纯逻辑，不要求有游戏在跑）")
    print("#" * 70)
    failures += run_games_tests()

    print("\n" + "#" * 70)
    print("# 6/15  群发失败重试")
    print("#" * 70)
    failures += run_send_retry_tests(path)

    print("\n" + "#" * 70)
    print("# 7/15  日志落盘前的密钥打码")
    print("#" * 70)
    failures += run_redact_tests()

    print("\n" + "#" * 70)
    print("# 8/15  圆角抗锯齿")
    print("#" * 70)
    failures += run_corner_tests()

    print("\n" + "#" * 70)
    print("# 9/15  启动豁免期（防止鼠标误触）")
    print("#" * 70)
    failures += run_click_guard_tests()

    print("\n" + "#" * 70)
    print("# 10/15  主题色不许被烤死在默认参数里")
    print("#" * 70)
    failures += run_theme_bake_tests()

    print("\n" + "#" * 70)
    print("# 11/15  模板占位符校验")
    print("#" * 70)
    failures += run_template_tests(path)

    print("\n" + "#" * 70)
    print("# 12/15  单实例锁")
    print("#" * 70)
    failures += run_single_instance_tests()

    print("\n" + "#" * 70)
    print("# 13/15  控制端口认证")
    print("#" * 70)
    failures += run_control_auth_tests()

    print("\n" + "#" * 70)
    print("# 14/15  配置安全检查")
    print("#" * 70)
    failures += run_config_safety_tests(path)

    print("\n" + "#" * 70)
    print("# 15/15  控件引用与创建必须对得上")
    print("#" * 70)
    failures += run_widget_presence_tests()

    print("\n" + "=" * 70)
    print("命令退出码：check={check}  test={test}  send={send}".format(**results))
    print("（check 返回 1 是预期的：333333333 是普通成员却要求 @全体成员，应当报警）")
    if failures:
        print("\n失败 {} 项：".format(len(failures)))
        for f in failures:
            print("  - " + f)
    else:
        print("全部通过。")
    print("=" * 70)

    try:
        os.remove(TEST_CONFIG)
    except OSError:
        pass
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

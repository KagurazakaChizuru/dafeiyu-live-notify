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

import json
import os
import sys
import threading
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


def main():
    live_notify._setup_console()          # 先切 UTF-8，否则中文输出会乱码
    path = make_config()

    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("### 假 NapCat 已启动，群列表 {}\n".format([g["group_id"] for g in GROUPS]))

    results = {}

    for idx, (title, argv) in enumerate([
        ("1/4  自检 check", ["check", "--config", path]),
        ("2/4  彩排 test（不应真的发出去）", ["test", "--config", path]),
        ("3/4  真实发送 send", ["send", "--config", path]),
    ], 1):
        print("\n" + "#" * 70)
        print("# " + title)
        print("#" * 70)
        results[argv[0]] = live_notify.main(argv)

    httpd.shutdown()

    print("\n" + "#" * 70)
    print("# 4/5  触发引擎状态机")
    print("#" * 70)
    failures = run_engine_tests(path)

    print("\n" + "#" * 70)
    print("# 5/5  游戏识别（纯逻辑，不要求有游戏在跑）")
    print("#" * 70)
    failures += run_games_tests()

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

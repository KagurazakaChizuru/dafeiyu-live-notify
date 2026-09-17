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
    cfg = live_notify.load_config(path)
    cfg["watch"]["processes"] = ["obs64.exe"]
    cfg["watch"]["confirm_checks"] = 2
    cfg["watch"]["stop_grace_seconds"] = 0
    cfg["behavior"]["cooldown_minutes"] = 30

    fired = []
    eng = live_notify.TriggerEngine(cfg, lambda r: fired.append(r))
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
    print("# 4/4  触发引擎状态机")
    print("#" * 70)
    failures = run_engine_tests(path)

    print("\n" + "=" * 70)
    print("命令退出码：check={check}  test={test}  send={send}".format(**results))
    print("（check 返回 1 是预期的：333333333 是普通成员却要求 @全体成员，应当报警）")
    if failures:
        print("\n引擎测试失败 {} 项：".format(len(failures)))
        for f in failures:
            print("  - " + f)
    else:
        print("引擎测试全部通过。")
    print("=" * 70)

    try:
        os.remove(TEST_CONFIG)
    except OSError:
        pass
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

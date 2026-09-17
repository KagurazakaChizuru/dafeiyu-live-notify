#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch 常驻模式集成测试 —— 不需要真 NapCat，也不用手动开直播软件。
=================================================================

思路：把 watch.processes 设成 "explorer.exe"（Windows 上必然在运行），
于是监控循环会立刻认为"开播了"，从而真实走完一遍：
    进程检测 -> 触发引擎 -> OneBot 请求 -> 假 NapCat 收到

同时验证本地控制端口 /status 和 /trigger。

用法：  python _watchtest.py
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _mock_napcat import Handler, PORT              # noqa: E402

CFG_PATH = os.path.join(HERE, "_watch-test.json")
CONTROL_PORT = 8899


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    cfg = {
        "onebot": {"base_url": "http://127.0.0.1:{}".format(PORT),
                   "access_token": "", "timeout": 5},
        "groups": [
            {"group_id": 123456789, "enabled": True, "at_all": True, "note": "群主群"},
        ],
        "watch": {
            "enabled": True,
            "interval_seconds": 1,
            "processes": ["explorer.exe"],     # 必然在运行，等于"已开播"
            "confirm_checks": 1,
            "stop_grace_seconds": 0,
        },
        "message": {"template": "🔴 我开播啦！{title}", "title": "测试直播", "link": ""},
        "behavior": {"cooldown_minutes": 0, "send_interval_seconds": 0, "dry_run": False},
        "control": {"enabled": True, "port": CONTROL_PORT, "token": ""},
    }
    with open(CFG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)

    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("### 假 NapCat 已启动\n", flush=True)

    print("### 启动 live_notify.py watch（子进程）\n", flush=True)
    proc = subprocess.Popen(
        [sys.executable, "live_notify.py", "watch", "--config", CFG_PATH],
        cwd=HERE)

    time.sleep(5)          # 等它自动检测并发送

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path, timeout=15):
        try:
            return opener.open("http://127.0.0.1:{}{}".format(CONTROL_PORT, path),
                               timeout=timeout).read().decode("utf-8", "replace")
        except Exception as exc:
            return "请求失败: {}".format(exc)

    print("\n" + "#" * 70)
    print("# 控制端口 /status")
    print("#" * 70)
    print(get("/status"))

    print("\n" + "#" * 70)
    print("# 控制端口 /trigger （手动触发一次）")
    print("#" * 70)
    print(get("/trigger"))

    time.sleep(2)

    print("\n### 关闭 watch 子进程\n", flush=True)
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    httpd.shutdown()
    try:
        os.remove(CFG_PATH)
    except OSError:
        pass

    print("### 测试结束（退出码 {}）".format(proc.returncode))
    return 0


if __name__ == "__main__":
    sys.exit(main())

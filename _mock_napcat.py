#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地假 NapCat —— 仅用于测试，不连接真实 QQ。
============================================

在没有安装真 NapCat 的情况下，用它在 127.0.0.1:3000 冒充 OneBot v11 服务端，
验证 live_notify.py 的整条链路（自检、发送、消息格式）是否正常。

用法：
    开一个命令行窗口跑：  python _mock-napcat.py
    再开一个窗口跑：      python live_notify.py check
                         python live_notify.py send

它会把你发出去的每一条消息原样打印出来（包括 at 消息段），方便核对格式。
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 3000

LOGIN_INFO = {"user_id": 10001, "nickname": "测试机器人"}

GROUPS = [
    {"group_id": 123456789, "group_name": "测试主群", "member_count": 128},
    {"group_id": 222222222, "group_name": "测试粉丝群", "member_count": 56},
    {"group_id": 333333333, "group_name": "测试无权限群", "member_count": 30},
]

# 机器人在各群里的身份：owner=群主  admin=管理员  member=普通成员
# 故意把 333333333 设成 member，用来验证"没有管理员权限"的告警逻辑。
ROLES = {123456789: "owner", 222222222: "member", 333333333: "member"}


class Handler(BaseHTTPRequestHandler):
    server_version = "MockNapCat/1.0"

    def log_message(self, fmt, *args):
        pass

    def _reply(self, data, status="ok", retcode=0):
        body = json.dumps({"status": status, "retcode": retcode, "data": data,
                           "message": "" if retcode == 0 else "mock error"},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}

        action = self.path.strip("/").split("?")[0]

        if action == "get_login_info":
            return self._reply(LOGIN_INFO)

        if action == "get_group_list":
            return self._reply(GROUPS)

        if action == "get_group_member_info":
            gid = int(payload.get("group_id", 0))
            return self._reply({"group_id": gid, "user_id": payload.get("user_id"),
                                "role": ROLES.get(gid, "member"), "nickname": "测试机器人"})

        if action == "send_group_msg":
            gid = payload.get("group_id")
            message = payload.get("message")
            print("\n>>> 收到发送请求 group_id={}".format(gid), flush=True)
            print("    原始消息段: {}".format(
                json.dumps(message, ensure_ascii=False)), flush=True)
            line = []
            for seg in (message or []):
                if seg.get("type") == "at":
                    qq = str(seg["data"].get("qq"))
                    line.append("@全体成员" if qq == "all" else "@" + qq)
                elif seg.get("type") == "text":
                    line.append(seg["data"].get("text", ""))
            print("    QQ 里会显示成：", flush=True)
            print("    +-----------------------------------", flush=True)
            for ln in "".join(line).split("\n"):
                print("    | " + ln, flush=True)
            print("    +-----------------------------------", flush=True)
            return self._reply({"message_id": 999})

        return self._reply(None, status="failed", retcode=1404)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("假 NapCat 已启动，监听 http://127.0.0.1:{}".format(PORT))
    print("群列表: {}".format([g["group_id"] for g in GROUPS]))
    print("身份: {}".format(ROLES))
    print("按 Ctrl+C 退出。\n")
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

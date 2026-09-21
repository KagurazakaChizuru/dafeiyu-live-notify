# -*- coding: utf-8 -*-
"""量界面文字：把 gui.py 里会显示给用户看的字符串全抽出来，按字数排队。

为什么要有这么个东西：用户提了三次"界面太复杂"。前两次我凭感觉改，改完她还是
说复杂 —— 说明"感觉"这个判据在我这儿是坏的。第三次先把数字摆出来：最长的一条
143 字，前十条加起来比一页说明文还长。有了尺子，砍哪条、砍到多少，就不用争了。

用法：
    python _ui_text.py            # 默认门限 45 字
    python _ui_text.py 30         # 换个门限
    python _ui_text.py 45 别的文件.py

只做一件事：AST 遍历，把字符串常量收集起来。**不算**文档字符串（那是写给自己
看的），算法上也不区分它是不是真的显示在界面上 —— 所以输出要人过一遍：群消息
模板和占位符速查表也在里面，那两类不该砍（正文是发给群友的，占位符表长度就是
信息本身）。

改前改后各跑一次，数字对不上就说明这轮白改了。
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT = os.path.join(HERE, "gui.py")


def collect(path, limit):
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)

    # 模块/函数文档字符串也是 Constant，但它们是写给我自己看的，不算界面文字。
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))

    hits = {}

    def add(text, line):
        text = text.strip()
        # 只挑中文：英文错误串、路径、URL 不占"看起来复杂"的份额。
        if len(text) > limit and any("\u4e00" <= c <= "\u9fff" for c in text):
            hits.setdefault(text, []).append(line)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "card_hint":
            # 卡片提示是重点对象：它们就是"界面上的说明文"。
            for arg in node.args:
                try:
                    val = ast.literal_eval(arg)
                except Exception:
                    continue
                if isinstance(val, str):
                    add(val, node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                add(node.value, node.lineno)
    return hits


def main(argv):
    argv = [a for a in argv if not a.startswith("-")]
    # 只给门限也行，省得每次敲一遍长路径。
    if argv and argv[0].isdigit():
        path, limit = DEFAULT, int(argv[0])
    else:
        path = argv[0] if argv else DEFAULT
        limit = int(argv[1]) if len(argv) > 1 else 45

    hits = collect(path, limit)
    total = sum(len(t) for t in hits)
    print("门限 %d 字，命中 %d 条，合计 %d 字" % (limit, len(hits), total))
    for text, lines in sorted(hits.items(), key=lambda kv: -len(kv[0])):
        print("%4d 字  行 %-14s %s" % (
            len(text),
            ",".join(str(n) for n in lines[:4]),
            text.replace("\n", "\\n")[:120],
        ))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

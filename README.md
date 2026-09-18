# 大肥鱼直播姬

开播时自动往 QQ 群发 `@全体成员` 通知的 Windows 小工具。
绿色免安装，纯 Python 标准库 + tkinter，**零第三方依赖**。

> **设计出发点**：打开直播软件 ≠ 开播。
> 你打开 OBS 之后还要调设备、试麦、试妆，这段时间不该打扰群友。
> 所以这个项目默认**不做进程检测**，而是用更精确的信号源。

```
直播间状态 0→1 ─┐
OBS 开始推流   ─┼──→ 冷却闸门 ──→ OneBot HTTP ──→ NapCat ──→ QQ 群 @全体成员
按下快捷键    ─┘
直播间状态 1→0 ────→ 下播闸门 ──→ 同上（默认不 @ 任何人）
```

---

## 📚 文档

| 文档 | 写给谁 | 内容 |
|---|---|---|
| **[使用文档](docs/使用文档.md)** | 主播本人 | 怎么装、怎么配、界面怎么读、坏了怎么办 |
| **[技术文档](docs/技术文档.md)** | 想读懂设计的人 | 架构分层、触发语义矩阵、OneBot / obs-websocket 接口规格、配置 Schema，以及 16 项实现中实际踩到的陷阱与对策 |
| **[开发文档](docs/开发文档.md)** | 要改代码的人 | 代码地图、必须遵守的工程约定、调试手段、扩展指南、打包发版 |

第一次用，直接看 [使用文档](docs/使用文档.md) 就够了。

---

## 特性

| | |
|---|---|
| **最通用的触发** | 轮询直播间状态，**不管用 OBS、直播姬、直播伴侣还是手机开播都能检测到** |
| **精确触发** | 也支持监听 OBS 的 `obs-websocket` 推流事件，精确到「按下开始推流」那一刻 |
| **下播提示** | 自动检测下播，附本次直播时长；带离线宽限期，断流重连不会误报 |
| **通用兜底** | 全局快捷键（可自定义），任何情况下按一下就推送 |
| **不占用你的 QQ** | 通过独立 QQ 副本实现双实例共存，通知器挂着时你照样能聊天 |
| **体积很小** | 私有 QQ 副本与系统 QQ 共享程序文件，整个部署文件夹约 40 MB |
| **零依赖** | 只用 Python 标准库 + tkinter；连 WebSocket 客户端和 PNG 编码器都是手写的 |
| **图形界面** | 一个大按钮搞定，不需要碰配置文件 |
| **可打包** | 附 PyInstaller 说明，可做成单文件 exe |
| **隐私友好** | 除查询你自己的开播状态外**全部本地通信**；聊天内容不落盘 |

> ⚠️ 开启「直播间开播时通知」后，程序会定期访问 B站的公开接口读取你自己的开播状态。
> 这是本程序**唯一**的外部网络请求，不携带任何身份信息，默认直连不走代理。
> 若你介意，把它关掉即可 —— 其余触发源完全在本地工作。

---

## 快速开始

```bash
git clone https://github.com/KagurazakaChizuru/dafeiyu-live-notify.git
cd dafeiyu-live-notify
copy config.example.json config.json
copy _account.txt.example _account.txt
python live_notify.py check     # 自检，它会告诉你还缺什么
python gui.py                   # 打开界面
```

需要 Python 3.8+，**不需要装任何第三方库**。
另外还需要一份 [NapCat](https://github.com/NapNeko/NapCatQQ)（本仓库不包含，见下）。

详细步骤、界面说明和排错，看 **[使用文档](docs/使用文档.md)**。

---

## 命令行用法

图形界面之外，核心逻辑也能单独跑：

```bash
python live_notify.py check     # 自检：配置 / NapCat 连通性 / 群权限
python live_notify.py test      # 彩排：打印将要发送的内容，不真发
python live_notify.py send      # 立即发送一次
python live_notify.py watch     # 常驻监控（默认）
python live_notify.py groups    # 列出机器人所在的所有群
```

---

## 常见问题

**Q：为什么不做成「检测到 OBS 启动就通知」？**
因为那不是你开播的时刻。默认关闭，可以在界面里打开，但做好被群友白提醒的准备。

**Q：直播姬 / 直播伴侣能自动检测吗？**
能 —— 走「直播间开播时通知」那条路，它不看你用什么软件。
只有「OBS 推流事件」这一种触发方式需要 OBS 自带 websocket。

**Q：开了通知器我的 QQ 会掉吗？**
不会。程序跑在独立 QQ 副本上，是第二个实例。

**Q：群里的 `@全体成员` 没提醒到人？**
机器人不是群主/管理员。跑 `python live_notify.py check` 会告诉你具体哪个群不行。

**Q：会不会泄露我的聊天记录？**
程序只跟 `127.0.0.1:3000` 通信，无任何外部上报，聊天内容不落盘。

---

## 关于许可证

- **本项目代码**：MIT（见 [`LICENSE`](LICENSE)）
- **NapCat**：采用 [Limited Redistribution License](https://github.com/NapNeko/NapCatQQ/blob/main/LICENSE) ——
  允许再分发（需附许可证全文并注明来源），**禁止商业用途**。
  本仓库不打包 NapCat，请自行从官方 Releases 下载。
- **QQ 本体**：腾讯专有软件。**任何情况下都不要把 QQ 的程序文件提交到公开仓库**，
  这属于侵权，会被 DMCA 下架。

---

## 致谢

- [NapCatQQ](https://github.com/NapNeko/NapCatQQ) —— QQ 协议端
- [OneBot v11](https://github.com/botuniverse/onebot-11) —— 通信协议
- [obs-websocket](https://github.com/obsproject/obs-websocket) —— OBS 事件来源

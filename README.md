# 大肥鱼直播姬

开播时自动往 QQ 群发 `@全体成员` 通知的 Windows 小工具。绿色免安装，纯 Python 标准库 + tkinter，无任何第三方依赖。

> **设计出发点**：打开直播软件 ≠ 开播。
> 你打开 OBS 之后还要调设备、试麦、试妆，这段时间不该打扰群友。
> 所以这个项目**默认不做进程检测**，而是用两种更精确的触发方式。

📐 **完整设计规格见 [SPEC.md](SPEC.md)** —— 架构分层、触发语义矩阵、OneBot / obs-websocket 接口规格、配置 Schema、测试策略，以及 16 项实现过程中实际踩到的陷阱与对策。

```
直播间状态 0→1 ─┐
OBS 开始推流   ─┼──→ 冷却闸门 ──→ OneBot HTTP ──→ NapCat ──→ QQ 群 @全体成员
按下快捷键    ─┘
直播间状态 1→0 ────→ 下播闸门 ──→ 同上（默认不 @ 任何人）
```

---

## 特性

| | |
|---|---|
| **最通用的触发** | 轮询直播间状态，**不管用 OBS、直播姬、直播伴侣还是手机开播都能检测到** |
| **精确触发** | 也支持监听 OBS 的 `obs-websocket` 推流事件，精确到「按下开始推流」那一刻 |
| **下播提示** | 同样自动检测下播，附本次直播时长；带离线宽限期，断流重连不会误报 |
| **通用兜底** | 全局快捷键（可自定义），任何情况下按一下就推送 |
| **不占用你的 QQ** | 通过独立 QQ 副本实现双实例共存，通知器挂着时你照样能聊天 |
| **零依赖** | 只用 Python 标准库 + tkinter；连 WebSocket 客户端和 PNG 编码器都是手写的 |
| **图形界面** | 一个大按钮搞定，不需要碰配置文件 |
| **可打包** | 附 PyInstaller 说明，可做成单文件 exe |
| **隐私友好** | 除轮询直播间状态外**全部本地通信**；WebUI 仅监听 127.0.0.1，聊天内容不落盘 |

> ⚠️ 开启「直播间开播时通知」后，程序会定期访问 B站 的公开接口读取你自己的开播状态。
> 这是本程序**唯一**的外部网络请求，不携带任何身份信息，默认直连不走代理。
> 若你介意，把它关掉即可 —— 其余触发源完全在本地工作。

---

## 它是怎么工作的

### 触发层

四个触发源，开播侧共用一个冷却闸门，所以同一次开播不会被发两遍：

1. **直播间状态轮询**（`triggers.PlatformWatcher`）—— **推荐**
   定期查询直播间状态（B站公开接口，无需登录/签名），只在
   `live_status` 由 `0` 跳变为 `1` 时触发。
   这是唯一**与开播软件无关**的信号源，读到的还是平台判定的真值。

2. **OBS 推流事件**（`triggers.ObsWatcher`）
   连上 OBS 内置的 `obs-websocket`，订阅 `StreamStateChanged`。
   端口和密码直接从 OBS 自己的配置文件读，**用户不需要手填**。

3. **全局快捷键**（`triggers.HotkeyListener`）
   Win32 `RegisterHotKey`。任何情况下都能用的手动兜底。

4. **进程检测**（`live_notify.TriggerEngine`，默认关闭）
   检测到指定进程就触发。因为会误报（打开软件 ≠ 开播），默认关掉。

### 下播提示

由直播间轮询驱动，走**独立于开播的闸门** —— 否则「只播了 10 分钟就下播」时，
下播通知会被开播那 30 分钟冷却静默吃掉。

三个防止打扰人的设计：

| 机制 | 作用 |
|---|---|
| 只在 `1→0` 跳变时触发 | 持续离线期间不会重复发 |
| 离线宽限期（默认 60 秒） | 断流重连造成的假下播不会误报 |
| `live_status == 2`（轮播）不触发 | 轮播是自动重播，不是你下播 |

文案里可用 `{duration}` 占位符，自动填成本次直播时长（如「2 小时 15 分钟」）。
默认**不 @ 任何人** —— 没在看直播的人不会关心你几点停。

### 发送层

```
OneBot v11 HTTP  →  NapCat  →  QQ
```

### 不占用你自己 QQ 的原理

NapCat 默认会启动**注册表里那个 QQ 安装**，而 QQ 桌面端同一时间只能跑一个实例 ——
结果就是通知器一开，你的 QQ 就用不了了。

解决办法是给 NapCat 一份**独立的 QQ 副本**：Windows 把它当成另一个程序，
于是成为第二个互不干扰的实例。见 `napcat/launcher-second.bat`，它和官方启动器
的唯一区别是不查注册表，直接指向 `../qq-napcat/QQ.exe`。

停止时也只杀路径里带 `qq-napcat` 的进程，**绝不碰你自己的 QQ**。

---

## 前置条件

- Windows 10 / 11（64 位）
- Python 3.8+（**仅开发时需要**；用打包好的 exe 则不需要）
- [NapCat](https://github.com/NapNeko/NapCatQQ)（必须，且**需要你自己下载**）
- 可选：OBS Studio 28+（自带 obs-websocket）

---

## 快速开始

### 1. 下载 NapCat

去 [NapCatQQ Releases](https://github.com/NapNeko/NapCatQQ/releases) 下载
`NapCat.Shell.zip`，解压到 `napcat/`。

> ⚠️ 本仓库**不包含** NapCat 和 QQ 本体，原因见下方「关于许可证」。

### 2. 配置 NapCat

启动 NapCat 并扫码登录一个 QQ 号（**建议用小号**），然后在
`napcat/config/onebot11.json` 里开一个 HTTP 服务端：

```json
{
  "network": {
    "httpServers": [
      {
        "name": "live-notify",
        "enable": true,
        "port": 3000,
        "host": "127.0.0.1",
        "messagePostFormat": "array",
        "token": ""
      }
    ]
  }
}
```

### 3. 配置本程序

```bash
copy config.example.json config.json
copy _account.txt.example _account.txt
```

编辑 `config.json`：填你的群号、直播间链接。

编辑 `_account.txt`：填机器人 QQ 号（用于快速登录，省去每次扫码）。

### 4. 运行

```bash
python gui.py
```

或者双击 `启动界面.bat`。

---

## 配置说明

### `trigger` —— 什么才算「开播了」

```json
"trigger": {
  "on_obs_stream": true,        // OBS 开始推流时通知（推荐）
  "on_process_start": false,    // 直播软件一启动就通知（会误报，默认关）
  "hotkey": "ctrl+alt+k"        // 全局快捷键，留空则关闭
}
```

快捷键格式为 `修饰键+键`，修饰键支持 `ctrl` / `alt` / `shift` / `win`，
按键支持字母、数字、`f1`~`f24`。

如果注册失败（被别的软件占用），日志里会提示并建议替代值。

### `groups`

```json
{
  "group_id": 123456789,
  "enabled": true,
  "at_all": true,           // @全体成员（需要机器人在群里是群主或管理员）
  "at_list": [],            // 或改成 @ 指定的人，填 QQ 号
  "note": "主群"
}
```

> `@全体成员` 只有**群主 / 管理员**发出去才真正生效。
> 普通成员发了消息会显示，但提醒不到人。程序自检时会明确告诉你哪个群不行。

### `message`

模板里可用占位符：`{title}` `{link}` `{time}` `{date}`。换行写 `\n`。

### 其它

| 字段 | 说明 |
|---|---|
| `watch.processes` | 进程名单（仅当 `on_process_start` 打开时生效） |
| `behavior.cooldown_minutes` | 两次通知的最小间隔，默认 30 |
| `behavior.dry_run` | 彩排模式，只打日志不真发 |
| `control.port` | 本地控制端口，浏览器点一下可手动触发 |

---

## 命令行用法

图形界面之外，核心逻辑也能单独跑：

```bash
python live_notify.py check     # 自检：配置 / NapCat 连通性 / 群权限
python live_notify.py test      # 彩排：打印将要发送的内容，不真发
python live_notify.py send      # 立即发送一次
python live_notify.py watch     # 常驻监控
python live_notify.py groups    # 列出机器人所在的所有群
```

---

## 打包成 exe

```bash
pip install pyinstaller
python -m PyInstaller --onefile --windowed --icon _build/app.ico \
       --name "大肥鱼直播姬" gui.py
```

图标由 `_build/_makeicon.py` 生成 —— 纯 Python 手写 PNG 编码 + 4 倍超采样抗锯齿，
不依赖 Pillow。

---

## 常见问题

**Q：为什么不做成「检测到 OBS 启动就通知」？**
因为那不是你开播的时刻。默认关闭，可以在界面里打开，但做好被群友白提醒的准备。

**Q：直播姬 / 直播伴侣能自动检测吗？**
不能。直播姬虽然是 OBS 内核，但**没有带 websocket 插件**；直播伴侣完全封闭。
这两个只能用快捷键。

**Q：开了通知器我的 QQ 会掉吗？**
不会。程序跑在独立 QQ 副本上，是第二个实例。实测你自己的 QQ 8 个进程全程不受影响。

**Q：群里的 `@全体成员` 没提醒到人？**
机器人不是群主/管理员。跑 `python live_notify.py check` 会告诉你具体哪个群不行，
改用 `at_all: false` + `at_list` 逐个 @ 即可。

**Q：会不会泄露我的聊天记录？**
程序只跟 `127.0.0.1:3000` 通信，无任何外部上报。
NapCat 的 WebUI 已限制为仅本机监听，聊天内容日志也是关闭的。
详见 `napcat/config/napcat.json` 的 `consoleLogLevel`。

---

## 关于许可证

- **本项目代码**：（见 `LICENSE`，默认 MIT，可自行替换）
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

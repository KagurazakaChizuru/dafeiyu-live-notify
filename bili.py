"""B 站空间数据的只读访问 —— 纯标准库实现。

用途
----
订阅某个 UP 主，看看他有没有新投稿。

为什么要自己算签名
------------------
B 站从 2023 年起给空间类接口上了 **wbi 签名**（参数里的 `w_rid`）。算法是公开
的：取 `x/web-interface/nav` 给的 `img_key` / `sub_key`，拼起来按一张固定的
64 位混淆表重排、取前 32 字当密钥；请求参数按 key 排序、连 `wts` 一起做 MD5。
**全程只需要 hashlib + urllib**，所以不必为此引入任何第三方库 —— 那是本项目
的硬约束（见开发文档「必须遵守的工程约定」）。

匿名可用性（2026-09-21 实测）
----------------------------
投稿列表 `x/space/wbi/arc/search` **不登录也能通**，但三样缺一不可：

    1. wbi 签名（w_rid + wts）
    2. 指纹 buvid3 + buvid4（`x/frontend/finger/spi` 匿名可取）
    3. web 端那串 `dm_img_*` 探针参数

第 3 样最容易被忽略，也最坑：**缺了它，同一个请求会返回 `-352`（风控）**，
而报错文案只说"风控校验失败"，看不出缺的是哪个参数。实测补齐后即 `code=0`。

动态接口 `x/polymer/web-dynamic/v1/feed/space` 即使带全指纹仍是 `-352` ——
**那条要登录态（SESSDATA）**，本模块刻意不做：一个"通知器"不该为了多读一种
内容而长期持有用户的 B 站登录态。

网络行为
--------
只访问 `api.bilibili.com`，**直连、不走系统代理**（`ProxyHandler({})`）。
和 OneBot 客户端同样的理由：系统代理会劫持本地/境内请求，实测直接 502。

请求头**刻意只发 User-Agent 和 Referer**。这不是偷懒：实测带上
`Accept` / `Accept-Language` 反而会被 WAF 判成伪造请求（HTTP 412，
`{"code":-412,"message":"request was banned"}`），去掉就 code=0。
"像浏览器缺一半"比"明摆着是脚本"更容易被挡。

**会被风控限流，这是实测的（2026-09-21）。** 同一个请求、同一份指纹，短时间内
打十几次之后，所有空间请求会一起变成 **HTTP 412**（连 JSON 都不是），换个
UP 主也一样 —— 它是按来源限的，不是按参数。等一段时间会自己恢复。

所以这个模块的调用方必须做到两点：

    · **轮询间隔按分钟算**（本项目默认 300 秒），不要秒级试
    · 拿到风控码就**退避**并如实记一行 WARN，别重试到把额度耗光

`_MIN_GAP` 只是"两次调用之间"的兜底，它不是轮询间隔。
"""

import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.bilibili.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

#: wbi 混淆表。它是 0..63 的一个置换，作用是把 img_key+sub_key 打散。
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

#: 风控探针参数。**值本身没有意义**，接口只校验它们存在、格式像那么回事。
#: 缺了这组参数会拿到 -352，而错误文案完全看不出是这个原因。
_DM_PROBE = {
    "dm_img_list": "[]",
    "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
    "dm_cover_img_str": "QU5HTEUgKEludGVsLCBNZXNhIEludGVsKFIpIFUrSCBHcmFwaGljcw",
    "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
}

#: 两次请求之间至少隔这么久。实测连打会拿到 -799（请求过于频繁），
#: 而那个码看起来像"没权限"。轮询间隔本来就有分钟级，这里只是兜底。
_MIN_GAP = 1.5

#: 扫码登录（passport，跟空间接口不是一套风控）
QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
QR_OK = 0              # 扫完并在手机上确认了
QR_WAIT = 86101        # 还没扫
QR_SCANNED = 86090     # 扫了，等手机确认
QR_EXPIRED = 86038     # 这张码过期了，得重新生成


class QrLogin:
    """扫码登录，拿到 SESSDATA —— 界面里那个"登录 B站"按钮用的就是它。

    三步（都是 B站网页版自己在用的那套）：

        start()  → passport 生成一张码，返回要编码进二维码的 url
        （界面把 url 画成二维码给用户扫）
        poll()   → 轮询；用户扫了、确认了，响应头里就有 Set-Cookie: SESSDATA=...

    为什么值得做：动态接口匿名读不到（实测，见 `Space.dynamics`），而让用户
    自己开 F12 抄 cookie 不该叫"登录"。

    fetch 是给测试注入的（不联网也能验状态机）。签名：
        fetch(url) -> (http_status, headers, body_text)
    """

    def __init__(self, fetch=None):
        self._fetch = fetch or _http_get
        self.url = ""
        self.key = ""

    def start(self):
        """生成一张新码。返回要画进二维码的 url。"""
        status, _hdr, body = self._fetch(QR_GENERATE)
        try:
            data = json.loads(body)
        except ValueError:
            raise BiliError("生成二维码失败：接口没回 JSON（HTTP {}）".format(status))
        if data.get("code") != 0:
            raise BiliError("生成二维码失败：code={} msg={}".format(
                data.get("code"), data.get("message")))
        info = data.get("data") or {}
        self.url = str(info.get("url") or "")
        self.key = str(info.get("qrcode_key") or "")
        if not self.url or not self.key:
            raise BiliError("生成二维码失败：接口没给 url / qrcode_key")
        return self.url

    def poll(self):
        """问一次扫得怎么样了。

        返回 (状态, sessdata)。状态是 "wait" / "scanned" / "ok" / "expired"；
        只有 "ok" 时第二项才是登录态（一串非空字符串）。
        """
        if not self.key:
            raise BiliError("还没生成二维码")
        query = urllib.parse.urlencode({"qrcode_key": self.key,
                                        "source": "main-fe-header"})
        status, headers, body = self._fetch(QR_POLL + "?" + query)
        try:
            data = json.loads(body)
        except ValueError:
            raise BiliError("轮询登录状态失败：接口没回 JSON（HTTP {}）".format(status))
        inner = data.get("data") or {}
        code = inner.get("code")
        if code == QR_OK:
            sess = sessdata_from(headers)
            if not sess:
                # 成功却没给 cookie：多半是接口改版，明说，别让用户干等
                raise BiliError("登录成功了，但响应里没有 SESSDATA（B站改版了？）")
            return "ok", sess
        if code == QR_SCANNED:
            return "scanned", ""
        if code == QR_EXPIRED:
            return "expired", ""
        return "wait", ""


def _http_get(url):
    """发一个 GET，把状态、响应头、正文都交出去（扫码登录要读 Set-Cookie）。"""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Referer", "https://www.bilibili.com/")
    opener = _opener()
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, resp.headers, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        raise BiliError("连不上 B站：{}".format(exc))


def sessdata_from(headers):
    """从响应头里把 SESSDATA 抠出来。

    `headers` 可以是 email.message.Message（urllib 给的）或普通 dict ——
    取全部 Set-Cookie 再找那一个，别假设它排第一（实测它跟在别的 cookie 后面）。
    """
    raw = []
    if hasattr(headers, "get_all"):
        raw = headers.get_all("Set-Cookie") or []
    elif isinstance(headers, dict):
        value = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
        raw = [value] if value else []
    for item in raw:
        for part in str(item).split(";"):
            name, _sep, value = part.strip().partition("=")
            if name == "SESSDATA" and value:
                return value
    return ""


#: 拿它当"凭据还有没有效"的对照账号：B站官方号，动态不断。
#: **别改成别的 mid** —— 这个自检成立的前提是"它一定有动态"。
CONTROL_MID = 2


class BiliError(Exception):
    """取数据失败。文案要能直接给用户看。"""


def _opener():
    # 显式禁代理：系统代理会把这个请求也劫走。
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _mixin_key(img_key, sub_key):
    raw = img_key + sub_key
    return "".join(raw[i] for i in _MIXIN_TAB)[:32]


def _sign(params, mixin_key):
    """算出带 w_rid 的查询串。

    `!'()*` 这几个字符要先去干净，B 站那边也是这么处理的 —— 不一致就签名
    对不上，而报错同样是含糊的风控码。
    """
    clean = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in params.items()}
    clean["wts"] = int(time.time())
    query = urllib.parse.urlencode(sorted(clean.items()))
    clean["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return urllib.parse.urlencode(sorted(clean.items()))


class Space:
    """一个可复用的会话：拿着 wbi 密钥与指纹去读空间数据。

    密钥与指纹都是**当天有效**的东西，所以缓存起来反复用；一旦接口回风控码
    就重新取一次（密钥会在服务端轮换，跨零点尤其容易失效）。
    """

    def __init__(self):
        self.opener = _opener()
        self._img = ""
        self._sub = ""
        self._cookie = ""
        self._last_call = 0.0
        self._token_day = ""

    # ---- 基础设施 ----
    def _get(self, path, query="", referer="https://www.bilibili.com/",
             cookie=""):
        """发一个请求。

        **`Referer` 必须像那么回事。** 实测：空间接口的 Referer 写成光秃秃的
        站根（`https://space.bilibili.com/`）会被挡在 WAF 外面，回 **HTTP 412**
        ——连 JSON 都不是，报错看不出原因。指到具体空间页（`/<mid>/video`）
        就正常返回 `code=0`。

        `cookie` 是附加在指纹后面的登录态，只有动态接口需要（见 `dynamics`）。
        """
        url = API + path + ("?" + query if query else "")
        req = urllib.request.Request(url)
        # 只发 User-Agent 和 Referer —— **多发头反而会被挡**。
        #
        # 实测（同一进程、同一个 URL、同一份 cookie）：
        #   带 Accept + Accept-Language        -> HTTP 412，body 是
        #                                        {"code":-412,"message":"request was banned"}
        #   只带 User-Agent + Referer          -> code=0，171 条投稿
        #   只带 User-Agent + Referer，无 cookie -> code=0
        # 两次独立复现。看着像是"声称自己是浏览器、却缺了 sec-ch-ua / sec-fetch
        # 那一整套"反而更像伪造请求 —— 所以这里刻意少发，不是漏了。
        req.add_header("User-Agent", UA)
        req.add_header("Referer", referer)
        jar = self._cookie
        if cookie:
            jar = (jar + "; " if jar else "") + cookie
        if jar:
            req.add_header("Cookie", jar)
        gap = _MIN_GAP - (time.time() - self._last_call)
        if gap > 0:
            time.sleep(gap)
        try:
            with self.opener.open(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise BiliError("B 站返回 HTTP {}（多半是风控，稍后再试）".format(exc.code))
        except Exception as exc:
            raise BiliError("连不上 B 站：{}".format(exc))
        finally:
            self._last_call = time.time()
        try:
            return json.loads(body)
        except ValueError:
            raise BiliError("B 站返回的不是 JSON（可能被劫持或改版）")

    def _ensure_token(self, force=False):
        today = time.strftime("%Y-%m-%d")
        if not force and self._cookie and self._token_day == today:
            return
        nav = self._get("/x/web-interface/nav",
                        referer="https://www.bilibili.com/")
        wbi = ((nav.get("data") or {}).get("wbi_img") or {})
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            raise BiliError("拿不到 wbi 密钥（B 站可能改版了）")
        spi = self._get("/x/frontend/finger/spi",
                        referer="https://www.bilibili.com/")
        data = spi.get("data") or {}
        b3, b4 = data.get("b_3", ""), data.get("b_4", "")
        if not b3:
            raise BiliError("拿不到 buvid 指纹（B 站可能改版了）")
        self._img, self._sub = img, sub
        self._cookie = "buvid3={}; buvid4={}; b_nut={}".format(
            b3, b4, int(time.time()))
        self._token_day = today

    # ---- 对外接口 ----
    def videos(self, mid, page_size=25):
        """取这个 UP 主最新的一批投稿（按发布时间倒序）。

        返回 [{"bvid","title","created","link"}, ...]；created 是 epoch 秒。
        """
        mid = int(mid)
        self._ensure_token()
        # 参数集对齐实测可用的那一份（多出的 c_loc / q 留空即可）。
        params = {
            "mid": mid, "ps": int(page_size), "pn": 1,
            "order": "pubdate", "platform": "web",
            "web_location": 1550101, "otype": "json", "c_loc": "", "q": "",
            "sort_field": 0, "tid": 0, "user_type": 0,
        }
        params.update(_DM_PROBE)
        ref = "https://space.bilibili.com/{}/video".format(mid)
        query = _sign(params, _mixin_key(self._img, self._sub))
        data = self._get("/x/space/wbi/arc/search", query, referer=ref)
        code = data.get("code")
        if code != 0:
            # 密钥可能刚轮换过，重取一次再试一回；再不行就如实报错。
            self._ensure_token(force=True)
            query = _sign(params, _mixin_key(self._img, self._sub))
            data = self._get("/x/space/wbi/arc/search", query, referer=ref)
            code = data.get("code")
        if code != 0:
            raise BiliError("取投稿列表失败：code={} msg={}（-352 是风控，"
                            "-799 是请求太快）".format(code, data.get("message")))

        vlist = (((data.get("data") or {}).get("list") or {}).get("vlist")) or []
        out = []
        for item in vlist:
            bvid = item.get("bvid") or ""
            if not bvid:
                continue
            out.append({
                "bvid": bvid,
                "title": str(item.get("title") or "").strip(),
                "created": int(item.get("created") or 0),
                "link": "https://www.bilibili.com/video/" + bvid,
                "cover": item.get("pic") or "",
            })
        return out

    def dynamics(self, mid, sessdata="", page_size=20):
        """取这个 UP 主最新的一批动态。**必须有登录态（SESSDATA）。**

        别浪费时间试匿名：实测（2026-09-21）这个接口不登录时要么回 `-352`，
        要么回 `code=0` 但 `items` 是空数组；而且**连 B站官方号（mid=2）
        结果一样** —— 不是账号问题，是匿名一律不给数据。老接口
        `api.vc.bilibili.com/dynamic_svr/space_history` 已经 404 没了。

        所以调用方要先去配置里拿到 SESSDATA；这里拿不到就**直接报错**，
        不装作"这个 UP 主没发动态"——那会让人以为功能是好的。

        返回 [{"id","created","text","type","link"}, ...]，按时间倒序。
        """
        mid = int(mid)
        sessdata = str(sessdata or "").strip()
        if not sessdata:
            raise BiliError("动态接口需要登录态：配置里 subscribe.sessdata 是空的"
                            "（匿名读不到动态，实测如此）")
        self._ensure_token()
        params = {
            "host_mid": mid, "offset": "", "timezone_offset": -480,
            "platform": "web", "features": "itemOpusStyle,listOnlyfans",
            "web_location": 333.1387,
        }
        params.update(_DM_PROBE)
        ref = "https://space.bilibili.com/{}/dynamic".format(mid)
        query = _sign(params, _mixin_key(self._img, self._sub))
        data = self._get("/x/polymer/web-dynamic/v1/feed/space", query,
                         referer=ref, cookie="SESSDATA=" + sessdata)
        code = data.get("code")
        if code in (-352, -401):
            # 注意：**凭据过期不会走到这里**。实测过期/写错的凭据照样回 code=0，
            # 只是 items 空。这里挡的是风控和"压根没带登录态"两种。
            raise BiliError("动态接口回了 {}（风控或没带登录态）。"
                            "凭据是否还有效要用 Space.sessdata_looks_ok() 对照着看 —— "
                            "过期的凭据不报错，只会读到空".format(code))
        if code != 0:
            raise BiliError("取动态失败：code={} msg={}".format(
                code, data.get("message")))
        items = (data.get("data") or {}).get("items") or []
        out = []
        for it in items:
            dyn_id = str(it.get("id_str") or "")
            if not dyn_id:
                continue
            author = ((it.get("modules") or {}).get("module_author") or {})
            out.append({
                "id": dyn_id,
                "created": int(author.get("pub_ts") or 0),
                "text": _dyn_text(it),
                "type": str(it.get("type") or ""),
                "link": "https://t.bilibili.com/" + dyn_id,
                "cover": _dyn_cover(it),
            })
        out.sort(key=lambda x: x["created"])
        return out

    def sessdata_looks_ok(self, sessdata):
        """这份登录态还能不能用。

        为什么要拿**别的账号**做对照：实测（2026-09-21）错的/过期的
        SESSDATA **不会报错** —— 接口照样回 `code=0`，只是 `items` 空。
        也就是说"某个 UP 主最近没发动态"和"凭据已经死了"从一次返回里
        长得一模一样。拿一个动态不断的账号（B站官方号）试一次，才分得出来。

        返回 True/False。取不到（网络问题、风控）也返回 False —— 调用方
        只该把它当"可疑"，别当判决。
        """
        try:
            return bool(self.dynamics(CONTROL_MID, sessdata))
        except BiliError:
            return False

    def up_name(self, mid):
        """UP 主的显示名。拿不到就返回空串（调用方自己决定怎么兜）。"""
        try:
            data = self._get(
                "/x/web-interface/card?mid={}&photo=false".format(int(mid)),
                referer="https://space.bilibili.com/{}".format(int(mid)))
            return str(((data.get("data") or {}).get("card") or {}).get("name") or "")
        except BiliError:
            return ""


#: 投稿动态。它跟 arc/search 拿到的是**同一件事** —— 两个都开就会为同一个
#: 视频播报两次，所以在 poll_subscriptions 里按 type 跳过（见那边的注释）。
DYN_TYPE_AV = "DYNAMIC_TYPE_AV"


#: 转发/投稿类动态里 B站塞进 desc.text 的**通用标签**。它们不是内容 ——
#: 实测她自己的动态里清一色是「分享视频」，照发出去群里看到的就是一句废话。
_GENERIC_DYN_TEXT = ("转发动态", "分享视频", "分享了视频", "投稿了视频",
                     "发布了动态", "发表了动态", "分享了动态", "转发了动态")


def _dyn_major_title(md):
    """卡片主体里的标题：投稿 / 专栏 / 图文 / 通用。取不到返回空串。"""
    major = (md or {}).get("major") or {}
    for key, field in (("archive", "title"), ("opus", "title"),
                       ("article", "title"), ("common", "title")):
        val = str((major.get(key) or {}).get(field) or "").strip()
        if val:
            return val
    draw = major.get("draw") or {}
    for pic in (draw.get("items") or []):
        val = str(pic.get("description") or "").strip()
        if val:
            return val
    return ""


def _dyn_cover(item):
    """动态的封面图地址。取不到返回空串（那就不带图）。

    位置随类型变，都是实测的：
      图文（MAJOR_TYPE_OPUS / DRAW）→ major.opus.pics[0].url / draw.items[0].src
      投稿（MAJOR_TYPE_ARCHIVE）    → major.archive.cover
      转发                          → 自己这层是空的，得去 orig 里找
    """
    def from_major(md):
        major = (md or {}).get("major") or {}
        arch = major.get("archive") or {}
        if arch.get("cover"):
            return str(arch["cover"]).strip()
        opus = major.get("opus") or {}
        for pic in (opus.get("pics") or []):
            if pic.get("url"):
                return str(pic["url"]).strip()
        draw = major.get("draw") or {}
        for pic in (draw.get("items") or []):
            if pic.get("src"):
                return str(pic["src"]).strip()
        common = major.get("common") or {}
        if common.get("cover"):
            return str(common["cover"]).strip()
        return ""

    md = ((item.get("modules") or {}).get("module_dynamic") or {})
    orig = item.get("orig") if isinstance(item.get("orig"), dict) else {}
    omd = ((orig.get("modules") or {}).get("module_dynamic") or {})
    return from_major(md) or from_major(omd)


def _dyn_text(item):
    """从一条动态里抠出能当标题用的那句话。

    动态的正文位置随类型变：纯文字在 desc.text，投稿在 major.archive.title，
    图文在 major.draw.items[].description，专栏在 major.opus.title。

    **通用标签要让位给真标题**：转发别人的视频时，desc.text 只是「分享视频」，
    真正的内容在被转发那张卡片的标题里。实测踩到过（她最新 5 条动态全是这样），
    照发出去群里就是一句废话。

    抠不到就返回空串 —— 调用方会把带它的那几行删掉，而不是发一条空的。
    """
    md = ((item.get("modules") or {}).get("module_dynamic") or {})
    text = str((md.get("desc") or {}).get("text") or "").strip()
    # 转发：真内容在 **orig** 里，不在自己这层的 major 里。
    # 实测她的转发动态：自己这层 desc 是「分享视频」、major.type 是 None，
    # 被转发那条视频的标题在 orig.modules.module_dynamic.major.archive.title。
    # （第一版只在自己这层找，测试又用的是扁平假结构 —— "本地过了、真实数据
    #  没变"。教训：测试得照着真响应写。）
    orig = item.get("orig") if isinstance(item.get("orig"), dict) else {}
    omd = ((orig.get("modules") or {}).get("module_dynamic") or {})
    title = _dyn_major_title(md) or _dyn_major_title(omd)
    if title and (not text or text in _GENERIC_DYN_TEXT):
        return title
    return text or title


def _fmt_time(epoch):
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def main(argv=None):
    """自检入口：python bili.py <mid> [条数]"""
    import sys
    argv = list(argv if argv is not None else sys.argv[1:])
    # 标题里可能有 emoji，而 Windows 控制台默认是 GBK —— 不切编码就崩在 print 上
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not argv:
        print("用法：python bili.py <mid> [条数]")
        return 2
    mid = argv[0]
    limit = int(argv[1]) if len(argv) > 1 else 5
    space = Space()
    try:
        name = space.up_name(mid)
        vids = space.videos(mid)
    except BiliError as exc:
        print("失败：{}".format(exc))
        return 1
    print("UP：{}（{}）  最新 {} 条投稿：".format(
        name or "?", mid, min(limit, len(vids))))
    for v in vids[:limit]:
        print("  {}  {}  {}".format(_fmt_time(v["created"]), v["bvid"], v["title"]))

    # 动态要登录态：给它就顺手验一下，没给就明说为什么跳过
    if len(argv) > 2:
        sess = argv[2]
        try:
            dyns = space.dynamics(mid, sess)
        except BiliError as exc:
            print("动态取不到：{}".format(exc))
            return 1
        print("\n最新 {} 条动态：".format(min(limit, len(dyns))))
        for d in dyns[-limit:]:
            print("  {}  {}  {}".format(_fmt_time(d["created"]), d["type"],
                                       (d["text"] or "（无文字）")[:40]))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

"""SCP 条目抓取与文本处理（纯逻辑层，不依赖 maibot_sdk）。

本模块承载全部可离线单测的核心逻辑，被 plugin.py 调用：
- HTML → 纯文本转换（strip_html）
- 正文提取（extract_page_content）
- 段落装箱分片（chunk_by_paragraphs）
- 条目目录解析（parse_catalog_items）
- 关键字匹配（search_catalog）
- 分站路由（slug_branch / force_branch_slug）
- 文章渲染 HTML 构建（build_article_html，配合宿主 render.html2png 出图）

设计要点：
- 全部函数为纯函数或仅依赖 httpx，便于脱离 MaiBot Runner 单测；
- 抓取与解析分离，解析函数可直接喂静态 HTML 字符串。
"""

from __future__ import annotations

import base64
import html
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

# ==================== 常量 ====================

# SCP 英文主站（国际站）与中文分部（中站）
# 0.2.0 起指令按分部路由：/scp → 国际站；/scp cn → 中站。
INT_SITE = "https://scp-wiki.wikidot.com"
CN_SITE = "https://scp-wiki-cn.wikidot.com"

# 站点白名单（SSRF 加固）：fetch.site_url_int / site_url_cn 是自由文本配置，
# 谁能改 WebUI 配置，谁就能把它指向内网地址并让插件把响应回显进群聊。
# 仅放行 https + 官方两个 wikidot 域名（validate_site_url）。
ALLOWED_SITE_HOSTS = {"scp-wiki.wikidot.com", "scp-wiki-cn.wikidot.com"}
# 兼容旧引用
DEFAULT_SITE = INT_SITE
EN_SITE = INT_SITE


def validate_site_url(url: str) -> Tuple[bool, str]:
    """校验站点根地址是否在白名单内（SSRF 加固）。

    仅放行 https + ALLOWED_SITE_HOSTS 中的官方 wikidot 域名，用于约束
    fetch.site_url_int / fetch.site_url_cn 两个自由文本配置：配置指向
    http://127.0.0.1、内网地址（RFC1918 / link-local）或非 http(s) scheme
    时一律拒绝，避免把内部服务响应回显进群聊。

    Args:
        url: 待校验的站点根地址。

    Returns:
        (是否放行, 拒绝原因)。放行时原因为空串。
    """
    raw = str(url or "").strip()
    if not raw:
        return False, "站点地址为空"
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https":
        return False, f"仅允许 https 站点（当前 scheme：{parsed.scheme or '无'}）"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "站点地址缺少域名"
    if host not in ALLOWED_SITE_HOSTS:
        return False, f"域名 {host} 不在允许的站点白名单内"
    return True, ""

# 各分部官方页头资源（来自两站 Sigma-9 主题 CSS 中的 #header / #container-wrap 定义，
# 2026-09-26 实测抓取验证）。「官方页面渲染头」即各站页头使用的 logo 与横幅底纹。
# 注意：两站 body_bg 均为 100×400 的整页背景图（顶部约 0–163 行为暗色页头带，
# 其后为浅色正文区），做横幅底纹前必须裁掉浅色部分（见 crop_banner_tile）。
SITE_HEADERS: Dict[str, Dict[str, str]] = {
    "INT": {
        "logo": "https://cdn.scpwiki.com/theme/en/sigma/images/header-logo.svg",
        "logo_mime": "image/svg+xml",
        "bg": "https://cdn.scpwiki.com/theme/en/sigma/images/body_bg.svg",
        "bg_mime": "image/svg+xml",
        "title": "SCP Foundation",
        "subtitle": "Secure · Contain · Protect",
        "banner_color": "#32302f",
    },
    "CN": {
        "logo": "https://sigma9.scpwikicn.com/cn/img/logo.png",
        "logo_mime": "image/png",
        "bg": "https://sigma9.scpwikicn.com/cn/img/body_bg.png",
        "bg_mime": "image/png",
        "title": "SCP 基金会",
        "subtitle": "控制 · 收容 · 保护",
        "banner_color": "#161010",
    },
}

# body_bg 设计总高 400 行中暗色页头带的行数（两站设计一致，实测 CN 在 165 行起变浅色）
BANNER_TILE_DARK_ROWS = 164

# PNG 解码像素总数上限（防御解压炸弹）：官方页头底纹远小于此值；
# 超限的图不参与解码，由调用方回退为不裁剪/兜底色。
MAX_PNG_PIXELS = 16_000_000

# 分部显示名（用于用户提示）
BRANCH_LABELS: Dict[str, str] = {"INT": "国际站", "CN": "中站"}

# 条目目录来源：系列索引页（每页列出该区间的全部条目编号与标题）
CATALOG_INDEX_PAGES: Tuple[str, ...] = (
    # 国际分部（SCP-001 ~ SCP-9999）
    "scp-series", "scp-series-2", "scp-series-3", "scp-series-4", "scp-series-5",
    "scp-series-6", "scp-series-7", "scp-series-8", "scp-series-9", "scp-series-10",
    # 中文分部（SCP-CN-001 ~ SCP-CN-4999）
    "scp-series-cn", "scp-series-cn-2", "scp-series-cn-3",
    "scp-series-cn-4", "scp-series-cn-5",
)

# 正文容器：<div id="page-content"> … <div class="page-tags">
_PAGE_CONTENT_RE = re.compile(
    r'<div\s+id="page-content"\s*>(.*?)<div\s+class="page-tags"', re.S | re.I
)
_PAGE_CONTENT_TAIL_RE = re.compile(
    r'<div\s+id="page-content"\s*>(.*?)(?:<div\s+class="page-tags"|</div>\s*<div\s+class="page-tags")',
    re.S | re.I,
)
_PAGE_TITLE_RE = re.compile(r'<div\s+id="page-title"[^>]*>(.*?)</div>', re.S | re.I)

# 系列索引页中的条目行：<li><a href="/scp-173">SCP-173</a> - 标题</li>
_CATALOG_ITEM_RE = re.compile(
    r'<li>\s*<a\s+href="/([^"#?]+)"[^>]*>\s*(SCP(?:-[A-Z]{0,3})?-?\d+[^<]*?)\s*</a>'
    r'\s*(?:[-\u2013\u2014]\s*([^<]*?))?\s*</li>',
    re.I,
)
# 编号合法性：SCP-173 / SCP-CN-173 / SCP-EN-173 等。
# re.ASCII 是刻意的：\d 默认匹配 Unicode 数字（全角「１７３」、阿拉伯-印度数字），
# 不限定会与「slug 必须为 ASCII」的约束脱节（目录里出现 code=SCP-１７３ 而 slug=scp-173）。
_CODE_RE = re.compile(r"^SCP(?:-[A-Z]{2,3})?-\d+$", re.I | re.ASCII)

# slug 安全形态（ASCII 严格）：小写字母数字与连字符，可含分段，长度受限。
# 用于两处把关：① 目录解析拒绝畸形 href；② 请求前最终校验，保证拼进
# f"{site}/{slug}" 的 slug 不含 %（百分号编码，%2e%2e 可被服务端解回上级目录）、
# 斜杠、反斜杠、空白、查询/锚点、非 ASCII 数字或超长路径。
_SAFE_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SLUG_LEN = 128


def is_safe_slug(slug: str) -> bool:
    """判断 slug 是否可安全拼进 URL 路径（ASCII 严格白名单 + 长度上限）。

    Args:
        slug: 待校验的 slug（如 `scp-173`、`scp-cn-2000`）。

    Returns:
        True 表示可安全用于 `f"{site}/{slug}"`。
    """
    s = str(slug or "").strip()
    if not s or len(s) > MAX_SLUG_LEN:
        return False
    return bool(_SAFE_SLUG_RE.match(s))

# 常见 HTML 实体（未以标准库覆盖的补充映射）
_EXTRA_ENTITIES: Dict[str, str] = {
    "&nbsp;": " ", "&ldquo;": "「", "&rdquo;": "」", "&lsquo;": "『",
    "&rsquo;": "』", "&mdash;": "—", "&ndash;": "–", "&hellip;": "…",
    "&middot;": "·", "&bull;": "•", "&times;": "×", "&laquo;": "«",
    "&raquo;": "»", "&dagger;": "†", "&permil;": "‰",
}


# ==================== HTML → 纯文本 ====================


def strip_html(fragment: str) -> str:
    """把一段 HTML 片段转为可读纯文本。

    处理规则：
    - 移除 script/style 内容；
    - 块级标签结束位置（p/div/h1-6/li/tr/blockquote）转换为换行；
    - <br> 转换行、<hr> 转为分隔线；
    - 表格单元 </td></th> 转为制表符，保持简单表格的可读性；
    - 去掉其余全部标签；
    - 解码 HTML 实体（含数字实体）；
    - 压缩连续空行，去除首尾空白与行尾空格。

    Args:
        fragment: HTML 片段。

    Returns:
        规整后的纯文本。
    """
    if not fragment:
        return ""
    s = fragment
    s = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", "", s, flags=re.S | re.I)
    # <hr> 显式转成一行分隔线，避免长文里段落粘连
    s = re.sub(r"<hr\s*/?>", "\n" + "-" * 24 + "\n", s, flags=re.I)
    # 块级标签结束 → 换行
    s = re.sub(r"</(p|div|h[1-6]|li|tr|blockquote|pre)\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    # 表格单元 → 制表符
    s = re.sub(r"</t[dh]\s*>", "\t", s, flags=re.I)
    # 去掉剩余标签
    s = re.sub(r"<[^>]*>", "", s)
    # 实体解码
    for k, v in _EXTRA_ENTITIES.items():
        s = s.replace(k, v)
    s = html.unescape(s)
    # 规整空白
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # 保护制表符（表格单元分隔符），避免被空格规整吞掉：
    # 先把制表符换成占位符，规整完再换回
    _TAB = "\x00TAB\x00"
    s = s.replace("\t", _TAB)
    s = re.sub(r"[ \u00a0]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r" *" + _TAB + r" *", "\t", s)
    s = s.replace(_TAB, "\t")
    s = re.sub(r"\n{3,}", "\n\n", s)
    # 清理「只有空白的行」造成的伪空行
    s = re.sub(r"\n[ \t]+\n", "\n\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# 页面挂件噪声行：页头评分挂件（rating: +11077+–× / 评分：+5604+–×）
_NOISE_LINE_RES: Tuple[re.Pattern, ...] = (
    re.compile(r"^\s*(?:rating|评分)\s*[:：]", re.I),
    # 独立的评分值行（如 +11077+–×）：仅由数字/加减乘除符号/空格组成、
    # 必须含且以 [+×–] 符号结尾、长度 3~20 —— 纯数字行（如正文中的「173」）、
    # 普通算式行（如「5 × 5」）不受影响
    re.compile(
        r"^\s*(?=[+\d\s\u2013\u2014\u2212\u00d7x]+$)"
        r"(?=.*[+\u00d7\u2013\u2014])(?=.*[\u00d7\u2013+]\s*$).{3,20}\s*$"
    ),
    re.compile(r"^\s*‡", re.I),  # 授权/引用折叠块标题行
    re.compile(r"^\s*(?:Cite this page as|引用此页面|Hide Licensing)\s*[:：/]?", re.I),
    re.compile(r"^\s*\"[^\"]+\"\s+by\s+.+\bfrom the SCP\b", re.I),  # 英文引用行
    re.compile(r"^\s*For information on how to use this (?:component|license)", re.I),
)
# 含「授权 + 引用」双关键词的行（中英文授权块标题的通用特征）
_LICENSE_HEADER_RE = re.compile(r"Licensing|授权信息", re.I)
_CITE_HEADER_RE = re.compile(r"Citation|引用", re.I)


def clean_article_text(text: str) -> str:
    """清理从页面提取的正文中混入的站点挂件噪声行。

    仅删除明确的挂件/授权块行（评分挂件、Licensing/Citation 折叠块、引用说明），
    不动正文；授权信息由渲染页脚统一标注（原页面链接 + CC BY-SA 3.0 声明）。
    """
    if not text:
        return ""
    out: List[str] = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            out.append(ln)
            continue
        if any(p.match(s) for p in _NOISE_LINE_RES):
            continue
        if (
            _LICENSE_HEADER_RE.search(s)
            and _CITE_HEADER_RE.search(s)
            and len(s) < 40
            and (s.startswith("‡") or "Licensing /" in s or "授权 /" in s)
        ):
            continue
        out.append(ln)
    cleaned = "\n".join(out)
    # 清理删除后残留的多余空行
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def normalize_code(raw: str) -> str:
    """把用户输入的编号规范化为小写 slug 形式。

    支持的输入：`scp-173`、`SCP-173`、`173`、`scpcn2000`、`SCP-CN-2000`、
    `cn-2000`、`cn2000`。无法识别时返回空串。

    数字一律按 **ASCII** 匹配（`re.ASCII`）：全角「１７３」、阿拉伯-印度数字等
    Unicode 数字不再被当成合法编号，最终还会经 `is_safe_slug` 把关。

    Args:
        raw: 用户原始输入。

    Returns:
        规范化后的 slug（如 `scp-173`、`scp-cn-2000`）；非法返回 ""。
    """
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    # 去掉全角字符、空格、下划线
    s = s.replace("　", "").replace(" ", "").replace("_", "-")
    s = re.sub(r"-{2,}", "-", s).strip("-")
    out = ""
    # 已含 scp 前缀：scp-173 / scpcn173 / scp-cn-173
    m = re.match(r"^scp-?([a-z]{2,3})?-?(\d+)$", s, re.ASCII)
    if m:
        branch, num = m.group(1), m.group(2)
        out = f"scp-{branch}-{num}" if branch else f"scp-{num}"
    # 纯数字：173
    elif re.match(r"^\d+$", s, re.ASCII):
        out = f"scp-{s}"
    else:
        # 分部前缀无 scp：cn-2000 / cn2000 / en-173
        m = re.match(r"^([a-z]{2,3})-?(\d+)$", s, re.ASCII)
        if m:
            out = f"scp-{m.group(1)}-{m.group(2)}"
    # 最终把关：拼 URL 前保证 slug 为安全的 ASCII 形态（拒绝 %、/、超长等）
    return out if is_safe_slug(out) else ""


def slug_branch(slug: str) -> str:
    """根据 slug 判断所属分部：`scp-cn-*` → CN（中站），其余 → INT（国际站）。"""
    return "CN" if "-cn-" in str(slug or "").lower() else "INT"


def force_branch_slug(slug: str, branch: str) -> str:
    """把 slug 强制归入指定分部（指令带 cn 前缀时使用）。

    - `/scp cn 173` → normalize 得 `scp-173` → 归入 CN → `scp-cn-173`；
    - 已含分部标记的 slug（`scp-cn-2000`）保持不变。
    """
    slug = str(slug or "").strip().lower()
    if branch == "CN" and "-cn-" not in slug:
        m = re.match(r"^scp-(\d+)$", slug)
        if m:
            return f"scp-cn-{m.group(1)}"
    return slug


# ==================== 管理员名单 ====================

# 0.3.3（2026-09-29 二轮）：管理员判定实现统一收敛到公共模块 admin_util.py
# （来源 cateye_common，随插件分发），此处仅 re-export 保持
# `from scp_core import plain_id / collect_admins` 的既有导入路径兼容。
try:  # 作为包内模块导入时走相对导入
    from .admin_util import collect_admins, plain_id
except ImportError:  # pragma: no cover - 独立模块加载时走绝对导入
    from admin_util import collect_admins, plain_id  # type: ignore[no-redef]


def to_data_uri(content: bytes, mime: str) -> str:
    """把二进制内容编码为 data URI（用于在离线渲染的 HTML 内嵌官方页头图片）。"""
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"


# data URI 白名单（0.3.3 加固）：页头资源 URI 注入 HTML src/CSS 前必须匹配
# 「data:image/(png|svg+xml);base64,<纯 base64>」形态。正常内容来自内部
# to_data_uri()（必然匹配）；磁盘缓存 header_assets_*.json / 内置快照若被写入
# 任意字符串（含 `"`、`()`、`javascript:` 等），不匹配即按资源缺失处理走兜底链，
# 杜绝注入逃逸出 src 属性 / CSS url()（html2png 默认禁网，实际影响限于渲染异常/
# 内容伪造，属本地文件信任边界加固）。
_DATA_URI_RE = re.compile(r"^data:image/(?:png|svg\+xml);base64,[A-Za-z0-9+/=]+$")
# 长度上限：MAX_ASSET_BYTES（4MB）资源 base64 后约 5.6M 字符，留余量
MAX_DATA_URI_CHARS = 8 * 1024 * 1024


def is_safe_data_uri(uri: str) -> bool:
    """判断 data URI 是否可安全注入 HTML 属性 / CSS url()（形态白名单 + 长度上限）。

    仅放行 `data:image/png` 与 `data:image/svg+xml` 的纯 base64 载荷
    （字符集 [A-Za-z0-9+/=]，不含引号/括号/空白/百分号等可逃逸属性或 CSS 的字符）。
    """
    s = str(uri or "")
    if not s or len(s) > MAX_DATA_URI_CHARS:
        return False
    return bool(_DATA_URI_RE.match(s))


# ==================== 官方底纹裁剪（纯 stdlib，无 Pillow 依赖） ====================


def _png_decode_rows(data: bytes) -> Optional[Tuple[int, int, int, List[bytes]]]:
    """解码 8-bit 非隔行 PNG（RGB/RGBA/Gray），返回 (w, h, 每像素字节数, 行数据)。

    不支持的格式（隔行 / 调色板 / 16-bit 等）返回 None，由调用方回退为不裁剪。
    像素总数超过 MAX_PNG_PIXELS 的按不支持处理（防御解压炸弹：4MB 以内的
    恶意 PNG 可声明超大 w×h，解压后吃满进程内存）。

    0.3.3 加固：IDAT 改为 decompressobj 分块解压并限制累计输出——
    MAX_PNG_PIXELS 只能约束 IHDR 声明的 w×h，拦不住「声明 100×400（可通过
    校验）却携带 4MB 高压缩比 IDAT」的炸弹（zlib.decompress 会先把整个流
    解压完）。解压输出上限取 `(stride + 1) * h`（合法 PNG 的精确解压尺寸：
    每行 1 字节滤波类型 + stride 字节像素），与 MAX_PNG_PIXELS 关联、
    超限立即中止并按不支持格式处理。
    """
    import struct
    import zlib

    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos, idat, meta = 8, b"", None
    while pos + 8 <= len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            if ln < 13:
                return None
            try:
                w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunk[:13])
            except struct.error:
                return None
            if depth != 8 or interlace != 0 or ctype not in (0, 2, 6):
                return None
            if w * h > MAX_PNG_PIXELS:
                return None
            meta = (w, h, ctype)
        elif typ == b"IDAT":
            idat += chunk
        pos += 12 + ln
    if not meta or not idat:
        return None
    w, h, ctype = meta
    bpp = {0: 1, 2: 3, 6: 4}[ctype]
    stride = w * bpp
    # 分块解压 + 累计输出上限（合法流恰好 (stride+1)*h 字节，超限即炸弹）
    limit = (stride + 1) * h
    d = zlib.decompressobj()
    raw = bytearray()
    try:
        for i in range(0, len(idat), 65536):
            raw += d.decompress(idat[i:i + 65536])
            if len(raw) > limit:
                return None
        raw += d.flush()
    except zlib.error:
        return None
    if len(raw) > limit:
        return None
    raw = bytes(raw)
    rows: List[bytes] = []
    prev = bytearray(stride)
    p = 0
    for _ in range(h):
        if p >= len(raw):
            return None
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if len(line) < stride:
            return None
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        # f == 0：无滤波
        prev = line
        rows.append(bytes(line))
    return w, h, bpp, rows


def _png_encode_rows(w: int, h: int, bpp: int, ctype: int, rows: List[bytes]) -> bytes:
    """把行数据重新编码为 PNG（每行 filter 0）。"""
    import struct
    import zlib

    def chunk(typ: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + typ + payload
                + struct.pack(">I", zlib.crc32(typ + payload) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + r for r in rows)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def crop_png_rows(data: bytes, keep_rows: int) -> bytes:
    """裁剪 PNG 顶部 keep_rows 行（8-bit 非隔行）；不支持或越界时原样返回。"""
    decoded = _png_decode_rows(data)
    if not decoded:
        return data
    w_, h_, bpp_, rows_ = decoded[0], decoded[1], decoded[2], decoded[3]
    if keep_rows <= 0 or keep_rows >= h_ or not rows_:
        return data
    # 反推 colortype：bpp 1→0(Gray) 3→2(RGB) 4→6(RGBA)
    ctype = {1: 0, 3: 2, 4: 6}.get(bpp_)
    if ctype is None:
        return data
    return _png_encode_rows(w_, keep_rows, bpp_, ctype, rows_[:keep_rows])


def crop_svg_top(data: bytes, total_rows: int, keep_rows: int) -> bytes:
    """裁剪 SVG 画布顶部：把 viewBox/height 从 total_rows 改为 keep_rows。

    适配两站 body_bg.svg（viewBox="0 0 100 400"）这类整页背景；模式不匹配时原样返回。
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    new = re.sub(r'viewBox="0\s+0\s+100\s+' + str(total_rows) + '"',
                 f'viewBox="0 0 100 {keep_rows}"', text, count=1)
    if new == text:
        return data
    # height 只在根 <svg> 标签内替换，避免误改内部元素的属性
    m = re.search(r"<svg[^>]*>", new)
    if not m:
        return data
    new_tag = re.sub(r'height="' + str(total_rows) + r'(?:px)?"',
                     f'height="{keep_rows}px"', m.group(0), count=1)
    new = new.replace(m.group(0), new_tag, 1)
    return new.encode("utf-8")


def crop_banner_tile(data: bytes, mime: str) -> bytes:
    """把整页背景图块（100×400，顶部为暗色页头带）裁剪为暗色带部分。

    用于横幅底纹：不裁剪的话浅色正文区会被一起压进横幅（表现为横幅下半发白）。
    PNG 走像素裁剪，SVG 改写 viewBox；无法处理时原样返回（退化为 CSS 兜底色）。
    """
    keep = BANNER_TILE_DARK_ROWS
    if mime == "image/png":
        return crop_png_rows(data, keep)
    if mime == "image/svg+xml":
        return crop_svg_top(data, 400, keep)
    return data


def extract_page_content(html_text: str) -> Tuple[str, str]:
    """从页面 HTML 中提取标题与正文纯文本。

    Args:
        html_text: 完整页面 HTML。

    Returns:
        (标题, 正文纯文本)。取不到正文时正文为空串。

    Note:
        以 `<div id="page-content">` 为起点、`<div class="page-tags">` 为终点截取。
        若页面无 page-tags（部分特殊页），退回在 page-content 后的首个闭合标签处截断。
    """
    if not html_text:
        return "", ""
    title = ""
    m = _PAGE_TITLE_RE.search(html_text)
    if m:
        title = strip_html(m.group(1)).strip()

    body = ""
    m = _PAGE_CONTENT_RE.search(html_text)
    if m:
        body = m.group(1)
    else:
        # 兜底：没有 page-tags 的页面，取 page-content 到页面底部区块之前
        m = _PAGE_CONTENT_TAIL_RE.search(html_text)
        if m:
            body = m.group(1)
    if not body:
        return title, ""
    return title, strip_html(body)


def is_error_page(html_text: str, title: str = "") -> bool:
    """判断是否抓到了「页面不存在」占位页。

    SCP 中文站对不存在的页面返回 200 + 占位文档（正文含「此页面不存在」）。

    Args:
        html_text: 页面 HTML。
        title: 已提取的标题（可选，用于快速判断）。

    Returns:
        True 表示该页面不存在（或为占位页）。
    """
    if "此页面不存在" in html_text or "This page does not exist" in html_text:
        return True
    if "该页面不存在" in html_text:
        return True
    # 标题形如「此页面不存在！」也算
    if title and ("不存在" in title and "页面" in title):
        return True
    return False


# ==================== 段落装箱分片 ====================


def split_paragraphs(text: str) -> List[str]:
    """按空行切分段落，过滤空白段。"""
    if not text:
        return []
    parts = re.split(r"\n{2,}", text)
    return [p.strip() for p in parts if p.strip()]


# 句级 / 子句级边界：句末标点后优先切，其次逗号顿号分号
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[。！？!?…」』”）>])")
_CLAUSE_BOUNDARY_RE = re.compile(r"(?<=[，、,;；：:])")


def _greedy_pack(pieces: Sequence[str], limit: int) -> Optional[List[str]]:
    """把切分碎片贪心装箱到 ≤ limit；单个碎片自身超限时返回 None（需更细级别）。

    保留片段间原有的单个空格（英文句读切分后的 "a, b" 重组回 "a, b" 而非 "a,b"）。
    """
    out: List[str] = []
    buf = ""
    for raw_piece in pieces:
        if not raw_piece.strip():
            continue
        prefix = " " if raw_piece[:1] == " " else ""
        piece = prefix + raw_piece.strip()
        if len(piece) > limit:
            return None
        if not buf:
            buf = piece
        elif len(buf) + len(piece) <= limit:
            buf += piece
        else:
            out.append(buf)
            buf = piece
    if buf:
        out.append(buf)
    return out or None


def split_paragraph_smart(para: str, limit: int) -> List[str]:
    """把超长段落切到每段 ≤ limit，优先在句子边界，其次子句边界，最后硬切。

    相比硬切分，可避免「读一半被拦腰截断」。
    """
    para = para.strip()
    if not para or len(para) <= limit:
        return [para] if para else []
    # 逐级放宽：句末标点 → 逗号/顿号/分号 → 硬切
    for pattern in (_SENTENCE_BOUNDARY_RE, _CLAUSE_BOUNDARY_RE):
        pieces = [p for p in pattern.split(para) if p and p.strip()]
        packed = _greedy_pack(pieces, limit)
        if packed:
            return packed
    # 硬切兜底（无任何标点的超长段落）
    return [para[i:i + limit] for i in range(0, len(para), limit)]


def chunk_by_paragraphs(
    text: str,
    per_chunk_chars: int,
    *,
    merge_tail: bool = False,
) -> List[str]:
    """把长文本按段落装箱切成若干片，每片不超过 per_chunk_chars 字符。

    优先在段落边界切分（保持语义完整）；单个段落本身超限时按**句子边界**
    智能切分（split_paragraph_smart），避免拦腰截断。

    Args:
        text: 待切分文本。
        per_chunk_chars: 每片字符上限（<=0 时视为不限制，整篇一片）。
        merge_tail: 尾部碎片合并（图片分页用）：最后一片过短（< 20% 上限）时
            尝试并入前一片，允许超出上限至多 30%，避免最后一页只有孤零零几行。

    Returns:
        分片列表；空文本返回 []。
    """
    if not text:
        return []
    limit = int(per_chunk_chars or 0)
    if limit <= 0:
        return [text]

    paragraphs = split_paragraphs(text)
    if not paragraphs:
        return []

    chunks: List[str] = []
    buf = ""
    for para in paragraphs:
        # 单段落超限：先清空缓冲，再把该段落按句子边界智能切分
        if len(para) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(split_paragraph_smart(para, limit))
            continue
        if not buf:
            buf = para
        elif len(buf) + 2 + len(para) <= limit:
            buf = f"{buf}\n\n{para}"
        else:
            chunks.append(buf)
            buf = para
    if buf:
        chunks.append(buf)

    # 尾部碎片合并（仅图片分页使用，允许小幅超限）
    if merge_tail and len(chunks) > 1 and len(chunks[-1]) < limit * 0.2:
        tail = chunks.pop()
        if len(chunks) and len(chunks[-1]) + 2 + len(tail) <= limit * 1.3:
            chunks[-1] = f"{chunks[-1]}\n\n{tail}"
        else:
            chunks.append(tail)
    return chunks


def build_forward_nodes(
    title: str,
    chunks: Sequence[str],
    *,
    nickname: str,
    user_id: str = "0",
    page_index: int = 0,
    total_pages: int = 1,
    source_url: str = "",
    truncated: bool = False,
    total_chars: int = 0,
    footer_lines: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """把分片组装为 send.forward 的消息节点列表。

    每个节点只含**一个 text 段**（符合文档对 forward 的建议），
    节点文本为「分片正文」，并在首节点顶部标注标题/页码等信息。

    Args:
        title: 条目标题。
        chunks: 本卡要发送的分片（已按 max_nodes 截取）。
        nickname: 转发气泡显示的昵称。
        user_id: 显示用 QQ 号（字符串，可 "0"）。
        page_index: 当前页号（0 基），仅用于显示。
        total_pages: 总页数，仅用于显示。
        source_url: 原页面链接（用于截断提示）。
        truncated: 是否发生了截断。
        total_chars: 正文总字数（用于截断提示）。
        footer_lines: 追加到末尾的额外说明行。

    Returns:
        forward 节点 dict 列表。
    """
    nodes: List[Dict[str, Any]] = []
    n = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        header_parts: List[str] = []
        if i == 1:
            header_parts.append(f"《{title}》" if title else "SCP 条目")
            if total_pages > 1:
                header_parts.append(
                    f"第 {page_index + 1} / {total_pages} 页（本消息为第 {i} / {n} 段）"
                )
            else:
                header_parts.append(f"共 {n} 段")
        else:
            header_parts.append(f"（{i} / {n}）")
        content = "\n".join(header_parts) + "\n" + ("─" * 20) + "\n" + chunk
        nodes.append({
            "user_id": user_id,
            "nickname": nickname,
            "segments": [{"type": "text", "content": content}],
        })

    # 截断/补充说明：作为最后一个独立节点
    if truncated or footer_lines:
        tips: List[str] = []
        if truncated:
            shown_chars = sum(len(c) for c in chunks)
            tips.append(f"本条消息共 {n} 段，约 {shown_chars} 字，未展示全文。")
            if total_chars:
                tips.append(f"本文正文共约 {total_chars} 字。")
            if source_url:
                tips.append(f"完整内容见原页面：{source_url}")
        if footer_lines:
            tips.extend(str(x) for x in footer_lines if str(x).strip())
        if tips:
            nodes.append({
                "user_id": user_id,
                "nickname": nickname,
                "segments": [{"type": "text", "content": "\n".join(tips)}],
            })
    return nodes


# ==================== 条目目录 ====================


def parse_catalog_items(html_text: str) -> List[Dict[str, str]]:
    """从系列索引页 HTML 中解析条目列表。

    Args:
        html_text: 系列索引页 HTML。

    Returns:
        [{"slug", "code", "title", "branch"}] 列表；无匹配返回 []。
    """
    if not html_text:
        return []
    m = _PAGE_CONTENT_RE.search(html_text)
    body = m.group(1) if m else html_text
    out: List[Dict[str, str]] = []
    for href, label, title in _CATALOG_ITEM_RE.findall(body):
        # 目录/SSRF 加固：只收同源相对路径。拒绝绝对 URL、协议相对（//）、上级目录
        # （..）、带 scheme（:）或查询/锚点的 href——随机抽取时会拿 slug 直接拼 URL，
        # 畸形路径不得进入目录。
        if (not href or href.startswith(("/", ".", "http", "www"))
                or any(c in href for c in (":", "..", "?", "#", "\\", "//"))):
            continue
        slug = href.strip().lower()
        # 最终形态校验（ASCII 严格 + 长度上限）：拦下百分号编码（%2e%2e 可被服务端
        # 解回上级目录）、尾随斜杠、空段、非 ASCII 数字、超长路径等漏网形态。
        if not is_safe_slug(slug):
            continue
        label = re.sub(r"\s+", " ", label).strip()
        if not _CODE_RE.match(label):
            continue
        title = re.sub(r"\s+", " ", title or "").strip()
        code = label.upper()
        out.append({
            "slug": slug,
            "code": code,
            "title": title,
            "branch": "CN" if "-CN-" in code else "INT",
        })
    return out


def merge_catalog(entries: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    """合并多页解析结果并按 slug 去重（保留首次出现）。"""
    seen: Dict[str, Dict[str, str]] = {}
    for it in entries:
        slug = str(it.get("slug") or "").strip().lower()
        if not slug or slug in seen:
            continue
        seen[slug] = it
    return list(seen.values())


def search_catalog(
    catalog: Sequence[Dict[str, str]],
    keyword: str,
    *,
    limit: int = 10,
    cn_first: bool = True,
    branch: str = "",
) -> List[Dict[str, str]]:
    """在本地目录中按关键字匹配条目。

    匹配规则（按优先级排序）：
    1. 编号精确匹配（用户直接给编号）；
    2. 编号前缀匹配；
    3. 标题完全相等；
    4. 标题包含关键字；
    5. 编号包含关键字。

    Args:
        catalog: 目录列表。
        keyword: 用户关键字。
        limit: 返回条数上限。
        cn_first: 同权重时中文分部优先。
        branch: 分部过滤（"" = 不过滤；"CN" / "INT" 只返回对应分部条目）。

    Returns:
        匹配结果列表（已排序、去重、截断至 limit）。
    """
    kw = str(keyword or "").strip()
    if not kw:
        return []
    kw_l = kw.lower()
    norm = normalize_code(kw)

    scored: List[Tuple[int, int, Dict[str, str]]] = []
    for it in catalog:
        if branch and str(it.get("branch") or "") != branch:
            continue
        code = str(it.get("code") or "")
        code_l = code.lower()
        title = str(it.get("title") or "")
        score = 0
        if norm and code_l == norm:
            score = 100
        elif norm and code_l.startswith(norm):
            score = 85
        elif title and title == kw:
            score = 80
        elif kw in title:
            score = 60
        elif kw_l in code_l:
            score = 40
        if score <= 0:
            continue
        branch_rank = 0 if (cn_first and it.get("branch") == "CN") else 1
        scored.append((-score, branch_rank, it))

    scored.sort(key=lambda x: (x[0], x[1], str(x[2].get("code"))))
    return [it for _, _, it in scored[: max(1, int(limit))]]


def entry_by_code(catalog: Sequence[Dict[str, str]], raw: str) -> Optional[Dict[str, str]]:
    """按用户输入精确查目录条目（编号匹配），未命中返回 None。"""
    norm = normalize_code(raw)
    if not norm:
        return None
    for it in catalog:
        if str(it.get("slug") or "").lower() == norm:
            return it
    return None


# ==================== 文章渲染 HTML ====================


def _html_para(text: str) -> str:
    """把一段纯文本段落转成 <p>：HTML 转义 + 保留换行/制表符（white-space:pre-wrap）。"""
    return "<p>" + html.escape(text) + "</p>"


def build_article_html(
    *,
    branch: str,
    title: str,
    body: str,
    page_index: int = 1,
    total_pages: int = 1,
    source_lines: Sequence[str] = (),
    logo_data_uri: str = "",
    bg_data_uri: str = "",
    site_title: str = "",
    site_subtitle: str = "",
    footer_lines: Optional[Sequence[str]] = None,
    font_scale: float = 1.5,
) -> str:
    """把条目正文构建为「仿官方页头」的文章 HTML，供宿主 render.html2png 渲染为图片。

    页头还原各分部官方 Sigma-9 主题的视觉：暗色横幅（官方 body_bg 底纹平铺）
    + 官方 logo（左置）+ 站点名与标语。图片资源均以 data URI 内嵌
    （html2png 默认禁网，外部资源不会加载）。

    Args:
        branch: 分部（"INT" / "CN"），决定页头兜底文案与配色细节。
        title: 条目标题（显示在正文顶部，仿官方 page-title 样式）。
        body: 正文纯文本（按段落切分后逐段转 <p>）。
        page_index: 当前页号（1 基，仅展示）。
        total_pages: 总页数（仅展示）。
        source_lines: 页脚来源行（已带标签，如 "原文：https://…" / "译文：https://…"）。
        logo_data_uri: 官方 logo 的 data URI；为空则用文字徽标兜底。
        bg_data_uri: 官方横幅底纹（已裁剪为暗色带）的 data URI；为空则用纯色兜底。
        site_title: 站点名（如 "SCP Foundation" / "SCP 基金会"）。
        site_subtitle: 站点标语。
        footer_lines: 页脚附加行（分页提示 / 授权声明等）。
        font_scale: 字号缩放倍率（1.0 为基准：正文 16px / 标题 28px），默认 1.5。

    Returns:
        完整 HTML 文档字符串。
    """
    meta = SITE_HEADERS.get(branch) or SITE_HEADERS["INT"]
    site_title = site_title or meta.get("title", "SCP Foundation")
    site_subtitle = site_subtitle or meta.get("subtitle", "")
    banner_color = meta.get("banner_color", "#32302f")

    # 注入前防御（0.3.3）：data URI 必须通过形态白名单校验，不合法一律丢弃、
    # 走文字徽标 / 纯色横幅兜底——上游缓存文件即使被写入恶意字符串也进不了 HTML。
    logo_uri = logo_data_uri if is_safe_data_uri(logo_data_uri) else ""
    bg_uri = bg_data_uri if is_safe_data_uri(bg_data_uri) else ""

    fs = min(3.0, max(0.5, float(font_scale or 1.5)))
    size_content = round(16 * fs, 1)
    size_title = round(28 * fs, 1)
    size_t1 = round(27 * fs, 1)
    size_t2 = round(14 * fs, 1)
    size_footer = round(12.5 * fs, 1)

    paragraphs = split_paragraphs(body) if body else []
    body_html = "\n".join(_html_para(p) for p in paragraphs) or "<p>（正文为空）</p>"

    # 页头：官方 logo（或文字兜底）+ 站名/标语
    logo_html = (
        f'<img class="logo" src="{logo_uri}" alt="logo"/>'
        if logo_uri
        else '<div class="logo logo-fallback">SCP</div>'
    )
    banner_style = (
        f'background-image:url({bg_uri});'
        if bg_uri
        else ""
    )

    foot: List[str] = []
    if total_pages > 1:
        foot.append(f"第 {page_index} / {total_pages} 页")
    foot.extend(str(x) for x in (source_lines or []) if str(x).strip())
    foot.extend(str(x) for x in (footer_lines or []) if str(x).strip())
    foot.append("内容来自 SCP 基金会维基，依 CC BY-SA 3.0 授权")
    footer_html = "<br/>".join(html.escape(x) for x in foot)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}
body{{margin:0;background:#fff;width:100%;font-family:"Noto Sans SC","Microsoft YaHei","PingFang SC",Arial,sans-serif;}}
.banner{{{banner_style}background-color:{banner_color};background-repeat:repeat-x;background-size:auto 100%;background-position:top left;height:132px;display:flex;align-items:center;gap:22px;padding:0 36px;}}
.banner img.logo{{width:88px;height:88px;flex:0 0 auto}}
.logo-fallback{{width:88px;height:88px;border:3px solid #fff;border-radius:50%;color:#fff;font-size:26px;font-weight:bold;display:flex;align-items:center;justify-content:center;letter-spacing:1px}}
.titles{{display:flex;flex-direction:column;min-width:0}}
.t1{{color:#eee;font-size:{size_t1}px;font-weight:bold;text-shadow:2px 2px 4px #000;letter-spacing:1px}}
.t2{{color:#f0f0c0;font-size:{size_t2}px;text-shadow:1px 1px 2px #000;margin-top:6px}}
.wrap{{padding:26px 38px 34px}}
.page-title{{font-size:{size_title}px;font-weight:bold;color:#111;border-bottom:2px solid #7c1f1f;padding-bottom:10px;margin-bottom:22px;line-height:1.3}}
.content{{font-size:{size_content}px;line-height:1.8;color:#222}}
.content p{{margin:0 0 15px;white-space:pre-wrap;word-break:break-word}}
.footer{{margin-top:30px;border-top:1px dashed #aaa;padding-top:12px;color:#777;font-size:{size_footer}px;line-height:1.7;word-break:break-all}}
</style></head>
<body>
<div class="banner">{logo_html}<div class="titles"><div class="t1">{html.escape(site_title)}</div><div class="t2">{html.escape(site_subtitle)}</div></div></div>
<div class="wrap">
<div class="page-title">{html.escape(title or "SCP 条目")}</div>
<div class="content">
{body_html}
</div>
<div class="footer">{footer_html}</div>
</div>
</body></html>"""


__all__ = [
    "INT_SITE",
    "CN_SITE",
    "DEFAULT_SITE",
    "EN_SITE",
    "SITE_HEADERS",
    "BRANCH_LABELS",
    "CATALOG_INDEX_PAGES",
    "strip_html",
    "normalize_code",
    "slug_branch",
    "force_branch_slug",
    "to_data_uri",
    "is_safe_data_uri",
    "MAX_DATA_URI_CHARS",
    "validate_site_url",
    "ALLOWED_SITE_HOSTS",
    "plain_id",
    "collect_admins",
    "is_safe_slug",
    "MAX_SLUG_LEN",
    "crop_png_rows",
    "crop_svg_top",
    "crop_banner_tile",
    "BANNER_TILE_DARK_ROWS",
    "MAX_PNG_PIXELS",
    "clean_article_text",
    "split_paragraph_smart",
    "extract_page_content",
    "is_error_page",
    "split_paragraphs",
    "chunk_by_paragraphs",
    "build_forward_nodes",
    "build_article_html",
    "parse_catalog_items",
    "merge_catalog",
    "search_catalog",
    "entry_by_code",
]

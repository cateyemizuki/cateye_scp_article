"""SCP 条目查询插件 — MaiBot v2 插件

功能：
- 指令 `/scp <编号|关键字>`：查询**国际站**（scp-wiki.wikidot.com）条目；
  指令 `/scp cn <编号|关键字>`：查询**中站**（scp-wiki-cn.wikidot.com）条目。
  编号本身带 cn 标记（如 `/scp cn-2000`）时自动路由到中站。
- 文章默认**渲染为图片**发送：抓取对应分部**官方页头资源**（Sigma-9 主题的
  logo 与横幅底纹；抓不到时依次用本机旧缓存、随插件发布的内置兜底资源）内嵌 HTML，
  经宿主 `render.html2png` 渲染成图后用 `send.image` 发出，还原官方页面观感；
  渲染/发送失败自动回退**合并转发文本**。
- 指令 `/scp <编号> <页号>`：读取超长条目的后续分页（图片按页编号）。
- 指令 `/scp rand` / `/scp cn rand`：从对应分部的本地目录随机抽一篇。
- 本地维护 SCP 条目目录（编号 + 标题，约 1.1 万条），支持关键字搜索与随机，
  避免依赖 wikidot 的 JS 搜索。
- **不注册任何 LLM 工具**：仅提供文本指令（按需求「暂时不要注册工具给 llm 调用」）。

长文本处理策略（详见 README「长文本处理」）：
1. 图片模式：正文按段落装箱切为若干「图片页」（每张约 `per_image_chars` 字），
   单次最多发 `max_image_pages` 张，超出截断并给出分页命令；
2. 文本模式：单节点不超过 `per_node_chars`（默认 1500 字），单卡节点数不超过
   `max_nodes`（默认 20），超出部分截断并给出分页提示；
3. 用户可用 `/scp <编号> <页号>` 继续读取后续分页。

配置结构：
- [plugin] 基础开关与 config_version；
- [fetch] 双站地址 / 超时 / 抓取开关；
- [render] 发送方式（send_mode）与图片/文本两种分片参数；
- [catalog] 本地目录刷新与缓存。

超时与失败约定：
- 抓取超时/网络失败：通过 QQ 消息返回可读错误，同时打印控制台日志；
- 图片渲染或 send.image 失败（宿主不支持 render / 适配器不支持图片）：
  自动回退文本合并转发；`send.forward` 再失败则回退 `send.text`。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Sequence, Tuple

import httpx

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

try:
    # 插件作为包被导入时走相对导入（如 plugins/cateye_scp_article/ 有 __init__.py）
    from .scp_core import (
        ALLOWED_SITE_HOSTS,
        BRANCH_LABELS,
        CATALOG_INDEX_PAGES,
        CN_SITE,
        INT_SITE,
        SITE_HEADERS,
        build_article_html,
        collect_admins,
        plain_id,
        build_forward_nodes,
        chunk_by_paragraphs,
        clean_article_text,
        crop_banner_tile,
        entry_by_code,
        extract_page_content,
        force_branch_slug,
        is_error_page,
        is_safe_slug,
        merge_catalog,
        normalize_code,
        parse_catalog_items,
        search_catalog,
        slug_branch,
        to_data_uri,
        is_safe_data_uri,
        validate_site_url,
    )
except ImportError:  # pragma: no cover - Runner 以独立模块加载 plugin.py 时走绝对导入
    # 兜底：把插件目录加入 sys.path，确保能导入同目录的 scp_core
    import sys as _sys

    _PLUGIN_DIR = Path(__file__).resolve().parent
    if str(_PLUGIN_DIR) not in _sys.path:
        _sys.path.insert(0, str(_PLUGIN_DIR))
    from scp_core import (  # type: ignore[no-redef]
        ALLOWED_SITE_HOSTS,
        BRANCH_LABELS,
        CATALOG_INDEX_PAGES,
        CN_SITE,
        INT_SITE,
        SITE_HEADERS,
        build_article_html,
        collect_admins,
        plain_id,
        build_forward_nodes,
        chunk_by_paragraphs,
        clean_article_text,
        crop_banner_tile,
        entry_by_code,
        extract_page_content,
        force_branch_slug,
        is_error_page,
        is_safe_data_uri,
        is_safe_slug,
        merge_catalog,
        normalize_code,
        parse_catalog_items,
        search_catalog,
        slug_branch,
        to_data_uri,
        validate_site_url,
    )

try:
    # 出站 URL 统一安全护栏（随插件分发，参考 cateye_common/url_guard.py）：
    # scheme 白名单 + 内网/元数据 IP 黑名单 + 重定向逐跳复验 + 错误脱敏
    from .url_guard import SafeUrl, UrlGuardError, check_redirect, check_url
except ImportError:  # pragma: no cover
    from url_guard import SafeUrl, UrlGuardError, check_redirect, check_url  # type: ignore[no-redef]

# ==================== 常量 ====================

# 配置版本（config_version）：与 _manifest.json 的 version 保持同步。
# 0.2.2：修复发送返回值未检查导致投递失败被静默吞掉的问题；
# 图片渲染显式传 device_scale_factor（render.render_scale）并加发送体积护栏。
# 0.2.3：渲染字号放大（render.font_scale 默认 1.5）；图片统一合并转发（单图也入卡）。
# 0.2.4：修复合并转发假阴性超时下的回退抢跑/内容重复；渲染缩放默认 1.0（字段更名 render_scale）。
# 0.3.0：站点配置白名单校验（SSRF 加固）；/scp刷新目录 per-stream 冷却；
# 抓取响应体大小上限（流式读取）；目录 slug 过滤畸形路径。
# 0.3.1：slug 严格形态校验（ASCII 白名单 + 长度上限，拦下百分号编码 / 尾随斜杠 /
# 非 ASCII 数字等漏网形态），并在请求前与随机抽取处做纵深防御；
# 新增内置兜底页头资源（assets/header_<BRANCH>.json，随插件发布）。
# 0.3.2：新增 admins 配置节——管理员 = 宿主 [plugin].permission ∪ 插件配置
# admins.admin_users（按剥平台前缀后的纯 ID 自动去重）；管理员执行 /scp刷新目录
# 可免 per-stream 冷却（admins.admin_bypass_refresh_cooldown，默认开）。
# 0.3.3：manifest capabilities 补声明 config.get（_get_admins 实际调用）；
# 重定向改手动逐跳复验白名单；页头 data URI 形态白名单；PNG 分块解压上限；
# 配置 i18n（en）补齐；刷新失败冷却缩短；timeout / max_image_pages 上界；
# 目录刷新与搜索分离锁（刷新期间旧目录继续应答）。
# 0.3.4：紧急修复 0.3.3 引入的抓取回归——重定向加固新调用的 ALLOWED_SITE_HOSTS
# 只定义在 scp_core，未随导入清单一起带进来，抓取与目录刷新跑到该行即抛
# NameError（目录读缓存故「搜索/随机正常、抓取必失败」）；两个导入块均补齐。
SUPPORTED_CONFIG_VERSION = "0.3.4"

# 默认发送方式：image（渲染为图片）；可配置为 text（合并转发文本）
DEFAULT_SEND_MODE = "image"

# 默认每张图片承载的正文汉字上限（按段落装箱；5000 字 ≈ 900px 宽下约 2500px 高）
DEFAULT_PER_IMAGE_CHARS = 5000

# 默认单次查询最多发送的图片张数（3 × 5000 = 15000 字，覆盖约 95% 条目全文）
DEFAULT_MAX_IMAGE_PAGES = 3

# 默认渲染图宽度（像素；高度按内容自适应）
DEFAULT_IMAGE_WIDTH = 900

# 默认渲染缩放倍率（device_scale_factor；越大越清晰但图片体积越大）
# 0.2.4 起默认 1.0：Platform IO 对大图合并转发可能超时假阴性（返回 FAILED 但实际迟到送达），
# 小图能让转发快速同步成功，避免回退链抢跑造成重复消息。
DEFAULT_RENDER_SCALE = 1.0

# 默认字号缩放倍率（1.0 为基准：正文 16px；1.5 = 正文 24px）
DEFAULT_FONT_SCALE = 1.5

# 体积回退缩放（负载超限时降到这里重渲染）
FALLBACK_SCALE = 1.0

# 合并转发负载安全阈值（base64 总字节；宿主 IPC 帧上限 16 MiB，留足余量）
MAX_FORWARD_PAYLOAD = 10 * 1024 * 1024

# 单张图片负载安全阈值（逐张发送时每帧独立）
MAX_SINGLE_IMAGE_PAYLOAD = 8 * 1024 * 1024

# 合并转发投递失败后、逐张回退前的等待秒数：Platform IO 对大消息可能「超时报失败
# 但实际迟到送达」，等待可避免回退消息与迟到的合并转发撞车造成重复。
FORWARD_FALLBACK_DELAY = 8.0

# 逐张回退时两张图片之间的间隔秒数（降低连发被限流的概率）
PER_IMAGE_INTERVAL = 1.0

# 默认每节点字符上限（文本模式；QQ 单条文本消息按字节计，中文 UTF-8 3 字节/字，
# 1500 字 ≈ 4.5KB，处于安全区间；实测 SCP 正文最长段落仅 ~300 字）
DEFAULT_PER_NODE_CHARS = 1500

# 默认单卡最大节点数（文本模式；20 × 1500 = 30000 字，可覆盖 95% 条目的全文）
DEFAULT_MAX_NODES = 20

# 默认转发气泡昵称（文本模式）
DEFAULT_FORWARD_NICKNAME = "SCP 基金会档案"

# 默认搜索返回条数
DEFAULT_SEARCH_LIMIT = 10

# 默认目录缓存有效期（小时）
DEFAULT_CATALOG_CACHE_HOURS = 168

# 官方页头资源缓存有效期（小时；资源基本不变，7 天足够）
DEFAULT_HEADER_CACHE_HOURS = 168

# 内置兜底页头资源（随插件发布的 assets/header_<BRANCH>.json）：
# 内容为「开发期抓到的官方页头」——logo 与已裁剪暗色页头带的 body_bg 的 data URI，
# 由 tools/build_header_assets.py 生成。运行时下载失败、且本机也没有任何（哪怕过期的）
# 磁盘缓存时用它出图，避免退化成纯色横幅 + 文字徽标。
BUNDLED_ASSET_DIR = "assets"
BUNDLED_ASSET_SCHEMA = 1

# HTTP 超时（秒）
HTTP_TIMEOUT = 20.0

# HTTP 超时上界（秒）：fetch.timeout 是配置侧自伤项，设成 3600 会拖死单请求，
# 运行时一律钳制到该值以内（0.3.3 加固）
MAX_HTTP_TIMEOUT = 60.0

# 单次查询最多发送的图片张数上界：max_image_pages 设得过大（如 1000）会触发
# 上千次渲染拖垮进程，运行时一律钳制到该值以内（0.3.3 加固）
MAX_IMAGE_PAGES_LIMIT = 10

# 重定向逐跳跟随上限（0.3.3 加固）：follow_redirects 改为手动跟随，
# 每跳经 url_guard 复验 scheme + host 白名单，超过该跳数视为异常
MAX_REDIRECTS = 5

# 官方页头资源 CDN 白名单（与 scp_core.SITE_HEADERS 中的 host 一致；
# 代码常量不受配置影响）。资源下载的重定向同样不得离开官方 CDN 域。
ALLOWED_ASSET_HOSTS = {"cdn.scpwiki.com", "sigma9.scpwikicn.com"}

# 抓取响应体大小上限（字节）：正文 HTML 5MB / 官方页头资源 4MB。
# 流式读取并在超限时中止，防御官方 CDN 改版或被污染时超大响应吃满进程内存。
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_ASSET_BYTES = 4 * 1024 * 1024

# /scp刷新目录 成功冷却的默认值（秒，per-stream）：一次刷新要抓 15 个索引页、
# 约 20 秒，无冷却会被群成员连发刷爆且 SCP 维基对爬取不友好（宿主 IP 被封所有
# 用户一起吃亏）。0.3.3 起可经 catalog.refresh_cooldown_minutes 配置（默认 10
# 分钟即该值），失败冷却单独缩短（catalog.refresh_fail_cooldown_seconds，默认 60 秒）。
CATALOG_REFRESH_COOLDOWN = 600

# 浏览器 UA（wikidot 对空 UA/爬虫 UA 可能拒绝）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 指令前缀修饰符（顺序任意，可组合）：
# text/文字/文本 = 文字版（合并转发文本）；cn/中文/中站 = 中站；en/英文/原文 = 英文原文
CMD_MODIFIERS: Dict[str, str] = {
    "text": "text", "文字": "text", "文本": "text",
    "cn": "cn", "中文": "cn", "中站": "cn", "cn站": "cn",
    "en": "en", "英文": "en", "原文": "en",
}

# 指令帮助文本
HELP_TEXT = (
    "SCP 条目查询：\n"
    "/scp <编号>          —— 查国际站条目（优先显示中站译文，无译文回退英文原文）\n"
    "/scp cn <编号>       —— 查中站条目，如 /scp cn 2000\n"
    "/scp en <编号>       —— 强制显示英文原文（跳过译文），如 /scp en 173\n"
    "/scp text <编号>     —— 文字版（合并转发文本），如 /scp text 173、/scp text cn 2000\n"
    "/scp <编号> <页号>   —— 读取超长条目的后续分页，如 /scp 5000 2\n"
    "/scp [cn] <关键字>   —— 搜索条目（默认国际站，加 cn 搜中站）\n"
    "/scp [cn] rand       —— 随机抽一篇（默认国际站，加 cn 抽中站）\n"
    "/scp help            —— 显示本帮助\n"
    "\n"
    "文章默认渲染为图片发送（带对应分部官方页头，多页图片以合并转发一并发出）；"
    "text 修饰符或配置 send_mode=text 可改用文字版。"
)


# ==================== 配置模型 ====================


def _ui_i18n(en_label: str, en_hint: str = "") -> dict:
    """字段级英文翻译（并入 json_schema_extra；WebUI 按 i18n[locale]['label'/'hint'] 取用）。

    规范要求（03-配置系统 §5.1）：每个字段至少提供 en 的 label/hint；
    zh-CN 文案即中文字段名/描述本身，无需重复。
    """
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}


class PluginSectionConfig(PluginConfigBase):
    """插件（plugin 配置节）：全局开关与配置版本。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Plugin", "description": "Global switches and config version"},
    }

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={
            "label": "启用插件", "hint": "插件总开关",
            **_ui_i18n("Enable plugin", "Master switch for the plugin"),
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步，用于检查配置文件是否需要更新）",
        json_schema_extra={
            "disabled": True, "hidden": True, "label": "配置版本", "hint": "勿改",
            **_ui_i18n("Config version", "Do not edit"),
        },
    )


class FetchSectionConfig(PluginConfigBase):
    """抓取配置（fetch 配置节）：双站地址与网络行为。"""

    __ui_label__ = "抓取配置"
    __ui_icon__ = "cloud_download"
    __ui_order__ = 1
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Fetch", "description": "Site addresses and network behavior"},
    }

    site_url_int: str = Field(
        default=INT_SITE,
        description="国际站（SCP-EN）地址：/scp 默认查询该站。留空则使用默认站点",
        json_schema_extra={
            "label": "国际站地址", "hint": "/scp 查询的站点",
            **_ui_i18n("International site URL",
                       "Site queried by /scp; whitelist-validated"),
        },
    )
    site_url_cn: str = Field(
        default=CN_SITE,
        description="中站（SCP-CN）地址：/scp cn 查询该站，目录标题也取自该站。留空则使用默认站点",
        json_schema_extra={
            "label": "中站地址", "hint": "/scp cn 查询的站点",
            **_ui_i18n("Chinese branch site URL",
                       "Site queried by /scp cn; whitelist-validated"),
        },
    )
    int_content_zh: bool = Field(
        default=True,
        description=(
            "国际站条目是否优先显示中站译文（/scp 173 显示中文内容，页头仍为国际站官方头；"
            "无译文的条目自动回退英文原文）。指令 /scp en <编号> 可单次强制英文原文"
        ),
        json_schema_extra={
            "label": "国际站显示中文译文", "hint": "无译文自动回退原文",
            **_ui_i18n("Prefer Chinese translation on EN site",
                       "Fall back to the original when no translation exists"),
        },
    )
    timeout: float = Field(
        default=HTTP_TIMEOUT,
        description="单次 HTTP 请求超时时间（秒，最大 60）",
        json_schema_extra={
            "label": "请求超时（秒）", "hint": "抓取超时（秒），上限 60",
            **_ui_i18n("Request timeout (seconds)",
                       "HTTP fetch timeout in seconds, capped at 60"),
        },
    )
    fetch_enabled: bool = Field(
        default=True,
        description=(
            "是否允许联网抓取条目正文。关闭后仅保留本地目录的搜索与随机功能"
            "（正文查询不可用），适用于完全离线的部署环境"
        ),
        json_schema_extra={
            "label": "允许联网抓取", "hint": "关闭后只能搜索目录",
            **_ui_i18n("Allow network fetching",
                       "When off, only local catalog search/random works"),
        },
    )


class RenderSectionConfig(PluginConfigBase):
    """展示配置（render 配置节）：发送方式与图片/文本分片参数。"""

    __ui_label__ = "展示配置"
    __ui_icon__ = "article"
    __ui_order__ = 2
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Display", "description": "Send mode and image/text splitting"},
    }

    send_mode: str = Field(
        default=DEFAULT_SEND_MODE,
        description=(
            "发送方式：image = 把文章渲染为图片发送（带对应分部官方页头，"
            "渲染失败自动回退文本）；text = 直接用合并转发文本发送"
        ),
        json_schema_extra={
            "label": "发送方式", "hint": "image 或 text",
            **_ui_i18n("Send mode",
                       "image = render to pictures; text = forwarded text"),
        },
    )
    per_image_chars: int = Field(
        default=DEFAULT_PER_IMAGE_CHARS,
        description=(
            "图片模式：每张图片承载的正文汉字上限（按段落装箱切分，尽量不切断语义）。"
            "调大可减少张数，但图片会更高"
        ),
        json_schema_extra={
            "label": "每张图片字数上限", "hint": "图片页字数上限",
            **_ui_i18n("Chars per image",
                       "Max body chars carried by one image page"),
        },
    )
    max_image_pages: int = Field(
        default=DEFAULT_MAX_IMAGE_PAGES,
        description=(
            "图片模式：单次查询最多发送的图片张数（默认 3，上限 10）。"
            "超出部分截断并在图上给出分页命令，可用 /scp <编号> <页号> 继续读取"
        ),
        json_schema_extra={
            "label": "单次最多图片张数", "hint": "图片页数上限（≤10）",
            **_ui_i18n("Max images per query",
                       "Extra pages are truncated; capped at 10"),
        },
    )
    image_width: int = Field(
        default=DEFAULT_IMAGE_WIDTH,
        description="图片模式：渲染图宽度（像素），高度按正文长度自适应",
        json_schema_extra={
            "label": "渲染图宽度", "hint": "图片宽度（像素）",
            **_ui_i18n("Image width (px)",
                       "Rendered image width in pixels"),
        },
    )
    render_scale: float = Field(
        default=DEFAULT_RENDER_SCALE,
        description=(
            "图片模式：渲染缩放倍率（device_scale_factor，1.0~3.0）。越大越清晰，"
            "但图片体积也越大、转发越慢；默认 1.0 以保证合并转发同步送达"
        ),
        json_schema_extra={
            "label": "渲染缩放倍率", "hint": "默认 1.0，调大图片更清晰但更慢",
            **_ui_i18n("Render scale",
                       "Higher is sharper but slower; 1.0 recommended"),
        },
    )
    font_scale: float = Field(
        default=DEFAULT_FONT_SCALE,
        description=(
            "图片模式：字号缩放倍率（1.0 为基准：正文 16px），默认 1.5 即正文 24px。"
            "调大字号会让图片更长、负载更大，超限时体积护栏会自动降缩放"
        ),
        json_schema_extra={
            "label": "字号缩放倍率", "hint": "默认 1.5（正文 24px）",
            **_ui_i18n("Font scale",
                       "1.0 base (16px body); 1.5 = 24px body"),
        },
    )
    per_node_chars: int = Field(
        default=DEFAULT_PER_NODE_CHARS,
        description=(
            "文本模式：合并转发中每个节点的字符上限（默认 1500）。"
            "按段落装箱切分，尽量不切断语义；调大需注意平台单消息上限（中文按 3 字节/字）"
        ),
        json_schema_extra={
            "label": "每节点字符上限（文本模式）", "hint": "单节点字符上限",
            **_ui_i18n("Chars per node (text mode)",
                       "Max chars per forward node"),
        },
    )
    max_nodes: int = Field(
        default=DEFAULT_MAX_NODES,
        description=(
            "文本模式：单张合并转发卡允许的最大节点数（默认 20）。"
            "超出部分截断并在卡末提示；用户可用 /scp <编号> <页号> 继续读取"
        ),
        json_schema_extra={
            "label": "单卡最大节点数（文本模式）", "hint": "单卡节点上限",
            **_ui_i18n("Max nodes per card (text mode)",
                       "Max nodes per forward card"),
        },
    )
    forward_nickname: str = Field(
        default=DEFAULT_FORWARD_NICKNAME,
        description="文本模式：合并转发气泡中显示的昵称",
        json_schema_extra={
            "label": "转发气泡昵称", "hint": "转发显示昵称",
            **_ui_i18n("Forward nickname",
                       "Nickname shown in forward bubbles"),
        },
    )
    show_source: bool = Field(
        default=True,
        description="是否显示原页面链接（图片页脚 / 报文首部）",
        json_schema_extra={
            "label": "显示原页面链接", "hint": "附上原链接",
            **_ui_i18n("Show source link",
                       "Attach the original page link"),
        },
    )


class AdminSectionConfig(PluginConfigBase):
    """管理员配置（admins 配置节）：与宿主管理员并集后自动去重。"""

    __ui_label__ = "管理员"
    __ui_icon__ = "shield_check"
    __ui_order__ = 4
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Admins",
               "description": "Merged with host permissions and deduplicated"},
    }

    admin_users: List[str] = Field(
        default_factory=list,
        description=(
            "插件侧管理员名单，与宿主 [plugin].permission 的管理员合并为并集"
            "（按剥平台前缀后的纯 ID 比对，指向同一人自动去重）。"
            "支持 '123456'（纯 ID）与 'qq:123456'（平台前缀）两种写法"
        ),
        json_schema_extra={
            "label": "管理员名单", "hint": "与宿主管理员自动去重合并",
            **_ui_i18n("Admin user list",
                       "Merged and deduplicated with host admins"),
        },
    )
    admin_bypass_refresh_cooldown: bool = Field(
        default=True,
        description="管理员执行 /scp刷新目录 时不受冷却限制（普通用户仍为每 10 分钟一次）",
        json_schema_extra={
            "label": "管理员免刷新冷却", "hint": "管理员不受 10 分钟冷却",
            **_ui_i18n("Admins bypass refresh cooldown",
                       "Admins skip the catalog refresh cooldown"),
        },
    )


class CatalogSectionConfig(PluginConfigBase):
    """目录配置（catalog 配置节）：本地条目目录的缓存与刷新。"""

    __ui_label__ = "目录配置"
    __ui_icon__ = "list"
    __ui_order__ = 3
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Catalog",
               "description": "Local entry catalog cache and refresh"},
    }

    catalog_cache_hours: int = Field(
        default=DEFAULT_CATALOG_CACHE_HOURS,
        description=(
            "本地条目目录缓存有效期（小时，默认 168 = 7 天）。"
            "超期后下次搜索会重新抓取系列索引页（约 15 页、20 余秒）"
        ),
        json_schema_extra={
            "label": "目录缓存有效期（小时）", "hint": "目录缓存时长",
            **_ui_i18n("Catalog cache lifetime (hours)",
                       "Index pages are re-fetched when expired"),
        },
    )
    search_limit: int = Field(
        default=DEFAULT_SEARCH_LIMIT,
        description="关键字搜索返回的候选条数上限",
        json_schema_extra={
            "label": "搜索返回条数", "hint": "搜索结果上限",
            **_ui_i18n("Search result limit",
                       "Max entries returned per search"),
        },
    )
    auto_refresh: bool = Field(
        default=True,
        description=(
            "目录缺失或超期时是否自动抓取。关闭后需等待目录首次构建"
            "（可通过 /scp help 提示手动处理）"
        ),
        json_schema_extra={
            "label": "自动刷新目录", "hint": "自动抓取目录",
            **_ui_i18n("Auto refresh catalog",
                       "Fetch index pages when catalog is missing or expired"),
        },
    )
    refresh_cooldown_minutes: int = Field(
        default=10,
        description="/scp刷新目录 成功后的 per-stream 冷却（分钟，默认 10）",
        json_schema_extra={
            "label": "刷新成功冷却（分钟）", "hint": "成功后同一聊天流冷却时长",
            **_ui_i18n("Refresh cooldown (minutes)",
                       "Per-chat cooldown after a successful refresh"),
        },
    )
    refresh_fail_cooldown_seconds: int = Field(
        default=60,
        description="/scp刷新目录 失败后的 per-stream 冷却（秒，默认 60；"
                    "失败多为网络抖动，缩短冷却便于尽快重试）",
        json_schema_extra={
            "label": "刷新失败冷却（秒）", "hint": "失败后短暂冷却即可重试",
            **_ui_i18n("Failed refresh cooldown (seconds)",
                       "Short cooldown after a failed refresh"),
        },
    )


class ScpArticleConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    fetch: FetchSectionConfig = Field(default_factory=FetchSectionConfig)
    render: RenderSectionConfig = Field(default_factory=RenderSectionConfig)
    admins: AdminSectionConfig = Field(default_factory=AdminSectionConfig)
    catalog: CatalogSectionConfig = Field(default_factory=CatalogSectionConfig)


# ==================== 插件主体 ====================


class ScpArticlePlugin(MaiBotPlugin):
    """SCP 条目查询插件。"""

    config_model: ClassVar[type[PluginConfigBase]] = ScpArticleConfig

    def __init__(self) -> None:
        # ⚠️ 必须调用基类构造：MaiBotPlugin.__init__ 会初始化 _dynamic_api_components、
        # 组件注册表等内部状态。漏掉它 → Runner 在 _register_plugin() 里调用
        # instance.get_components() 时抛 AttributeError（'...' object has no attribute
        # '_dynamic_api_components'），该异常无人捕获，会**直接杀掉整个 Runner 进程**，
        # 导致该分组下所有插件一起挂掉。切勿删除这一行。
        super().__init__()

        # 目录内存缓存：{"items": [...], "timestamp": int}
        self._catalog: List[Dict[str, str]] = []
        self._catalog_ts: int = 0
        # 目录刷新锁（0.3.3 改名/改职责）：仅用于刷新单飞（同一时间只有一个
        # 请求在联网重建目录），构建期间**不再**串行化搜索——搜索/随机直接读
        # self._catalog 旧快照（列表整体赋值原子替换），不再等 20 秒的锁
        self._catalog_lock: Optional[asyncio.Lock] = None
        # 官方页头资源内存缓存：{"INT": (logo_data_uri, bg_data_uri), ...}
        self._header_cache: Dict[str, Tuple[str, str]] = {}
        # 内置兜底页头资源缓存（随插件发布的 assets/header_<BRANCH>.json）：
        # 命中一次就缓存（含失败时的空串，避免每次查询都读盘）
        self._bundled_cache: Dict[str, Tuple[str, str]] = {}
        # /scp刷新目录 per-stream 冷却：{stream_id: 上次刷新时间戳}
        self._refresh_ts: Dict[str, float] = {}

    # ==================== 生命周期 ====================

    async def on_load(self) -> None:
        self._catalog_lock = asyncio.Lock()
        try:
            os.makedirs(str(self.ctx.paths.data_dir), exist_ok=True)
        except Exception as e:
            self.ctx.logger.warning("创建数据目录失败：%s", e)
        # 尝试从本地缓存载入目录（不联网）
        loaded = self._load_catalog_cache()
        self.ctx.logger.info(
            "SCP 条目查询插件已加载（/scp 国际站、/scp cn 中站），本地目录 %d 条%s",
            len(self._catalog),
            "（来自缓存）" if loaded else "（尚未构建，首次搜索时抓取）",
        )

    async def on_unload(self) -> None:
        self.ctx.logger.info("SCP 条目查询插件已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        del config_data, version
        if scope == "self":
            self.ctx.logger.info("SCP 条目查询插件配置已更新")

    # ==================== 路径与缓存 ====================

    def _catalog_file(self) -> str:
        """本地目录缓存文件路径（Host 注入的插件专属目录）。"""
        return os.path.join(str(self.ctx.paths.data_dir), "catalog.json")

    def _load_catalog_cache(self) -> bool:
        """从磁盘载入目录缓存到内存。成功返回 True。

        读取插件数据目录下的 catalog.json（未命中返回 False，等待联网构建）。
        """
        # 1) 插件数据目录缓存
        try:
            with open(self._catalog_file(), "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items") if isinstance(data, dict) else None
            if isinstance(items, list) and items:
                self._catalog = [i for i in items if isinstance(i, dict) and i.get("slug")]
                self._catalog_ts = int(data.get("timestamp") or 0)
                return bool(self._catalog)
        except Exception:
            pass
        return False

    def _save_catalog_cache(self) -> None:
        """把目录写入磁盘缓存。"""
        try:
            os.makedirs(str(self.ctx.paths.data_dir), exist_ok=True)
            with open(self._catalog_file(), "w", encoding="utf-8") as f:
                json.dump(
                    {"timestamp": self._catalog_ts, "items": self._catalog},
                    f,
                    ensure_ascii=False,
                )
        except Exception as e:
            self.ctx.logger.warning("写入目录缓存失败：%s", e)

    def _catalog_fresh(self) -> bool:
        """判断内存中的目录是否仍在有效期内。"""
        if not self._catalog or not self._catalog_ts:
            return False
        hours = int(self.config.catalog.catalog_cache_hours or DEFAULT_CATALOG_CACHE_HOURS)
        return (time.time() - float(self._catalog_ts)) < hours * 3600

    # ==================== 管理员 ====================

    @staticmethod
    def _extract_requester(kwargs: Dict[str, Any]) -> str:
        """从组件 kwargs 里取触发者的用户 ID（剥平台前缀后的纯 ID）。"""
        message = kwargs.get("message") if isinstance(kwargs.get("message"), dict) else {}
        user_info = message.get("user_info") if isinstance(message.get("user_info"), dict) else {}
        user_id = kwargs.get("user_id") or user_info.get("user_id") or ""
        return plain_id(user_id)

    async def _get_admins(self) -> List[str]:
        """管理员并集：宿主 [plugin].permission ∪ 插件配置 admins.admin_users。

        两侧统一剥平台前缀（'qq:10001' 与 '10001' 视为同一人）后去重；
        读取宿主配置失败时降级为仅按插件配置判定（记 debug 日志，不抛异常）。
        """
        host_perms: Any = None
        try:
            host_perms = await self.ctx.config.get("plugin.permission", None)
        except Exception as e:
            self.ctx.logger.debug("读取宿主管理员配置（plugin.permission）失败，仅按插件配置判定：%s", e)
        return collect_admins(host_perms, list(self.config.admins.admin_users or []))

    async def _is_admin(self, kwargs: Dict[str, Any]) -> bool:
        """判定触发者是否管理员：本地控制台天然放行，其余比对宿主+插件配置并集。"""
        if kwargs.get("is_local_operator"):
            return True
        requester = self._extract_requester(kwargs)
        if not requester:
            return False
        return requester in await self._get_admins()

    # ==================== HTTP ====================

    def _site_for_branch(self, branch: str) -> str:
        """返回指定分部（"INT" 国际站 / "CN" 中站）规范化后的站点根地址。

        SSRF 加固：配置地址必须通过站点白名单校验（https + 官方 wikidot 域名），
        校验失败时记告警并回退默认站，绝不使用可疑地址发请求。
        """
        fallback = CN_SITE if branch == "CN" else INT_SITE
        site = (
            str(self.config.fetch.site_url_cn or "").strip()
            if branch == "CN"
            else str(self.config.fetch.site_url_int or "").strip()
        )
        if not site:
            site = fallback
        else:
            ok, reason = validate_site_url(site)
            if not ok:
                self.ctx.logger.warning(
                    "配置站点地址未通过白名单校验（%s：%s），回退默认站 %s",
                    reason, site, fallback,
                )
                site = fallback
        return site.rstrip("/")

    def _content_site_order(self, branch: str, force_lang: str = "") -> List[str]:
        """返回正文抓取站点的尝试顺序。

        分部主站即「原文」所在站：INT 分部原文在国际站，CN 分部原文在中站。
        默认优先中文内容：国际站条目先取中站同 slug 译文（页头仍为国际站官方头），
        无译文自动回退国际站原文；`/scp en` 强制英文原文（INT 分部只试国际站，
        CN 分部先试国际站的英文版再回退中站原文）。配置 int_content_zh=false
        可全局关闭译文优先。
        """
        home = self._site_for_branch(branch)
        other = self._site_for_branch("CN" if branch == "INT" else "INT")
        if force_lang == "en":
            if branch == "INT":
                return [home]
            return [other, home]
        want_zh = branch == "CN" or bool(self.config.fetch.int_content_zh)
        if want_zh and branch == "INT":
            return [other, home]
        return [home]

    def _client(self) -> httpx.AsyncClient:
        """构造带 UA 与超时的 HTTP 客户端。

        0.3.3 加固：follow_redirects 改为 False——httpx 自动跟随重定向时不复验
        目标地址，白名单域 302 到内网/元数据地址会把响应抓回来；改由
        `_send_follow_redirects()` 手动逐跳跟随并复验。超时钳制到 MAX_HTTP_TIMEOUT
        （配置侧自伤项上界）。
        """
        timeout = float(self.config.fetch.timeout or HTTP_TIMEOUT)
        if timeout <= 0:
            timeout = HTTP_TIMEOUT
        timeout = min(timeout, MAX_HTTP_TIMEOUT)
        return httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
        )

    @staticmethod
    async def _send_follow_redirects(
        client: httpx.AsyncClient, url: str, allowed_hosts
    ) -> Tuple[httpx.Response, "SafeUrl"]:
        """发送 GET 请求并手动逐跳跟随重定向（每跳复验 scheme + host 白名单）。

        SSRF 加固（0.3.3 M2）：初始 URL 与每个重定向 Location 都经 url_guard
        校验——https scheme、host 在 allowed_hosts 白名单内、DNS 解析结果不落
        内网/元数据段；相对 Location 继承当前 host 后同样做 host 白名单比对。
        跳数超过 MAX_REDIRECTS 视为异常。

        Args:
            client: httpx 异步客户端（follow_redirects=False）。
            url: 初始请求地址。
            allowed_hosts: 允许的 host 集合（页面用官方 wikidot 域，
                页头资源用官方 CDN 域），语义与 fetch.site_url 白名单一致。

        Returns:
            (最终响应, 最终地址 SafeUrl)；调用方负责读取响应体与 aclose()。

        Raises:
            ValueError: 地址未通过白名单/黑名单校验、重定向缺 Location 或跳数超限
                （文案已脱敏，可直接回显聊天）。
        """
        allowed = {str(h).lower().rstrip(".") for h in allowed_hosts}
        try:
            safe = await check_url(url, allowed_hosts=tuple(allowed))
        except UrlGuardError as e:
            raise ValueError(f"目标地址未通过安全校验（{e}）") from e
        for _ in range(MAX_REDIRECTS + 1):
            req = client.build_request("GET", safe.reconstruct())
            resp = await client.send(req, stream=True)
            if not resp.is_redirect:
                return resp, safe
            location = resp.headers.get("location", "")
            await resp.aclose()
            if not location:
                raise ValueError("重定向缺少跳转地址，已中止")
            try:
                nxt = await check_redirect(safe, location)
            except UrlGuardError as e:
                raise ValueError(f"重定向目标未通过安全校验（{e}）") from e
            if nxt.host.lower() not in allowed:
                raise ValueError("重定向目标不在允许的站点白名单内，已中止")
            safe = nxt
        raise ValueError("重定向跳数超过上限，已中止")

    @staticmethod
    async def _read_capped(resp: httpx.Response, limit: int) -> bytes:
        """流式读取响应体并施加大小上限（防御超大/被污染响应吃满内存）。

        先查 Content-Length（可提前拒绝），再逐块读取，累计超限即中止并抛异常。
        """
        clen = resp.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > limit:
            raise ValueError(f"响应体 Content-Length {clen} 超过上限 {limit} 字节")
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > limit:
                raise ValueError(f"响应体超过 {limit} 字节上限，已中止读取")
        return bytes(buf)

    async def _fetch_page(self, slug: str, site: str) -> str:
        """抓取指定站点上 slug 页面的 HTML（流式 + 大小上限），失败抛异常。

        拼 URL 前再校验一次 slug 形态（纵深防御）：slug 可能来自本地目录缓存文件
        （旧版本写入，未经新过滤）或 /scp rand 的随机抽取，绝不允许含 `%`、
        斜杠、空白等的值进入请求路径。
        """
        slug = str(slug or "").strip().lower()
        if not is_safe_slug(slug):
            raise ValueError(f"slug 形态非法，拒绝请求：{slug[:64]!r}")
        url = f"{site.rstrip('/')}/{slug}"
        async with self._client() as client:
            # 重定向手动逐跳复验白名单（0.3.3 M2 加固）
            resp, _ = await self._send_follow_redirects(client, url, ALLOWED_SITE_HOSTS)
            try:
                resp.raise_for_status()
                raw = await self._read_capped(resp, MAX_RESPONSE_BYTES)
                charset = resp.charset_encoding or "utf-8"
            finally:
                await resp.aclose()
        return raw.decode(charset, errors="replace")

    async def _fetch_asset(self, client: httpx.AsyncClient, url: str) -> bytes:
        """抓取官方页头资源（流式 + 大小上限），失败抛异常。

        重定向手动逐跳复验官方 CDN 域白名单（0.3.3 M2 加固）。
        """
        resp, _ = await self._send_follow_redirects(client, url, ALLOWED_ASSET_HOSTS)
        try:
            resp.raise_for_status()
            return await self._read_capped(resp, MAX_ASSET_BYTES)
        finally:
            await resp.aclose()

    # ==================== 目录构建 ====================

    async def _ensure_catalog(self, *, force: bool = False) -> Tuple[bool, str]:
        """确保内存目录可用。返回 (是否可用, 错误信息)。

        顺序：内存新鲜 → 磁盘缓存 → 联网抓取系列索引页。
        0.3.3 起刷新与搜索分离：`_catalog_lock` 只保证刷新单飞（同一时间
        只有一个请求在联网重建，约 20 秒），构建完成后对 `self._catalog`
        做整体赋值（原子替换快照）；刷新进行中若有旧目录，搜索/随机会直接
        用旧目录应答、不再等锁。
        """
        if not force and self._catalog_fresh():
            return True, ""
        if self._catalog_lock is None:
            self._catalog_lock = asyncio.Lock()
        if not force and self._catalog_lock.locked() and self._catalog:
            # 其他请求正在刷新：旧目录（可能已过期）先顶上，不阻塞搜索
            return True, ""
        async with self._catalog_lock:
            # 双重检查：等锁期间可能已被其他请求刷新
            if not force and self._catalog_fresh():
                return True, ""
            # auto_refresh=False 时不主动联网；有旧目录就继续用（可能已过期）
            if not force and not self.config.catalog.auto_refresh:
                if self._catalog:
                    return True, ""
                # 先尝试读磁盘缓存（可能是上次运行留下的）
                if self._load_catalog_cache() and self._catalog:
                    return True, ""
                return False, "目录尚未构建，且已关闭自动刷新（可执行 /scp刷新目录 手动构建）"
            if not self.config.fetch.fetch_enabled:
                return (bool(self._catalog),
                        "" if self._catalog else "插件已关闭联网抓取，且本地无目录缓存")
            try:
                items = await self._build_catalog()
            except Exception as e:
                self.ctx.logger.error("构建条目目录失败：%s", e)
                if self._catalog:
                    return True, ""
                return False, f"构建条目目录失败：{e}"
            if not items:
                if self._catalog:
                    return True, ""
                return False, "未从系列索引页解析到任何条目（站点结构可能已变化）"
            # 快照原子替换：搜索/随机读到的要么是旧列表要么是新列表，无中间态
            self._catalog = items
            self._catalog_ts = int(time.time())
            self._save_catalog_cache()
            self.ctx.logger.info("条目目录已刷新，共 %d 条", len(self._catalog))
            return True, ""

    async def _build_catalog(self) -> List[Dict[str, str]]:
        """抓取全部系列索引页并合并解析为条目目录。

        目录统一取自**中站**：中站的系列索引页同时覆盖国际分部（标题已译为中文）
        与中文分部，一次构建即可服务两个分部的搜索与随机；
        国际站条目的 slug（scp-173 等）在中站与国际站一致，仅正文语言不同。
        """
        site = self._site_for_branch("CN")
        collected: List[Dict[str, str]] = []
        async with self._client() as client:
            for slug in CATALOG_INDEX_PAGES:
                try:
                    # 重定向手动逐跳复验白名单（0.3.3 M2 加固）
                    resp, _ = await self._send_follow_redirects(
                        client, f"{site}/{slug}", ALLOWED_SITE_HOSTS
                    )
                    try:
                        if resp.status_code != 200:
                            self.ctx.logger.warning("索引页 %s 返回 HTTP %s", slug, resp.status_code)
                            continue
                        html_text = (await self._read_capped(resp, MAX_RESPONSE_BYTES)).decode(
                            resp.charset_encoding or "utf-8", errors="replace"
                        )
                    finally:
                        await resp.aclose()
                    items = parse_catalog_items(html_text)
                    collected.extend(items)
                except Exception as e:
                    # 单个索引页失败不影响整体（网络抖动容忍）
                    self.ctx.logger.warning("索引页 %s 抓取失败：%s", slug, e)
        return merge_catalog(collected)

    # ==================== 官方页头资源 ====================

    def _header_cache_file(self, branch: str) -> str:
        """官方页头资源的磁盘缓存路径。"""
        return os.path.join(str(self.ctx.paths.data_dir), f"header_assets_{branch}.json")

    @staticmethod
    def _bundled_header_file(branch: str) -> str:
        """内置兜底页头资源的路径（随插件发布的 assets/header_<BRANCH>.json）。"""
        return str(Path(__file__).resolve().parent / BUNDLED_ASSET_DIR
                   / f"header_{branch}.json")

    @staticmethod
    def _has_assets(uris: Tuple[str, str]) -> bool:
        """判断页头资源 (logo, bg) 是否**真的**可用。

        注意：不能用 `if uris:` —— `("", "")` 是非空元组，真值为 True，
        会把「两个都没有」误判成有资源（这是 0.3.0 潜在的判断错误）。
        """
        return bool(uris) and bool(uris[0]) and bool(uris[1])

    def _load_bundled_header(self, branch: str) -> Tuple[str, str]:
        """载入内置兜底页头资源（开发期抓到的官方页头），返回 (logo, bg) data URI。

        仅在「下载失败且本机无任何磁盘缓存」时使用：把当时获取到的官方页头渲染
        固化为长期兜底，避免退化为纯色横幅 + 文字徽标。文件缺失或版本不符时返回
        空串（仍走原兜底），结果按分部缓存（失败也缓存，避免每次查询都读盘）。
        """
        if branch in self._bundled_cache:
            return self._bundled_cache[branch]
        uris: Tuple[str, str] = ("", "")
        path = self._bundled_header_file(branch)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (isinstance(data, dict) and data.get("logo") and data.get("bg")
                    and int(data.get("schema") or 0) >= BUNDLED_ASSET_SCHEMA):
                uris = (str(data["logo"]), str(data["bg"]))
                # data URI 形态白名单（0.3.3 L1 加固）：不合法按缺失处理走兜底链
                if not (is_safe_data_uri(uris[0]) and is_safe_data_uri(uris[1])):
                    self.ctx.logger.warning(
                        "内置兜底页头资源 data URI 未通过形态校验，按缺失处理：%s", path)
                    uris = ("", "")
                else:
                    self.ctx.logger.info(
                        "已载入%s内置兜底页头资源（快照 %s）",
                        BRANCH_LABELS.get(branch, branch), data.get("generated_at") or "未知",
                    )
            else:
                self.ctx.logger.warning("内置兜底页头资源格式不符：%s", path)
        except FileNotFoundError:
            self.ctx.logger.debug("未找到内置兜底页头资源：%s", path)
        except Exception as e:
            self.ctx.logger.warning("读取内置兜底页头资源失败（%s）：%s", path, e)
        self._bundled_cache[branch] = uris
        return uris

    async def _get_header_assets(self, branch: str) -> Tuple[str, str]:
        """获取指定分部官方页头资源（logo + 横幅底纹）的 data URI。

        对应站点官方 Sigma-9 主题 CSS 中 #header / #container-wrap 引用的图片，
        经 httpx 下载后内嵌为 data URI（html2png 默认禁网，外链资源不会加载）。
        底纹为 100×400 整页背景图，下载后立即用 crop_banner_tile 裁出暗色页头带，
        避免浅色正文区被压进横幅（横幅下半发白）。

        取资源顺序：内存缓存 → 磁盘缓存（7 天内直接用）→ 联网下载 → 过期磁盘缓存
        → **内置兜底资源**（assets/header_<BRANCH>.json，随插件发布）→ 空串
        （纯色横幅 + 文字徽标）。即：旧缓存优先于内置快照，内置快照只补"什么都没有"
        的空缺——离线部署、首次运行就撞上 CDN 故障、或官方改版导致下载失败时，
        仍能出带官方页头的图。

        资源的"有无"一律用 `_has_assets()` 判断：`("", "")` 真值为 True，
        直接写 `if uris:` 会把空资源误判成有资源。
        """
        cached = self._header_cache.get(branch)
        if cached and self._has_assets(cached):
            return cached

        meta = SITE_HEADERS.get(branch) or SITE_HEADERS["INT"]
        path = self._header_cache_file(branch)

        # 1) 磁盘缓存（新鲜直接用；过期留作下载失败的兜底）
        stale: Tuple[str, str] = ("", "")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (isinstance(data, dict) and data.get("logo") and data.get("bg")
                    and int(data.get("v") or 0) >= 2):
                stale = (str(data["logo"]), str(data["bg"]))
                # data URI 形态白名单（0.3.3 L1 加固）：缓存文件被写入任意字符串时
                # 不合法即丢弃，走重建（联网下载 → 内置兜底）
                if not (is_safe_data_uri(stale[0]) and is_safe_data_uri(stale[1])):
                    self.ctx.logger.warning(
                        "页头资源缓存 data URI 未通过形态校验，已丢弃待重建：%s", path)
                    stale = ("", "")
                elif (time.time() - float(data.get("ts") or 0)) < DEFAULT_HEADER_CACHE_HOURS * 3600:
                    self._header_cache[branch] = stale
                    return stale
        except Exception:
            pass

        # 2) 下载官方资源；失败时按「过期磁盘缓存 → 内置兜底」降级
        uris: Tuple[str, str] = ("", "")
        if self.config.fetch.fetch_enabled:
            try:
                async with self._client() as client:
                    logo_bytes = await self._fetch_asset(client, meta["logo"])
                    bg_bytes = await self._fetch_asset(client, meta["bg"])
                bg_cropped = crop_banner_tile(bg_bytes, meta["bg_mime"])
                if len(bg_cropped) != len(bg_bytes):
                    self.ctx.logger.debug("已裁剪%s横幅底纹暗色带（%d -> %d 字节）",
                                          BRANCH_LABELS.get(branch, branch),
                                          len(bg_bytes), len(bg_cropped))
                uris = (
                    to_data_uri(logo_bytes, meta["logo_mime"]),
                    to_data_uri(bg_cropped, meta["bg_mime"]),
                )
                try:
                    os.makedirs(str(self.ctx.paths.data_dir), exist_ok=True)
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump({"ts": time.time(), "v": 2,
                                   "logo": uris[0], "bg": uris[1]}, f)
                except Exception as e:
                    self.ctx.logger.warning("写入页头资源缓存失败：%s", e)
            except Exception as e:
                # 降级：过期磁盘缓存 → 内置兜底资源 → 空串（纯色横幅）
                uris = stale if self._has_assets(stale) else self._load_bundled_header(branch)
                self.ctx.logger.warning(
                    "获取%s官方页头资源失败，%s",
                    BRANCH_LABELS.get(branch, branch),
                    "使用过期缓存兜底" if self._has_assets(stale) else
                    ("使用内置兜底资源渲染" if self._has_assets(uris) else "将以纯色横幅渲染"),
                )
                self.ctx.logger.debug("页头资源下载失败详情：%s", e)
        else:
            # 关闭联网抓取：过期缓存 → 内置兜底
            uris = stale if self._has_assets(stale) else self._load_bundled_header(branch)

        # 失败结果（空 URI）不进内存缓存：下次查询重试下载
        if self._has_assets(uris):
            self._header_cache[branch] = uris
        return uris

    # ==================== 发送 ====================

    async def _send_text(self, text: str, stream_id: str) -> None:
        """发送普通文本（失败只记日志，不再抛异常打断命令流程）。"""
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as e:
            self.ctx.logger.error("发送文本失败：%s", e)

    async def _send_forward_or_fallback(
        self, nodes: List[Dict[str, Any]], stream_id: str, fallback_text: str
    ) -> bool:
        """发送合并转发卡；失败则回退发送普通文本。

        Args:
            nodes: 合并转发节点列表。
            stream_id: 目标聊天流。
            fallback_text: 回退时发送的文本（应为**有完整语义的一段正文**，
                而不是仅含表头的首个节点片段）。

        Returns:
            True 表示转发成功；False 表示已回退为文本。
        """
        try:
            resp = await self.ctx.send.forward(nodes, stream_id, return_details=True)
        except TypeError:
            # 旧版 SDK 不支持 return_details 形参
            try:
                resp = await self.ctx.send.forward(nodes, stream_id)
            except Exception as e:
                resp = None
                self.ctx.logger.warning("合并转发调用失败：%s", e)
        except Exception as e:
            resp = None
            self.ctx.logger.warning("合并转发调用失败：%s", e)
        ok = self._delivery_ok(resp)
        if not ok:
            # 返回 False = Platform IO 投递失败（如适配器/NapCat 拒收），同样回退
            self.ctx.logger.warning("合并转发投递失败，回退为普通文本")
            await self._send_text(fallback_text, stream_id)
            return False
        return True

    @staticmethod
    def _delivery_ok(resp: Any) -> bool:
        """判定一次 send.* 的返回是否视为投递成功。

        兼容三种形态：bool；详情 dict（{"sent": bool, "message_id": ...}——
        有 message_id 即视为已被平台接受，防止对「超时假阴性但迟到送达」的消息
        重复发送）；None/空（失败）。
        """
        if isinstance(resp, dict):
            return bool(resp.get("sent")) or bool(resp.get("message_id"))
        return bool(resp)

    def _image_mode_enabled(self) -> bool:
        """是否启用图片渲染发送（send_mode != "text" 时均视为图片模式）。"""
        return str(self.config.render.send_mode or DEFAULT_SEND_MODE).strip().lower() != "text"

    def _build_report(
        self,
        title: str,
        text: str,
        slug: str,
        page: int,
        *,
        branch: str = "INT",
        site: str = "",
        note: str = "",
    ) -> Tuple[List[Dict[str, Any]], bool, int]:
        """按分片策略构造文本模式的转发节点。

        Args:
            title: 条目标题。
            text: 正文纯文本。
            slug: 条目 slug（用于拼原页面链接与分页命令）。
            page: 页号（0 基）。
            branch: 分部（决定默认站点）。
            site: 内容来源站点（缺省用分部主站）。
            note: 追加到卡末的额外说明。

        Returns:
            (节点列表, 是否截断, 总页数)
        """
        per_node = max(200, int(self.config.render.per_node_chars or DEFAULT_PER_NODE_CHARS))
        max_nodes = max(1, int(self.config.render.max_nodes or DEFAULT_MAX_NODES))

        chunks = chunk_by_paragraphs(text, per_node)
        total = len(chunks)
        total_pages = max(1, (total + max_nodes - 1) // max_nodes)
        page = max(0, min(page, total_pages - 1))
        start = page * max_nodes
        window = chunks[start:start + max_nodes]
        truncated = (start + len(window)) < total

        footer: List[str] = []
        if truncated:
            footer.append(f"读取后续分页：/scp {slug.replace('scp-', '', 1)} {page + 2}")
            footer.append(f"本条目共 {total_pages} 页（每页 {max_nodes} 段）")
        if note:
            footer.append(note)

        nodes = build_forward_nodes(
            title,
            window,
            nickname=str(self.config.render.forward_nickname or DEFAULT_FORWARD_NICKNAME),
            page_index=page,
            total_pages=total_pages,
            source_url=(f"{(site or self._site_for_branch(branch)).rstrip('/')}/{slug}"
                        if self.config.render.show_source else ""),
            truncated=truncated,
            total_chars=len(text),
            footer_lines=footer,
        )
        return nodes, truncated, total_pages

    # ==================== 图片渲染发送 ====================

    async def _send_article_images(
        self,
        stream_id: str,
        branch: str,
        title: str,
        body: str,
        slug: str,
        page: int,
        source_lines: Sequence[str] = (),
    ) -> bool:
        """图片模式：把正文渲染为若干张带官方页头的图片并发送。

        正文按段落装箱切成「图片页」（尾部碎片自动并入前一页），单次最多发
        max_image_pages 张；图片**统一以一张合并转发卡**发出（单图也入卡，每节点
        一个 image 段），避免多连发刷屏。

        体积护栏（防「帧大小超过最大限制 / 适配器拒收」导致的无响应）：
        - 渲染时显式传 device_scale_factor（配置 render.render_scale，默认 1.0）；
        - 合并转发负载（base64 总量）超过 MAX_FORWARD_PAYLOAD 时，自动用更低
          缩放重渲染一遍；仍超限则改为**逐张** send.image（每张独立成帧）；
        - 单张超过 MAX_SINGLE_IMAGE_PAYLOAD 时同样降缩放重渲染。

        发送返回值**逐级检查**：send.forward（return_details=True，sent 或
        message_id 任一为真即算成功）/ send.image 返回 False（Platform IO 投递失败，
        常见于大消息超时假阴性——消息可能迟到送达）也走回退链；逐张回退前先等待
        FORWARD_FALLBACK_DELAY 秒错开迟到送达，且**部分送达后不再追加文字版**
        （避免内容重复），仅当一张都没发出时才抛异常交由调用方回退文本模式。

        Args:
            stream_id: 目标聊天流。
            branch: 分部（"INT" / "CN"），决定官方页头。
            title: 条目标题。
            body: 正文纯文本。
            slug: 条目 slug。
            page: 页号（0 基）。
            source_lines: 页脚来源行（原文/译文链接）。

        Returns:
            True 表示发送成功。
        Raises:
            渲染 / 发送失败时抛异常，由调用方回退到文本模式。
        """
        per_img = max(1000, int(self.config.render.per_image_chars or DEFAULT_PER_IMAGE_CHARS))
        # 上界钳制（0.3.3 L5 加固）：max_image_pages 只在配置侧有下限，设成 1000
        # 会触发上千次渲染拖垮进程，运行时钳制到 MAX_IMAGE_PAGES_LIMIT
        max_pages = min(MAX_IMAGE_PAGES_LIMIT,
                        max(1, int(self.config.render.max_image_pages or DEFAULT_MAX_IMAGE_PAGES)))
        width = max(400, int(self.config.render.image_width or DEFAULT_IMAGE_WIDTH))

        slices = chunk_by_paragraphs(body, per_img, merge_tail=True)
        if not slices:
            return False
        total_pages = max(1, (len(slices) + max_pages - 1) // max_pages)
        page = max(0, min(page, total_pages - 1))
        start = page * max_pages
        window = slices[start:start + max_pages]
        truncated = (start + len(window)) < len(slices)

        logo_uri, bg_uri = await self._get_header_assets(branch)
        cmd_num = slug.replace("scp-", "", 1)
        last_footer: List[str] = []
        if truncated:
            last_footer.append(f"内容较长，读取后续分页：/scp {cmd_num} {page + 2}")

        font_scale = min(3.0, max(0.5, float(self.config.render.font_scale or DEFAULT_FONT_SCALE)))

        async def render_all(scale: float, fs: Optional[float] = None) -> List[str]:
            """以指定缩放渲染本卡全部图片页（字号按 font_scale 放大）。"""
            use_fs = font_scale if fs is None else fs
            out: List[str] = []
            for i, chunk in enumerate(window):
                doc = build_article_html(
                    branch=branch,
                    title=title,
                    body=chunk,
                    page_index=start + i + 1,
                    total_pages=total_pages,
                    source_lines=source_lines,
                    logo_data_uri=logo_uri,
                    bg_data_uri=bg_uri,
                    footer_lines=last_footer if i == len(window) - 1 else [],
                    font_scale=use_fs,
                )
                png = await self.ctx.render.html2png(
                    doc,
                    viewport={"width": width, "height": 800},
                    full_page=True,
                    device_scale_factor=scale,
                )
                b64 = str((png or {}).get("image_base64") or "")
                if not b64:
                    raise RuntimeError("html2png 未返回图像数据")
                out.append(b64)
            return out

        scale = min(3.0, max(1.0, float(self.config.render.render_scale or DEFAULT_RENDER_SCALE)))
        images = await render_all(scale)
        payload = sum(len(b) for b in images)
        # 负载护栏：合并转发整体超限 → 先降渲染缩放、再降字号重渲染
        # （scale 已在 1.0 时只降字号；两级都到位则接受现状交给回退链）
        if payload > MAX_FORWARD_PAYLOAD and scale > FALLBACK_SCALE:
            self.ctx.logger.warning(
                "图片负载约 %.1f MB 超过安全阈值 %d MB，改用缩放 %.1f 重渲染",
                payload / 1048576, MAX_FORWARD_PAYLOAD // 1048576, FALLBACK_SCALE,
            )
            images = await render_all(FALLBACK_SCALE)
            payload = sum(len(b) for b in images)
            scale = FALLBACK_SCALE
        if payload > MAX_FORWARD_PAYLOAD and font_scale > 1.0:
            self.ctx.logger.warning(
                "图片负载仍约 %.1f MB 超过安全阈值，改用基准字号重渲染",
                payload / 1048576,
            )
            images = await render_all(scale, fs=1.0)
            payload = sum(len(b) for b in images)
            font_scale = 1.0

        async def render_single_b64(index: int, b64: str) -> str:
            """单张超限时降清晰度重渲染该页（先降缩放，已到底则降字号）。"""
            if len(b64) <= MAX_SINGLE_IMAGE_PAYLOAD:
                return b64
            if scale > FALLBACK_SCALE:
                use_scale, use_fs = FALLBACK_SCALE, font_scale
                why = f"缩放 {FALLBACK_SCALE:.1f}"
            elif font_scale > 1.0:
                use_scale, use_fs = scale, 1.0
                why = "基准字号"
            else:
                return b64
            self.ctx.logger.warning(
                "单张图片约 %.1f MB 超过 %d MB，以%s重渲染该页",
                len(b64) / 1048576, MAX_SINGLE_IMAGE_PAYLOAD // 1048576, why,
            )
            doc = build_article_html(
                branch=branch,
                title=title,
                body=window[index],
                page_index=start + index + 1,
                total_pages=total_pages,
                source_lines=source_lines,
                logo_data_uri=logo_uri,
                bg_data_uri=bg_uri,
                footer_lines=last_footer if index == len(window) - 1 else [],
                font_scale=use_fs,
            )
            png = await self.ctx.render.html2png(
                doc,
                viewport={"width": width, "height": 800},
                full_page=True,
                device_scale_factor=use_scale,
            )
            b64 = str((png or {}).get("image_base64") or "")
            if not b64:
                raise RuntimeError("html2png 未返回图像数据")
            return b64

        # 统一使用合并转发（单图也入卡）：每节点一个 image 段，避免连发刷屏。
        # return_details=True：SDK 返回 {"sent": bool, "message_id": ...}，
        # 有 message_id 即视为已被接受（防止对迟到送达的消息重复发送）。
        nickname = str(self.config.render.forward_nickname or DEFAULT_FORWARD_NICKNAME)
        nodes = [
            {
                "user_id": "0",
                "nickname": nickname,
                "segments": [{"type": "image", "content": b64}],
            }
            for b64 in images
        ]
        try:
            resp = await self.ctx.send.forward(nodes, stream_id, return_details=True)
        except TypeError:
            # 旧版 SDK 不支持 return_details 形参
            try:
                resp = await self.ctx.send.forward(nodes, stream_id)
            except Exception as e:
                resp = None
                self.ctx.logger.warning("图片合并转发调用失败：%s", e)
        except Exception as e:
            self.ctx.logger.warning("图片合并转发调用失败：%s", e)
            resp = None
        forward_ok = self._delivery_ok(resp)
        if isinstance(resp, dict) and not forward_ok:
            self.ctx.logger.debug("合并转发投递详情：%s", resp)

        if forward_ok:
            self.ctx.logger.info(
                "已渲染发送《%s》图片 %d 张（%s，第 %d/%d 页，合并转发，负载约 %.1f MB）",
                title, len(images), BRANCH_LABELS.get(branch, branch),
                page + 1, total_pages, payload / 1048576,
            )
            return True

        # 合并转发投递失败（Platform IO 返回 FAILED，多为大消息超时假阴性——
        # 消息可能仍会迟到送达）。等待 FORWARD_FALLBACK_DELAY 秒再逐张回退，
        # 错开迟到送达，避免用户收到重复内容。
        self.ctx.logger.warning(
            "图片合并转发投递失败（负载约 %.1f MB），%.0f 秒后改为逐张发送"
            "（若合并转发延迟送达，请忽略后续重复图片）",
            payload / 1048576, FORWARD_FALLBACK_DELAY,
        )
        await asyncio.sleep(FORWARD_FALLBACK_DELAY)
        delivered = 0
        for i, b64 in enumerate(images):
            b64 = await render_single_b64(i, b64)
            try:
                sent_i = await self.ctx.send.image(b64, stream_id)
            except Exception as e:
                self.ctx.logger.warning("第 %d/%d 张图片发送异常：%s", i + 1, len(images), e)
                sent_i = False
            if sent_i:
                delivered += 1
            else:
                # 单张失败不中断：已发出的图片不再补文字版（避免内容重复），
                # 用户可用分页命令重看缺失页
                self.ctx.logger.warning(
                    "第 %d/%d 张图片发送失败（可能被平台限流），可用 /scp %s %d 重看该页",
                    i + 1, len(images), cmd_num, i + 1,
                )
            if i < len(images) - 1:
                await asyncio.sleep(PER_IMAGE_INTERVAL)
        if delivered == 0:
            raise RuntimeError("合并转发与逐张发送均失败")
        if delivered < len(images):
            self.ctx.logger.warning(
                "图片仅部分送达（%d/%d），不追加文字版以避免内容重复", delivered, len(images),
            )
            await self._send_text(
                f"⚠️ 有 {len(images) - delivered} 张图片发送失败，"
                f"可回复 /scp {cmd_num} {page + 1} 重新查看本页",
                stream_id,
            )
        self.ctx.logger.info(
            "已渲染发送《%s》图片 %d 张（%s，第 %d/%d 页，逐张回退送达 %d 张，负载约 %.1f MB）",
            title, len(images), BRANCH_LABELS.get(branch, branch),
            page + 1, total_pages, delivered, payload / 1048576,
        )
        return True

    # ==================== 业务：查条目 ====================

    async def _handle_article(
        self, stream_id: str, arg: str, page: int, branch: str = "INT",
        *, text_mode: bool = False, force_lang: str = "",
    ) -> Tuple[bool, str, int]:
        """处理「按编号查询条目正文」。

        分部判定：指令带 cn 前缀时 branch=CN；编号自带 cn 标记（scp-cn-*）时
        自动路由到中站；其余走国际站。页头跟随分部归属；正文站点按
        _content_site_order 决定（国际站条目默认优先中站同 slug 译文，
        页脚同时标注原文/译文链接）。text_mode=True 时直接走文本合并转发。
        """
        slug = normalize_code(arg)
        if not slug:
            await self._send_text(
                f"无法识别的条目编号：{arg}\n"
                "示例：/scp 173（国际站）、/scp cn 2000（中站）",
                stream_id,
            )
            return False, "编号非法", 2

        # 编号自带 cn 标记 → 中站；/scp cn <纯编号> → 强制归入中站
        if slug_branch(slug) == "CN":
            branch = "CN"
        slug = force_branch_slug(slug, branch)
        home_site = self._site_for_branch(branch)

        if not self.config.fetch.fetch_enabled:
            await self._send_text("插件已关闭联网抓取，无法获取条目正文。", stream_id)
            return False, "联网抓取已关闭", 1

        # 先尝试用本地目录补全标题（目录不可用也不阻塞正文查询）
        title = ""
        try:
            ok, _ = await self._ensure_catalog()
            if ok:
                hit = entry_by_code(self._catalog, slug)
                if hit:
                    title = str(hit.get("title") or "")
                    slug = str(hit.get("slug") or slug)
        except Exception:
            pass

        # 依次尝试内容站点（译文优先，可配置 / 可用 en 强制原文）
        chosen_site = ""
        page_title = ""
        body = ""
        # 聚合各站点失败原因：译文优先时最多尝试两站，只报最后一站的错误
        # 会误导（如国际站 404 + 中站超时只显示"超时"）
        net_errs: List[str] = []
        for site_url in self._content_site_order(branch, force_lang):
            try:
                html_text = await self._fetch_page(slug, site_url)
            except httpx.TimeoutException:
                net_errs.append(f"{site_url} 抓取超时")
                continue
            except httpx.HTTPStatusError as e:
                net_errs.append(f"{site_url} 返回 HTTP {e.response.status_code}")
                continue
            except Exception as e:
                self.ctx.logger.error("抓取 %s@%s 失败：%s", slug, site_url, e)
                net_errs.append(f"{site_url} 失败：{e}")
                continue
            t, b = extract_page_content(html_text)
            if not b or is_error_page(html_text, t):
                net_errs.append(f"{site_url} 无该条目")
                continue
            chosen_site, page_title, body = site_url, t, b
            break

        if not chosen_site:
            if net_errs:
                await self._send_text(f"《{slug}》抓取失败（{'；'.join(net_errs)}）。", stream_id)
                return False, "抓取失败", 2
            await self._send_text(
                f"未找到条目《{slug}》，请检查编号是否正确。\n"
                f"可试试搜索：/scp {arg if branch == 'INT' else 'cn ' + arg}",
                stream_id,
            )
            return False, "条目不存在", 2

        body = clean_article_text(body)
        title = title or page_title or slug.upper()

        # 页脚来源行：内容非来自分部主站（即用了译文）时，同时标注原文与译文
        source_lines: List[str] = []
        if self.config.render.show_source:
            content_url = f"{chosen_site.rstrip('/')}/{slug}"
            if chosen_site.rstrip("/") != home_site.rstrip("/"):
                source_lines = [f"原文：{home_site}/{slug}", f"译文：{content_url}"]
            else:
                source_lines = [f"原页面：{content_url}"]

        # 图片模式优先（text_mode 显式要文字版时跳过）；失败自动回退文本合并转发
        if not text_mode and self._image_mode_enabled():
            try:
                if await self._send_article_images(
                    stream_id, branch, title, body, slug, page, source_lines
                ):
                    return True, f"已返回《{title}》（图片）", 2
                self.ctx.logger.warning("图片渲染无内容可发，回退为文本转发")
            except Exception as e:
                self.ctx.logger.warning("图片渲染/发送失败，回退为文本转发：%s", e)

        nodes, truncated, total_pages = self._build_report(
            title, body, slug, page, branch=branch, site=chosen_site
        )
        if not nodes:
            await self._send_text(f"《{title}》正文为空。", stream_id)
            return False, "正文为空", 2

        # 回退文本：从当前请求页的转发节点累积正文（跳过末尾提示节点），
        # 累积到约一个节点的容量，保证含实质正文且与所请求页一致
        # （首个节点可能只是表头+极短首段）
        fb_parts: List[str] = []
        fb_len = 0
        min_fb = max(300, int(self.config.render.per_node_chars or DEFAULT_PER_NODE_CHARS))
        for n in nodes:
            seg = n.get("segments") or []
            content = str(seg[0].get("content") or "") if seg else ""
            if content.startswith("本条消息共"):  # 末尾截断提示节点
                continue
            fb_parts.append(content)
            fb_len += len(content)
            if fb_len >= min_fb:
                break
        first_text = "\n".join(fb_parts) if fb_parts else title
        if truncated:
            first_text += "\n\n（内容较长，已截断显示）"
            if self.config.render.show_source:
                site_url = chosen_site.rstrip("/")
                first_text += f"\n完整内容见原页面：{site_url}/{slug}"
        ok = await self._send_forward_or_fallback(nodes, stream_id, first_text)
        self.ctx.logger.info(
            "已返回《%s》（%s，%s，%d 字，%d 段，第 %d/%d 页，%s）",
            title, BRANCH_LABELS.get(branch, branch),
            "文字版" if text_mode else "文本回退",
            len(body), len(nodes), page + 1, total_pages,
            "合并转发" if ok else "文本回退",
        )
        return True, f"已返回《{title}》", 2

    # ==================== 业务：搜索 ====================

    async def _handle_search(
        self, stream_id: str, keyword: str, branch: str = "INT"
    ) -> Tuple[bool, str, int]:
        """处理「按关键字搜索条目」（仅在指定分部的目录子集内搜索）。"""
        ok, err = await self._ensure_catalog()
        if not ok:
            await self._send_text(f"条目目录不可用：{err}", stream_id)
            return False, err or "目录不可用", 2

        label = BRANCH_LABELS.get(branch, branch)
        limit = max(1, int(self.config.catalog.search_limit or DEFAULT_SEARCH_LIMIT))
        hits = search_catalog(self._catalog, keyword, limit=limit, branch=branch)
        if not hits:
            # 跨分部提示：另一分部有相关条目时给出引导
            other = "CN" if branch == "INT" else "INT"
            alt = search_catalog(self._catalog, keyword, limit=3, branch=other)
            if alt:
                prefix = "/scp cn" if other == "CN" else "/scp"
                lines = [
                    f"未在{label}找到与「{keyword}」匹配的条目。",
                    f"{BRANCH_LABELS.get(other, other)}有相关条目：",
                    "",
                ]
                for i, it in enumerate(alt, start=1):
                    lines.append(f"{i}. {str(it.get('code') or '')}　{str(it.get('title') or '').strip()}")
                lines.append("")
                lines.append(f"试试：{prefix} {keyword}")
                await self._send_text("\n".join(lines), stream_id)
                return False, "本分部无匹配（另一分部有）", 2
            await self._send_text(
                f"未在{label}找到与「{keyword}」匹配的条目。\n"
                "可尝试更短的关键字，或直接输入编号（如 /scp 173、/scp cn 2000）。",
                stream_id,
            )
            return False, "无匹配", 2

        # 分页命令示例：中站建议 /scp cn <数字>；国际站直接 /scp <编号>
        example = str(hits[0].get("code") or "")
        if branch == "CN":
            m = re.search(r"(\d+)$", example)
            if m:
                example = m.group(1)
            example_cmd = f"/scp cn {example}"
        else:
            example_cmd = f"/scp {example}"

        lines = [f"「{keyword}」在{label}的搜索结果（{len(hits)} 条）：", ""]
        for i, it in enumerate(hits, start=1):
            code = str(it.get("code") or "")
            title = str(it.get("title") or "").strip()
            lines.append(f"{i}. {code}　{title}")
        lines.append("")
        lines.append(f"用 {('cn ' if branch == 'CN' else '') + '编号'} 查看正文，如 {example_cmd}")
        await self._send_text("\n".join(lines), stream_id)
        return True, f"搜索到 {len(hits)} 条", 2

    # ==================== 业务：随机 ====================

    async def _handle_random(
        self, stream_id: str, branch: str = "INT",
        *, text_mode: bool = False, force_lang: str = "",
    ) -> Tuple[bool, str, int]:
        """处理「随机抽一篇」：从对应分部的目录子集中抽取后走正文查询。"""
        ok, err = await self._ensure_catalog()
        if not ok:
            await self._send_text(f"条目目录不可用：{err}", stream_id)
            return False, err or "目录不可用", 2

        pool = [i for i in self._catalog if str(i.get("branch") or "") == branch]
        if not pool:
            label = BRANCH_LABELS.get(branch, branch)
            await self._send_text(
                f"本地目录中没有{label}的条目，可先执行 /scp刷新目录。", stream_id
            )
            return False, "目录无该分部", 2

        pick = random.choice(pool)
        slug = str(pick.get("slug") or "").strip().lower()
        if not slug:
            await self._send_text("随机抽取失败，请重试。", stream_id)
            return False, "抽取失败", 2
        # 纵深防御：目录可能来自旧版本写入的 catalog.json（未经新过滤）
        if not is_safe_slug(slug):
            await self._send_text(
                "随机抽到的条目路径异常，已忽略。可执行 /scp刷新目录 重建目录后重试。",
                stream_id,
            )
            return False, "目录条目路径异常", 2
        return await self._handle_article(
            stream_id, slug, 0, branch, text_mode=text_mode, force_lang=force_lang
        )

    # ==================== 指令入口 ====================

    @Command(
        "scp",
        description="查询 SCP 条目正文（/scp 国际站，/scp cn 中站；按编号 / 关键字 / 随机）",
        pattern=r"(?<!\S)/?scp(?:\s+(?P<arg>.*?))?\s*$",
        aliases=["scp查询"],
    )
    async def cmd_scp(self, **kwargs: Any) -> Tuple[bool, str, int]:
        """`/scp` 指令入口。

        支持形式（text / cn / en 修饰符顺序任意，可组合）：
        - `/scp <编号>`          → 国际站条目（优先中站译文，页头为国际站官方头）
        - `/scp cn <编号>`       → 中站条目
        - `/scp en <编号>`       → 强制英文原文
        - `/scp text [cn] <编号>` → 文字版（合并转发文本）
        - `/scp [cn] <编号> <页号>` → 对应分部正文（指定页）
        - `/scp [cn] <关键字>`   → 对应分部的目录搜索
        - `/scp [cn] rand`       → 对应分部随机条目
        - `/scp` / `/scp help`   → 帮助
        """
        stream_id = str(kwargs.get("stream_id") or "")
        raw = str(kwargs.get("matched_groups", {}).get("arg") or "").strip()

        # 无参数 / help → 帮助
        if not raw or raw.lower() in ("help", "?", "帮助"):
            await self._send_text(HELP_TEXT, stream_id)
            return True, "已返回帮助", 2

        parts = raw.split()

        # 前缀修饰符（顺序任意）：text=文字版；cn=中站；en=英文原文
        branch = "INT"
        text_mode = False
        force_lang = ""
        while parts and parts[0].lower() in CMD_MODIFIERS:
            kind = CMD_MODIFIERS[parts.pop(0).lower()]
            if kind == "text":
                text_mode = True
            elif kind == "cn":
                branch = "CN"
            else:
                force_lang = "en"
        if not parts or parts[0].lower() in ("help", "?", "帮助"):
            await self._send_text(HELP_TEXT, stream_id)
            return True, "已返回帮助", 2

        head = parts[0]

        # 随机
        if head.lower() in ("rand", "random", "随机"):
            return await self._handle_random(
                stream_id, branch, text_mode=text_mode, force_lang=force_lang
            )

        # 「编号 + 页号」：第二个 token 为纯数字且第一个 token 能识别为编号
        if len(parts) >= 2 and parts[1].isdigit() and normalize_code(head):
            page = max(1, int(parts[1])) - 1
            return await self._handle_article(
                stream_id, head, page, branch, text_mode=text_mode, force_lang=force_lang
            )

        # 能识别为编号 → 直取正文
        if normalize_code(head):
            return await self._handle_article(
                stream_id, head, 0, branch, text_mode=text_mode, force_lang=force_lang
            )

        # 多词关键字 → 整体作为关键字
        return await self._handle_search(stream_id, " ".join(parts), branch)

    @Command(
        "scp刷新目录",
        description="强制刷新 SCP 本地条目目录",
        pattern=r"(?<!\S)/?scp刷新目录\s*$",
    )
    async def cmd_refresh_catalog(self, **kwargs: Any) -> Tuple[bool, str, int]:
        """`/scp刷新目录`：强制重新抓取系列索引页构建条目目录。

        带 per-stream 冷却（catalog.refresh_cooldown_minutes，默认 10 分钟，
        仅**成功**后落全量冷却）：一次刷新要抓 15 个索引页、约 20 秒，无冷却会
        被群成员连发刷爆且 SCP 维基对爬取不友好（宿主 IP 被封所有用户吃亏）。
        失败（多为网络抖动）只落短暂冷却（catalog.refresh_fail_cooldown_seconds，
        默认 60 秒），便于尽快重试。管理员（宿主 [plugin].permission ∪ 插件配置
        admins.admin_users，去重后）可免冷却（admins.admin_bypass_refresh_cooldown，
        默认开）。
        """
        stream_id = str(kwargs.get("stream_id") or "")
        if not self.config.fetch.fetch_enabled:
            await self._send_text("插件已关闭联网抓取，无法刷新目录。", stream_id)
            return False, "联网抓取已关闭", 2
        cooldown = max(0, int(self.config.catalog.refresh_cooldown_minutes or 10)) * 60
        fail_cd = max(0, int(self.config.catalog.refresh_fail_cooldown_seconds or 60))
        now = time.time()
        remaining = int(cooldown - (now - self._refresh_ts.get(stream_id, 0.0)))
        if remaining > 0:
            # 冷却中：仅当开关打开且触发者确实是管理员时放行（避免每次冷却期都白读一遍配置）
            bypass = False
            if self.config.admins.admin_bypass_refresh_cooldown:
                bypass = await self._is_admin(kwargs)
            if not bypass:
                minutes = max(1, (remaining + 59) // 60)
                await self._send_text(
                    f"刷新太频繁（每 {cooldown // 60 or 1} 分钟一次），"
                    f"请约 {minutes} 分钟后再试（防止 SCP 维基限流/封 IP）。",
                    stream_id,
                )
                return False, "刷新冷却中", 2
        # 先落全量冷却戳防连发；失败时回拨为短暂冷却（0.3.3 前失败也吃全量冷却）
        self._refresh_ts[stream_id] = now
        # 顺带清理过期的冷却键（L5：长驻进程缓慢累积）
        for key in [k for k, ts in self._refresh_ts.items()
                    if now - ts > max(cooldown, fail_cd, 60) * 2]:
            del self._refresh_ts[key]
        await self._send_text("正在刷新 SCP 条目目录（约需 20 秒）…", stream_id)
        ok, err = await self._ensure_catalog(force=True)
        if not ok:
            # 失败回拨时间戳，使剩余冷却 = refresh_fail_cooldown_seconds
            self._refresh_ts[stream_id] = now - max(0.0, float(cooldown - fail_cd))
            await self._send_text(f"刷新失败：{err}", stream_id)
            return False, err or "刷新失败", 2
        await self._send_text(f"目录刷新完成，当前共 {len(self._catalog)} 条。", stream_id)
        return True, "目录已刷新", 2


def create_plugin() -> ScpArticlePlugin:
    """Runner 加载入口。"""
    return ScpArticlePlugin()

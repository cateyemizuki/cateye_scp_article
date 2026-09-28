# -*- coding: utf-8 -*-
"""开发期工具：抓取两站当前官方页头资源，生成本插件内置兜底 assets/header_<BRANCH>.json。

用途：把「当前获取到的站点头部渲染」固化为插件内置兜底——运行时下载失败、
且本机也没有任何（哪怕过期的）磁盘缓存时，用这份内置资源出图，避免退化为
纯色横幅 + 文字徽标。

用法（需联网，开发机执行一次即可；日常无需运行）：
    python tools/build_header_assets.py

产出（随插件发布，仅含图片的 base64 data URI）：
    assets/header_INT.json  国际站 logo + 已裁剪暗色页头带的 body_bg
    assets/header_CN.json   中站   logo + 已裁剪暗色页头带的 body_bg
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import scp_core as core  # noqa: E402

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(PLUGIN_DIR, "assets")

# 与 plugin.py 保持一致
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MAX_ASSET_BYTES = 4 * 1024 * 1024
ASSET_SCHEMA_VERSION = 1

# 各分部页头图片资源的授权与署名信息（写入产物 JSON，同时同步到 NOTICE.md）
LICENSES = {
    "INT": {
        "copyright": (
            "SCP 基金会维基（scp-wiki.wikidot.com）及其作者；互见 "
            "https://scp-wiki.wikidot.com/component:theme （原 Sigma-9 主题设计：Aelanna）"
        ),
        "source_pages": [
            "https://cdn.scpwiki.com/theme/en/sigma/images/header-logo.svg",
            "https://cdn.scpwiki.com/theme/en/sigma/images/body_bg.svg",
            "https://scp-wiki.wikidot.com/component:theme",
        ],
        "changes": "body_bg 由整页背景（100x400）裁剪为页头暗色带（100x164）；均以 base64 data URI 内嵌。",
    },
    "CN": {
        "copyright": (
            "SCP 基金会中文分部（scp-wiki-cn.wikidot.com）及其作者；中站声明其 Sigma-9 版式与样式"
            "由 Aelanna 设计，适用 CC BY-SA 3.0（https://scp-wiki-cn.wikidot.com/）"
        ),
        "source_pages": [
            "https://sigma9.scpwikicn.com/cn/img/logo.png",
            "https://sigma9.scpwikicn.com/cn/img/body_bg.png",
            "https://scp-wiki-cn.wikidot.com/",
        ],
        "changes": "body_bg 由整页背景（100x400）裁剪为页头暗色带（100x164）；均以 base64 data URI 内嵌。",
    },
}


def fetch(client: httpx.Client, url: str) -> bytes:
    """下载单个资源并施加体积上限（与运行时同一套护栏）。"""
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        clen = resp.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > MAX_ASSET_BYTES:
            raise ValueError(f"{url} Content-Length {clen} 超过上限")
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf.extend(chunk)
            if len(buf) > MAX_ASSET_BYTES:
                raise ValueError(f"{url} 超过 {MAX_ASSET_BYTES} 字节上限")
    return bytes(buf)


def build_branch(client: httpx.Client, branch: str) -> dict:
    meta = core.SITE_HEADERS[branch]
    print(f"[{branch}] logo <- {meta['logo']}")
    logo = fetch(client, meta["logo"])
    print(f"[{branch}] bg   <- {meta['bg']}")
    bg = fetch(client, meta["bg"])
    bg_cropped = core.crop_banner_tile(bg, meta["bg_mime"])
    print(f"[{branch}] logo {len(logo)} 字节；底纹 {len(bg)} -> 裁剪后 {len(bg_cropped)} 字节")
    lic = LICENSES[branch]
    return {
        "schema": ASSET_SCHEMA_VERSION,
        "branch": branch,
        "generated_at": time.strftime("%Y-%m-%d"),
        "note": (
            "内置兜底页头资源（官方 Sigma 主题 logo 与 body_bg 暗色页头带裁剪版），"
            "仅用于标注来源页面。由 tools/build_header_assets.py 生成；"
            "逐项署名与修改说明见仓库根目录 NOTICE.md。"
        ),
        # 机器可读的授权信息（CC BY-SA 3.0 要求署名 + 标明是否修改）
        "license": "CC BY-SA 3.0",
        "license_url": "https://creativecommons.org/licenses/by-sa/3.0/",
        "copyright": lic["copyright"],
        "source_pages": lic["source_pages"],
        "changes": lic["changes"],
        "logo_mime": meta["logo_mime"],
        "bg_mime": meta["bg_mime"],
        "logo": core.to_data_uri(logo, meta["logo_mime"]),
        "bg": core.to_data_uri(bg_cropped, meta["bg_mime"]),
    }


def main() -> int:
    os.makedirs(ASSETS_DIR, exist_ok=True)
    with httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        for branch in ("INT", "CN"):
            data = build_branch(client, branch)
            path = os.path.join(ASSETS_DIR, f"header_{branch}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            print(f"[{branch}] 已写出 {path}（{os.path.getsize(path)} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

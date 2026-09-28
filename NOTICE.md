# 第三方内容与许可声明（NOTICE）

本插件**代码与第三方内容分层授权**。本文件说明各部分的许可以及必须随附的署名。

## 1. 分层许可总览

| 路径 / 内容 | 许可 | 权利人 |
|---|---|---|
| `plugin.py`、`scp_core.py`、`tools/` | **MIT**（见 [LICENSE](LICENSE)） | cateyemizuki |
| `assets/header_INT.json`、`assets/header_CN.json` | **CC BY-SA 3.0**（其中的官方页头图片资源） | SCP 基金会维基及其作者（见下方逐项署名） |
| 运行期抓取并渲染的条目正文 | **CC BY-SA 3.0** | 各条目作者 + SCP 基金会维基 |
| 运行期下载的官方页头资源（同 `assets/` 内容） | **CC BY-SA 3.0** | 同上 |

`_manifest.json` 的 `license: "MIT"` 指的是**插件代码**（与 `LICENSE` 一致）；仓库内的第三方内容不因此变成 MIT，仍按本文件第 3 节以 CC BY-SA 3.0 使用。

> 关于为什么代码不整体改用 CC：知识共享官方**不建议**将 CC 协议用于软件（缺少专利授权、Source/Binary 术语不适用于二进制），SCP 维基的[授权指南](https://scp-wiki.wikidot.com/licensing-guide)对此也给出同样的建议（并为软件提供了 GPLv3 这一兼容选项）。

## 2. 来源与许可链接

- SCP 基金会（国际站）：<https://scp-wiki.wikidot.com> — 站点内容依 **CC BY-SA 3.0** 授权
- SCP 基金会中文分部（中站）：<https://scp-wiki-cn.wikidot.com> — 站点内容依 **CC BY-SA 3.0** 授权
- 许可全文：<https://creativecommons.org/licenses/by-sa/3.0/>
- 维基图片使用政策：<https://scp-wiki.wikidot.com/image-use-policy>
- 维基授权指南（署名与 ShareAlike 要求）：<https://scp-wiki.wikidot.com/licensing-guide>

## 3. 随插件分发的第三方内容（逐项署名）

CC BY-SA 3.0 要求署名（BY）并标明**是否做过修改**（indicate if changes were made）。

### 3.1 `assets/header_INT.json`（国际站）

| 项目 | 内容 |
|---|---|
| 资源 1 | Sigma 主题站点 logo `header-logo.svg` |
| 资源 2 | Sigma 主题整页背景图 `body_bg.svg` |
| 来源 | <https://cdn.scpwiki.com/theme/en/sigma/images/header-logo.svg>、<https://cdn.scpwiki.com/theme/en/sigma/images/body_bg.svg> |
| 出处页面 | 国际站 Sigma 主题（原 Sigma-9 主题设计：**Aelanna**）<https://scp-wiki.wikidot.com/component:theme> |
| 许可 | CC BY-SA 3.0 |
| 修改 | **有**。`body_bg` 由整页背景（100×400）裁剪为页头暗色带（100×164）；两者均以 base64 data URI 内嵌。 |
| 署名 | 素材来自 [SCP 基金会](https://scp-wiki.wikidot.com) 的 Sigma 主题，依 CC BY-SA 3.0 使用；本插件对其做了裁剪与内嵌处理。 |

### 3.2 `assets/header_CN.json`（中站）

| 项目 | 内容 |
|---|---|
| 资源 1 | Sigma-9 中文主题站点 logo `logo.png` |
| 资源 2 | Sigma-9 中文主题整页背景图 `body_bg.png` |
| 来源 | <https://sigma9.scpwikicn.com/cn/img/logo.png>、<https://sigma9.scpwikicn.com/cn/img/body_bg.png> |
| 出处页面 | 中站首页与其 Sigma-9 版式（中站原文：「SCP 基金会维基 Sigma-9 版式和样式由 Aelanna 设计，适用知识共享-版权归属-相同方式共享 3.0 授权协议（CC-BY-SA）」，2026-09-28 实测抓取 <https://scp-wiki-cn.wikidot.com/>） |
| 许可 | CC BY-SA 3.0 |
| 修改 | **有**。`body_bg` 由整页背景（100×400）裁剪为页头暗色带（100×164）；两者均以 base64 data URI 内嵌。 |
| 署名 | 素材来自 [SCP 基金会中文分部](https://scp-wiki-cn.wikidot.com) 的 Sigma-9 主题，依 CC BY-SA 3.0 使用；本插件对其做了裁剪与内嵌处理。 |

> 两站的 logo 是站点标识图形。维基授权指南在示例授权声明中把「SCP Foundation logo」一并纳入 CC BY-SA 3.0 范围，故此处按 CC BY-SA 3.0 署名；本插件仅将其用于**标注内容来源**，不对其主张任何权利。
>
> 中站主题图片的具体设计者未在中站页面上逐一署名（页面只署了版式设计者 Aelanna），故此处按「中站维基及其作者」courtesy 署名；若需要更精确的署名，可运行维护工具 `tools/build_header_assets.py` 重新生成，并联系中站管理团队确认。

## 4. 运行期行为与署名

- 插件按用户指令**只读抓取**条目正文，渲染为图片或文本**即时展示**，不在仓库中缓存或转载文章内容。
- 渲染图的页脚固定输出「内容来自 SCP 基金会维基，依 CC BY-SA 3.0 授权」，并在开启 `render.show_source`（默认开启）时附上**原页面链接**，以满足署名要求。
- 本插件**不含** SCP-173 的旧配图（Izumi Kato 的 *Untitled 2004*）。该图**不在** CC 授权范围内、禁止商用，插件只抓取页面文本，因此不会获取或分发该图片。

## 5. 再分发者须知

如果你重新分发本插件（或其修改版）：

- 代码部分保留 [LICENSE](LICENSE)（MIT）与版权声明即可；
- **`assets/` 内的图片资源**必须继续以 CC BY-SA 3.0 发布，并保留本文件的署名与「已修改」说明；
- 若你修改了 `assets/` 内容，请在署名中说明你的修改。

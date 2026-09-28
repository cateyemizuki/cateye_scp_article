# SCP 条目查询（MaiBot 插件）

为麦麦（MaiBot）框架提供的 SCP 基金会条目查询插件：

- **分站路由**：`/scp <编号>` 查询**国际站**条目，`/scp cn <编号>` 查询**中站**条目；
- 文章默认**渲染为图片**发送：抓取对应分部**官方页头**（Sigma-9 主题的 logo 与横幅底纹）
  内嵌 HTML，经宿主 `render.html2png` 渲染、`send.image` 发送，还原官方页面观感；
- 渲染/发送失败**自动回退**文本合并转发（正文按段落装箱后用**单卡多节点**发送）；
- 长文自动**分页**（图片按张、文本按节点），卡末提示字数、已展示范围、原页面链接与分页命令；
- 指令 `/scp [cn] <关键字>` 在**本地条目目录**（约 1.1 万条，编号 + 标题）中按分部搜索；
- 指令 `/scp [cn] rand` 从对应分部随机抽一篇；
- **不注册任何 LLM 工具**：仅提供文本指令（按需求「暂时不要注册工具给 llm 调用」）。

## 功能特性

- **分站路由**：
  - `/scp 173` → 国际站条目 `https://scp-wiki.wikidot.com/scp-173`；
  - `/scp cn 2000` → 中站条目 `https://scp-wiki-cn.wikidot.com/scp-cn-2000`；
  - 编号自带 cn 标记（`/scp cn-2000`）时自动路由到中站；
  - 搜索与随机按分部过滤；本分部无结果而另一分部有相关条目时给出跨分部提示。
- **国际站条目显示中文译文**（`fetch.int_content_zh`，默认开启）：
  - `/scp 173` 的内容取自**中站同 slug 译文页**，页头仍为国际站官方头；
  - 无译文的条目自动回退英文原文；页脚同时标注「原文 / 译文」链接；
  - `/scp en <编号>` 单次强制英文原文。
- **图片渲染发送**（默认，`render.send_mode = "image"`；`/scp text` 可单次改用文字版）：
  - 官方页头：国际站 logo 取自 `cdn.scpwiki.com`（Sigma-9 主题）、中站取自
    `sigma9.scpwikicn.com`（Sigma-9 中文主题），横幅底纹（整页背景图）**自动裁剪出暗色页头带**
    后连同 logo 以 data URI 内嵌，经宿主 `render.html2png`（禁网安全渲染）出图后发送；
  - 页头资源四级取用（内存 → 磁盘 7 天 → 联网下载 → **内置兜底**）：本机旧缓存
    （哪怕过期）优先，内置兜底（随插件发布的 `assets/header_INT.json` /
    `assets/header_CN.json`，内容为开发期抓到的官方页头渲染）只在**完全没有任何资源**时
    顶上；两者都没有才退化为纯色横幅 + 文字徽标。因此离线部署或首启即遇 CDN 故障
    也能出带官方页头的图；
  - 长文按段落装箱切为「图片页」（每张约 5000 字，宽 900px 高度自适应），
    单次最多 3 张，**图片统一以单张合并转发卡发出（单图也入卡）**，
    投递失败回退逐张发送，超出截断并给出分页命令；
    `/scp <编号> <页号>` 读取后续图片页；
  - 分段为**智能切分**：超长段落按句子边界（其次子句边界）切开，不拦腰截断；
    末页碎片自动并入前一页，避免孤行页；
  - **字号可调**（`render.font_scale`，默认 1.5 → 正文 24px），手机阅读更清晰；
  - 页脚含页码、原页面链接与 CC BY-SA 3.0 授权声明。
- **文本模式兜底**（`render.send_mode = "text"` 或渲染失败时）：
  - 正文按**段落**装箱切分（不撕裂句子/表格），每节点不超过 `per_node_chars`（默认 1500 字），
    单卡节点数不超过 `max_nodes`（默认 20），每节点只含**一个 text 段**
    （符合文档对 `send.forward` 的建议）；
  - 超出部分**截断**，卡末追加说明节点（总字数 / 已展示段数 / 原页面链接 / 分页命令），
    实测 6.9 万字条目分 3 页可 100% 覆盖全文。
- **纯 ASCII 编号容错**：`173`、`scp-173`、`SCP-173`、`cn2000`、`SCP-CN-2000`、
  `cn-2000`、`/scp cn 2000` 均可识别。
- **本地条目目录**：抓取中站 15 个系列索引页（`/scp-series*`、`/scp-series-cn*`）解析出
  **11,476 条**（SCP-CN 4,434 / 国际 7,042，国际条目标题为中站中文译名），
  带磁盘缓存（默认 7 天），使搜索与随机**不依赖 wikidot 的 JS 搜索**。
- **回退机制**：图片渲染失败 → 文本合并转发 → 普通文本，逐级兜底不空手。
- **离线可用**：`fetch.fetch_enabled = false` 时关闭联网，仅保留目录搜索与随机
  （此时图片模式的页头走本机缓存 → 内置兜底资源，仍能出图）。

## 安装方式

1. 将本插件目录（含 `_manifest.json`、`plugin.py`、`scp_core.py` 等）放入 MaiBot 的 `plugins/` 目录。
2. 重启 MaiBot，或在 WebUI 插件中心安装。
3. 插件依赖 `httpx`，已声明于 `_manifest.json`，Host 会自动安装。

> 兼容性声明：`host_application` `1.0.0 ~ 1.99.99`，`sdk` `2.5.0 ~ 2.99.99`（Manifest v2）。
> 图片渲染依赖宿主 `render.html2png` 能力（MaiBot 1.2.x 实测支持）；宿主不支持时自动回退文本模式。

## 使用方式

| 命令 | 说明 |
|---|---|
| `/scp 173` | 查询国际站 SCP-173 正文（中文译文 + 国际站页头，图片） |
| `/scp cn 2000` | 查询中站 SCP-CN-2000 正文（图片） |
| `/scp 5000 2` | 读取国际站 SCP-5000 的第 2 页（超长条目） |
| `/scp cn 3000 2` | 读取中站 SCP-CN-3000 的第 2 页 |
| `/scp en 173` | 强制显示 SCP-173 英文原文 |
| `/scp text 173` / `/scp text cn 2000` | 文字版（合并转发文本，不渲染图片） |
| `/scp 深红之王` | 在国际站条目中按关键字搜索 |
| `/scp cn 门` | 在中站条目中按关键字搜索 |
| `/scp rand` | 随机抽一篇国际站条目 |
| `/scp cn rand` | 随机抽一篇中站条目 |
| `/scp` / `/scp help` | 显示帮助 |
| `/scp刷新目录` | 强制刷新本地条目目录（约 20 秒；同一聊天流 10 分钟冷却） |

## 配置说明

插件加载后由 Runner 在插件目录生成 `config.toml`，可在 WebUI 修改：

```toml
[plugin]
enabled = true
config_version = "0.3.1"

[fetch]                # 抓取配置
site_url_int = "https://scp-wiki.wikidot.com"      # 国际站地址（/scp 查询）
site_url_cn = "https://scp-wiki-cn.wikidot.com"    # 中站地址（/scp cn 查询）
int_content_zh = true    # 国际站条目优先显示中站译文（无译文回退原文）
timeout = 20.0                                     # 单次 HTTP 请求超时（秒）
fetch_enabled = true                               # 是否允许联网抓取正文

[render]               # 展示配置
send_mode = "image"    # 发送方式：image=渲染图片；text=文本合并转发
per_image_chars = 5000 # 图片模式：每张图片承载的正文汉字上限
max_image_pages = 3    # 图片模式：单次最多发送的图片张数
image_width = 900      # 图片模式：渲染图宽度（像素）
render_scale = 1.0    # 图片模式：渲染缩放倍率（1.0 保证合并转发同步送达）
font_scale = 1.5       # 图片模式：字号缩放倍率（1.0 基准正文 16px → 1.5 即 24px）
per_node_chars = 1500  # 文本模式：每个转发节点的字符上限
max_nodes = 20         # 文本模式：单张转发卡的最大节点数
forward_nickname = "SCP 基金会档案"  # 文本模式：转发气泡昵称
show_source = true     # 是否显示原页面链接

[catalog]              # 目录配置
catalog_cache_hours = 168  # 本地目录缓存有效期（小时）
search_limit = 10          # 搜索返回条数上限
auto_refresh = true        # 目录缺失/超期时是否自动抓取
```

### 关键配置项说明

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `render.send_mode` | image | image=图片发送（带官方页头，多图合并转发）；text=文本合并转发。渲染失败自动回退 |
| `fetch.int_content_zh` | true | 国际站条目优先显示中站译文；`/scp en` 可单次强制原文 |
| `render.max_image_pages` | 3 | 单次最多 3 张图（约 1.5 万字），覆盖约 95% 条目全文 |
| `render.image_width` | 900 | 渲染图宽度；高度按内容自适应 |
| `render.render_scale` | 1.0 | 渲染缩放倍率；1.0 保证大图合并转发同步送达（0.2.3 前的 image_scale 已更名） |
| `render.font_scale` | 1.5 | 字号缩放倍率（正文 16px 基准），默认正文 24px |
| `render.per_node_chars` | 1500 | 文本模式；中文 UTF-8 约 3 字节/字，1500 字 ≈ 4.5KB，QQ 单条消息安全区间 |
| `render.max_nodes` | 20 | 文本模式；20 × 1500 = 3 万字，可覆盖约 95% 条目的全文 |
| `fetch.fetch_enabled` | true | 关闭后仅保留目录搜索/随机（需已有目录缓存） |
| `catalog.auto_refresh` | true | 关闭后不会自动联网构建目录，可用 `/scp刷新目录` 手动构建 |

### 安全约束（0.3.0 / 0.3.1）

- **站点地址白名单**：`fetch.site_url_int` / `site_url_cn` 只接受 **https + 官方两个
  wikidot 域名**（`scp-wiki.wikidot.com` / `scp-wiki-cn.wikidot.com`）。填内网/环回/
  元数据地址（`127.0.0.1`、`192.168.x.x`、`169.254.169.254` 等）、非 http(s) scheme 或
  白名单外域名都会记告警并回退默认站——配置入口不会被用来把内部服务响应回显进群聊。
- **slug 严格形态**：条目 slug 一律须为 ASCII 小写字母数字与连字符（≤128 字符），
  目录解析、编号解析、请求拼接三处都校验，百分号编码、`..`、`//`、尾随斜杠等畸形路径
  都进不了请求 URL。
- **抓取体积上限**：正文 HTML 5MB / 页头资源 4MB，流式读取，先查 `Content-Length`
  再逐块累计，超限立即中止。
- **目录刷新冷却**：`/scp刷新目录` 同一聊天流 10 分钟一次（一次 15 个索引页、约 20 秒，
  防止群成员连发导致 SCP 维基限流/封宿主 IP）。

> 从 0.1.x/0.2.0 升级：`fetch.site_url` 已拆分为 `site_url_int` / `site_url_cn`，
> `render.cn_first` 已移除、新增 `fetch.int_content_zh` 与 `render.send_mode / per_image_chars /
> max_image_pages / image_width / image_scale`；Runner 在 config_version 变化时会自动重建配置骨架。
> 发送可靠性：`send.forward`（return_details）/ `send.image` 返回值逐级检查；投递失败先等待 8 秒
> （Platform IO 对大消息可能「超时报失败但迟到送达」）再逐张回退，且部分送达后不补文字版，
> 杜绝一条查询返回多份重复内容；仅当图片一张都没发出时才回退文字版。

## 长文本处理策略

SCP 条目正文字数差异极大（实测中位数 2,459 字，最长 69,191 字），核心矛盾是
**单条消息 / 单张图片容量有限**而长尾文章远超之。本插件采用三层机制：

| 层 | 机制（图片模式） | 机制（文本模式） | 目的 |
|---|---|---|---|
| 1 | 每张图 ≤ `per_image_chars` 字 | 单节点 ≤ `per_node_chars` | 控制单张图片高度 / 规避平台单消息上限 |
| 2 | 单次 ≤ `max_image_pages` 张 | 单卡节点数 ≤ `max_nodes` | 控制消息数量 / 规避单卡节点数上限 |
| 3 | 超出截断 + 图脚分页命令 | 超出截断 + 卡末提示 + 分页 | 明确告知用户「未完」，并给出继续读取的方式 |

按段落装箱可保证**最长段落仅约 300 字**，因此切分几乎总在自然语义边界发生。

超长条目图片页脚提示示例：

```
第 1 / 9 页
原页面：https://scp-wiki-cn.wikidot.com/scp-cn-3000
内容较长，读取后续分页：/scp cn-3000 2
内容来自 SCP 基金会维基，依 CC BY-SA 3.0 授权
```

## 工程结构

```
cateye_scp_article/
├── _manifest.json    # Manifest v2（capabilities: send.text, send.forward, send.image, render.html2png）
├── plugin.py         # 插件入口：配置模型、生命周期、分站路由、图片/文本双发送管线
├── scp_core.py       # 纯逻辑层（不依赖 SDK，可离线单测）
├── assets/           # 内置兜底页头资源（官方 logo + 已裁剪底纹的 data URI 快照）
│   ├── header_INT.json
│   └── header_CN.json
├── tools/
│   └── build_header_assets.py   # 开发期工具：重新抓取官方页头并生成上面的快照
├── README.md         # 本文件
├── COMMANDS.md       # 指令速查
├── NOTICE.md         # 第三方内容与分层许可声明（CC BY-SA 3.0 署名、修改说明）
├── LICENSE           # 插件代码的 MIT 许可
└── CHANGELOG.md      # 更新日志
```

`scp_core.py` 承载全部可离线单测的核心逻辑：`strip_html` / `extract_page_content` /
`chunk_by_paragraphs` / `build_forward_nodes` / `build_article_html` / `parse_catalog_items` /
`search_catalog` / `slug_branch` / `force_branch_slug`。

## 数据来源与授权

**本插件分层授权**（详见 [NOTICE.md](NOTICE.md)）：

| 内容 | 许可 |
|---|---|
| `plugin.py` / `scp_core.py` / `tools/`（插件代码） | **MIT**（见 [LICENSE](LICENSE)，与 manifest 的 `license: "MIT"` 一致） |
| `assets/header_*.json` 内的官方页头图片资源 | **CC BY-SA 3.0** |
| 运行期抓取并展示的条目正文、页头资源 | **CC BY-SA 3.0** |

- 条目正文与标题来自 **SCP 基金会**各分部维基（国际站 `scp-wiki.wikidot.com`、
  中站 `scp-wiki-cn.wikidot.com`），依 **CC BY-SA 3.0** 授权：
  - 国际站页头素材：来自 [国际站 Sigma 主题](https://scp-wiki.wikidot.com/component:theme)
    （原 Sigma-9 主题设计：Aelanna），来源 `cdn.scpwiki.com/theme/en/sigma/images/`；
  - 中站页头素材：来自 [SCP 基金会中文分部](https://scp-wiki-cn.wikidot.com) Sigma-9 主题，
    来源 `sigma9.scpwikicn.com/cn/img/`（中站声明其版式与样式由 Aelanna 设计，适用 CC BY-SA 3.0）；
  - **对素材的修改**：`body_bg` 由整页背景（100×400）裁剪为页头暗色带（100×164），
    并以 base64 data URI 内嵌（CC BY-SA 要求标明修改）。
- 本插件仅做**只读抓取与即时展示**，不缓存正文内容、不转载文章；
  磁盘缓存中仅保存「编号 + 标题」目录与官方页头图片资源（用于渲染页头）。
- 随插件发布的 `assets/header_INT.json` / `assets/header_CN.json` 是**同一批官方页头
  图片资源**（logo + 已裁剪底纹）的 base64 内嵌副本，性质与运行时缓存一致，
  仅用于在抓取失败时仍能渲染来源页头；可用 `tools/build_header_assets.py` 重新生成。
  文件内已写入 `license` / `license_url` / `copyright` / `source_pages` / `changes` 字段。
- 渲染图的页脚固定输出「内容来自 SCP 基金会维基，依 CC BY-SA 3.0 授权」，
  并默认附带原页面链接，以满足维基的署名要求。
- 本插件**不含** SCP-173 的旧配图（Izumi Kato 的 *Untitled 2004*，不在 CC 授权范围内、禁止商用）；
  插件只抓取页面文本，不会获取或分发该图片。
- 请遵守目标站点的访问频率与使用条款；插件已加超时与容错，不对同一页面做并发轰炸。

> 为什么代码保持 MIT 而不整体改用 CC：知识共享官方不建议将 CC 协议用于软件
> （缺少专利授权、Source/Binary 术语不适用于二进制），SCP 维基的
> [授权指南](https://scp-wiki.wikidot.com/licensing-guide)亦给出同样建议
> （并为软件提供 GPLv3 这一兼容选项）。

## 已知限制

- wikidot 的**全文搜索**依赖 JS，服务端不可直接调用；本插件改为「系列索引页目录 + 本地匹配」，
  因此**搜索范围限于编号与标题**，不覆盖正文内容。
- 条目若使用折叠块（collapsible）、Tab 页等交互元素，其隐藏内容会被展开为普通文本，
  顺序可能与网页视觉略有差异；图片渲染为纯文本排版，不含原文图片与复杂 CSS 组件。
- 国际分部的部分特殊条目（如 001 提案、多语言分部）标题可能为占位或英文。
- 渲染图片高度随正文增长；极长段落分页后单张图片仍可能较高（约 2500px），
  客户端会缩放显示，可点开查看原图。

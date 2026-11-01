# MaiBot 搜索中枢（maibot-search-hub）

插件 ID：`kamionichan.search_hub`　版本：0.5.4　许可：MIT　要求：MaiBot ≥ 1.3.17、SDK ≥ 2.19

MaiBot 的**搜索编排层与对话衔接层**。它不是又一个搜索供应商插件，而是把各家搜索接口、搜索引擎、Agent 搜索和以图搜图统一管起来：

- **组合多个来源**：模型只说“要查什么、查多深”，插件按语言和问题类型挑来源、并发、失败换源、控制额度。
- **不占用聊天**：几秒内能完成的直接返回；慢的自动转到后台，bot 照常聊天，结果查到后自动送回对话。
- **衔接自然**：搜过的每条结果都有编号（S1、S2……），追问时可以直接读原文、引用出处；Replyer 生成回复时会带上相关的出处。
- **以图搜图**：番剧截图找出处（trace.moe）、认角色（AnimeTrace）、插画出处（SauceNAO）、相似图片和照片（Yandex、Bing）。

## 要解决的问题

MaiBot 生态里已有不少搜索能力：插件市场里的各种搜索插件、Host 自带的 `web_search` / `web_fetch`、Agent 模型自带的联网搜索、反向图片搜索服务……但它们**各管各的**：

- 选用哪个工具，完全取决于 Planner 模型读工具描述后的临场判断；Host 不做路由。
- 多数插件工具默认 deferred（不直接可见），一次普通搜索常需要“`tool_search` 发现 → 调用 → 回复”三轮 Planner。
- 各工具结果格式不同，搜过的内容不能在后续对话中按编号引用，追问时常重新搜索或凭印象回答，对话出现“断层”。
- 搜索执行期间占用 Planner 的时间、轮次和上下文，影响机器人正常聊天。

## 安装

1. 在 MaiBot WebUI「插件市场」搜索“搜索中枢”安装；或从 [Releases](https://github.com/kamionichan/maibot-search-hub/releases) 下载 zip，在「插件管理」中上传，或解压到 `plugins/kamionichan_search_hub/`。
2. 重启 MaiBot，或在插件管理中重新加载。
3. **不用填任何 key 就能用**：默认启用 Keenable、Tavily（免 key 模式）、trace.moe、AnimeTrace、Yandex、Bing。如果 Host 配置了联网服务，也会作为来源之一。
4. 想要更好的效果，再按 [KEYS.md](KEYS.md) 申请免费 key，填进 WebUI 插件配置里对应来源的节，**填上就生效**，不用另开开关。

插件只依赖 Host 自带的库（httpx、Pillow、jieba），不会额外安装依赖。

### 推荐的 Host 设置

搜索类回答句子较多。MaiBot 分句发送时，句子数超过 `response_splitter.max_sentence_num` 会把整条回复换成“不知道”“懒得说”之类的随机回复。建议在 `bot_config.toml` 里设置：

```toml
[response_splitter]
enable_overflow_return_all = true   # 句子太多时保留完整回复，不再换成默认回复
```

插件不会修改 Host 配置。

## 给 bot 用的工具

| 工具 | 作用 |
|---|---|
| `search` | 搜索。`query` 填搜索词；以图搜时 `image` 填图片所在消息的编号。`depth` 选 quick / normal / deep，`intent` 选问题类型 |
| `read_source` | 读取某条结果的正文（填 S 编号或网址），只返回与关注点相关的段落 |

两个工具都直接对 Planner 可见，不需要先 `tool_search`。

## 命令

| 命令 | 作用 | 谁能用 |
|---|---|---|
| `/search status` | 来源状态、冷却、今日额度、共存情况、旧插件提示 | 所有人 |
| `/search jobs` | 本会话最近的搜索任务 | 所有人 |
| `/search trace J3` | 某次搜索走了哪些来源、各用了多久 | 所有人 |
| `/search cancel J3` | 取消进行中的搜索 | 所有人 |
| `/search clear` | 清除本会话的搜索记录 | 操作员 |

WebUI 插件配置页顶部有“运行状态”和“清空全部搜索记录”按钮。配置分为五个标签页：常用、免费来源、需要 Key 的来源、以图搜图、高级；没填 Key 的来源默认折叠。

## 与其他搜索工具共存

配置页「常用 → 与其他搜索工具共存」：

| 对象 | 默认 | 说明 |
|---|---|---|
| Host 自带的 `web_search` / `web_fetch` | **隐藏** | 本插件已经通过同一个联网服务调用它，再暴露给模型只会重复。隐藏只是不让模型看到，不会改 Host 配置 |
| 其他插件的搜索工具 | **观察** | 不干预它们，只把它们搜到的网址收进本插件的编号，之后可以用 `read_source` 读取 |

隐藏和观察只在 bot 的主规划请求里生效，不影响子代理（子代理的工具是主规划明确交给它的）和其他请求。可以在 `extra_tools` / `ignore_tools` 里追加或排除工具名。

工具重名时 Host 的处理（按 MaiBot 1.3.18 源码）：

- **两个插件之间重名**：Host 只保留其中一个，另一个直接不出现，不会改名。比如另一个插件也叫 `search`，两边只有一个能被模型看到。
- **插件与 Host 自带工具或 MCP 工具重名**：后注册的一方被改名为 `plugin_<插件ID>_<原名>` 或 `mcp_<服务名>_<原名>`。例如某插件的 `web_search` 会变成 `plugin_xxx_web_search`，本插件能认出来，按“其他插件的搜索工具”处理；本插件自己的工具被改名时也会自动改用新名字，`/search status` 会提示。改名后超过 64 个字符的会被 Host 截断加哈希，那种名字认不出来，需要手动写进 `extra_tools`。

## 从旧版（kamionichan.search，MaiBot 聚合搜索 0.6.x）迁移

新插件 ID 不同，可以和旧插件同时安装，但**建议停用旧插件**：两个插件会争着处理同一个搜索请求。新插件检测到旧插件仍在运行时，会在日志、`/search status` 和配置页上提示。

旧配置不会自动迁移，对照如下：

| 旧配置 | 新配置 |
|---|---|
| `[search] service_url`、`api_key`（以图搜容器） | `[image] container_url`、`container_key`（可选；Yandex、Bing 已内置，不部署容器也能用） |
| `[search] default_engine` | 不再需要：插件按问题类型自动选引擎；容器引擎顺序见 `[image] container_engines` |
| `[search] allow_external_requests`、`[text] allow_external_requests` | 默认允许；关闭文字搜索用 `[search] allow_text_outbound`，关闭以图搜用 `[image] enabled` |
| `[text.providers.tavily] api_key` | `[tavily] api_key`（不填也能用免 key 模式） |
| `[text.providers.exa] api_key` | `[exa] api_key` |
| `[text.network] proxy_url` | `[search] proxy` |
| `[text.limits] max_per_day` 等 | 各来源节里的 `daily_limit` |
| `[text.cache] ttl_seconds` | `[search] cache_enabled`（按时效自动定缓存时长） |
| 工具 `text_web_search` | `search` |
| 工具 `image_search_engines`、`image_publication_search` | `search` 加 `image` 参数 |

## 以图搜图会把图片发到哪里

bot 调用以图搜时，插件先把图片清洗一遍（去掉 EXIF 和元数据、重新编码为 JPEG，必要时缩小到 2MB 以内），再按问题依次发给下面的服务，拿到确定结果就停：

| 服务 | 何时会用 | 怎么关 |
|---|---|---|
| trace.moe | 番剧截图 | `[image] tracemoe_enabled = false` |
| AnimeTrace | 动漫/游戏角色 | `[image] animetrace_enabled = false` |
| SauceNAO | 插画出处（填了 key 才用） | 清空 `saucenao_key` |
| Yandex、Bing | 相似图片、照片、上面几个都没结果时 | `[image] web_enabled = false` |
| 以图搜容器 | 只有你自己部署并填了地址才用 | 清空 `container_url` |

不想让图片外发，就把 `[image] enabled` 关掉。成人向结果默认过滤（`[image] allow_adult`）。

## 隐私与记录

- 搜索记录（结果编号、任务、调用轨迹）保存在插件数据目录的 `hub.sqlite3`，默认保留 30 天，轨迹保留 7 天；不保存网页全文，也不保存图片（只存哈希）。
- 记录始终保存；不需要时用配置页的“清空全部搜索记录”。清空不影响正在进行的搜索，它们完成后照常送回并记录。插件长期不重启时，也会在搜索时顺带按保留天数清理。
- 结果编号（S1、S2……）在每个聊天里只增不减：旧记录被清理或清空后，编号不会再分给别的网址。
- key 只保存在插件配置里。上游返回的错误文字里出现 key 时会被替换成 `***`；SauceNAO 只接受网址参数里的 key，插件在 httpx/httpcore 日志上挂了遮蔽过滤器，即使管理员打开这两个库的日志也看不到 key。
- “代理”留空时直连，不读取系统的 `HTTP_PROXY` 等环境变量。
- 读取网页按 Host `web.fetch` → fetch-url 插件 → 内置读取器的顺序尝试。插件对每个网址都先做公网地址校验；内置读取器还会逐跳校验跳转、限制大小。交给 Host 或 fetch-url 读取时，插件只做了第一步校验，后续跳转和大小限制由它们自己负责。

## 开发

运行测试：`python -m pytest`（需要 `maibot-plugin-sdk`、`httpx`、`Pillow`、`jieba`、`pytest-asyncio`）。打包：`python tools/build_zip.py`，生成 `dist/maibot-search-hub-<版本>.zip`。

## 许可

MIT。移植代码的来源和许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

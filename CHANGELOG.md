# 更新日志

## 0.5.5（2026-11-01）

- 新增插件图标。
- 重写 README：按使用场景介绍功能，新增常见问题；新增本更新日志。插件详情页的「更新日志」标签现在有内容了。
- 插件代码没有变化。

## 0.5.4（2026-11-01）

- 首个公开版本，发布到 GitHub。
- 安装包里附带 key 申请清单（KEYS.md）。

## 0.5.3（2026-11-01）

- 某个来源因为“本次搜索的付费次数用完”没被调用时，不再把结果标成“部分来源失败”。
- 改了某个来源的 key 后，立即解除它的冷却（之前鉴权失败后要等 24 小时）。
- 读取网页工具兼容更多参数写法，模型写成 `source`、`url` 也能读。
- 统计哪些搜索结果被用到了回复里。

## 0.5.2（2026-10-30）

修复一次独立代码审查发现的问题：

- 资源与安全：按解压后的大小限制下载；key 不会出现在错误信息和网络库日志里；“代理”留空时真正直连；组播等地址不再被当成公网地址。
- 结果更可靠：搜索超时或被取消时，保留已经拿到的结果；内部出错的搜索不再一直显示“进行中”；付费来源之间不再同时请求。
- 结果卡给出完整链接，方便注明出处。
- 结果编号（S1、S2……）只增不减，清空或过期后不会再分给别的网址；长期不重启也会定期清理过期记录。
- 关闭“读取网页原文”后，深度搜索也不再读正文。
- 子代理的搜索次数上限不能再被绕过；与其他搜索工具共存只作用于主对话；能认出被 MaiBot 改名后的其他插件搜索工具。
- 去掉“保存到磁盘”开关，搜索记录始终保存（可以随时清空）。清空时正在进行的搜索不受影响。

## 0.5.1（2026-10-29）

- 配置页全部中文化，分为常用、免费来源、需要 Key 的来源、以图搜图、高级五个标签页。
- 修复插件装好但还没重启时，配置页反复跳动的问题。

## 0.5.0（2026-10-29）

第一个完整版本，替代旧插件 `kamionichan.search`（MaiBot 聚合搜索 0.6.x）和 maibot-picimagesearch。

- 两个工具：`search`（文字搜索和以图搜图）和 `read_source`（读原文）。
- 多来源编排：按语言和问题类型挑来源，失败自动换源，按免费额度控制每日用量。
- 慢搜索转后台，查到后自动发回对话。
- 以图搜图内置 trace.moe、AnimeTrace、SauceNAO、Yandex、Bing。
- 与 MaiBot 自带搜索和其他搜索插件共存。

### 从旧插件 kamionichan.search 迁移

新插件 ID 不同，可以和旧插件同时安装，但**建议停用旧插件**，否则两个插件会争着处理同一个搜索请求。新插件检测到旧插件仍在运行时，会在日志、`/search status` 和配置页上提示。

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

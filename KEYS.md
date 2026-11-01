# 来源 key 申请清单

额度与价格以各家官网为准（下面是 2026 年 10 月底的情况）。

填写位置：MaiBot WebUI → 插件 → 搜索中枢 → 对应来源的配置页 →「API Key」；
或插件目录下 `config.toml` 的 `[来源名]` 节，例如 `[exa]` 下 `api_key = "..."`。**填上就生效**，不用另开开关。

每个来源还有 `daily_limit`（每日上限），已按免费额度折算好默认值，0 表示不限。

## 不需要 key（装上就能用）

| 来源 | 说明 |
|---|---|
| Keenable | 免 key 每个 IP 每小时 1000 次；填 key 后每月 10 万次（https://keenable.ai，key 以 `keen_` 开头） |
| Tavily（免 key 模式） | 不填 key 时自动使用，有限流 |
| SearXNG | 填你自己部署的地址即可（`[searxng] base_url`） |
| MaiBot 联网服务 | 在 MaiBot 自己的联网设置里配置，插件会作为来源之一使用 |
| trace.moe、AnimeTrace、Yandex、Bing | 以图搜图，免 key |

## 建议申请（免费额度够日常用，不需要绑卡）

| 来源 | 擅长 | 免费额度 | 申请地址 | 配置节 |
|---|---|---|---|---|
| 百度千帆 AI 搜索 | 中文 | 每月 1500 次（需实名） | https://console.bce.baidu.com/qianfan | `[baidu]` |
| Exa | 英文、技术、学术 | 每月 $10 credits（约 2000 次） | https://dashboard.exa.ai | `[exa]` |
| Tavily | 英文 | 每月 1000 次 | https://app.tavily.com | `[tavily]` |
| 博查 | 中文 | 注册送 1000 次（一次性） | https://open.bochaai.com | `[bocha]` |
| Firecrawl | 搜索 + 网页读取 | 每月 1000 credits（约 500 次搜索） | https://www.firecrawl.dev | `[firecrawl]` |
| LangSearch | 兜底 | 每日免费额度；中文效果一般 | https://langsearch.com | `[langsearch]` |
| SauceNAO | 插画出处、作者（以图搜图） | 免费账号 | https://saucenao.com/user.php 注册后在 api 页面拿 key | `[image] saucenao_key` |

## 可选（有条件或收费）

| 来源 | 说明 | 申请地址 | 配置节 |
|---|---|---|---|
| Brave Search API | 独立英文索引；每月 $5 credits，**需要绑信用卡** | https://api-dashboard.search.brave.com | `[brave]` |
| Parallel | AI 搜索；每月 $5 积分，**需要绑卡** | https://platform.parallel.ai | `[parallel]` |
| Linkup | AI 搜索；注册送 $10（一次性） | https://app.linkup.so | `[linkup]` |
| Serper | Google 结果；一次性 2500 次 | https://serper.dev | `[serper]` |
| Perplexity Search API | 返回搜索结果（不是问答）；无免费额度，按次计费 | https://www.perplexity.ai/settings/api | `[perplexity]` |
| 阿里云百炼（千问联网搜索） | Agent 搜索，只在深度搜索或讨论类问题、结果不足时使用；搜索费约 ¥3/千次，另加模型 token 费，没有免费的搜索额度 | https://bailian.console.aliyun.com | `[qwen]` |

## 以图搜图容器（可选）

想用 Google Lens、百度识图、TinEye 等没有内置的引擎，可以自己部署一个兼容的以图搜图服务，在 `[image] container_url`、`container_key` 里填地址和 key。不部署也不影响内置引擎。

# 来源 key 申请清单

额度与价格以各家官网为准（下面是 2026 年 10 月底到 11 月初核对的情况）。

填写位置：MaiBot WebUI → 插件 → 搜索中枢 →「搜索来源」里对应来源的「API Key」；
或插件目录下 `config.toml` 的 `[来源名]` 节，例如 `[exa]` 下 `api_key = "..."`。**填上就生效**，不用另开开关。

需要 key 的搜索来源还有 `daily_limit`（每日上限），默认值按免费额度折算，0 表示不限。它只限制本插件每天的调用次数，不是账户的账单上限：插件不知道同一个 key 在别处用了多少，也不知道厂商的计费周期。Keenable 和以图搜图来源不经过这个上限，例外是 SerpApi（Google Lens，默认每天 8 次）。

**“网络”一列是一般情况**：“国内”= 国内服务，中国大陆通常能直连；“境外”= 境外服务，中国大陆通常需要在「常用 → HTTP 代理」填代理。实际以你的服务器能不能访问为准，详见 README 的「网络环境与代理」。

## 不需要 key（装上就能用）

| 来源 | 网络 | 说明 |
|---|---|---|
| Keenable | 境外 | 免 key 每个 IP 每小时 1000 次；填 key 后每月 10 万次（https://keenable.ai，key 以 `keen_` 开头）。填 key 的账户如果开了自动充值，超出后会继续计费 |
| Tavily（免 key 模式） | 境外 | 不填 key 时自动使用，有限流 |
| SearXNG | 自建 | 填你自己部署的地址即可（`[searxng] base_url`）。它再去访问哪些搜索引擎，由它自己的网络决定 |
| MaiBot 联网服务 | 跟随 MaiBot | 在 MaiBot 自己的联网设置里配置，插件会作为来源之一使用 |
| trace.moe、Yandex、Bing | 境外 | 以图搜图，免 key |
| AnimeTrace、百度识图 | 国内 | 以图搜图，免 key |

## 建议申请（免费额度够日常用，不需要绑卡）

| 来源 | 网络 | 擅长 | 免费额度 | 申请地址 | 配置节 |
|---|---|---|---|---|---|
| 百度千帆 AI 搜索 | 国内 | 中文；国内服务器首选 | 每月 1500 次（需实名） | https://console.bce.baidu.com/qianfan | `[baidu]` |
| Exa | 境外 | 英文、技术、学术 | 每月 $10 credits，每月 1 日重置。本插件用 Auto 搜索（$7/千次），约 1400 次；默认每天 45 次 | https://dashboard.exa.ai | `[exa]` |
| Tavily | 境外 | 英文 | 每月 1000 次 | https://app.tavily.com | `[tavily]` |
| 博查 | 国内 | 中文 | 注册送 1000 次（一次性） | https://open.bochaai.com | `[bocha]` |
| Firecrawl | 境外 | 搜索 | 每月 1000 credits（约 500 次搜索）。插件只用它搜索；读网页原文走 MaiBot 联网服务等，不用 Firecrawl | https://www.firecrawl.dev | `[firecrawl]` |
| LangSearch | 境外 | 兜底 | 每日免费额度（按 token 计，UTC 0 点重置）；中文效果一般 | https://langsearch.com | `[langsearch]` |
| SauceNAO | 境外 | 插画出处、作者（以图搜图） | 免费账号 | https://saucenao.com/user.php 注册后在 api 页面拿 key | `[image] saucenao_key` |
| SerpApi | 境外 | Google Lens（以图搜图）：照片、实物、插画的网页出处和完全匹配 | 每月 250 次，不用绑卡；插件默认每天最多 8 次 | https://serpapi.com/users/sign_up 注册后在 Dashboard 拿 API Key | `[image] serpapi_key` |

## 可选（有条件或收费）

| 来源 | 网络 | 说明 | 申请地址 | 配置节 |
|---|---|---|---|---|
| Brave Search API | 境外 | 独立英文索引；每月 $5 credits，**需要绑信用卡** | https://api-dashboard.search.brave.com | `[brave]` |
| Parallel | 境外 | AI 搜索；每月 $5 积分，**需要绑卡** | https://platform.parallel.ai | `[parallel]` |
| Linkup | 境外 | AI 搜索；免费额度和资格条件见官网（作者没有申请） | https://app.linkup.so | `[linkup]` |
| Serper | 境外 | Google 结果；注册有一次性免费次数，以官网为准 | https://serper.dev | `[serper]` |
| Perplexity Search API | 境外 | 返回搜索结果（不是问答）；没有常规的每月免费额度，按次计费，以账户权益为准 | https://www.perplexity.ai/settings/api | `[perplexity]` |
| 阿里云百炼（千问联网搜索） | 国内 | Agent 搜索，主要在深度搜索或讨论类问题、结果不足时作为补充；搜索费约 ¥3/千次（北京地域），另加模型 token 费，没有免费的搜索额度 | https://bailian.console.aliyun.com | `[qwen]` |

## Google Lens 浏览器（可选）

不想申请 SerpApi，也可以自己部署 [camofox-browser](https://github.com/jo-inc/camofox-browser)（反指纹的 Firefox 浏览器服务，MIT），在 `[image] camofox_url` 填它的地址（设了 `CAMOFOX_ACCESS_KEY` 或 `CAMOFOX_API_KEY` 的话 `camofox_key` 也填上；前者保护全部接口，后者只保护部分接口，以你部署的版本为准。地址会收到上传的图片，跨机器时优先用 HTTPS）。

- 插件只用它固定地打开 Google Lens、上传图片、读结果，麦麦不能直接操作这个浏览器。
- **camofox 所在机器要能访问 Google**——插件的 HTTP 代理不经过 camofox。
- Google 连续查十几次就会要求验证，所以插件默认每小时最多查 6 次、两次之间至少隔 60 秒（再加随机间隔），遇到验证会暂停 2 小时，不尝试绕过。可在「高级 → 以图搜图限速」调整。

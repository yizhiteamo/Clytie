# 新闻订阅

`recent_news` 是轻量工具，从最近七天的订阅条目中返回标题、链接、可核实的发布时间和摘要。它可以单独运行。启用 `proactive-chat` 后，插件还会把突发新闻和每日简报候选交给主人的私聊。

## 默认来源

以下地址由发布者提供。GitHub 项目搜索使用最近七天的创建日期和 AI 主题作为查询条件。Hacker News 从首页列表取故事，再从官方接口读取条目热度。

| 来源 | 检查间隔 | 订阅地址 |
|---|---|---|
| OpenAI 新闻 | 10 分钟 | [RSS](https://openai.com/news/rss.xml) |
| GitHub 博客 | 10 分钟 | [RSS](https://github.blog/feed/) |
| Hugging Face 博客 | 10 分钟 | [RSS](https://huggingface.co/blog/feed.xml) |
| TechCrunch AI | 10 分钟 | [RSS](https://techcrunch.com/category/artificial-intelligence/feed/) |
| Hacker News | 10 分钟 | [首页列表](https://hacker-news.firebaseio.com/v0/topstories.json) |
| Hugging Face 每日论文 | 60 分钟 | [论文列表](https://huggingface.co/api/daily_papers) |
| GitHub 新项目 | 60 分钟 | [仓库搜索接口](https://api.github.com/search/repositories) |
| arXiv 人工智能、计算与语言、机器学习 | 60 分钟 | [cs.AI](https://rss.arxiv.org/rss/cs.AI)、[cs.CL](https://rss.arxiv.org/rss/cs.CL)、[cs.LG](https://rss.arxiv.org/rss/cs.LG) |

## 选择来源和主题

`sources` 可以替换默认列表，每个来源有独立的 `id`、`type` 和 `url`。需要跟踪某个 GitHub 仓库的新版本时，把该仓库的 `releases.atom` 作为 `rss` 来源添加。大公司的官方发布源可设 `major: true`。GitHub 搜索可选用 `github_credential` 配置的凭据引用。

`exclude` 按标题和摘要排除主题。默认来源偏向科技和 AI，Hacker News 的综合首页另按主题筛选。设置 `interests` 可以进一步限定关心的主题。

## 热度与用量

大公司发布、两小时内三个不同网站报道同一事件、或 Hacker News 首页前三且每小时涨至少 60 分，会成为突发候选。判断只使用公开的标题、来源和摘要。判断为否或失败的候选进入简报。论文按点赞数、GitHub 新项目按单位时间涨星量，各来源取前三。Hacker News 首页前十和跨来源的同一论文或项目也可进入简报。

突发候选逐件判断。宿主对同一插件每分钟最多接受十次模型调用；集中到达的候选若被限流，就进入简报。未启用主动私聊时，订阅和 `recent_news` 仍然可用，插件不会为主动提醒调用模型。

插件通过受限的公共 HTTP 后端收取订阅，单个响应上限为 16 MiB。Hacker News 每轮最多查看首页前 30 条。状态页 `GET /plugins/news-feeds/status` 可查看各源最近读取情况和当天的分档数量。

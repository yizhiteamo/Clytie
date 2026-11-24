"""Public feeds and APIs. Tracked GitHub release feeds are deployment choices."""

SOURCES = (
    {"id": "openai-news", "type": "rss", "url": "https://openai.com/news/rss.xml", "major": True},
    {"id": "github-blog", "type": "rss", "url": "https://github.blog/feed/", "major": True},
    {"id": "hf-blog", "type": "rss", "url": "https://huggingface.co/blog/feed.xml", "major": True},
    {
        "id": "techcrunch-ai",
        "type": "rss",
        "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
    },
    {
        "id": "hn-front",
        "type": "hackernews",
        "url": "https://hacker-news.firebaseio.com/v0/topstories.json",
    },
    {"id": "hf-papers", "type": "hf_papers", "url": "https://huggingface.co/api/daily_papers"},
    {
        "id": "github-rising",
        "type": "github_rising",
        "url": "https://api.github.com/search/repositories",
    },
    *(
        {
            "id": "arxiv-" + category.lower(),
            "type": "arxiv",
            "url": "https://rss.arxiv.org/rss/cs." + category,
        }
        for category in ("AI", "CL", "LG")
    ),
)

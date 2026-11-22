# -*- coding: utf-8 -*-
"""抓取当日新闻，生成 data/news.json。

板块：要闻 / 国际 / 社会 / 科技 / 体育 / 热榜
只用公开 RSS 和热搜接口，每条保留标题 + 最多 500 字正文摘要。

正文策略：
  - RSS 里 description 足够长（>=300 字）就直接截取
  - 太短的（中新网 69 字、量子位 20 字）去抓文章页正文
  - 抓取失败就退回用 description，宁可短也不空着

选稿策略（重要）：
  先把每个源的候选池全部读出来，**跨源去重之后再按配额取**，不够的从池子深处补。
  否则"社会"这种和"要闻"高度重复的板块，前 6 条全被去重吃掉，只剩空壳。
"""

import datetime
import html as htmllib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_JSON = ROOT / "data" / "news.json"

TZ = datetime.timezone(datetime.timedelta(hours=8))
NOW = datetime.datetime.now(TZ)

BODY_MAX = 500          # 每条正文上限（字）
DESC_ENOUGH = 300       # description 超过这个字数就不用再抓网页了
POOL_FACTOR = 4         # 每个源读候选池的倍数，给去重留余量

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# ---------------- 数据源 ----------------
# quota：最终想要几条；hours：只收这个时间范围内的。
FEEDS = [
    dict(cat="要闻", name="中国新闻网", url="https://www.chinanews.com.cn/rss/scroll-news.xml", quota=8, hours=30),
    dict(cat="要闻", name="中国新闻网", url="https://www.chinanews.com.cn/rss/china.xml", quota=6, hours=30),
    dict(cat="国际", name="中国新闻网", url="https://www.chinanews.com.cn/rss/world.xml", quota=6, hours=30),
    dict(cat="社会", name="中国新闻网", url="https://www.chinanews.com.cn/rss/society.xml", quota=6, hours=30),
    dict(cat="科技", name="IT之家", url="https://www.ithome.com/rss/", quota=6, hours=30),
    dict(cat="科技", name="量子位", url="https://www.qbitai.com/feed", quota=3, hours=48),
    dict(cat="科技", name="爱范儿", url="https://www.ifanr.com/feed", quota=3, hours=48),
    dict(cat="体育", name="中国新闻网", url="https://www.chinanews.com.cn/rss/sports.xml", quota=5, hours=30),
]

# 板块顺序（决定页面里先出现什么）
CAT_ORDER = ["要闻", "国际", "社会", "科技", "体育", "热榜"]

HOT_QUOTA = 8

# 热榜里的娱乐八卦过滤（只看标题）
ENT_BLACK = ["明星", "综艺", "演唱会", "恋情", "塌房", "粉丝", "爱豆", "偶像",
             "红毯", "杀青", "开播", "新剧", "离婚", "求婚", "领证", "爆改",
             "造型", "生图", "路透", "cp", "塌了"]

# 正文页容器（按域名），命中后只取该区域里的 <p>
CONTAINERS = {
    "chinanews.com.cn": r'<div[^>]*class="[^"]*\bleft_zw\b[^"]*"',
    "qbitai.com": r'<div[^>]*class="[^"]*(?:entry-content|article-content|post-content)[^"]*"',
    "ifanr.com": r'<div[^>]*class="[^"]*(?:entry-content|article-content|post-content)[^"]*"',
}

TAG_RE = re.compile(r"<[^>]+>")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
WS_RE = re.compile(r"\s+")

# 判断一段文字是不是抓成了网页导航
NAV_WORDS = ("首页", "登录", "注册", "下载", "讨论", "播客", "商店", "关注",
             "收藏", "历史", "专题", "预告", "会员", "购物", "分享到")


def looks_like_nav(text: str) -> bool:
    head = text[:80]
    return sum(1 for w in NAV_WORDS if w in head) >= 3


def get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", s or "")
    s = COMMENT_RE.sub(" ", s)
    s = TAG_RE.sub("", s)
    s = htmllib.unescape(s)
    s = s.replace("\u3000", " ")
    return WS_RE.sub(" ", s).strip()


def clip(text: str, limit: int = BODY_MAX) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for ch in "。！？；":
        p = cut.rfind(ch)
        if p > limit * 0.6:
            return cut[:p + 1]
    return cut.rstrip("，、；： ") + "…"


def extract_page(url: str) -> str:
    """从文章页抓正文段落。失败返回空串。"""
    host = re.sub(r"^www\.", "", urllib.parse.urlparse(url).netloc)
    html_text = get(url).decode("utf-8", "ignore")
    html_text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html_text)
    html_text = COMMENT_RE.sub(" ", html_text)

    region = html_text
    for domain, p in CONTAINERS.items():
        if host.endswith(domain):
            m = re.search(p, html_text)
            if m:
                region = html_text[m.start(): m.start() + 60000]
            break

    paras = []
    for raw in re.findall(r"(?is)<p[^>]*>(.*?)</p>", region):
        t = strip_html(raw)
        if len(t) < 12:
            continue
        if re.search(r"责任编辑|版权|免责声明|扫一扫|原标题|举报|相关阅读|分享到", t):
            break
        paras.append(t)
        if sum(len(x) for x in paras) >= BODY_MAX * 1.5:
            break
    return clip("".join(paras))


def fmt_hot(v) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    if n >= 100000000:
        return "%.1f亿" % (n / 100000000)
    if n >= 10000:
        return "%.1f万" % (n / 10000)
    return str(int(n))


def dedupe_key(title: str) -> str:
    return re.sub(r"\s+", "", title)[:20]


# ---------------------------------------------------------------- 百度热搜
def fetch_hot() -> list:
    raw = get("https://top.baidu.com/api/board?platform=pc&tab=realtime")
    data = json.loads(raw)
    out, skipped = [], 0
    for card in data["data"]["cards"]:
        for it in card.get("content", []):
            title = strip_html(it.get("word") or it.get("query") or it.get("title") or "")
            if not title:
                continue
            if any(k in title.lower() for k in ENT_BLACK):
                skipped += 1
                continue
            out.append({
                "cat": "热榜", "time": "", "source": "百度热搜", "title": title[:40],
                "summary": clip(strip_html(it.get("desc") or ""), 180),
                "hot": fmt_hot(it.get("hotScore")),
                "url": it.get("appUrl") or it.get("url") or "",
            })
    print("      热榜过滤掉 %d 条娱乐内容" % skipped)
    return out[:HOT_QUOTA]


# ---------------------------------------------------------------- RSS
def parse_feed(feed: dict) -> list:
    """只解析 RSS，不抓正文，返回候选池（按时间倒序）。"""
    since = NOW - datetime.timedelta(hours=feed["hours"])
    limit = feed["quota"] * POOL_FACTOR
    root = ET.fromstring(get(feed["url"]))
    rows = []
    for it in root.iter("item"):
        title = strip_html(it.findtext("title") or "")[:40]
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        try:
            pub = parsedate_to_datetime((it.findtext("pubDate") or "").strip())
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=TZ)
            pub = pub.astimezone(TZ)
        except Exception:
            continue
        if pub < since:
            continue
        rows.append({"title": title, "link": link, "pub": pub,
                     "desc": strip_html(it.findtext("description") or "")})
        if len(rows) >= limit:
            break
    rows.sort(key=lambda x: x["pub"], reverse=True)
    return rows


def make_item(feed: dict, row: dict) -> dict:
    """给一条候选补上正文。"""
    body = ""
    if len(row["desc"]) >= DESC_ENOUGH:
        body = clip(row["desc"])
    else:
        try:
            body = extract_page(row["link"])
        except Exception:
            body = ""
        time.sleep(0.4)          # 别把人家站点打疼
        if len(body) < len(row["desc"]) or looks_like_nav(body):
            body = clip(row["desc"]) if row["desc"] else body

    if looks_like_nav(body):
        body = ""
    return {
        "cat": feed["cat"], "time": row["pub"].strftime("%m-%d %H:%M"),
        "source": feed["name"], "title": row["title"], "summary": body,
        "hot": "", "url": row["link"],
    }


def main() -> int:
    errors, all_items = [], []

    try:
        hot = fetch_hot()
        print("[OK]   热榜        %d 条" % len(hot))
        all_items.extend(hot)
    except Exception as e:
        print("[FAIL] 热榜        %r" % e)
        errors.append("热榜: %r" % e)
    claimed = {dedupe_key(x["title"]) for x in all_items}

    # 第一步：把所有源的候选池读进来（很快，只解析 XML）
    pools = []
    for f in FEEDS:
        try:
            rows = parse_feed(f)
            pools.append((f, rows))
            print("[池]   %-4s %-6s 候选 %2d 条" % (f["cat"], f["name"], len(rows)))
        except Exception as e:
            print("[FAIL] %-4s %-6s %r" % (f["cat"], f["name"], e))
            errors.append("%s-%s: %r" % (f["cat"], f["name"], e))

    # 第二步：跨源去重，不够的从池子深处补，然后再抓正文
    picked = []
    for f, rows in pools:
        n = 0
        for row in rows:
            if n >= f["quota"]:
                break
            key = dedupe_key(row["title"])
            if key in claimed:
                continue
            claimed.add(key)
            picked.append((f, row))
            n += 1
        print("[选]   %-4s %-6s 选中 %2d 条（配额 %d）" % (f["cat"], f["name"], n, f["quota"]))

    for f, row in picked:
        try:
            item = make_item(f, row)
        except Exception:
            continue
        if len(item["summary"]) < 40:
            continue                      # 正文太短，宁可不收
        all_items.append(item)

    if len(all_items) <= HOT_QUOTA:
        print("几乎全部源都失败，保留上一次的数据。", file=sys.stderr)
        return 1

    order = {c: i for i, c in enumerate(CAT_ORDER)}
    all_items.sort(key=lambda x: order.get(x["cat"], 99))   # 稳定排序，板块内保持原序

    for i, it in enumerate(all_items, 1):
        it["id"] = "a%02d" % i

    payload = {
        "_note": "内容来自公开 RSS 与热搜接口，各保留标题与正文摘要，仅供个人阅读。",
        "site": "口袋新闻",
        "date": NOW.strftime("%m-%d"),
        "weekday": "周" + "一二三四五六日"[NOW.weekday()],
        "updated": NOW.strftime("%Y-%m-%d %H:%M"),
        "items": all_items,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    stat = {}
    for it in all_items:
        stat[it["cat"]] = stat.get(it["cat"], 0) + 1
    lens = [len(x["summary"]) for x in all_items]
    print("共 %d 条：%s" % (len(all_items),
                          " / ".join("%s %d" % (c, stat.get(c, 0)) for c in CAT_ORDER)))
    print("正文长度 平均 %d 字 / 最短 %d / 最长 %d"
          % (sum(lens) // len(lens), min(lens), max(lens)))
    print("写入 %s" % OUT_JSON)
    if errors:
        print("警告：%d 个源失败，已用其余源生成。" % len(errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

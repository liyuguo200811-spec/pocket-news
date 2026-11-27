# -*- coding: utf-8 -*-
"""抓取当日新闻，生成 data/news.json。

板块：要闻 / 国际 / 科技 / 深度 / 热榜

深度板块由三路凑成：
  1. 当天科技长文（正文 >= 900 字，来自极客公园 / 爱范儿 / IT之家 / 量子位）
  2. 知乎日报（非时效，可翻历史；剔除"瞎扯"栏目和 600 字以下的图集）
  3. 少数派（长文，正文 >= 1000 字；剔除"派早报"新闻汇总）

正文用换行保留段落结构，build.py 再按 600 字一页切分。
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

# 天气：改成你所在城市，用拼音或英文，例如 shanghai / chengdu / wuhan。
# 留空字符串 "" 就不抓天气。抓不到也不会影响其他内容。
CITY = "beibei"

# 倒计时：名称 + 目标日期。过去的自动跳过，写几组都行。
COUNTDOWNS = [("高考", "2027-06-07")]

# wttr.in 的天气代码 → 中文（它的中文翻译不全，雾霾天会直接给英文）
WCODE = {
    "113": "晴", "149": "雾霾", "116": "多云", "119": "阴", "122": "阴", "143": "雾",
    "248": "雾", "260": "雾", "176": "阵雨", "263": "小雨", "266": "小雨",
    "293": "小雨", "296": "小雨", "299": "中雨", "302": "中雨", "305": "大雨",
    "308": "大雨", "353": "小阵雨", "356": "阵雨", "359": "暴雨",
    "179": "雨夹雪", "182": "雨夹雪", "185": "冻雨", "281": "冻雨",
    "284": "冻雨", "311": "冻雨", "314": "冻雨", "200": "雷阵雨",
    "386": "雷阵雨", "389": "雷阵雨", "227": "雪", "230": "暴雪",
    "323": "小雪", "326": "小雪", "329": "中雪", "332": "中雪",
    "335": "大雪", "338": "大雪", "368": "阵雪", "371": "阵雪",
}

TZ = datetime.timezone(datetime.timedelta(hours=8))
NOW = datetime.datetime.now(TZ)

BODY_MAX = 500          # 普通新闻正文上限
BODY_MAX_DEEP = 1800    # 深度文章正文上限
SSPAI_MAX = 3000        # 少数派单独放宽
DEEP_MIN = 900          # 当天稿正文超过这个字数才算"深度"
DESC_ENOUGH = 300
POOL_FACTOR = 4

DEEP_DAY_QUOTA = 4      # 深度里：当天科技长文
DEEP_ZHIHU_QUOTA = 3    # 深度里：知乎日报
DEEP_SSPAI_QUOTA = 2    # 深度里：少数派

ZH_MIN = 600            # 知乎日报正文低于这个字数直接丢（实测"瞎扯"都是 359~507）
ZH_BLACK = ["瞎扯", "吐槽", "图集", "广告", "推广", "招聘", "冷知识合集"]
SSPAI_MIN = 1000
# 少数派的清单/促销/资讯类不要，只要真正的长文
SSPAI_BLACK = ("派早报", "值得买", "本周看什么", "优惠", "折扣", "促销",
               "好价", "清单", "特价", "开箱", "促销季")

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

FEEDS = [
    dict(cat="要闻", name="中国新闻网", url="https://www.chinanews.com.cn/rss/scroll-news.xml", quota=3, hours=30),
    dict(cat="要闻", name="中国新闻网", url="https://www.chinanews.com.cn/rss/china.xml", quota=3, hours=30),
    dict(cat="国际", name="中国新闻网", url="https://www.chinanews.com.cn/rss/world.xml", quota=7, hours=30),
    dict(cat="科技", name="IT之家", url="https://www.ithome.com/rss/", quota=7, hours=30, deep=True),
    dict(cat="科技", name="量子位", url="https://www.qbitai.com/feed", quota=4, hours=48, deep=True),
    dict(cat="科技", name="爱范儿", url="https://www.ifanr.com/feed", quota=4, hours=48, deep=True),
    dict(cat="科技", name="极客公园", url="https://www.geekpark.net/rss", quota=4, hours=48, deep=True),
]

CAT_ORDER = ["要闻", "国际", "科技", "深度", "热榜"]
CAT_LIMIT = {"科技": 12}

HOT_QUOTA = 8

ENT_BLACK = ["明星", "综艺", "演唱会", "恋情", "塌房", "粉丝", "爱豆", "偶像",
             "红毯", "杀青", "开播", "新剧", "离婚", "求婚", "领证", "爆改",
             "造型", "生图", "路透", "cp", "塌了"]

CONTAINERS = {
    "chinanews.com.cn": r'<div[^>]*class="[^"]*\bleft_zw\b[^"]*"',
    "qbitai.com": r'<div[^>]*class="[^"]*(?:entry-content|article-content|post-content)[^"]*"',
    "ifanr.com": r'<div[^>]*class="[^"]*(?:entry-content|article-content|post-content)[^"]*"',
    "geekpark.net": r'<div[^>]*class="[^"]*(?:article-content|entry-content|post-content)[^"]*"',
}

TAG_RE = re.compile(r"<[^>]+>")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
WS_RE = re.compile(r"\s+")

NAV_WORDS = ("首页", "登录", "注册", "下载", "讨论", "播客", "商店", "关注",
             "收藏", "历史", "专题", "预告", "会员", "购物", "分享到")


def looks_like_nav(text: str) -> bool:
    return sum(1 for w in NAV_WORDS if w in text[:80]) >= 3


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


def extract_paras(html_text: str) -> list:
    """按 <p> 抽段落，保留分段信息（用 \\n 连接）。"""
    out = []
    for raw in re.findall(r"(?is)<p[^>]*>(.*?)</p>", html_text or ""):
        t = strip_html(raw)
        if len(t) < 8:
            continue
        if re.search(r"责任编辑|版权|免责声明|扫一扫|原标题|举报|相关阅读|分享到|查看知乎原文", t):
            continue
        out.append(t)
    return out


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


def extract_page(url: str, limit: int = BODY_MAX) -> str:
    host = re.sub(r"^www\.", "", urllib.parse.urlparse(url).netloc)
    html_text = get(url).decode("utf-8", "ignore")
    html_text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html_text)
    html_text = COMMENT_RE.sub(" ", html_text)

    region = html_text
    for domain, p in CONTAINERS.items():
        if host.endswith(domain):
            m = re.search(p, html_text)
            if m:
                region = html_text[m.start(): m.start() + 90000]
            break
    return clip("\n".join(extract_paras(region)), limit)


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


def fetch_weather() -> str:
    """抓当前城市天气，返回类似「晴 3~9℃」的短句。失败返回空串。"""
    if not CITY:
        return ""
    d = json.loads(get("https://wttr.in/%s?format=j1&lang=zh" % CITY, timeout=15))
    cur = d["current_condition"][0]
    desc = WCODE.get(str(cur.get("weatherCode", "")), "")
    if not desc and cur.get("lang_zh"):
        desc = cur["lang_zh"][0].get("value", "")
    if not desc:
        desc = cur["weatherDesc"][0]["value"]
    today = d["weather"][0]
    return "%s %s~%s℃" % (desc, today["mintempC"], today["maxtempC"])


def build_countdown() -> str:
    """返回「距高考192天」，多组用 · 连接。"""
    parts = []
    for name, ds in COUNTDOWNS:
        try:
            d = datetime.date.fromisoformat(ds)
        except Exception:
            continue
        n = (d - NOW.date()).days
        if n >= 0:
            parts.append("距%s%d天" % (name, n))
    return "·".join(parts)


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


# ---------------------------------------------------------------- 知乎日报
def fetch_zhihu(quota: int) -> list:
    """从今天往前翻，凑够 quota 篇合格长文。知乎日报每天 4 篇，其中 1 篇是"瞎扯"。"""
    out, scanned = [], 0
    for back in range(1, 60):
        day = NOW - datetime.timedelta(days=back)
        ds = day.strftime("%Y%m%d")
        try:
            js = json.loads(get("https://news-at.zhihu.com/api/4/news/before/%s" % ds))
        except Exception:
            continue
        scanned += 1
        for s in js.get("stories", []):
            title = (s.get("title") or "").strip()
            if not title or any(k in title for k in ZH_BLACK):
                continue
            try:
                det = json.loads(get("https://news-at.zhihu.com/api/4/news/%s" % s["id"]))
            except Exception:
                continue
            time.sleep(0.2)
            paras = extract_paras(det.get("body", ""))
            text = "\n".join(paras)
            if len(text) < ZH_MIN:
                continue
            out.append({
                "cat": "深度", "time": day.strftime("%m-%d"), "source": "知乎日报",
                "title": title[:40], "summary": clip(text, BODY_MAX_DEEP),
                "hot": "", "url": det.get("share_url") or s.get("url") or "",
            })
            if len(out) >= quota:
                return out
        if scanned >= 12 and not out:      # 翻了 12 天还一篇都没有，别死磕
            break
    return out


# ---------------------------------------------------------------- 少数派
def fetch_sspai(quota: int) -> list:
    js = json.loads(get("https://sspai.com/api/v1/article/index/page/get?limit=20&offset=0"))
    rows = js.get("data") or []
    out = []
    for r in rows:
        title = (r.get("title") or "").strip()
        if not title or any(k in title for k in SSPAI_BLACK):
            continue
        try:
            det = json.loads(get("https://sspai.com/api/v1/article/info/get?id=%s&view=second"
                                 % r.get("id")))
        except Exception:
            continue
        time.sleep(0.25)
        paras = extract_paras((det.get("data") or {}).get("body", ""))
        text = "\n".join(paras)
        if len(text) < SSPAI_MIN:
            continue
        ts = r.get("released_time") or r.get("created_time") or 0
        try:
            tstr = datetime.datetime.fromtimestamp(int(ts), TZ).strftime("%m-%d")
        except Exception:
            tstr = ""
        out.append({
            "cat": "深度", "time": tstr, "source": "少数派",
            "title": title[:40], "summary": clip(text, SSPAI_MAX),
            "hot": "", "url": "https://sspai.com/post/%s" % r.get("id"),
        })
        if len(out) >= quota:
            break
    return out


# ---------------------------------------------------------------- RSS
def parse_feed(feed: dict) -> list:
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
    limit = BODY_MAX_DEEP if feed.get("deep") else BODY_MAX
    body = ""
    if len(row["desc"]) >= DESC_ENOUGH:
        body = clip(row["desc"], limit)
    else:
        try:
            body = extract_page(row["link"], limit)
        except Exception:
            body = ""
        time.sleep(0.4)
        if len(body) < len(row["desc"]) or looks_like_nav(body):
            body = clip(row["desc"], limit) if row["desc"] else body
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

    pools = []
    for f in FEEDS:
        try:
            rows = parse_feed(f)
            pools.append((f, rows))
        except Exception as e:
            print("[FAIL] %-4s %-6s %r" % (f["cat"], f["name"], e))
            errors.append("%s-%s: %r" % (f["cat"], f["name"], e))

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
            continue
        all_items.append(item)

    # ---- 当天科技稿按长度分流 ----
    tech = [x for x in all_items if x["cat"] == "科技"]
    deep_day = sorted([x for x in tech if len(x["summary"]) >= DEEP_MIN],
                      key=lambda x: len(x["summary"]), reverse=True)[:DEEP_DAY_QUOTA]
    for x in deep_day:
        x["cat"] = "深度"
    deep_ids = {id(x) for x in deep_day}
    rest = [x for x in tech if id(x) not in deep_ids]
    over = len(rest) - CAT_LIMIT.get("科技", 99)
    if over > 0:
        for x in rest[-over:]:
            all_items.remove(x)

    # ---- 知乎日报 + 少数派 ----
    for label, fn, q in (("知乎日报", fetch_zhihu, DEEP_ZHIHU_QUOTA),
                         ("少数派", fetch_sspai, DEEP_SSPAI_QUOTA)):
        try:
            got = fn(q)
            print("[OK]   %-8s %d 条（正文均 %d 字）"
                  % (label, len(got), sum(len(x["summary"]) for x in got) // max(1, len(got))))
            all_items.extend(got)
        except Exception as e:
            print("[FAIL] %-8s %r" % (label, e))
            errors.append("%s: %r" % (label, e))

    if len(all_items) <= HOT_QUOTA:
        print("几乎全部源都失败，保留上一次的数据。", file=sys.stderr)
        return 1

    order = {c: i for i, c in enumerate(CAT_ORDER)}
    all_items.sort(key=lambda x: order.get(x["cat"], 99))

    for i, it in enumerate(all_items, 1):
        it["id"] = "a%02d" % i

    weather = ""
    try:
        weather = fetch_weather()
        print("[OK]   天气        %s" % (weather or "跳过"))
    except Exception as e:
        print("[FAIL] 天气        %r" % e)

    payload = {
        "_note": "内容来自公开 RSS、知乎日报与少数派接口，各保留标题与正文摘要，仅供个人阅读。",
        "site": "口袋新闻",
        "weather": weather,
        "countdown": build_countdown(),
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

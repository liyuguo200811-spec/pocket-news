#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""口袋新闻 · 老年机极简版生成器

把 data/news.json 渲染成 XHTML MP 1.2 静态页：
  - 零 JS、零 CSS 文件、零图片、零外链资源
  - 列表页 <= 3KB，正文页 <= 6KB（未 gzip）
  - 每页 8 条，纯数字翻页

链接一律写成不带 .html 的形式（如 a01、list-2）。原因：Cloudflare Pages
默认开启 Pretty URLs，会把 /a01.html 用 308 重定向到 /a01，而 308 是 2015 年
才有的状态码，老年机浏览器不认识，会直接报“解析错误”。写成扩展名省略式
可以拿到 200，浏览器一个重定向都不用跳。
"""

import gzip
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "site"
PER_PAGE = 8
BUDGET_LIST = 3 * 1024
BUDGET_ART = 6 * 1024

SLUGS = {"热榜": "rebang", "要闻": "yaowen", "科技": "keji", "生活": "shenghuo"}

DOCTYPE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<!DOCTYPE html PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.2//EN" '
    '"http://www.openmobilealliance.org/tech/DTD/xhtml-mobile12.dtd">'
)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def shell(title: str, paras: list) -> str:
    body = "".join("<p>%s</p>" % p for p in paras if p)
    return (
        f'{DOCTYPE}<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        f'<meta http-equiv="Content-Type" content="text/html; charset=UTF-8"/>'
        f"<title>{esc(title)}</title></head><body>{body}</body></html>\n"
    )


def split_paras(text: str, target: int = 110) -> list:
    """把长正文按句子切成约 110 字一段，免得一小屏全是密密麻麻的字。"""
    if not text:
        return []
    if len(text) <= target * 1.3:
        return [text]
    out, buf = [], ""
    for ch in text:
        buf += ch
        if len(buf) >= target and ch in "。！？；":
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def pager(page_no: int, npages: int, url_for) -> str:
    parts = []
    for i in range(1, npages + 1):
        parts.append(str(i) if i == page_no else '<a href="%s">%d</a>' % (url_for(i), i))
    if page_no < npages:
        parts.append('<a href="%s">下页</a>' % url_for(page_no + 1))
    return " ".join(parts)


def render_list(items, page_no, npages, url_for, cats, brand, sub, start_no) -> str:
    nav = " ".join('<a href="cat-%s">%s</a>' % (slug, name) for name, slug in cats)
    paras = ["<b>%s</b>" % esc(brand)]
    if sub:
        paras.append(esc(sub))
    paras.append(nav)
    for i, it in enumerate(items):
        paras.append('%d <a href="%s">%s</a>'
                     % (start_no + i, it["id"], esc(it["title"])))
    paras.append(pager(page_no, npages, url_for))
    return shell(brand, paras)


def render_article(it, prev_id, next_id, brand) -> str:
    paras = ['<a href="/">返回</a>', "<b>%s</b>" % esc(it["title"])]

    meta = [esc(it["source"])]
    if it.get("time"):
        meta.append(it["time"])
    if it.get("hot"):
        meta.append("热度 " + esc(it["hot"]))
    paras.append("　".join(meta))

    paras.extend(esc(p) for p in split_paras(it.get("summary") or ""))

    tail = []
    if prev_id:
        tail.append('<a href="%s">上一条</a>' % prev_id)
    if next_id:
        tail.append('<a href="%s">下一条</a>' % next_id)
    if tail:
        paras.append(" ".join(tail))

    return shell(it["title"][:16], paras)


def main() -> int:
    data = json.loads((ROOT / "data" / "news.json").read_text(encoding="utf-8"))
    brand = data.get("site", "口袋新闻")
    items = data["items"]

    cats = []
    for it in items:
        if it["cat"] not in cats:
            cats.append(it["cat"])
    cat_pairs = [(c, SLUGS.get(c, "c%d" % (i + 1))) for i, c in enumerate(cats)]

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    written = {}

    def put(name: str, html: str):
        raw = html.encode("utf-8")
        (OUT / name).write_bytes(raw)
        written[name] = (len(raw), len(gzip.compress(raw, 9)))

    head = "%s%s %d条" % (data.get("date", ""), data.get("weekday", ""), len(items))

    # --- 全部新闻，分页 ---
    chunks = [items[i:i + PER_PAGE] for i in range(0, len(items), PER_PAGE)]
    npages = len(chunks)

    def all_url(i):
        return "/" if i == 1 else "list-%d" % i

    def all_file(i):
        return "index.html" if i == 1 else "list-%d.html" % i

    for pno, chunk in enumerate(chunks, 1):
        put(all_file(pno), render_list(chunk, pno, npages, all_url, cat_pairs,
                                       brand, head if pno == 1 else "",
                                       (pno - 1) * PER_PAGE + 1))

    # --- 分类页 ---
    for name, slug in cat_pairs:
        sub = [it for it in items if it["cat"] == name]
        cchunks = [sub[i:i + PER_PAGE] for i in range(0, len(sub), PER_PAGE)] or [[]]
        cn = len(cchunks)

        def cat_url(i, slug=slug):
            return "cat-%s" % slug if i == 1 else "cat-%s-%d" % (slug, i)

        def cat_file(i, slug=slug):
            return "cat-%s.html" % slug if i == 1 else "cat-%s-%d.html" % (slug, i)

        for pno, chunk in enumerate(cchunks, 1):
            put(cat_file(pno), render_list(chunk, pno, cn, cat_url, cat_pairs,
                                           brand, "%s %d条" % (name, len(sub)),
                                           (pno - 1) * PER_PAGE + 1))

    # --- 正文页 ---
    for idx, it in enumerate(items):
        prev_id = items[idx - 1]["id"] if idx > 0 else None
        next_id = items[idx + 1]["id"] if idx < len(items) - 1 else None
        put("%s.html" % it["id"], render_article(it, prev_id, next_id, brand))

    # ---------- 自检 ----------
    problems = []

    for f in sorted(OUT.glob("*.html")):
        raw = f.read_bytes()
        try:
            ET.fromstring(raw)
        except ET.ParseError as e:
            problems.append("%s XML 非法：%s" % (f.name, e))
        text = raw.decode("utf-8")
        for bad in ("<script", "<img", "<style", "@import", "style=", "class=", "src="):
            if bad in text:
                problems.append("%s 含 %s" % (f.name, bad))
        for href in re.findall(r'href="([^"]+)"', text):
            if href.startswith(("http://", "https://")) or href == "/":
                continue
            if not (OUT / href).exists() and not (OUT / (href + ".html")).exists():
                problems.append("%s 死链 -> %s" % (f.name, href))
        if re.search(r'href="[^"]*\.html"', text):
            problems.append("%s 链接里还带 .html，会触发 308 重定向" % f.name)

    is_art = lambda n: re.fullmatch(r"a\d+\.html", n) is not None
    list_bytes = [(n, b) for n, (b, _) in written.items() if not is_art(n)]
    art_bytes = [(n, b) for n, (b, _) in written.items() if is_art(n)]
    for n, b in list_bytes:
        if b > BUDGET_LIST:
            problems.append("%s 列表页 %d 字节，超 3KB 预算" % (n, b))
    for n, b in art_bytes:
        if b > BUDGET_ART:
            problems.append("%s 正文页 %d 字节，超 6KB 预算" % (n, b))

    # ---------- 报告 ----------
    total = sum(b for b, _ in written.values())
    gz_total = sum(g for _, g in written.values())
    max_list = max(b for _, b in list_bytes)
    max_art = max(b for _, b in art_bytes)
    lines = [
        "口袋新闻 · 体积报告（%s）" % data.get("updated", ""),
        "=" * 52,
        "文件总数        : %d 个" % len(written),
        "全部文件合计    : %d 字节（gzip 后 %d 字节）" % (total, gz_total),
        "列表页最大      : %d 字节（预算 3072）" % max_list,
        "正文页平均/最大 : %d / %d 字节（预算 6144）"
        % (sum(b for _, b in art_bytes) // len(art_bytes), max_art),
        "",
        "链接形式        : 扩展名省略（a01 而非 a01.html），避免 308 重定向",
        "",
        "逐个文件：",
        "  %-18s %7s %7s" % ("文件", "字节", "gzip"),
    ]
    for n in sorted(written):
        b, g = written[n]
        lines.append("  %-18s %7d %7d" % (n, b, g))
    report = "\n".join(lines) + "\n"
    (ROOT / "report.txt").write_text(report, encoding="utf-8")
    print(report)

    if problems:
        print("自检发现 %d 个问题：" % len(problems), file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        return 1
    print("自检通过：XML 合法、无死链、无外链资源、无 .html 链接、体积全部达标。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日德语新闻精读 —— 抓取 & 分析脚本
=====================================

流程：
  1. 读取 data/news.json，收集已抓取过的 URL / 音频 URL 用于去重
  2. 按优先级（或按日轮换）遍历候选来源：
       RSS/Podcast XML  →  (失败) 列表页自动发现 RSS  →  (失败) 列表页 HTML 链接抓取
  3. 对每篇候选文章：抓正文段落 + 音频 → 德语断句 → 找 > MIN_WORDS 词的完整句子
       - 找到且有音频：立刻选用
       - 否则记为“备胎”，继续换篇 / 换源；全部失败时取“有音频 + 最长句”的备胎
  4. 生成精读数据：
       - 有 GEMINI_API_KEY：调用 1 次 Gemini（JSON Schema 结构化输出）
       - 无 Key / 调用失败：simplemma 词形还原 + deep-translator 免费翻译 + 规则语法提示
  5. 写回 data/news.json（原子写入），由 GitHub Actions 负责 commit & push

本地运行：
  pip install -r scripts/requirements.txt
  python scripts/fetch_news.py                 # 正常运行
  python scripts/fetch_news.py --dry-run       # 只打印结果，不写文件
  python scripts/fetch_news.py --force --source dw_langsam

环境变量（均可选）：
  GEMINI_API_KEY        Gemini API Key（GitHub Secrets 注入）
  GEMINI_MODEL          默认 gemini-2.5-flash
  SITE_TZ               “今天”按哪个时区计算，默认 Asia/Shanghai
  MIN_WORDS             长句阈值（严格大于），默认 20
  SOURCE_STRATEGY       priority（默认，按优先级）| rotate（按日期轮换起始来源）
  REQUIRE_AUDIO         1（默认）= 优先要求有音频；0 = 不要求
  MAX_ENTRIES_PER_SOURCE 每个来源最多检查几篇，默认 6
  NO_TRANSLATE          1 = 降级模式下不调用在线翻译（离线调试用）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:  # 这些依赖在 Actions 中一定安装；本地离线测试时允许缺失
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None
try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None
try:
    import simplemma
except ImportError:  # pragma: no cover
    simplemma = None
try:
    from deep_translator import GoogleTranslator
except ImportError:  # pragma: no cover
    GoogleTranslator = None

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "news.json"

SITE_TZ = ZoneInfo(os.getenv("SITE_TZ", "Asia/Shanghai"))
MIN_WORDS = int(os.getenv("MIN_WORDS", "20"))          # 严格大于该值才算“长句”
IDEAL_MAX_WORDS = 45                                    # 超过这个长度的句子只在没得选时使用
MIN_ARTICLE_WORDS = 40
MAX_ENTRIES_PER_SOURCE = int(os.getenv("MAX_ENTRIES_PER_SOURCE", "6"))
REQUIRE_AUDIO = os.getenv("REQUIRE_AUDIO", "1") == "1"
SOURCE_STRATEGY = os.getenv("SOURCE_STRATEGY", "priority")
NO_TRANSLATE = os.getenv("NO_TRANSLATE", "0") == "1"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip() or "gemini-2.5-flash"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

HTTP_TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (compatible; DailyGermanReader/1.0; "
    "+https://github.com/) python-requests"
)

# 候选来源池（priority 越小越优先）。
# 说明：nachrichtenleicht 与 DW 有官方 RSS；NDR / MDR / tagesschau 目前没有公开的
# Leichte/Einfache-Sprache RSS，因此走“列表页自动发现 RSS → HTML 链接抓取”。
# 网站改版后只需修改这里的 URL / link_pattern 即可。
SOURCES: list[dict[str, Any]] = [
    {
        "id": "ndr_leicht",
        "name": "NDR · Nachrichten in Leichter Sprache",
        "level": "Leichte Sprache",
        "priority": 1,
        "homepage": "https://www.ndr.de/fernsehen/barrierefreie_angebote/leichte_sprache/",
        "feeds": [],
        "listing": "https://www.ndr.de/fernsehen/barrierefreie_angebote/leichte_sprache/index.html",
        "link_pattern": r"ndr\.de/.*leichte_sprache/.+\.html$",
        "content_selectors": [".modulepadding", "article", "main"],
    },
    {
        "id": "mdr_leicht",
        "name": "MDR · Nachrichten in Leichter Sprache",
        "level": "Leichte Sprache",
        "priority": 2,
        "homepage": "https://www.mdr.de/nachrichten-leicht/",
        "feeds": [],
        "listing": "https://www.mdr.de/nachrichten-leicht/index.html",
        "link_pattern": r"mdr\.de/nachrichten-leicht/.+-\d+\.html$",
        "content_selectors": [".paragraph", "article", "main"],
    },
    {
        "id": "nachrichtenleicht",
        "name": "nachrichtenleicht.de (Deutschlandfunk)",
        "level": "Einfache Sprache",
        "priority": 3,
        "homepage": "https://www.nachrichtenleicht.de/",
        "feeds": [
            "https://www.nachrichtenleicht.de/nachrichtenleicht-nachrichten-100.rss",
            "https://www.nachrichtenleicht.de/nachrichtenleicht-vermischtes-100.rss",
            "https://www.nachrichtenleicht.de/nachrichtenleicht-kultur-index-100.rss",
        ],
        "listing": "https://www.nachrichtenleicht.de/",
        "link_pattern": r"nachrichtenleicht\.de/[a-z0-9\-]+-100\.html$",
        "content_selectors": [".article-details-text", "article", "main"],
    },
    {
        "id": "tagesschau_einfach",
        "name": "tagesschau in Einfacher Sprache",
        "level": "Einfache Sprache",
        "priority": 4,
        "homepage": "https://www.tagesschau.de/multimedia/sendung/tagesschau_in_einfacher_sprache",
        "feeds": [],
        "listing": "https://www.tagesschau.de/multimedia/sendung/tagesschau_in_einfacher_sprache",
        "link_pattern": r"tagesschau\.de/.*einfache.*\.html$",
        "content_selectors": [".textabsatz", "article", "main"],
    },
    {
        "id": "dw_langsam",
        "name": "DW · Langsam gesprochene Nachrichten",
        "level": "Langsam gesprochen (B2)",
        "priority": 5,
        "homepage": "https://learngerman.dw.com/de/langsam-gesprochene-nachrichten/s-60040332",
        "feeds": ["https://rss.dw.com/xml/DKpodcast_lgn_de"],
        "listing": None,
        "link_pattern": None,
        "content_selectors": ["[class*=richtext]", "article", "main"],
    },
]

log = logging.getLogger("fetch_news")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    source: dict
    url: str
    title: str = ""
    published: str = ""
    audio: dict | None = None
    summary_html: str = ""


@dataclass
class Article:
    candidate: Candidate
    title: str
    paragraphs: list[str]
    audio: dict | None


@dataclass
class Selection:
    sentence: str
    paragraph_index: int
    word_count: int

    @property
    def long_enough(self) -> bool:
        return self.word_count > MIN_WORDS


# --------------------------------------------------------------------------- #
# HTTP 工具
# --------------------------------------------------------------------------- #
_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(total=2, backoff_factor=1.5,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset(["GET", "HEAD"]))
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://", HTTPAdapter(max_retries=retry))
        s.headers.update({"User-Agent": USER_AGENT,
                          "Accept-Language": "de-DE,de;q=0.9,en;q=0.5"})
        _session = s
    return _session


def fetch(url: str) -> requests.Response | None:
    try:
        r = session().get(url, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            log.info("  HTTP %s  %s", r.status_code, url)
            return None
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"
        return r
    except requests.RequestException as e:
        log.info("  请求失败 %s: %s", url, e)
        return None


_TRACKING = re.compile(r"^(utm_|wt_|at_|ref$|xtor$)", re.I)


def norm_url(url: str) -> str:
    """去掉 fragment、跟踪参数、末尾斜杠，用于去重。"""
    if not url:
        return ""
    p = urlparse(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k)]
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower() or "https", p.netloc.lower(), path, "", urlencode(q), ""))


# --------------------------------------------------------------------------- #
# 德语断句 & 分词
# --------------------------------------------------------------------------- #
_PH = "\u2063"  # 不可见占位符，临时替换“不是句末的点”

ABBREVIATIONS = [
    "z. B.", "u. a.", "d. h.", "v. a.", "o. ä.", "s. o.", "s. u.", "u. U.", "z. T.",
    "i. d. R.", "u. v. m.", "u. Ä.", "v. Chr.", "n. Chr.", "a. D.", "i. A.",
    "bzw.", "ca.", "Dr.", "Prof.", "Nr.", "usw.", "etc.", "vgl.", "evtl.", "ggf.",
    "inkl.", "exkl.", "Mio.", "Mrd.", "Tsd.", "St.", "Str.", "Jh.", "Jhd.", "Abs.",
    "Art.", "Hr.", "Fr.", "Hrsg.", "geb.", "gest.", "Tel.", "bspw.", "sog.", "zzgl.",
    "Std.", "Min.", "Sek.", "max.", "min.", "Jan.", "Feb.", "Apr.", "Aug.", "Sept.",
    "Okt.", "Nov.", "Dez.", "Co.", "Abb.", "Bd.", "Kap.", "Pkt.", "Ziff.", "Univ.",
    "allg.", "eigtl.", "gem.", "insb.", "jährl.", "lt.", "mtl.", "rd.", "tägl.", "u.",
]


def _abbr_pattern(a: str) -> str:
    # "z. B." 也要匹配 "z.B."；前面不能紧跟字母，避免误伤单词结尾
    body = re.escape(a).replace(r"\ ", r"\s?")
    return rf"(?<![^\W\d_]){body}"


_ABBR_RE = re.compile("|".join(_abbr_pattern(a) for a in sorted(ABBREVIATIONS, key=len, reverse=True)))
_MONTHS = ("Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|"
           "Dezember|Jan|Feb|Mär|Apr|Jun|Jul|Aug|Sep|Sept|Okt|Nov|Dez")
# 序数词 / 日期：“1. Januar”、“am 3. Oktober”、“das 21. Jahrhundert”、“1. FC Köln”、“3.10. war …”
_ORDINAL_RE = re.compile(
    rf"(?<!\w)(\d{{1,4}})\.(?=\s+(?:{_MONTHS}|Jahrhundert|Mal|Platz|Klasse|Liga|Stock|Etage|"
    rf"Weltkrieg|Bundesliga|Runde|Spieltag|Lebensjahr|Geburtstag|Advent|FC|Quartal|"
    rf"Halbjahr|Juli|[a-zäöüß(]))"
)
_INITIAL_RE = re.compile(r"(?<![^\W\d_])([A-ZÄÖÜ])\.(?=\s+[A-ZÄÖÜ][a-zäöüß])")  # "A. Merkel"
_SPLIT_RE = re.compile(r"[.!?…]+[\"'“”»«’)\]]*(?=\s+[\"'„“‚»«(\[]?[A-ZÄÖÜ0-9])")
_END_RE = re.compile(r"[.!?…][\"'“”»«’)\]]*$")
TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+(?:[-‐'’][^\W_]+)*")


def clean_text(s: str) -> str:
    s = s.replace("\u00a0", " ").replace("\u00ad", "")  # nbsp / 软连字符
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    return s


def split_sentences(text: str) -> list[str]:
    """德语断句：保护缩写、序数词、姓名首字母后再按句末标点切分。"""
    text = clean_text(text)
    if not text:
        return []
    protected = _ABBR_RE.sub(lambda m: m.group(0).replace(".", _PH), text)
    protected = _ORDINAL_RE.sub(lambda m: m.group(1) + _PH, protected)
    protected = _INITIAL_RE.sub(lambda m: m.group(1) + _PH, protected)
    out, start = [], 0
    for m in _SPLIT_RE.finditer(protected):
        out.append(protected[start:m.end()])
        start = m.end()
    out.append(protected[start:])
    return [s.replace(_PH, ".").strip() for s in out if s.strip()]


def tokenize(sentence: str) -> list[str]:
    return TOKEN_RE.findall(sentence)


def count_words(sentence: str) -> int:
    return len(tokenize(sentence))


def unique_tokens(sentence: str) -> list[str]:
    seen, out = set(), []
    for t in tokenize(sentence):
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def is_complete_sentence(s: str) -> bool:
    return bool(_END_RE.search(s)) and (s[:1].isupper() or s[:1].isdigit() or s[:1] in "\"„“‚»«(")


def choose_sentence(paragraphs: list[str]) -> Selection | None:
    """优先：MIN_WORDS < 词数 ≤ IDEAL_MAX 中最长的；其次：超长句中最短的；再次：最长句。"""
    cands: list[tuple[str, int, int, bool]] = []
    for pi, p in enumerate(paragraphs):
        for s in split_sentences(p):
            cands.append((s, pi, count_words(s), is_complete_sentence(s)))
    if not cands:
        return None
    pool = [c for c in cands if c[3]] or cands
    long_ones = [c for c in pool if c[2] > MIN_WORDS]
    if long_ones:
        best = max(long_ones, key=lambda c: (c[2] <= IDEAL_MAX_WORDS,
                                             c[2] if c[2] <= IDEAL_MAX_WORDS else -c[2]))
    else:
        best = max(pool, key=lambda c: c[2])
    return Selection(best[0], best[1], best[2])


# --------------------------------------------------------------------------- #
# 候选文章发现：RSS → 自动发现 RSS → HTML 列表页
# --------------------------------------------------------------------------- #
_AUDIO_EXT = re.compile(r"\.(mp3|m4a|aac|ogg|opus)(\?|$)", re.I)


def guess_mime(url: str) -> str:
    m = _AUDIO_EXT.search(url or "")
    ext = (m.group(1).lower() if m else "mp3")
    return {"mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
            "ogg": "audio/ogg", "opus": "audio/ogg"}.get(ext, "audio/mpeg")


def candidates_from_feed(source: dict, feed_url: str, seen: set[str]) -> list[Candidate]:
    if feedparser is None:
        return []
    r = fetch(feed_url)
    if not r:
        return []
    parsed = feedparser.parse(r.content)
    if not parsed.entries:
        log.info("  RSS 无条目：%s", feed_url)
        return []
    out = []
    for e in parsed.entries:
        link = e.get("link") or ""
        if not link or norm_url(link) in seen:
            continue
        audio = None
        encs = list(e.get("enclosures", [])) + [l for l in e.get("links", []) if l.get("rel") == "enclosure"]
        for enc in encs:
            href = enc.get("href") or enc.get("url")
            typ = enc.get("type", "") or ""
            if href and (typ.startswith("audio") or _AUDIO_EXT.search(href)):
                if norm_url(href) in seen:
                    audio = "SEEN"
                    break
                audio = {"url": href, "mime": typ or guess_mime(href),
                         "duration": e.get("itunes_duration", "") or ""}
                break
        if audio == "SEEN":
            continue
        summary = ""
        if e.get("content"):
            summary = e["content"][0].get("value", "")
        summary = summary or e.get("summary", "") or ""
        out.append(Candidate(source, link, clean_text(e.get("title", "")),
                             e.get("published", "") or e.get("updated", ""), audio, summary))
    log.info("  RSS %s → %d 篇新文章", feed_url, len(out))
    return out


def candidates_from_listing(source: dict, seen: set[str]) -> tuple[list[str], list[Candidate]]:
    """返回 (页面里自动发现的 RSS 链接, 通过 HTML 链接抓到的候选文章)。"""
    listing = source.get("listing")
    if not listing or BeautifulSoup is None:
        return [], []
    r = fetch(listing)
    if not r:
        return [], []
    soup = BeautifulSoup(r.text, "lxml")
    feeds = [urljoin(listing, l["href"]) for l in soup.find_all("link", href=True)
             if re.search(r"rss|atom", l.get("type", ""), re.I)]
    pattern = re.compile(source["link_pattern"]) if source.get("link_pattern") else None
    out, local = [], set()
    if pattern:
        for a in soup.find_all("a", href=True):
            url = urljoin(listing, a["href"]).split("#")[0]
            key = norm_url(url)
            if (not pattern.search(url) or key in seen or key in local
                    or key == norm_url(listing)):
                continue
            local.add(key)
            out.append(Candidate(source, url, clean_text(a.get_text(" ", strip=True))))
    log.info("  列表页 %s → 发现 RSS %d 个、文章链接 %d 个", listing, len(feeds), len(out))
    return feeds, out


def discover_candidates(source: dict, seen: set[str]) -> list[Candidate]:
    cands: list[Candidate] = []
    for f in source.get("feeds", []):
        cands += candidates_from_feed(source, f, seen)
        if len(cands) >= MAX_ENTRIES_PER_SOURCE:
            break
    if not cands:
        feeds, html_cands = candidates_from_listing(source, seen)
        for f in feeds:
            if f not in source.get("feeds", []):
                cands += candidates_from_feed(source, f, seen)
        if not cands:
            cands = html_cands
    # 同一来源内去重
    uniq, keys = [], set()
    for c in cands:
        k = norm_url(c.url)
        if k not in keys:
            keys.add(k)
            uniq.append(c)
    return uniq[:MAX_ENTRIES_PER_SOURCE]


# --------------------------------------------------------------------------- #
# 正文 & 音频提取
# --------------------------------------------------------------------------- #
# 普通字符和转义斜杠分别匹配，避免同一段 URL 被反复拆分。
_AUDIO_IN_RAW = re.compile(
    r"https?:(?:\\?/){2}(?:[^\s\"'<>\\]|\\/)+\.(?:mp3|m4a|aac|ogg)"
    r"(?:\?(?:[^\s\"'<>\\]|\\/)*)?(?=$|[\s\"'<>\\),;\]}])",
    re.I)
_BOILERPLATE = re.compile(
    r"(cookie|javascript|©|copyright|datenschutz|impressum|newsletter|alle rechte|"
    r"mehr zum thema|zum anhören|herunterladen|teilen auf|bildunterschrift|^foto:|^bild:|"
    r"^stand:|^quelle:|^audio|^video|weiterlesen|^\d+ min|^mehr nachrichten)", re.I)
GENERIC_SELECTORS = ["[itemprop=articleBody]", "article", "main", "#content", ".content", "body"]


def find_audio(soup, raw: str, base: str) -> dict | None:
    found: list[str] = []
    for tag in soup.find_all(["audio", "source"]):
        src = tag.get("src") or tag.get("data-src")
        typ = tag.get("type", "") or ""
        if src and (typ.startswith("audio") or _AUDIO_EXT.search(src) or tag.name == "audio"):
            found.append(urljoin(base, src))
    for meta in soup.find_all("meta", attrs={"property": re.compile(r"^og:audio")}):
        if meta.get("content"):
            found.append(urljoin(base, meta["content"]))
    for a in soup.find_all("a", href=True):
        if _AUDIO_EXT.search(a["href"]):
            found.append(urljoin(base, a["href"]))
    for m in _AUDIO_IN_RAW.finditer(unescape(raw)):
        found.append(m.group(0).replace("\\/", "/"))
    found = [u for u in dict.fromkeys(found) if u.startswith("http")]
    if not found:
        return None
    found.sort(key=lambda u: (".mp3" not in u.lower(), len(u)))
    return {"url": found[0], "mime": guess_mime(found[0]), "duration": ""}


def _jsonld_body(soup) -> list[str]:
    texts: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("articleBody", "transcript", "text") and isinstance(v, str) and len(v) > 200:
                    texts.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(s.string or ""))
        except (ValueError, TypeError):
            continue
    if not texts:
        return []
    body = max(texts, key=len)
    return [clean_text(p) for p in re.split(r"\n+", body) if clean_text(p)]


def _paragraphs_from_container(container) -> list[str]:
    paras: list[str] = []
    for el in container.find_all(["p", "li"]):
        outer = el.find_parent(["p", "li"])
        if outer is not None and any(a is container for a in outer.parents):
            continue  # 嵌套的 p/li 只取最外层，避免重复
        if el.name == "li" and el.find("a") and len(el.get_text(strip=True)) == len(el.find("a").get_text(strip=True)):
            continue  # 纯链接列表项（导航）
        t = clean_text(el.get_text(" ", strip=True))
        if count_words(t) < 3 or _BOILERPLATE.search(t):
            continue
        if not paras or paras[-1] != t:
            paras.append(t)
    return paras


def html_to_paragraphs(html: str) -> list[str]:
    if BeautifulSoup is None or not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    ps = _paragraphs_from_container(soup)
    if ps:
        return ps
    return [clean_text(t) for t in soup.get_text("\n").split("\n") if count_words(clean_text(t)) >= 3]


def total_words(paragraphs: list[str]) -> int:
    return sum(count_words(p) for p in paragraphs)


def extract_article(cand: Candidate) -> Article | None:
    title, audio, paragraphs = cand.title, cand.audio, []
    r = fetch(cand.url)
    if r is not None and BeautifulSoup is not None:
        raw = r.text
        soup = BeautifulSoup(raw, "lxml")
        og = soup.find("meta", property="og:title")
        h1 = soup.find("h1")
        title = title or (og.get("content") if og else "") or (h1.get_text(" ", strip=True) if h1 else "")
        audio = audio or find_audio(soup, raw, cand.url)
        jsonld = _jsonld_body(soup)
        for t in soup(["script", "style", "noscript", "nav", "header", "footer", "aside",
                       "form", "button", "svg", "iframe", "figure", "figcaption"]):
            t.decompose()
        best: list[str] = []
        for sel in cand.source.get("content_selectors", []) + GENERIC_SELECTORS:
            for container in soup.select(sel)[:3]:
                ps = _paragraphs_from_container(container)
                if total_words(ps) > total_words(best):
                    best = ps
            if total_words(best) >= 120:  # 已经足够好，不再尝试更宽泛的选择器
                break
        paragraphs = best
        if total_words(paragraphs) < MIN_ARTICLE_WORDS and total_words(jsonld) > total_words(paragraphs):
            paragraphs = jsonld
    if total_words(paragraphs) < MIN_ARTICLE_WORDS and cand.summary_html:
        alt = html_to_paragraphs(cand.summary_html)
        if total_words(alt) > total_words(paragraphs):
            paragraphs = alt
    if total_words(paragraphs) < MIN_ARTICLE_WORDS:
        log.info("    正文过短，跳过：%s", cand.url)
        return None
    return Article(cand, clean_text(title) or "(ohne Titel)", paragraphs, audio)


# --------------------------------------------------------------------------- #
# 选文：多源降级 + 长句约束
# --------------------------------------------------------------------------- #
def order_sources(source_arg: str | None, today: str) -> list[dict]:
    srcs = sorted(SOURCES, key=lambda s: s["priority"])
    if SOURCE_STRATEGY == "rotate":
        k = dt.date.fromisoformat(today).toordinal() % len(srcs)
        srcs = srcs[k:] + srcs[:k]
    if source_arg and source_arg != "auto":
        first = [s for s in srcs if s["id"] == source_arg]
        if not first:
            raise SystemExit(f"未知来源：{source_arg}，可选：{[s['id'] for s in SOURCES]}")
        srcs = first + [s for s in srcs if s["id"] != source_arg]  # 指定源失败仍可降级
    return srcs


def pick_article(sources: list[dict], seen: set[str]) -> tuple[Article, Selection] | None:
    backup: tuple[tuple, Article, Selection] | None = None
    for src in sources:
        log.info("▶ 尝试来源：%s", src["name"])
        try:
            cands = discover_candidates(src, seen)
        except Exception as e:  # 任何来源异常都不应中断整体流程
            log.warning("  来源异常：%s", e)
            continue
        for cand in cands:
            try:
                art = extract_article(cand)
            except Exception as e:
                log.warning("    解析异常 %s: %s", cand.url, e)
                continue
            if not art:
                continue
            sel = choose_sentence(art.paragraphs)
            if not sel:
                continue
            has_audio = bool(art.audio)
            log.info("    %s | 最佳句 %d 词 | 音频：%s", art.title[:50], sel.word_count, "有" if has_audio else "无")
            if sel.long_enough and (has_audio or not REQUIRE_AUDIO):
                return art, sel
            score = (sel.long_enough and has_audio, has_audio, sel.long_enough, sel.word_count)
            if backup is None or score > backup[0]:
                backup = (score, art, sel)
    if backup:
        log.warning("没有同时满足“>%d 词 + 有音频”的文章，使用最佳备选。", MIN_WORDS)
        return backup[1], backup[2]
    return None


# --------------------------------------------------------------------------- #
# 精读分析：Gemini（每天 1 次）
# --------------------------------------------------------------------------- #
GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "vocab_list": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "word": {"type": "STRING"},
                    "lemma": {"type": "STRING"},
                    "pos": {"type": "STRING"},
                    "meaning": {"type": "STRING"},
                },
                "required": ["word", "lemma", "pos", "meaning"],
            },
        },
        "reference_translation": {"type": "STRING"},
        "grammar_notes": {
            "type": "OBJECT",
            "properties": {
                "skeleton": {"type": "STRING"},
                "structure": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "text": {"type": "STRING"},
                            "type": {"type": "STRING"},
                            "explanation": {"type": "STRING"},
                        },
                        "required": ["text", "type", "explanation"],
                    },
                },
                "points": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "title": {"type": "STRING"},
                            "explanation": {"type": "STRING"},
                        },
                        "required": ["title", "explanation"],
                    },
                },
            },
            "required": ["skeleton", "structure", "points"],
        },
    },
    "required": ["vocab_list", "reference_translation", "grammar_notes"],
}

GEMINI_PROMPT = """你是一名经验丰富的德语教师，学生母语为中文、德语水平约 A2–B1。
请对下面这句德语新闻长句做“精读分析”，严格输出 JSON。

【新闻标题】{title}
【上下文（仅供理解语境）】
{context}

【精读句】
{sentence}

要求：
1. vocab_list：按原句顺序列出句中的每一个词（跳过标点；完全相同的词只列一次；冠词、介词、连词、代词等虚词也要保留）。
   - word：与原句写法完全一致（包括连字符、大小写）。
   - lemma：词典原形 + 标准德语拼写。名词写成“定冠词 + 单数主格”，并去掉易读德语的拆分连字符，
     例如 "Hör-Behinderung" → "die Hörbehinderung"、"Menschen" → "der Mensch"；
     动词写不定式（可分动词写完整形式，如 "fängt … an" 中的 "fängt" → "anfangen"）；形容词写原级；缩合词如 "im" → "in + dem"。
   - pos：中文词性，可带补充信息，如 "名词（阴性）"、"动词（过去分词）"、"情态动词"、"助动词（被动）"、
     "介词（+三格）"、"连词（从属）"、"冠词（阴性·三格）"、"形容词"、"副词"、"代词"、"数词"、"专有名词"。
   - meaning：在本句语境下的中文释义，简洁（一般不超过 12 个字）。
2. reference_translation：准确、通顺、符合中文表达习惯的参考译文。
3. grammar_notes：
   - skeleton：句子主干（主语 + 变位动词 + 核心成分），在德语成分后用括号标注中文成分名称。
   - structure：把整句按顺序拆成主句 / 各类从句 / zu 不定式 / 插入语 / 重要介词短语等片段；
     每段给出 text（原文片段，拼接后应覆盖整句）、type（片段类型）、explanation（中文说明其作用、语序、格位）。
   - points：2–5 个本句最值得学的语法点（如动词第二位/后置、框型结构、情态动词、被动态、虚拟式、
     可分动词、格位与介词支配、形容词词尾、易读德语连字符等），每个给 title 与 explanation。
"""


def _strip_json_fence(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s


def normalize_analysis(obj: Any) -> dict | None:
    if not isinstance(obj, dict):
        return None
    vocab = []
    for v in obj.get("vocab_list") or []:
        if isinstance(v, dict) and str(v.get("word", "")).strip():
            vocab.append({k: str(v.get(k, "") or "").strip() for k in ("word", "lemma", "pos", "meaning")})
    trans = str(obj.get("reference_translation", "") or "").strip()
    gn = obj.get("grammar_notes") or {}
    if not vocab or not trans or not isinstance(gn, dict):
        return None
    grammar = {
        "skeleton": str(gn.get("skeleton", "") or "").strip(),
        "structure": [
            {k: str(x.get(k, "") or "").strip() for k in ("text", "type", "explanation")}
            for x in gn.get("structure") or [] if isinstance(x, dict)
        ],
        "points": [
            {k: str(x.get(k, "") or "").strip() for k in ("title", "explanation")}
            for x in gn.get("points") or [] if isinstance(x, dict)
        ],
    }
    return {"vocab_list": vocab, "reference_translation": trans, "grammar_notes": grammar}


def analyze_with_gemini(sentence: str, title: str, paragraphs: list[str]) -> dict | None:
    """每天只发起 1 次请求；失败直接返回 None，由降级方案接管。"""
    if not GEMINI_API_KEY:
        log.info("未配置 GEMINI_API_KEY，使用降级方案。")
        return None
    context = "\n".join(paragraphs)
    if len(context) > 4000:
        context = context[:4000] + " …"
    body = {
        "contents": [{"role": "user", "parts": [{"text": GEMINI_PROMPT.format(
            title=title, context=context, sentence=sentence)}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseSchema": GEMINI_SCHEMA,
        },
    }
    url = GEMINI_ENDPOINT.format(model=GEMINI_MODEL)
    log.info("调用 Gemini（%s）…", GEMINI_MODEL)
    try:
        r = requests.post(url, json=body, timeout=180,
                          headers={"x-goog-api-key": GEMINI_API_KEY,
                                   "Content-Type": "application/json"})
    except requests.RequestException as e:
        log.warning("Gemini 请求失败：%s", e)
        return None
    if r.status_code != 200:
        log.warning("Gemini 返回 HTTP %s：%s", r.status_code, r.text[:400])
        return None
    try:
        data = r.json()
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        result = normalize_analysis(json.loads(_strip_json_fence(text)))
    except (KeyError, IndexError, ValueError, TypeError) as e:
        log.warning("Gemini 结果解析失败：%s", e)
        return None
    if result is None:
        log.warning("Gemini 结果缺少必要字段，使用降级方案。")
    return result


# --------------------------------------------------------------------------- #
# 精读分析：降级方案（无 Key / Gemini 失败）
# --------------------------------------------------------------------------- #
ARTICLES = {"der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer",
            "eines", "kein", "keine", "keinen", "keinem", "keiner", "keines"}
PREPOSITIONS = {
    "in": "+三/四格", "an": "+三/四格", "auf": "+三/四格", "über": "+三/四格", "unter": "+三/四格",
    "vor": "+三/四格", "hinter": "+三/四格", "neben": "+三/四格", "zwischen": "+三/四格",
    "aus": "+三格", "bei": "+三格", "mit": "+三格", "nach": "+三格", "von": "+三格", "zu": "+三格",
    "seit": "+三格", "gegenüber": "+三格", "ab": "+三格",
    "für": "+四格", "gegen": "+四格", "ohne": "+四格", "um": "+四格", "durch": "+四格", "bis": "+四格",
    "während": "+二格", "wegen": "+二/三格", "trotz": "+二格", "statt": "+二格", "laut": "+二/三格",
    "im": "in+dem", "am": "an+dem", "zum": "zu+dem", "zur": "zu+der", "vom": "von+dem",
    "beim": "bei+dem", "ins": "in+das", "ans": "an+das", "aufs": "auf+das",
}
CONTRACTIONS = {"im": "in + dem", "am": "an + dem", "zum": "zu + dem", "zur": "zu + der",
                "vom": "von + dem", "beim": "bei + dem", "ins": "in + das", "ans": "an + das",
                "aufs": "auf + das"}
CONJ_COORD = {"und", "oder", "aber", "denn", "sondern", "doch", "sowie", "sowohl", "als auch"}
CONJ_SUB = {"dass", "weil", "wenn", "ob", "obwohl", "nachdem", "bevor", "damit", "sodass",
            "falls", "indem", "sobald", "solange", "seitdem", "ehe", "als", "während", "bis", "wie"}
RELATIVES = {"der", "die", "das", "dem", "den", "deren", "dessen", "denen", "welche", "welcher",
             "welches", "wo", "was"}
PRONOUNS = {"ich", "du", "er", "sie", "es", "wir", "ihr", "man", "sich", "ihm", "ihn", "ihnen",
            "uns", "euch", "mich", "dich", "mir", "dir", "sein", "seine", "seinen", "seinem",
            "seiner", "ihre", "ihren", "ihrem", "ihrer", "unser", "unsere", "dieser", "diese",
            "dieses", "diesen", "diesem", "jeder", "jede", "jedes", "alle", "viele", "etwas",
            "nichts", "jemand", "niemand", "das", "dies"}
MODALS = {"können", "kann", "kannst", "konnte", "konnten", "könnte", "könnten", "müssen", "muss",
          "musste", "mussten", "müsste", "dürfen", "darf", "durfte", "dürfte", "sollen", "soll",
          "sollte", "sollten", "wollen", "will", "wollte", "wollten", "mögen", "mag", "möchte",
          "möchten"}
AUX_WERDEN = {"werden", "wird", "wirst", "wurde", "wurden", "worden", "würde", "würden"}
AUX_HABEN_SEIN = {"haben", "hat", "habe", "hast", "hatte", "hatten", "hätte", "hätten", "sein",
                  "ist", "sind", "bin", "bist", "war", "waren", "wäre", "wären", "gewesen"}
ADVERBS = {"auch", "nicht", "noch", "schon", "sehr", "nur", "jetzt", "heute", "immer", "bereits",
           "etwa", "fast", "gern", "gerne", "oft", "dann", "da", "dort", "hier", "so", "zum beispiel",
           "besonders", "vielleicht", "wieder", "bald", "mehr", "weniger", "zusammen"}


def normalize_compound(word: str) -> str:
    """易读德语连字符还原：Hör-Behinderung → Hörbehinderung；EU-Kommission 等缩写复合词保留。"""
    parts = re.split(r"[-‐]", word)
    if len(parts) < 2 or any(not p.isalpha() for p in parts):
        return word
    if parts[0].isupper() or len(parts[0]) <= 1:
        return word
    return parts[0] + "".join(p.lower() for p in parts[1:])


def guess_lemma(word: str) -> str:
    low = word.lower()
    if low in CONTRACTIONS:
        return CONTRACTIONS[low]
    if low in ARTICLES or low in PREPOSITIONS or low in CONJ_COORD or low in CONJ_SUB:
        return low  # 句首大写的虚词还原为小写
    base = normalize_compound(word)
    if simplemma is not None:
        try:
            lem = simplemma.lemmatize(base, lang="de")
            if lem:
                return lem
        except Exception:
            pass
    return base


def guess_pos(word: str, lemma: str, index: int, prev: str | None) -> str:
    low = word.lower()
    if re.fullmatch(r"\d+(?:[.,]\d+)*", word):
        return "数词"
    if low in CONTRACTIONS:
        return f"介词+冠词缩合（{PREPOSITIONS[low]}）"
    if low in RELATIVES and prev == ",":
        return "关系代词（可能）"
    if low in CONJ_SUB and low not in PREPOSITIONS:
        return "连词（从属，动词后置）"
    if low in CONJ_COORD:
        return "连词（并列）"
    if low in ARTICLES:
        return "冠词"
    if low in PREPOSITIONS:
        return f"介词（{PREPOSITIONS[low]}）"
    if low in MODALS:
        return "情态动词"
    if low in AUX_WERDEN:
        return "助动词 werden（将来时/被动/虚拟）"
    if low in AUX_HABEN_SEIN:
        return "助动词/系动词"
    if low in PRONOUNS:
        return "代词"
    if low in ADVERBS:
        return "副词"
    if word[:1].isupper() and (index > 0 or lemma[:1].isupper()):
        return "名词（词性请查词典）"
    if re.match(r"^ge\w+(t|en)$", low) or re.search(r"iert$", low):
        return "动词（过去分词，可能）"
    if lemma.endswith(("en", "ern", "eln")) and lemma == lemma.lower():
        return "动词（可能）"
    return "实词（详见词典）"


def translate_lines(lines: list[str]) -> list[str]:
    """免费翻译（deep-translator → Google 网页版）。多行合并成一次请求，失败返回空串。"""
    if NO_TRANSLATE or GoogleTranslator is None or not lines:
        return [""] * len(lines)
    try:
        tr = GoogleTranslator(source="de", target="zh-CN")
        out = tr.translate("\n".join(lines)) or ""
        parts = [p.strip() for p in out.split("\n") if p.strip()]
        if len(parts) == len(lines):
            return parts
        return [(tr.translate(l) or "").strip() for l in lines]
    except Exception as e:
        log.warning("免费翻译失败：%s", e)
        return [""] * len(lines)


def fallback_vocab(sentence: str, words: list[str] | None = None) -> list[dict]:
    words = words if words is not None else unique_tokens(sentence)
    # 记录每个词前一个“非空白字符”，用于粗略判断关系从句
    prev_char: dict[str, str] = {}
    for m in TOKEN_RE.finditer(sentence):
        before = sentence[:m.start()].rstrip()
        prev_char.setdefault(m.group(0).lower(), before[-1:] if before else "")
    first = (tokenize(sentence) or [""])[0]
    items = []
    for w in words:
        lemma = guess_lemma(w)
        pos = guess_pos(w, lemma, 0 if w == first else 1, prev_char.get(w.lower()))
        items.append({"word": w, "lemma": lemma, "pos": pos, "meaning": ""})
    meanings = translate_lines([i["lemma"] for i in items])
    for i, m in zip(items, meanings):
        i["meaning"] = m
    return items


def fallback_grammar(sentence: str) -> dict:
    toks = [t.lower() for t in tokenize(sentence)]
    segs = [s.strip() for s in re.split(r",\s*", sentence.rstrip(".!?… ")) if s.strip()]
    structure = []
    for i, seg in enumerate(segs):
        first = (tokenize(seg) or [""])[0].lower()
        if i == 0 and first not in CONJ_SUB:
            typ, exp = "主句（或主句开头）", "陈述句中变位动词位于第二位。"
        elif first in CONJ_SUB:
            typ, exp = f"{first} 引导的从句", "从句中的变位动词通常位于从句末尾。"
        elif first in RELATIVES:
            typ, exp = "关系从句（可能）", "修饰前面的名词，变位动词位于从句末尾。"
        elif re.search(r"\bzu\s+[a-zäöüß]+en\b", seg):
            typ, exp = "zu 不定式结构（可能）", "zu + 不定式位于结构末尾。"
        elif first in CONJ_COORD:
            typ, exp = "并列成分", "并列连词不占位，后接正常语序。"
        else:
            typ, exp = "补充/插入成分", "请结合上下文判断其修饰对象。"
        structure.append({"text": seg, "type": typ, "explanation": exp})

    points = []
    if any(t in MODALS for t in toks):
        points.append({"title": "情态动词 + 不定式（框型结构）",
                       "explanation": "情态动词变位后放在第二位，实义动词不定式放在句末（从句中则一起放在末尾）。"})
    if any(t in AUX_WERDEN for t in toks) and any(re.match(r"^(ge\w+(t|en)|\w+iert|\w+t)$", t) for t in toks):
        points.append({"title": "被动态 werden + 过去分词（可能）",
                       "explanation": "句中出现 werden 的变位形式和过去分词，可能构成“被……”的被动态。"})
    if any(t in AUX_HABEN_SEIN for t in toks) and any(re.match(r"^ge\w+(t|en)$", t) for t in toks):
        points.append({"title": "完成时 haben/sein + 过去分词（可能）",
                       "explanation": "助动词 haben/sein 变位放第二位，过去分词放句末。"})
    if any(s["type"].endswith("从句") or "从句" in s["type"] for s in structure[1:]):
        points.append({"title": "从句动词后置",
                       "explanation": "dass / weil / wenn 等从属连词及关系代词引导的从句，变位动词位于从句最后。"})
    if any(re.search(r"[^\W\d_][-‐][A-ZÄÖÜ]", t) for t in tokenize(sentence)):
        points.append({"title": "易读德语（Leichte Sprache）的连字符",
                       "explanation": "长复合词被连字符拆开以便阅读，标准拼写需去掉连字符并把后半部分小写。"})
    if not points:
        points.append({"title": "动词第二位原则",
                       "explanation": "德语陈述句中变位动词固定位于第二位，第一位可以是主语、时间状语或其他成分。"})
    return {
        "skeleton": "（自动规则模式无法可靠识别主干，请参考下方结构拆分，或使用“第三步 AI 深度批改”。）",
        "structure": structure,
        "points": points,
        "note": "⚠️ 本条解析由规则 + 免费机器翻译自动生成，仅供参考。",
    }


def analyze_fallback(sentence: str) -> dict:
    log.info("使用降级方案生成词表与解析…")
    trans = translate_lines([sentence])[0]
    return {
        "vocab_list": fallback_vocab(sentence),
        "reference_translation": trans or "（暂无自动译文，请点击单词查词典，或使用第三步 AI 批改。）",
        "grammar_notes": fallback_grammar(sentence),
    }


def complete_vocab(analysis: dict, sentence: str) -> dict:
    """确保 vocab_list 覆盖原句中的每个词，并按原句顺序排列。"""
    tokens = unique_tokens(sentence)
    by_word: dict[str, dict] = {}
    for v in analysis["vocab_list"]:
        by_word.setdefault(v["word"].lower(), v)
    missing = [t for t in tokens if t.lower() not in by_word]
    if missing:
        log.info("Gemini 词表缺少 %d 个词，降级补全：%s", len(missing), missing)
        for v in fallback_vocab(sentence, missing):
            by_word[v["word"].lower()] = v
    ordered = [by_word[t.lower()] for t in tokens]
    used = {id(v) for v in ordered}
    extras = [v for v in analysis["vocab_list"] if id(v) not in used and v["word"].lower() not in
              {t.lower() for t in tokens}]
    analysis["vocab_list"] = ordered + extras
    return analysis


# --------------------------------------------------------------------------- #
# 数据读写
# --------------------------------------------------------------------------- #
def load_data() -> dict:
    if not DATA_FILE.exists() or DATA_FILE.stat().st_size == 0:
        return {"version": 1, "updated_at": None, "items": []}
    with DATA_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):  # 兼容旧格式
        data = {"version": 1, "updated_at": None, "items": data}
    data.setdefault("items", [])
    return data


def save_data(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_FILE.parent, prefix=".news.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, DATA_FILE)


def write_step_summary(item: dict) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"### 📰 {item['date']} · {item['source']['name']}\n\n"
                f"**{item['title']}**  \n{item['url']}\n\n"
                f"> {item['sentence']}\n\n"
                f"- 词数：{item['sentence_meta']['word_count']}\n"
                f"- 音频：{'✅' if item['audio'] else '❌'}\n"
                f"- 分析引擎：`{item['analysis']['engine']}`\n")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="抓取每日德语新闻并生成精读数据")
    p.add_argument("--force", action="store_true",
                   default=os.getenv("FORCE", "0").lower() in ("1", "true"),
                   help="今天已有数据也重新抓取并覆盖")
    p.add_argument("--source", default=os.getenv("SOURCE", "auto"),
                   help="优先使用的来源 id（auto = 自动）")
    p.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD（默认按 SITE_TZ 的今天）")
    p.add_argument("--dry-run", action="store_true", help="只打印结果，不写入文件")
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    args = parse_args()
    now = dt.datetime.now(SITE_TZ)
    today = args.date or now.date().isoformat()

    data = load_data()
    items: list[dict] = data["items"]
    existing = [i for i in items if i.get("date") == today]
    if existing and not args.force:
        log.info("%s 已有数据，跳过（使用 --force 可覆盖）。", today)
        return 0
    items[:] = [i for i in items if i.get("date") != today]

    seen: set[str] = set()
    for i in items:
        if i.get("demo"):
            continue
        seen.add(norm_url(i.get("url", "")))
        if i.get("audio") and i["audio"].get("url"):
            seen.add(norm_url(i["audio"]["url"]))

    picked = pick_article(order_sources(args.source, today), seen)
    if not picked:
        log.error("❌ 所有来源均失败，今天没有可用文章。")
        return 1
    art, sel = picked
    cand = art.candidate
    log.info("✅ 选中：[%s] %s\n   %s\n   精读句（%d 词）：%s",
             cand.source["id"], art.title, cand.url, sel.word_count, sel.sentence)

    analysis = analyze_with_gemini(sel.sentence, art.title, art.paragraphs)
    engine = GEMINI_MODEL
    if analysis is None:
        analysis = analyze_fallback(sel.sentence)
        engine = "fallback"
    else:
        analysis = complete_vocab(analysis, sel.sentence)

    src = cand.source
    item = {
        "id": today,
        "date": today,
        "fetched_at": now.isoformat(timespec="seconds"),
        "source": {"id": src["id"], "name": src["name"], "level": src["level"],
                   "homepage": src["homepage"]},
        "title": art.title,
        "url": cand.url,
        "published": cand.published,
        "audio": art.audio,
        "paragraphs": art.paragraphs,
        "sentence": sel.sentence,
        "sentence_meta": {"paragraph_index": sel.paragraph_index,
                          "word_count": sel.word_count,
                          "meets_min_words": sel.long_enough},
        "vocab_list": analysis["vocab_list"],
        "reference_translation": analysis["reference_translation"],
        "grammar_notes": analysis["grammar_notes"],
        "analysis": {"engine": engine,
                     "generated_at": dt.datetime.now(SITE_TZ).isoformat(timespec="seconds")},
    }
    items.append(item)
    # 首次产生真实数据后，自动移除示例条目
    items[:] = [i for i in items if not i.get("demo")]
    items.sort(key=lambda i: i.get("date", ""), reverse=True)
    data["version"] = 1
    data["updated_at"] = item["fetched_at"]

    if args.dry_run:
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return 0
    save_data(data)
    write_step_summary(item)
    log.info("💾 已写入 %s（共 %d 篇）", DATA_FILE.relative_to(ROOT), len(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# coding: utf-8
"""
Analyze crawler output JSON files and generate report files.

Default behavior:
  1. Scan ./output/*.json
  2. Ask the user to choose one JSON file
  3. Re-fetch article pages and extract text, images, links, and assets
  4. Write reports to this script's directory
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests


BASE_DIR = Path(__file__).resolve().parent
CLAW_DIR = BASE_DIR
DEFAULT_OUTPUT_JSON_DIR = CLAW_DIR / "output"

if CLAW_DIR.exists():
    sys.path.insert(0, str(CLAW_DIR))

try:
    from read_wechat_article import WechatArticleFetcher, WechatArticleParser
except ImportError as exc:
    print(f"[x] 无法导入 read_wechat_article.py: {exc}")
    print(f"[x] 请确认项目目录存在: {CLAW_DIR}")
    raise


URL_PATTERN = re.compile(r"https?://[^\s\"'<>，。；；、）)]+", re.I)
IMAGE_HOSTS = {"mmbiz.qpic.cn", "mmbiz.qlogo.cn", "wx.qlogo.cn"}
WECHAT_HOST_SUFFIXES = (
    "mp.weixin.qq.com",
    "weixin.qq.com",
    "wx.qq.com",
    "qq.com",
    "qpic.cn",
    "qlogo.cn",
    "gtimg.cn",
)
IMAGE_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
}
MINIPROGRAM_HINTS = (
    "weapp",
    "miniprogram",
    "data-miniprogram",
    "weapp_username",
    "weapp_path",
)


def safe_filename(value):
    value = (value or "unknown").strip()
    value = re.sub(r'[\\/:*?"<>|\s]+', "_", value)
    return value.strip("_") or "unknown"


def normalize_url(url):
    return html.unescape(html.unescape(url or "")).replace("\\/", "/").strip()


def get_hostname(url):
    return (urlparse(url).hostname or "").lower()


def is_wechat_host(host):
    host = (host or "").lower()
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in WECHAT_HOST_SUFFIXES)


def is_http_url(url):
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def decode_wechat_redirect(url):
    parsed = urlparse(url)
    if parsed.hostname != "mp.weixin.qq.com":
        return url
    query = parse_qs(parsed.query)
    for key in ("url", "target", "redirect_url"):
        values = query.get(key)
        if values and values[0]:
            return normalize_url(unquote(values[0]))
    return url


def classify_url(url):
    host = get_hostname(url)
    path = urlparse(url).path.lower()
    if host in IMAGE_HOSTS or path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")):
        return "image"
    if host == "mp.weixin.qq.com":
        return "wechat_link"
    if "qq.com" in host or "weixin.qq.com" in host:
        return "wechat_asset"
    if path.endswith((".mp4", ".mov", ".m3u8", ".mp3", ".wav")):
        return "media"
    if path.endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar")):
        return "file"
    return "external_url"


def list_output_jsons(output_dir):
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        return []
    return sorted(output_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)


def choose_input_file(output_dir):
    files = list_output_jsons(output_dir)
    if not files:
        print(f"[x] 没有发现 JSON 文件: {output_dir}")
        sys.exit(1)

    print(f"\n发现 {len(files)} 个 JSON 文件: {output_dir}")
    for index, path in enumerate(files, 1):
        size = path.stat().st_size
        mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {index}. {path.name}  ({size} bytes, {mtime})")

    while True:
        value = input("\n选择编号: ").strip()
        if value.isdigit() and 1 <= int(value) <= len(files):
            return files[int(value) - 1]
        print("请输入有效编号。")


def load_articles(json_path):
    with open(json_path, "r", encoding="utf-8") as fp:
        data = json.load(fp)

    if isinstance(data, list):
        return "unknown", data
    if isinstance(data, dict):
        return data.get("account") or data.get("nickname") or "unknown", data.get("articles", [])
    raise ValueError("Unsupported JSON format")


def article_url(article):
    return normalize_url(
        article.get("url")
        or article.get("link")
        or article.get("content_url")
        or article.get("source_url")
        or article.get("article_url")
        or ""
    )


def normalize_article(url):
    url = normalize_url(url)
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def normalize_proxy(proxy):
    proxy = str(proxy or "").strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        return f"http://{proxy}"
    return proxy


def load_proxy_pool(proxies=None, proxy_file=None):
    proxy_pool = []
    if proxies:
        proxy_items = [proxies] if isinstance(proxies, str) else proxies
        for item in proxy_items:
            proxy_pool.extend(part.strip() for part in str(item).replace(";", ",").split(","))
    if proxy_file:
        proxy_path = Path(proxy_file)
        if proxy_path.is_file():
            with open(proxy_path, "r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        proxy_pool.extend(part.strip() for part in line.replace(";", ",").split(","))
        else:
            print(f"[!] 代理文件不存在，已跳过: {proxy_file}")
    return [proxy for proxy in (normalize_proxy(item) for item in proxy_pool) if proxy]


def pick_proxy(proxy_pool, article_index):
    if not proxy_pool:
        return None
    return proxy_pool[(article_index - 1) % len(proxy_pool)]


def article_container(soup):
    for kwargs in ({"id": "js_content"}, {"id": "img-content"}, {"class_": "rich_media_content"}):
        node = soup.find(**kwargs)
        if node is not None:
            return node
    return soup


def add_asset(assets, seen, article_index, article, resource_type, value, source, context=""):
    value = decode_wechat_redirect(normalize_url(value))
    if not value or not is_http_url(value):
        return
    key = (article_index, resource_type, value)
    if key in seen:
        return
    seen.add(key)
    assets.append(
        {
            "article_index": article_index,
            "article_title": article.get("title", ""),
            "article_url": article.get("url", ""),
            "resource_type": resource_type,
            "resource_value": value,
            "host": get_hostname(value),
            "source": source,
            "context": context,
        }
    )


def extract_miniprograms(container):
    results = []
    seen = set()
    for tag in container.find_all(True):
        attrs = {key: value for key, value in tag.attrs.items() if isinstance(value, str)}
        joined = " ".join([tag.name, *attrs.keys(), *attrs.values()])
        if not any(hint in joined for hint in MINIPROGRAM_HINTS):
            continue
        appid = attrs.get("data-miniprogram-appid") or attrs.get("data-weappid") or attrs.get("appid") or ""
        username = attrs.get("weapp_username") or attrs.get("data-weapp-username") or ""
        path = attrs.get("data-miniprogram-path") or attrs.get("data-weapp-path") or attrs.get("weapp_path") or ""
        label = tag.get_text(" ", strip=True)
        value = {"appid": appid, "username": username, "path": path, "label": label}
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if (appid or username or path) and key not in seen:
            seen.add(key)
            results.append(value)
    return results


def extract_assets_from_html(page_html, article_index, detail):
    soup = BeautifulSoup(page_html or "", "html.parser")
    container = article_container(soup)
    assets = []
    seen = set()

    url_attrs = ("href", "src", "data-src", "data-original", "data-backsrc", "poster")
    for tag in container.find_all(True):
        for attr in url_attrs:
            raw = tag.get(attr)
            if not raw:
                continue
            value = normalize_url(urljoin(detail["url"], raw))
            add_asset(assets, seen, article_index, detail, classify_url(value), value, f"html_{attr}", tag.get_text(" ", strip=True))

        style = tag.get("style")
        if style:
            for raw in re.findall(r"url\(([^)]+)\)", style, re.I):
                value = normalize_url(urljoin(detail["url"], raw.strip("'\" ")))
                add_asset(assets, seen, article_index, detail, classify_url(value), value, "inline_style")

    container_html = str(container)
    for raw in URL_PATTERN.findall(container_html):
        value = decode_wechat_redirect(normalize_url(raw))
        add_asset(assets, seen, article_index, detail, classify_url(value), value, "html_text")

    miniprograms = extract_miniprograms(container)
    for item in miniprograms:
        pseudo_value = "weapp://appid={appid};username={username};path={path}".format(**item)
        key = (article_index, "miniprogram", pseudo_value)
        if key not in seen:
            seen.add(key)
            assets.append(
                {
                    "article_index": article_index,
                    "article_title": detail.get("title", ""),
                    "article_url": detail.get("url", ""),
                    "resource_type": "miniprogram",
                    "resource_value": pseudo_value,
                    "host": "wechat_miniprogram",
                    "source": "html_miniprogram",
                    "context": item.get("label", ""),
                }
            )

    return assets, miniprograms


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


def dedupe_articles(articles):
    result = []
    seen = set()
    for article in articles:
        url = normalize_article(article_url(article))
        if not url or url in seen:
            continue
        seen.add(url)
        copied = dict(article)
        copied["url"] = url
        result.append(copied)
    return result


def analyze_one_article(raw_article, index, total, args, proxy=None):
    fetcher = None
    if args.refetch:
        fetcher = WechatArticleFetcher(
            timeout=args.timeout,
            max_retries=args.retries,
            retry_delay=args.retry_delay,
            proxy=proxy,
        )
    parser = WechatArticleParser()
    original_url = raw_article["url"]
    title_hint = raw_article.get("title", "")
    log_item = {"index": index, "title": title_hint, "url": original_url, "status": "pending"}

    detail = {
        "index": index,
        "title": title_hint,
        "author": raw_article.get("author", ""),
        "pub_time": raw_article.get("pub_time") or raw_article.get("update_time") or "",
        "url": original_url,
        "content": raw_article.get("content") or "",
        "content_length": len(raw_article.get("content") or ""),
        "fetch_status": "from_input",
        "assets_count": 0,
        "images_count": 0,
        "miniprograms_count": 0,
    }
    if proxy:
        log_item["proxy"] = proxy

    page_html = ""
    if args.refetch:
        fetched = fetcher.fetch(original_url)
        if "error" in fetched:
            detail.update({"fetch_status": "error", "error": fetched.get("error"), "message": fetched.get("message", "")})
            log_item.update({"status": "error", "error": fetched.get("error"), "message": fetched.get("message", "")})
        else:
            page_html = fetched.get("page_html", "")
            parsed = parser.parse(page_html)
            content = parsed.get("content") or detail["content"]
            detail.update(
                {
                    "title": parsed.get("title") or detail["title"],
                    "author": parsed.get("author") or detail["author"],
                    "pub_time": parsed.get("pub_time") or detail["pub_time"],
                    "url": fetched.get("source_url") or detail["url"],
                    "content": content,
                    "content_length": len(content or ""),
                    "fetch_status": "ok",
                }
            )
            log_item.update({"status": "ok", "http_status": fetched.get("status")})
    else:
        log_item.update({"status": "ok", "note": "no_refetch"})

    assets = []
    images = []
    if page_html:
        assets, miniprograms = extract_assets_from_html(page_html, index, detail)
        detail["assets_count"] = len(assets)
        detail["images_count"] = sum(1 for item in assets if item["resource_type"] == "image")
        detail["miniprograms_count"] = len(miniprograms)
        images = [item for item in assets if item["resource_type"] == "image"]

    return index, detail, assets, images, log_item


def analyze_articles(articles, account, args):
    selected = dedupe_articles(articles)
    if args.max:
        selected = selected[: args.max]

    details = [None] * len(selected)
    all_assets = []
    all_images = []
    logs = [None] * len(selected)
    total = len(selected)
    workers = max(1, int(args.workers or 16))
    proxy_pool = load_proxy_pool(args.proxy, args.proxy_file)

    print(f"\n开始分析 {account}: {total} 篇")
    if args.refetch:
        print(f"⚡ 重抓并发: {workers} 线程")
    if proxy_pool:
        print(f"🌐 代理轮询: {len(proxy_pool)} 个代理（支持 HTTP/HTTPS/SOCKS5/SOCKS5H；未写协议默认 http://）")

    if workers == 1 or not args.refetch:
        for index, raw_article in enumerate(selected, 1):
            title_hint = raw_article.get("title", "")
            print(f"  [{index}/{total}] {title_hint[:40] or raw_article['url'][:60]}")
            _, detail, assets, images, log_item = analyze_one_article(
                raw_article,
                index,
                total,
                args,
                proxy=pick_proxy(proxy_pool, index),
            )
            details[index - 1] = detail
            logs[index - 1] = log_item
            all_assets.extend(assets)
            all_images.extend(images)
            if args.delay and index < total:
                time.sleep(args.delay)
        return details, all_assets, all_images, logs

    remaining_indices = list(range(1, total + 1))
    while remaining_indices:
        wave_size = min(len(remaining_indices), workers * 2)
        wave_indices = remaining_indices[:wave_size]
        remaining_indices = remaining_indices[wave_size:]

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(
                    analyze_one_article,
                    selected[index - 1],
                    index,
                    total,
                    args,
                    pick_proxy(proxy_pool, index),
                ): index
                for index in wave_indices
            }

            for future in as_completed(future_map):
                index = future_map[future]
                raw_article = selected[index - 1]
                title_hint = raw_article.get("title", "")
                try:
                    _, detail, assets, images, log_item = future.result()
                except Exception as exc:
                    detail = {
                        "index": index,
                        "title": title_hint,
                        "author": raw_article.get("author", ""),
                        "pub_time": raw_article.get("pub_time") or raw_article.get("update_time") or "",
                        "url": raw_article["url"],
                        "content": raw_article.get("content") or "",
                        "content_length": len(raw_article.get("content") or ""),
                        "fetch_status": "error",
                        "error": "worker_exception",
                        "message": str(exc),
                        "assets_count": 0,
                        "images_count": 0,
                        "miniprograms_count": 0,
                    }
                    assets = []
                    images = []
                    log_item = {
                        "index": index,
                        "title": title_hint,
                        "url": raw_article["url"],
                        "status": "error",
                        "error": "worker_exception",
                        "message": str(exc),
                    }

                details[index - 1] = detail
                logs[index - 1] = log_item
                all_assets.extend(assets)
                all_images.extend(images)
                if log_item.get("status") == "ok":
                    print(f"  [{index}/{total}] ✅ {title_hint[:40] or raw_article['url'][:60]}")
                else:
                    print(f"  [{index}/{total}] ❌ {title_hint[:40] or raw_article['url'][:60]} {log_item.get('message', log_item.get('error', 'unknown'))}")

        if args.delay and remaining_indices:
            time.sleep(args.delay)

    return details, all_assets, all_images, logs


def unique_network_assets(assets):
    seen = set()
    rows = []
    for item in assets:
        value = item["resource_value"]
        if value in seen:
            continue
        seen.add(value)
        rows.append(
            {
                "resource_value": value,
                "resource_type": item["resource_type"],
                "host": item.get("host", ""),
                "first_article_title": item.get("article_title", ""),
                "first_article_url": item.get("article_url", ""),
            }
        )
    return rows


def non_wechat_network_assets(assets):
    return [item for item in unique_network_assets(assets) if not is_wechat_host(item.get("host"))]


def image_file_extension(url, content_type=""):
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if content_type in IMAGE_EXT_BY_CONTENT_TYPE:
        return IMAGE_EXT_BY_CONTENT_TYPE[content_type]

    path = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def download_image_assets(images, image_dir, timeout=20, max_images=None):
    download_dir = Path(image_dir) / "本地图片"
    download_dir.mkdir(parents=True, exist_ok=True)
    session = curl_requests.Session(impersonate="chrome124", timeout=timeout)
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 MicroMessenger/8.0",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )

    seen = set()
    rows = []
    for item in images:
        url = item.get("resource_value", "")
        if not url or url in seen:
            continue
        seen.add(url)
        if max_images and len(rows) >= max_images:
            break

        url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        title = safe_filename(item.get("article_title") or "image")[:40]
        row = {
            "resource_value": url,
            "article_title": item.get("article_title", ""),
            "article_url": item.get("article_url", ""),
            "host": item.get("host", ""),
            "status": "pending",
            "local_file": "",
            "message": "",
        }

        try:
            response = session.get(
                url,
                headers={
                    "Referer": item.get("article_url") or "https://mp.weixin.qq.com/",
                },
            )
            content = response.content or b""
            content_type = response.headers.get("content-type", "")
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}")
            if not content:
                raise RuntimeError("empty response")

            ext = image_file_extension(url, content_type)
            local_name = f"{len(rows) + 1:04d}_{title}_{url_hash}{ext}"
            local_path = download_dir / local_name
            with open(local_path, "wb") as fp:
                fp.write(content)

            row.update(
                {
                    "status": "ok",
                    "local_file": str(local_path.relative_to(image_dir.parent)),
                    "bytes": len(content),
                    "content_type": content_type,
                }
            )
        except Exception as exc:
            row.update({"status": "error", "message": str(exc)})

        rows.append(row)

    return rows


def write_downloaded_images_text(path, rows):
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("本地图片下载清单\n")
        fp.write("=" * 80 + "\n\n")
        if not rows:
            fp.write("未发现可下载图片。\n")
            return
        for index, item in enumerate(rows, 1):
            fp.write(f"[{index}] {item.get('status')}\n")
            fp.write(f"本地文件：{item.get('local_file', '')}\n")
            fp.write(f"原始URL：{item.get('resource_value', '')}\n")
            fp.write(f"来源文章：{item.get('article_title', '')}\n")
            fp.write(f"文章URL：{item.get('article_url', '')}\n")
            if item.get("bytes"):
                fp.write(f"大小：{item.get('bytes')} bytes\n")
            if item.get("content_type"):
                fp.write(f"类型：{item.get('content_type')}\n")
            if item.get("message"):
                fp.write(f"说明：{item.get('message')}\n")
            fp.write("\n")


def write_asset_text(path, rows, title):
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(title + "\n")
        fp.write("=" * 80 + "\n\n")
        if not rows:
            fp.write("未发现。\n")
            return
        for index, item in enumerate(rows, 1):
            fp.write(f"[{index}] {item.get('resource_value', '')}\n")
            fp.write(f"类型：{item.get('resource_type', '')}\n")
            fp.write(f"域名：{item.get('host', '')}\n")
            fp.write(f"来源文章：{item.get('article_title') or item.get('first_article_title', '')}\n")
            fp.write(f"文章URL：{item.get('article_url') or item.get('first_article_url', '')}\n")
            if item.get("source"):
                fp.write(f"提取位置：{item.get('source')}\n")
            if item.get("context"):
                fp.write(f"上下文：{item.get('context')}\n")
            fp.write("\n")


def save_reports(
    report_dir,
    account,
    source_file,
    details,
    assets,
    images,
    logs,
    keep_json=False,
    download_images=True,
    image_download_timeout=20,
    image_download_max=None,
):
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    image_dir = report_dir / "图片资源"
    asset_dir = report_dir / "资产明细"
    image_dir.mkdir(exist_ok=True)
    asset_dir.mkdir(exist_ok=True)
    network = unique_network_assets(assets)
    external_network = non_wechat_network_assets(assets)
    downloaded_images = []
    if download_images:
        print(f"🖼️  下载图片到本地: {image_dir / '本地图片'}")
        downloaded_images = download_image_assets(
            images,
            image_dir,
            timeout=image_download_timeout,
            max_images=image_download_max,
        )

    type_counts = {}
    host_counts = {}
    for item in assets:
        type_counts[item["resource_type"]] = type_counts.get(item["resource_type"], 0) + 1
        host = item.get("host") or "unknown"
        host_counts[host] = host_counts.get(host, 0) + 1

    summary = {
        "account": account,
        "source_file": str(source_file),
        "generated_at": datetime.now().isoformat(),
        "articles": len(details),
        "success": sum(1 for item in details if item.get("fetch_status") == "ok"),
        "assets": len(assets),
        "unique_network_assets": len(network),
        "non_wechat_network_assets": len(external_network),
        "images": len(images),
        "downloaded_images": sum(1 for item in downloaded_images if item.get("status") == "ok"),
        "failed_image_downloads": sum(1 for item in downloaded_images if item.get("status") == "error"),
        "errors": sum(1 for item in details if item.get("fetch_status") == "error"),
    }

    with open(report_dir / "分析报告.md", "w", encoding="utf-8") as fp:
        fp.write(f"# {account} 分析报告\n\n")
        fp.write("## 基本信息\n\n")
        fp.write(f"- 来源文件：{source_file}\n")
        fp.write(f"- 生成时间：{summary['generated_at']}\n")
        fp.write(f"- 文章数量：{summary['articles']}\n")
        fp.write(f"- 成功重新获取详情：{summary['success']}\n")
        fp.write(f"- 获取失败：{summary['errors']}\n")
        fp.write(f"- 发现资源总数：{summary['assets']}\n")
        fp.write(f"- 唯一网络资产：{summary['unique_network_assets']}\n")
        fp.write(f"- 非微信网络资产：{summary['non_wechat_network_assets']}\n")
        fp.write(f"- 图片资源：{summary['images']}\n\n")
        fp.write(f"- 本地图片下载成功：{summary['downloaded_images']}\n")
        fp.write(f"- 本地图片下载失败：{summary['failed_image_downloads']}\n\n")

        fp.write("## 资源类型统计\n\n")
        if type_counts:
            for name, count in sorted(type_counts.items(), key=lambda kv: kv[0]):
                fp.write(f"- {name}：{count}\n")
        else:
            fp.write("- 未发现资源\n")

        fp.write("\n## 域名统计\n\n")
        if host_counts:
            for host, count in sorted(host_counts.items(), key=lambda kv: kv[1], reverse=True):
                fp.write(f"- {host}：{count}\n")
        else:
            fp.write("- 未发现域名\n")

        fp.write("\n## 文章详情\n\n")
        for item in details:
            fp.write(f"### [{item.get('index')}] {item.get('title') or '无标题'}\n\n")
            fp.write(f"- URL：{item.get('url')}\n")
            fp.write(f"- 作者：{item.get('author') or '未知'}\n")
            fp.write(f"- 时间：{item.get('pub_time') or '未知'}\n")
            fp.write(f"- 正文字数：{item.get('content_length', 0)}\n")
            fp.write(f"- 资源数量：{item.get('assets_count', 0)}\n")
            fp.write(f"- 图片数量：{item.get('images_count', 0)}\n")
            fp.write(f"- 小程序数量：{item.get('miniprograms_count', 0)}\n")
            if item.get("error"):
                fp.write(f"- 错误：{item.get('error')} {item.get('message', '')}\n")
            content = (item.get("content") or "").strip()
            if content:
                fp.write("\n正文摘录：\n\n")
                fp.write(content[:1200])
                if len(content) > 1200:
                    fp.write("\n\n...（正文过长，已截断）")
            fp.write("\n\n")

    with open(report_dir / "网络资产.txt", "w", encoding="utf-8") as fp:
        for item in network:
            fp.write(item["resource_value"] + "\n")

    with open(report_dir / "非微信网络资产.txt", "w", encoding="utf-8") as fp:
        for item in external_network:
            fp.write(item["resource_value"] + "\n")

    write_asset_text(image_dir / "图片资源清单.txt", images, "图片资源清单")
    write_downloaded_images_text(image_dir / "本地图片清单.txt", downloaded_images)
    write_asset_text(asset_dir / "全部资产清单.txt", assets, "全部资产清单")
    write_asset_text(asset_dir / "非微信网络资产清单.txt", external_network, "非微信网络资产清单")

    assets_by_type = {}
    for item in assets:
        assets_by_type.setdefault(item.get("resource_type") or "unknown", []).append(item)
    for resource_type, rows in sorted(assets_by_type.items()):
        write_asset_text(asset_dir / f"{safe_filename(resource_type)}.txt", rows, f"{resource_type} 清单")

    with open(report_dir / "运行日志.txt", "w", encoding="utf-8") as fp:
        fp.write(f"公众号：{account}\n")
        fp.write(f"来源文件：{source_file}\n")
        fp.write(f"生成时间：{summary['generated_at']}\n\n")
        for item in logs:
            fp.write("[{index}] {status} {title}\n".format(**item))
            fp.write(f"URL：{item.get('url', '')}\n")
            if item.get("error"):
                fp.write(f"错误：{item.get('error')}\n")
            if item.get("message"):
                fp.write(f"说明：{item.get('message')}\n")
            if item.get("http_status") is not None:
                fp.write(f"HTTP状态：{item.get('http_status')}\n")
            fp.write("\n")

    if keep_json:
        write_json(report_dir / "原始详情.json", details)
        write_json(report_dir / "原始资源.json", assets)
        write_json(report_dir / "原始日志.json", logs)
        write_json(report_dir / "本地图片下载.json", downloaded_images)

    return summary


def parse_args():
    parser = argparse.ArgumentParser(description="选择 output JSON 并生成公众号文章资产报告")
    parser.add_argument("-i", "--input", help="指定 JSON 文件；不指定则交互选择")
    parser.add_argument("-o", "--output-dir", help="报告输出目录；默认在当前目录生成")
    parser.add_argument("--project-dir", default=str(CLAW_DIR), help="项目目录")
    parser.add_argument("--output-json-dir", default=None, help="JSON 输出目录；默认 project-dir/output")
    parser.add_argument("--max", type=int, default=None, help="最多处理文章数")
    parser.add_argument("--delay", type=float, default=0.0, help="并发批次间隔秒数，单线程时为每篇间隔秒数")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--workers", "-w", type=int, default=16, help="重抓页面并发线程数，默认 16")
    parser.add_argument(
        "--proxy",
        "-p",
        action="append",
        default=None,
        help="重抓页面代理，可重复传入，也支持逗号/分号分隔；支持 http://、https://、socks5://、socks5h://",
    )
    parser.add_argument("--proxy-file", "-pf", help="代理文件，每行一个代理，# 开头为注释")
    parser.add_argument("--no-refetch", dest="refetch", action="store_false", help="不重新请求页面，仅整理已有 JSON")
    parser.add_argument("--keep-json", action="store_true", help="额外保留原始 JSON 明细")
    parser.set_defaults(refetch=True)
    return parser.parse_args()


def main():
    args = parse_args()
    project_dir = Path(args.project_dir).resolve()
    output_json_dir = Path(args.output_json_dir).resolve() if args.output_json_dir else project_dir / "output"
    source_file = Path(args.input).resolve() if args.input else choose_input_file(output_json_dir)

    if not source_file.exists():
        print(f"[x] 文件不存在: {source_file}")
        return 1

    account, articles = load_articles(source_file)
    if not articles:
        print(f"[x] 没有文章: {source_file}")
        return 1

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = Path(args.output_dir).resolve() if args.output_dir else BASE_DIR / f"{safe_filename(account)}_analysis_{timestamp}"
    details, assets, images, logs = analyze_articles(articles, account, args)
    summary = save_reports(report_dir, account, source_file, details, assets, images, logs, keep_json=args.keep_json)

    print("\n完成")
    print(f"报告目录: {report_dir}")
    print(f"文章数: {summary['articles']}")
    print(f"成功详情: {summary['success']}")
    print(f"图片数: {summary['images']}")
    print(f"网络资产: {summary['unique_network_assets']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

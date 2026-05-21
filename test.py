# coding: utf-8
import html
import argparse
import io
import json
import os
import re
import sys
import time
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import cv2
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
from PIL import Image
from wechatarticles.utils import timestamp2date

try:
    import ddddocr
except Exception:
    ddddocr = None


OUTPUT_FILE = "wechat_external_resources.xlsx"
URLS_FILE = "urls.txt"
REQUEST_TIMEOUT = 20
PAGE_SLEEP_SECONDS = 5
ARTICLE_SLEEP_SECONDS = 1
MAX_IMAGE_SCAN = int(os.getenv("WECHAT_MAX_IMAGE_SCAN", "30"))
SCAN_IMAGE_QR = os.getenv("WECHAT_SCAN_IMAGE_QR", "1") != "0"
SCAN_IMAGE_OCR = os.getenv("WECHAT_SCAN_IMAGE_OCR", "1") != "0"

URL_PATTERN = re.compile(r"https?://[^\s\"'<>，。；;）)]+", re.I)
MINIPROGRAM_HINTS = (
    "weapp",
    "miniprogram",
    "小程序",
    "data-miniprogram",
    "weapp_username",
    "weapp_path",
)


def normalize_wechat_param(value):
    if not value:
        return value
    return unquote(value.strip())


def normalize_url(url):
    return html.unescape(html.unescape(url or "")).replace("\\/", "/").strip()


def parse_burp_request_text(text):
    text = text or ""
    lines = text.splitlines()
    request_line = lines[0] if lines else ""
    host_match = re.search(r"(?im)^\s*Host:\s*([^\s]+)\s*$", text)
    host = host_match.group(1).strip() if host_match else ""

    url = ""
    if request_line:
        request_target = request_line.split(" ")[1] if len(request_line.split(" ")) > 1 else ""
        if request_target.startswith("http://") or request_target.startswith("https://"):
            url = normalize_url(request_target)
        elif host and request_target:
            url = normalize_url("https://{}{}".format(host, request_target))

    params = {}
    if url:
        parsed = urlparse(url)
        params.update({k: v[0] for k, v in parse_qs(parsed.query).items() if v})
    else:
        url_match = re.search(r"(https?://[^\s]+)", text)
        if url_match:
            url = normalize_url(url_match.group(1))
            parsed = urlparse(url)
            params.update({k: v[0] for k, v in parse_qs(parsed.query).items() if v})

    cookie_match = re.search(r"(?im)^\s*Cookie:\s*(.+)$", text)
    cookie = cookie_match.group(1).strip() if cookie_match else ""

    return {
        "url": url,
        "params": params,
        "cookie": cookie,
        "raw": text,
    }


def parse_cookie_value(cookie, name):
    if not cookie:
        return ""
    match = re.search(r"(?:^|;\s*)" + re.escape(name) + r"=([^;]+)", cookie)
    return match.group(1).strip() if match else ""


def parse_wechat_inputs_from_request(request_text):
    parsed = parse_burp_request_text(request_text)
    url = parsed["url"]
    params = parsed["params"]
    cookie = parsed["cookie"]

    result = {
        "url": url,
        "cookie": cookie,
        "biz": params.get("__biz", ""),
        "uin": params.get("uin", parse_cookie_value(cookie, "wxuin") or parse_cookie_value(cookie, "uin")),
        "key": params.get("key", ""),
        "appmsg_token": params.get("appmsg_token", parse_cookie_value(cookie, "appmsg_token")),
    }
    return result


def get_article_items(message):
    timestamp = message["comm_msg_info"]["datetime"]
    date = timestamp2date(timestamp)
    ext_info = message.get("app_msg_ext_info") or {}
    article_infos = []

    if ext_info.get("content_url"):
        article_infos.append(
            {
                "title": ext_info.get("title", ""),
                "article_url": normalize_url(ext_info.get("content_url", "")),
                "publish_time": date,
            }
        )

    for item in ext_info.get("multi_app_msg_item_list") or []:
        if item.get("content_url"):
            article_infos.append(
                {
                    "title": item.get("title", ""),
                    "article_url": normalize_url(item.get("content_url", "")),
                    "publish_time": date,
                }
            )

    return article_infos


def fetch_history_articles(biz, uin, key, cookie, start_count=0, end_count=10):
    session = requests.Session()
    headers = build_headers(cookie)
    articles = []
    offset = start_count

    while offset < end_count:
        params = {
            "action": "getmsg",
            "__biz": biz,
            "f": "json",
            "offset": str(offset),
            "count": "10",
            "uin": uin,
            "key": key,
        }
        response = session.get(
            "https://mp.weixin.qq.com/mp/profile_ext",
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        if "general_msg_list" not in data:
            print(
                "历史列表获取失败：ret={}, errmsg={}".format(
                    data.get("ret"), data.get("errmsg")
                )
            )
            print("请重新复制同一个 profile_ext?action=getmsg 请求里的 key、uin、Cookie。")
            return articles

        messages = [
            item
            for item in json.loads(data["general_msg_list"]).get("list", [])
            if "app_msg_ext_info" in item
        ]
        if not messages:
            break

        for message in messages:
            articles.extend(get_article_items(message))

        offset += 10
        last_datetime = messages[-1]["comm_msg_info"]["datetime"]
        print("已抓取历史消息 offset={}, 最后一条日期={}".format(offset, timestamp2date(last_datetime)))

        if offset < end_count:
            time.sleep(PAGE_SLEEP_SECONDS)

    return articles


def load_article_urls(urls_file):
    if not os.path.exists(urls_file):
        return []

    urls = []
    with open(urls_file, "r", encoding="utf-8") as fp:
        for line in fp:
            url = normalize_url(line.strip())
            if not url or url.startswith("#"):
                continue
            if is_http_url(url):
                urls.append(url)
    return urls


def build_headers(cookie):
    return {
        "Cookie": cookie,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://mp.weixin.qq.com/",
    }


def decode_wechat_redirect(url):
    parsed = urlparse(url)
    if parsed.hostname != "mp.weixin.qq.com":
        return url

    query = parse_qs(parsed.query)
    for param_name in ("url", "target", "redirect_url"):
        values = query.get(param_name)
        if values and values[0]:
            return normalize_url(values[0])

    return url


def is_http_url(url):
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def is_wechat_internal(url):
    hostname = (urlparse(url).hostname or "").lower()
    if not hostname:
        return False
    internal_hosts = {
        "mp.weixin.qq.com",
        "mmbiz.qpic.cn",
        "mmbiz.qlogo.cn",
        "res.wx.qq.com",
        "weixin.qq.com",
        "wx.qlogo.cn",
    }
    return hostname in internal_hosts or hostname.endswith(".qq.com")


def classify_http_resource(url):
    hostname = (urlparse(url).hostname or "").lower()
    if hostname in {"mmbiz.qpic.cn", "mmbiz.qlogo.cn", "wx.qlogo.cn"}:
        return "image"
    if is_wechat_internal(url):
        return "wechat_internal"
    return "external_url"


def add_row(rows, seen, article, resource_type, value, source, context=""):
    value = normalize_url(value)
    if not value:
        return

    key = (
        article["article_url"],
        resource_type,
        value,
    )
    if key in seen:
        return

    seen.add(key)
    rows.append(
        {
            "publish_time": article["publish_time"],
            "article_title": article["title"],
            "article_url": article["article_url"],
            "resource_type": resource_type,
            "resource_value": value,
            "source": source,
            "context": context,
        }
    )


def collect_tag_urls(soup, article_url):
    attrs = ("href", "src", "data-src", "data-original", "data-backsrc")
    for tag in soup.find_all(True):
        for attr in attrs:
            value = tag.get(attr)
            if not value:
                continue
            yield tag, attr, normalize_url(urljoin(article_url, value))


def get_article_container(soup):
    selectors = [
        {"id": "img-content"},
        {"class_": "rich_media_content"},
        {"class_": "rich_media_area_primary_inner"},
    ]
    for kwargs in selectors:
        node = soup.find(**kwargs)
        if node is not None:
            return node
    return soup


def extract_miniprograms(soup):
    for tag in soup.find_all(True):
        attrs = {key: value for key, value in tag.attrs.items() if isinstance(value, str)}
        joined = " ".join([tag.name, *attrs.keys(), *attrs.values()])
        if not any(hint in joined for hint in MINIPROGRAM_HINTS):
            continue

        username = (
            attrs.get("data-miniprogram-appid")
            or attrs.get("data-weappid")
            or attrs.get("weapp_username")
            or attrs.get("data-weapp-username")
            or attrs.get("appid")
            or ""
        )
        path = (
            attrs.get("data-miniprogram-path")
            or attrs.get("data-weapp-path")
            or attrs.get("weapp_path")
            or attrs.get("path")
            or ""
        )
        label = tag.get_text(" ", strip=True)
        value = "appid_or_username={}, path={}".format(username, path).strip()
        if username or path:
            yield value, label


def read_image_bytes(session, headers, image_url):
    response = session.get(image_url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "image" not in content_type and not image_url.lower().split("?")[0].endswith(
        (".jpg", ".jpeg", ".png", ".webp", ".bmp")
    ):
        return None
    return response.content


def decode_qr_from_image(image_bytes):
    image_array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
    if image is None:
        return []

    detector = cv2.QRCodeDetector()
    values = []
    value, _, _ = detector.detectAndDecode(image)
    if value:
        values.append(value)

    try:
        ok, decoded_values, _, _ = detector.detectAndDecodeMulti(image)
        if ok:
            values.extend([item for item in decoded_values if item])
    except Exception:
        pass

    return list(dict.fromkeys(values))


def ocr_urls_from_image(image_bytes, ocr):
    if ocr is None:
        return []

    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.thumbnail((1600, 1600))
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=90)
        text = ocr.classification(buffer.getvalue())
    except Exception:
        return []

    return URL_PATTERN.findall(text or "")


def scan_images_for_resources(article, image_urls, session, headers):
    rows = []
    seen = set()
    ocr = None
    if SCAN_IMAGE_OCR and ddddocr is not None:
        try:
            ocr = ddddocr.DdddOcr(show_ad=False)
        except TypeError:
            ocr = ddddocr.DdddOcr()
        except Exception:
            ocr = None

    for image_index, image_url in enumerate(image_urls[:MAX_IMAGE_SCAN], 1):
        try:
            image_bytes = read_image_bytes(session, headers, image_url)
            if not image_bytes:
                continue
        except Exception as exc:
            add_row(rows, seen, article, "image_scan_error", image_url, "image", str(exc))
            continue

        if SCAN_IMAGE_QR:
            for qr_value in decode_qr_from_image(image_bytes):
                add_row(rows, seen, article, "qr_code", qr_value, image_url, "image #{}".format(image_index))
                if is_http_url(qr_value):
                    add_row(
                        rows,
                        seen,
                        article,
                        classify_http_resource(qr_value),
                        decode_wechat_redirect(qr_value),
                        "qr_code",
                        image_url,
                    )

        if SCAN_IMAGE_OCR:
            for url in ocr_urls_from_image(image_bytes, ocr):
                url = decode_wechat_redirect(normalize_url(url))
                if is_http_url(url):
                    add_row(
                        rows,
                        seen,
                        article,
                        classify_http_resource(url),
                        url,
                        "image_ocr",
                        image_url,
                    )

    return rows


def extract_resources_from_article(article, session, headers):
    response = session.get(article["article_url"], headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    html_text = response.text
    soup = BeautifulSoup(html_text, "html.parser")
    container = get_article_container(soup)

    if "请在微信客户端打开" in html_text or "环境异常" in html_text:
        raise RuntimeError("当前返回的是微信校验页，不是文章正文；请带上文章请求的 Cookie 重试")

    rows = []
    seen = set()
    image_urls = []

    for tag, attr, raw_url in collect_tag_urls(container, article["article_url"]):
        url = decode_wechat_redirect(raw_url)
        if not is_http_url(url):
            continue

        resource_type = classify_http_resource(url)
        if resource_type == "wechat_internal":
            continue
        context = tag.get_text(" ", strip=True)
        add_row(rows, seen, article, resource_type, url, "html_{}".format(attr), context)

        if resource_type == "image":
            image_urls.append(url)

    container_text = str(container)
    for raw_url in URL_PATTERN.findall(container_text):
        url = decode_wechat_redirect(normalize_url(raw_url))
        if is_http_url(url):
            resource_type = classify_http_resource(url)
            if resource_type == "wechat_internal":
                continue
            add_row(rows, seen, article, resource_type, url, "html_text")
            if resource_type == "image":
                image_urls.append(url)

    for value, label in extract_miniprograms(container):
        add_row(rows, seen, article, "miniprogram", value, "html_miniprogram", label)

    rows.extend(scan_images_for_resources(article, list(dict.fromkeys(image_urls)), session, headers))
    return rows


def save_results(rows, output_file):
    columns = [
        "publish_time",
        "article_title",
        "article_url",
        "resource_type",
        "resource_value",
        "source",
        "context",
    ]
    pd.DataFrame(rows, columns=columns).to_excel(output_file, index=False)


def dedupe_articles(articles):
    result = []
    seen = set()
    for article in articles:
        url = normalize_url(article.get("article_url", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        article["article_url"] = url
        result.append(article)
    return result


def parse_args():
    parser = argparse.ArgumentParser(description="微信公众号文章资源提取")
    parser.add_argument(
        "--mode",
        "-m",
        choices=["a", "u", "h", "auto", "urls", "history"],
        default="a",
        help="a=auto，u=urls，h=history",
    )
    parser.add_argument("--u", "--urls-file", dest="urls_file", default=URLS_FILE, help="urls.txt")
    parser.add_argument("--o", "--output", dest="output", default=OUTPUT_FILE, help="输出文件")
    parser.add_argument("--b", "--biz", dest="biz", default=os.getenv("WECHAT_BIZ", "Mzg3NzkyNTc5Nw%3D%3D"))
    parser.add_argument("--n", "--uin", dest="uin", default=os.getenv("WECHAT_UIN", "MTc1NTQxMDI2OA%3D%3D"))
    parser.add_argument("--k", "--key", dest="key", default=os.getenv("WECHAT_HISTORY_KEY", ""))
    parser.add_argument("--hc", "--history-cookie", dest="history_cookie", default=os.getenv("WECHAT_HISTORY_COOKIE", ""))
    parser.add_argument("--ac", "--article-cookie", dest="article_cookie", default=os.getenv("WECHAT_ARTICLE_COOKIE", ""))
    parser.add_argument("--e", "--end-count", dest="end_count", type=int, default=int(os.getenv("WECHAT_END_COUNT", "10")))
    parser.add_argument("--mi", "--max-image-scan", dest="max_image_scan", type=int, default=int(os.getenv("WECHAT_MAX_IMAGE_SCAN", "30")))
    parser.add_argument(
        "--qr",
        "--scan-image-qr",
        dest="scan_image_qr",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("WECHAT_SCAN_IMAGE_QR", "1") != "0",
    )
    parser.add_argument(
        "--oc",
        "--scan-image-ocr",
        dest="scan_image_ocr",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("WECHAT_SCAN_IMAGE_OCR", "1") != "0",
    )
    parser.add_argument("--clip", action="store_true", help="从剪贴板读取 Burp 请求并自动解析")
    return parser.parse_args()


def main():
    args = parse_args()

    mode = args.mode
    if mode == "a":
        mode = "auto"
    elif mode == "u":
        mode = "urls"
    elif mode == "h":
        mode = "history"

    global OUTPUT_FILE, URLS_FILE, MAX_IMAGE_SCAN, SCAN_IMAGE_QR, SCAN_IMAGE_OCR
    OUTPUT_FILE = args.output
    URLS_FILE = args.urls_file
    MAX_IMAGE_SCAN = args.max_image_scan
    SCAN_IMAGE_QR = args.scan_image_qr
    SCAN_IMAGE_OCR = args.scan_image_ocr

    articles = []
    history_cookie = args.history_cookie
    article_cookie = args.article_cookie
    history_mode = False

    if args.clip:
        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            request_text = root.clipboard_get()
            root.destroy()
        except Exception as exc:
            print("无法读取剪贴板，请先复制 Burp 里的完整请求。")
            print(exc)
            sys.exit(1)

        parsed = parse_wechat_inputs_from_request(request_text)
        print("已解析剪贴板请求")
        print("url:", parsed["url"] or "(none)")
        print("biz:", parsed["biz"] or "(none)")
        print("uin:", parsed["uin"] or "(none)")
        print("key length:", len(parsed["key"]))
        print("cookie length:", len(parsed["cookie"]))

        if parsed["url"]:
            articles.append(
                {
                    "title": "CLIP URL",
                    "article_url": parsed["url"],
                    "publish_time": "",
                }
            )
            article_cookie = parsed["cookie"] or article_cookie
        else:
            if not all([parsed["biz"], parsed["uin"], parsed["key"], parsed["cookie"]]):
                print("剪贴板里没有足够的参数。")
                print("文章链接请求：至少要 URL + Cookie。")
                print("历史列表请求：要 __biz、uin、key、Cookie。")
                sys.exit(1)
            history_mode = True
            articles = fetch_history_articles(
                normalize_wechat_param(parsed["biz"]),
                normalize_wechat_param(parsed["uin"]),
                normalize_wechat_param(parsed["key"]),
                parsed["cookie"],
                end_count=args.end_count,
            )
            if not articles:
                print("未从剪贴板请求中抓到历史文章。")
                sys.exit(1)

    if not articles and mode in {"auto", "urls"}:
        urls = load_article_urls(URLS_FILE)
        if urls:
            print("已读取 urls.txt，文章数量:", len(urls))
            for index, url in enumerate(urls, 1):
                articles.append(
                    {
                        "title": "URL {}".format(index),
                        "article_url": normalize_url(url),
                        "publish_time": "",
                    }
                )
        elif mode == "urls":
            print("urls 模式下未找到可用链接文件:", URLS_FILE)
            sys.exit(1)

    if not articles:
        biz = normalize_wechat_param(args.biz)
        uin = normalize_wechat_param(args.uin)
        key = normalize_wechat_param(args.key)
        if not all([biz, uin, key, history_cookie]):
            print("历史列表模式缺少必要参数。")
            print("需要：--biz --uin --key --history-cookie")
            print("或者先创建 urls.txt 并使用 --mode urls/auto。")
            sys.exit(1)

        history_mode = True
        print("开始抓取历史文章列表")
        print("biz:", biz)
        print("uin:", uin)
        print("key length:", len(key))
        print("end_count:", args.end_count)
        print("scan qr:", SCAN_IMAGE_QR)
        print("scan image ocr:", SCAN_IMAGE_OCR and ddddocr is not None)
        print("max image scan per article:", MAX_IMAGE_SCAN)

        articles = fetch_history_articles(
            biz,
            uin,
            key,
            history_cookie,
            end_count=args.end_count,
        )
        if not articles:
            print("未获取到历史文章，请检查 profile_ext?action=getmsg 的 key/Cookie 是否过期。")
            sys.exit(1)

        print("历史文章数量:", len(articles))

    if not articles:
        print("没有可处理的文章链接。")
        sys.exit(1)

    articles = dedupe_articles(articles)
    print("去重后文章数量:", len(articles))

    session = requests.Session()
    headers = (
        build_headers(history_cookie)
        if history_mode
        else {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
    )
    if not history_mode and article_cookie:
        headers["Cookie"] = article_cookie
    all_rows = []

    for index, article in enumerate(articles, 1):
        print("检查文章 {}/{}: {}".format(index, len(articles), article["title"]))
        try:
            rows = extract_resources_from_article(article, session, headers)
            all_rows.extend(rows)
            print("  资源数量:", len(rows))
        except Exception as exc:
            print("  文章处理失败:", exc)

        time.sleep(ARTICLE_SLEEP_SECONDS)

    save_results(all_rows, OUTPUT_FILE)
    print("完成，资源数量:", len(all_rows))
    print("结果文件:", OUTPUT_FILE)
    if not history_mode:
        print("当前使用的是 urls 模式，不需要历史列表参数。")


if __name__ == "__main__":
    main()

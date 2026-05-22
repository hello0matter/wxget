# coding: utf-8
"""
微信公众号文章内容批量抓取

读取 crawler.py 生成的文章列表 JSON，逐篇抓取文章正文内容。
依赖 read_wechat_article.py 中的 WechatArticleFetcher 和 WechatArticleParser。

用法:
  python fetch_content.py output/数字生命卡兹克_20260324_152815.json
  python fetch_content.py output/数字生命卡兹克_20260324_152815.json --max 5
"""

import json
import os
import sys
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from read_wechat_article import WechatArticleFetcher, WechatArticleParser


def summarize_fetch_error(fetched):
    logs = fetched.get("logs") or {}
    attempts = logs.get("attempts") or []
    last_attempt = attempts[-1] if attempts else {}
    status = logs.get("http_status") or last_attempt.get("status")
    error_text = last_attempt.get("error") or fetched.get("message") or fetched.get("error", "unknown")
    parts = [str(fetched.get("error", "unknown"))]
    if status is not None:
        parts.append(f"HTTP {status}")
    if error_text and error_text != fetched.get("error"):
        parts.append(str(error_text))
    return " / ".join(parts)


def load_article_list(json_path):
    """加载 crawler.py 生成的文章列表 JSON"""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 支持两种格式：直接列表 或 带 metadata 的字典
    if isinstance(data, list):
        return data, "未知"
    elif isinstance(data, dict):
        return data.get("articles", []), data.get("account", "未知")
    else:
        print("[✗] 无法识别的 JSON 格式")
        sys.exit(1)


def normalize_proxy(proxy):
    """标准化代理地址；未写协议时默认按 HTTP 代理处理。"""
    proxy = str(proxy).strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        return f"http://{proxy}"
    return proxy


def load_proxy_pool(proxies=None, proxy_file=None):
    """加载代理池，支持 list、逗号分隔字符串和按行保存的文件。"""
    proxy_pool = []

    if proxies:
        if isinstance(proxies, str):
            proxy_pool.extend(item.strip() for item in proxies.replace(";", ",").split(","))
        else:
            for item in proxies:
                if isinstance(item, str):
                    proxy_pool.extend(part.strip() for part in item.replace(";", ",").split(","))
                else:
                    proxy_pool.append(str(item).strip())

    if proxy_file:
        with open(proxy_file, "r", encoding="utf-8") as f:
            for line in f:
                proxy = line.strip()
                if proxy and not proxy.startswith("#"):
                    proxy_pool.extend(item.strip() for item in proxy.replace(";", ",").split(","))

    return [proxy for proxy in (normalize_proxy(item) for item in proxy_pool) if proxy]


def pick_proxy(proxy_pool, article_index):
    """按文章序号轮询代理；返回 None 时表示直连。"""
    if not proxy_pool:
        return None
    return proxy_pool[article_index % len(proxy_pool)]


def compact_results(results):
    """去掉尚未完成的占位项，并保持原始顺序。"""
    return [result for result in results if result is not None]


def fetch_one_article(article, article_index, total, timeout, max_retries, proxy=None):
    """抓取并解析单篇文章，便于串行和并发复用。"""
    fetcher = WechatArticleFetcher(timeout=timeout, max_retries=max_retries, proxy=proxy)
    parser = WechatArticleParser()

    url = article.get("link") or article.get("url") or article.get("content_url")
    title = article.get("title", "无标题")

    if not url:
        return article_index, {
            "title": title,
            "url": None,
            "error": "missing_url",
            "content": None,
        }, False

    if url.startswith("http://"):
        url = url.replace("http://", "https://", 1)

    fetched = fetcher.fetch(url)

    if "error" in fetched:
        return article_index, {
            "title": title,
            "url": url,
            "error": fetched["error"],
            "message": summarize_fetch_error(fetched),
            "logs": fetched.get("logs", {}),
            "content": None,
        }, False

    parsed = parser.parse(fetched["page_html"])
    content = parsed.get("content", "")
    result = {
        "title": parsed.get("title") or title,
        "author": parsed.get("author", ""),
        "pub_time": parsed.get("pub_time", ""),
        "url": fetched["source_url"],
        "content": content,
        "content_length": len(content),
    }

    return article_index, result, bool(content)


def fetch_all_content(
    articles,
    max_articles=None,
    delay=3,
    timeout=20,
    max_retries=3,
    progress_callback=None,
    workers=16,
    proxies=None,
    proxy_file=None,
    adaptive=True,
    error_threshold=0.5,
    min_workers=1,
):
    """
    批量抓取文章正文内容

    Parameters
    ----------
    articles : list
        文章列表（每项需包含 link 或 url 字段）
    max_articles : int, optional
        最多抓取的文章数量
    delay : int
        每篇文章之间的等待秒数
    timeout : int
        单次 HTTP 请求超时秒数
    max_retries : int
        每篇文章的最大重试次数
    progress_callback : callable, optional
        每处理完一篇文章后接收当前结果列表，用于 checkpoint 落盘
    workers : int
        正文抓取并发数；列表接口不受此参数影响
    proxies : list or str, optional
        代理池，支持 http://、https://、socks5://、socks5h://
    proxy_file : str, optional
        按行保存代理的文件路径
    adaptive : bool
        错误率过高时是否自动降低并发
    error_threshold : float
        单轮失败率达到该值时降低并发
    min_workers : int
        自动降低并发时的最小并发数
    """
    if max_articles:
        articles = articles[:max_articles]

    total = len(articles)
    results = [None] * total
    success_count = 0
    fail_count = 0
    worker_count = max(1, int(workers or 16))
    min_worker_count = max(1, min(int(min_workers or 1), worker_count))
    proxy_pool = load_proxy_pool(proxies=proxies, proxy_file=proxy_file)

    print(f"\n📖 开始抓取 {total} 篇文章正文...\n")
    if worker_count > 1:
        print(f"⚡ 正文并发: {worker_count} 线程")
    if proxy_pool:
        print(f"🌐 代理轮询: {len(proxy_pool)} 个代理（支持 HTTP/HTTPS/SOCKS5/SOCKS5H；未写协议默认 http://）")

    if worker_count == 1:
        for article_index, article in enumerate(articles):
            title = article.get("title", "无标题")
            print(f"  [{article_index+1}/{total}] 抓取: {title[:40]}...")
            _, result, ok = fetch_one_article(
                article,
                article_index,
                total,
                timeout,
                max_retries,
                proxy=pick_proxy(proxy_pool, article_index),
            )
            results[article_index] = result
            if ok:
                print(f"           ✅ 成功 ({result.get('content_length', 0)} 字)")
                success_count += 1
            else:
                print(f"           ❌ 失败: {result.get('message', result.get('error', 'unknown'))}")
                fail_count += 1
            if progress_callback:
                progress_callback(compact_results(results))
            if article_index < total - 1:
                time.sleep(delay)

        print(f"\n{'='*50}")
        print(f"✅ 抓取完成: 成功 {success_count} / 失败 {fail_count} / 总计 {total}")
        return compact_results(results)

    remaining_indices = list(range(total))
    current_workers = worker_count

    while remaining_indices:
        wave_size = min(len(remaining_indices), current_workers * 2)
        wave_indices = remaining_indices[:wave_size]
        remaining_indices = remaining_indices[wave_size:]
        wave_success_count = 0
        wave_fail_count = 0

        with ThreadPoolExecutor(max_workers=current_workers) as executor:
            future_map = {
                executor.submit(
                    fetch_one_article,
                    articles[article_index],
                    article_index,
                    total,
                    timeout,
                    max_retries,
                    pick_proxy(proxy_pool, article_index),
                ): article_index
                for article_index in wave_indices
            }

            for future in as_completed(future_map):
                article_index = future_map[future]
                title = articles[article_index].get("title", "无标题")
                try:
                    _, result, ok = future.result()
                except Exception as exc:
                    result = {
                        "title": title,
                        "url": articles[article_index].get("link") or articles[article_index].get("url"),
                        "error": "worker_exception",
                        "message": str(exc),
                        "content": None,
                    }
                    ok = False

                results[article_index] = result
                if ok:
                    success_count += 1
                    wave_success_count += 1
                    print(f"  [{article_index+1}/{total}] ✅ {title[:40]}... ({result.get('content_length', 0)} 字)")
                else:
                    fail_count += 1
                    wave_fail_count += 1
                    error_detail = result.get("message") or result.get("error") or "unknown"
                    print(f"  [{article_index+1}/{total}] ❌ {title[:40]}... {error_detail}")

                if progress_callback:
                    progress_callback(compact_results(results))

        wave_total = wave_success_count + wave_fail_count
        if (
            adaptive
            and current_workers > min_worker_count
            and wave_total
            and wave_fail_count / wave_total >= error_threshold
        ):
            new_workers = max(min_worker_count, current_workers // 2)
            if new_workers < current_workers:
                print(f"  [!] 本轮失败率 {wave_fail_count}/{wave_total}，并发降到 {new_workers}")
                current_workers = new_workers

        if remaining_indices and delay:
            time.sleep(delay)

    print(f"\n{'='*50}")
    print(f"✅ 抓取完成: 成功 {success_count} / 失败 {fail_count} / 总计 {total}")

    return compact_results(results)


def main():
    cli = argparse.ArgumentParser(description="批量抓取微信公众号文章正文内容")
    cli.add_argument(
        "input",
        help="crawler.py 生成的文章列表 JSON 文件路径",
    )
    cli.add_argument(
        "--max",
        type=int,
        default=None,
        dest="max_articles",
        help="最多抓取的文章数量",
    )
    cli.add_argument(
        "--delay",
        type=int,
        default=3,
        help="每篇文章间隔秒数 (默认: 3)",
    )
    cli.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="单次 HTTP 请求超时秒数 (默认: 20)",
    )
    cli.add_argument(
        "--workers",
        type=int,
        default=16,
        help="正文抓取并发数 (默认: 16，可按代理数量继续调高)",
    )
    cli.add_argument(
        "--proxy",
        action="append",
        default=None,
        help="代理，可重复传入，也支持逗号/分号分隔；支持 http://、https://、socks5://、socks5h://",
    )
    cli.add_argument(
        "--proxy-file",
        type=str,
        default=None,
        help="代理文件，每行一个代理",
    )
    cli.add_argument(
        "--min-workers",
        type=int,
        default=1,
        help="失败率高时自动降到的最小并发数 (默认: 1)",
    )
    cli.add_argument(
        "--error-threshold",
        type=float,
        default=0.5,
        help="单轮失败率达到该值时降低并发 (默认: 0.5)",
    )
    cli.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="输出目录 (默认: 与输入文件同目录)",
    )
    args = cli.parse_args()

    # 加载文章列表
    if not os.path.exists(args.input):
        print(f"[✗] 文件不存在: {args.input}")
        sys.exit(1)

    articles, account_name = load_article_list(args.input)
    print(f"📄 公众号: {account_name}")
    print(f"📄 文章列表: {len(articles)} 篇 (来自 {args.input})")

    # 批量抓取正文
    results = fetch_all_content(
        articles,
        max_articles=args.max_articles,
        delay=args.delay,
        timeout=args.timeout,
        workers=args.workers,
        proxies=args.proxy,
        proxy_file=args.proxy_file,
        min_workers=args.min_workers,
        error_threshold=args.error_threshold,
    )

    # 保存结果
    output_dir = args.output_dir or os.path.dirname(args.input) or "output"
    os.makedirs(output_dir, exist_ok=True)

    safe_name = account_name.replace("/", "_").replace(" ", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = os.path.join(output_dir, f"{safe_name}_content_{timestamp}.json")

    output_data = {
        "account": account_name,
        "total": len(results),
        "success": sum(1 for r in results if r.get("content")),
        "crawled_at": datetime.now().isoformat(),
        "source_file": args.input,
        "articles": results,
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    print(f"💾 结果保存到: {output_file}")

    # 预览
    for i, r in enumerate(results[:3]):
        title = r.get("title", "无标题")
        length = r.get("content_length", 0)
        status = f"✅ {length}字" if r.get("content") else "❌ 失败"
        print(f"   [{i+1}] {title[:40]} — {status}")


if __name__ == "__main__":
    main()

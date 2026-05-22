# coding: utf-8
"""
微信公众号文章获取工具

用法:
  1. 本地全自动: python crawler.py --nickname "目标公众号"
  2. 云端手动: python crawler.py --credentials '{"cookie":"xxx","token":"xxx"}'
"""

import json
import time
import os
import sys
import argparse
from datetime import datetime
from types import SimpleNamespace

from fetch_content import fetch_all_content

try:
    from wechatarticles import PublicAccountsWeb
except ImportError:
    print("请先安装依赖: pip install -r requirements.txt")
    sys.exit(1)


def load_config(config_path="config.json"):
    """加载配置文件"""
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_credentials(cookie, token, path="credentials.json"):
    """保存凭证到本地文件"""
    data = {
        "cookie": cookie,
        "token": token,
        "updated_at": datetime.now().isoformat(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"[✓] 凭证已保存到 {path}")


def load_credentials(path="credentials.json"):
    """从本地文件加载凭证"""
    if not os.path.exists(path):
        return None, None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"[i] 使用已保存的凭证 (更新于 {data.get('updated_at', '未知')})")
    return data["cookie"], data["token"]


def get_credentials_auto(headless=False):
    """通过 Playwright 自动化获取凭证"""
    from wechat_login import playwright_login
    print("=" * 50)
    print("准备启动浏览器获取微信公众平台登录凭证...")
    print("=" * 50)

    try:
        cookie, token = playwright_login(headless=headless)
        if not cookie or not token:
            print("[✗] 获取凭证失败")
            sys.exit(1)
            
        save_credentials(cookie, token)
        return cookie, token
    except Exception as e:
        print(f"[✗] 启动自动化登录失败: {e}")
        print("请确保已安装依赖: pip install playwright && playwright install chromium")
        sys.exit(1)


def get_credentials_smart(headless=False):
    """智能获取凭证：支持回车直接启动 Playwright，或者手工粘贴 JSON"""
    print("=" * 50)
    print("你想如何提供登录凭证？")
    print("1. [按回车键] -> 自动启动浏览器扫码获取 (Playwright 推荐本地使用)")
    print("2. [粘贴 JSON] -> 贴入手工抓包获取的 {\"cookie\":\"...\",\"token\":\"...\"} 文本 (推荐云服务器使用)")
    print("=" * 50)

    raw = input("> ").strip()
    if not raw:
        return get_credentials_auto(headless=headless)

    try:
        data = json.loads(raw)
        cookie = data["cookie"]
        token = data["token"]
        save_credentials(cookie, token)
        return cookie, token
    except (json.JSONDecodeError, KeyError) as e:
        print(f"[✗] 凭证格式错误: {e}")
        sys.exit(1)


def get_wechat_api_error(data):
    """从微信接口响应中提取错误信息；响应正常时返回 None。"""
    if not isinstance(data, dict):
        return f"接口返回不是 JSON 对象: {type(data).__name__}"

    base_resp = data.get("base_resp") or {}
    ret = base_resp.get("ret", data.get("errcode", data.get("ret")))
    err_msg = base_resp.get("err_msg", data.get("errmsg", data.get("err_msg", "")))

    if ret in (None, 0, "0"):
        return None

    return f"ret={ret}, err_msg={err_msg or '未知错误'}"


def is_freq_control_error(error):
    """判断是否为微信后台接口频控。"""
    text = str(error).lower()
    return "ret=200013" in text or "freq control" in text


def save_json_atomic(path, data):
    """原子写入 JSON，避免中断时留下半截文件。"""
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(temp_path, path)


def get_article_key(article):
    """生成文章去重键，优先使用稳定的微信文章标识。"""
    aid = article.get("aid")
    if aid:
        return ("aid", aid)

    link = article.get("link") or article.get("url") or article.get("content_url")
    if link:
        return ("link", link)

    appmsgid = article.get("appmsgid")
    itemidx = article.get("itemidx")
    if appmsgid is not None:
        return ("appmsgid", str(appmsgid), str(itemidx or ""))

    return ("fallback", article.get("title", ""), article.get("update_time") or article.get("create_time"))


def build_article_list_result(nickname, articles, status, articles_sum=None, next_begin=None):
    """构造列表阶段结果，供 checkpoint 与最终输出复用。"""
    return {
        "account": nickname,
        "total": len(articles),
        "reported_total": articles_sum,
        "status": status,
        "next_begin": next_begin,
        "crawled_at": datetime.now().isoformat(),
        "articles": articles,
    }


def run_analysis_report(nickname, full_output_file, articles, report_dir, settings):
    """爬取完成后自动生成 analyze_output 同款资产报告。"""
    if not settings.get("analyze_after_crawl", True):
        return None

    try:
        from analysis_report import analyze_articles, save_reports
    except ImportError as exc:
        print(f"[!] 分析模块加载失败，已跳过资产报告: {exc}")
        return None

    analysis_args = SimpleNamespace(
        max=None,
        delay=settings.get("analysis_delay_seconds", settings.get("content_delay_seconds", 0)),
        timeout=settings.get("analysis_timeout", settings.get("content_timeout", 20)),
        retries=settings.get("analysis_retries", settings.get("content_max_retries", 3)),
        retry_delay=settings.get("analysis_retry_delay", 1.0),
        workers=settings.get("analysis_workers", settings.get("content_workers", 16)),
        proxy=settings.get("analysis_proxies", settings.get("content_proxies")),
        proxy_file=settings.get("analysis_proxy_file", settings.get("content_proxy_file")),
        refetch=settings.get("analysis_refetch", True),
    )

    print(f"\n🔎 第三阶段：生成资产/图片/正文分析报告")
    print(f"💾 分析报告目录: {report_dir}")
    details, assets, images, logs = analyze_articles(articles, nickname, analysis_args)
    summary = save_reports(
        report_dir,
        nickname,
        full_output_file,
        details,
        assets,
        images,
        logs,
        keep_json=settings.get("analysis_keep_json", False),
    )
    print(f"✅ 分析完成: 文章 {summary['articles']} / 资源 {summary['assets']} / 图片 {summary['images']}")
    return summary


def crawl_account(cookie, token, nickname, settings, fakeid=None, max_articles=None, since_date=None):
    """
    抓取指定公众号的全部文章 URL

    Parameters
    ----------
    cookie : str
        微信公众平台的 cookie
    token : str
        微信公众平台的 token
    nickname : str
        目标公众号名称（需要精确匹配）
    settings : dict
        抓取配置（batch_size, delay_seconds, output_dir）
    fakeid : str, optional
        公众号的 fakeid（固定不变），提供后可跳过搜索步骤
    since_date : datetime, optional
        只抓取此日期之后的文章
    """
    batch_size = settings.get("batch_size", 5)
    delay = settings.get("delay_seconds", 3)
    max_stalled_pages = settings.get("max_stalled_pages", 3)
    list_freq_cooldown = settings.get("list_freq_cooldown_seconds", 60)
    max_freq_retries = settings.get("list_max_freq_retries", 6)
    output_dir = settings.get("output_dir", "output")

    os.makedirs(output_dir, exist_ok=True)
    safe_name = nickname.replace("/", "_").replace(" ", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_output_dir = os.path.join(output_dir, f"{safe_name}_{timestamp}")
    os.makedirs(run_output_dir, exist_ok=True)
    output_file = os.path.join(run_output_dir, "article_list.json")
    full_output_file = os.path.join(run_output_dir, "article_full.json")

    print(f"\n{'='*50}")
    print(f"开始抓取公众号: {nickname}")
    print(f"{'='*50}")

    paw = PublicAccountsWeb(cookie=cookie, token=token)

    # 1. 确定 fakeid
    if fakeid:
        # 手动提供了 fakeid，跳过搜索
        print(f"  使用提供的 FakeID: {fakeid}")
    else:
        # 通过 nickname 搜索 fakeid
        try:
            info = paw.official_info(nickname)
            if info:
                found = info[0]
                fakeid = found['fakeid']
                print(f"  公众号: {found['nickname']}")
                print(f"  FakeID: {fakeid}")
                print(f"  [提示] 下次可在 config.json 中填入 fakeid 跳过搜索")
            else:
                print(f"[✗] 未找到公众号: {nickname}")
                return []
        except Exception as e:
            print(f"[✗] 查询公众号失败: {e}")
            print("[!] 可能是 cookie/token 已过期，请重新提取")
            return []

    # 2. 获取第一页文章和总数（微信接口有时不返回 app_msg_cnt，不能强依赖）
    first_page_data = None
    for retry_index in range(max_freq_retries + 1):
        try:
            first_page_data = paw._PublicAccountsWeb__get_articles_data(
                "", begin="0", biz=fakeid, count=batch_size
            )
            api_error = get_wechat_api_error(first_page_data)
            if api_error:
                raise RuntimeError(api_error)
            break
        except Exception as e:
            if is_freq_control_error(e) and retry_index < max_freq_retries:
                cooldown = min(list_freq_cooldown * (retry_index + 1), 300)
                print(f"[!] 获取文章列表触发频控，冷却 {cooldown} 秒后重试: {e}")
                time.sleep(cooldown)
                continue

            print(f"[✗] 获取文章列表失败: {e}")
            if is_freq_control_error(e):
                print("[!] 这是微信频控，不是凭证过期；建议等 5~30 分钟后再试")
            else:
                print("[!] 可能是 cookie/token 已过期，请重新提取")
            return []

    first_page_articles = first_page_data.get("app_msg_list", [])
    articles_sum = first_page_data.get("app_msg_cnt")
    if articles_sum is not None:
        articles_sum = int(articles_sum)

    # 如果设置了最大数量限制；总数缺失时按分页抓到空列表或达到 max 为止
    if articles_sum is None:
        crawl_total = max_articles
        print("[!] 微信接口未返回文章总数，将按分页抓取直到列表为空")
    elif max_articles and max_articles < articles_sum:
        print(f"ℹ️  限制抓取数量: {max_articles}")
        crawl_total = max_articles
    else:
        crawl_total = articles_sum

    if since_date:
        print(f"📅 时间过滤: 仅抓取 {since_date.strftime('%Y-%m-%d')} 之后的文章")
    total_label = articles_sum if articles_sum is not None else "未知"
    limit_label = crawl_total if crawl_total is not None else "自动翻页至末尾"
    print(f"📄 文章总数: {total_label}，本次抓取上限: {limit_label}")

    if articles_sum == 0 or (articles_sum is None and not first_page_articles):
        print("[!] 未找到文章")
        return []

    # 3. 循环翻页获取全部文章
    all_articles = []
    article_keys = set()
    failed_count = 0
    freq_retry_count = 0
    stalled_pages = 0
    reached_date_limit = False
    interrupted = False
    since_ts = since_date.timestamp() if since_date else None

    save_json_atomic(
        output_file,
        build_article_list_result(nickname, all_articles, "in_progress", articles_sum, 0),
    )
    print(f"💾 列表会边抓边保存到: {output_file}")

    begin = 0
    try:
        while True:
            try:
                if begin == 0:
                    data = first_page_data
                else:
                    # 使用 fakeid 直接调用，避免每次都搜索 nickname
                    data = paw._PublicAccountsWeb__get_articles_data(
                        "", begin=str(begin), biz=fakeid, count=batch_size
                    )

                api_error = get_wechat_api_error(data)
                if api_error:
                    raise RuntimeError(api_error)

                article_data = data.get("app_msg_list", [])
                if not article_data:
                    print("  已到达列表末尾")
                    break

                new_article_count = 0
                # 按日期过滤（文章按时间倒序排列，遇到早于 since 的就停止）
                for article in article_data:
                    article_time = article.get("update_time") or article.get("create_time", 0)
                    if since_ts and article_time < since_ts:
                        reached_date_limit = True
                        article_date = datetime.fromtimestamp(article_time).strftime('%Y-%m-%d')
                        print(f"  📅 遇到 {article_date} 的文章，已到达时间边界")
                        break

                    article_key = get_article_key(article)
                    if article_key in article_keys:
                        continue

                    article_keys.add(article_key)
                    all_articles.append(article)
                    new_article_count += 1

                failed_count = 0
                if new_article_count:
                    stalled_pages = 0
                else:
                    stalled_pages += 1

                print(f"  进度: {len(all_articles)} 篇 (+{new_article_count}，偏移 {begin})")
                next_begin = begin + batch_size
                save_json_atomic(
                    output_file,
                    build_article_list_result(
                        nickname, all_articles, "in_progress", articles_sum, next_begin
                    ),
                )

                # 达到日期限制时停止
                if reached_date_limit:
                    break

                # 达到数量限制时截断
                if max_articles and len(all_articles) >= max_articles:
                    all_articles = all_articles[:max_articles]
                    break

                if stalled_pages >= max_stalled_pages:
                    print(f"  [!] 连续 {stalled_pages} 批没有新文章，停止翻页")
                    break
            except Exception as e:
                if is_freq_control_error(e):
                    freq_retry_count += 1
                    if freq_retry_count > max_freq_retries:
                        print(f"  [!] 频控重试超过 {max_freq_retries} 次，先停止列表抓取")
                        break

                    cooldown = min(list_freq_cooldown * freq_retry_count, 300)
                    print(f"  [!] 第 {begin} 批触发频控，冷却 {cooldown} 秒后继续: {e}")
                    save_json_atomic(
                        output_file,
                        build_article_list_result(
                            nickname, all_articles, "rate_limited", articles_sum, begin
                        ),
                    )
                    time.sleep(cooldown)
                    continue

                failed_count += 1
                print(f"  [!] 第 {begin} 批获取失败: {e}")
                if failed_count >= 3:
                    print("[✗] 连续失败 3 次，停止抓取")
                    print("[!] 如果不是频控，可能是 cookie/token 已过期，请重新提取")
                    break
                time.sleep(delay * 2)
                continue

            begin += batch_size
            if crawl_total is not None and begin >= crawl_total:
                break

            if articles_sum is None and len(article_data) < batch_size:
                print("  已到达列表末尾")
                break

            time.sleep(delay)
    except KeyboardInterrupt:
        interrupted = True
        print("\n[!] 已停止列表抓取，保留当前 checkpoint")

    # 4. 保存结果
    list_status = "interrupted" if interrupted else "completed"
    save_json_atomic(
        output_file,
        build_article_list_result(nickname, all_articles, list_status, articles_sum, begin),
    )

    status_label = "已中断" if interrupted else "抓取完成"
    print(f"\n✅ 第一阶段（文章列表）{status_label}!")
    print(f"   公众号: {nickname}")
    print(f"   文章数: {len(all_articles)}")
    print(f"   基础列表保存到: {output_file}")

    if interrupted:
        return all_articles

    # 5. 自动无缝进入第二阶段：请求文章内容（默认情况下）
    # 在 settings 中可以允许不请求正文，但通常大家都需要连贯的抓出正文
    skip_content = settings.get("skip_content", False)
    if not skip_content and all_articles:
        def save_content_checkpoint(results, status="in_progress"):
            save_json_atomic(
                full_output_file,
                {
                    "account": nickname,
                    "total": len(results),
                    "success": sum(1 for r in results if r.get("content")),
                    "status": status,
                    "crawled_at": datetime.now().isoformat(),
                    "articles": results,
                },
            )

        save_content_checkpoint([], status="in_progress")
        print(f"💾 正文会边抓边保存到: {full_output_file}")
        try:
            results = fetch_all_content(
                all_articles,
                max_articles=len(all_articles),
                delay=settings.get("content_delay_seconds", 2),
                timeout=settings.get("content_timeout", 20),
                max_retries=settings.get("content_max_retries", 3),
                progress_callback=save_content_checkpoint,
                workers=settings.get("content_workers", 16),
                proxies=settings.get("content_proxies"),
                proxy_file=settings.get("content_proxy_file"),
                adaptive=settings.get("content_adaptive", True),
                error_threshold=settings.get("content_error_threshold", 0.5),
                min_workers=settings.get("content_min_workers", 1),
            )
        except KeyboardInterrupt:
            print(f"\n[!] 已停止正文抓取，保留当前 checkpoint: {full_output_file}")
            return all_articles

        save_content_checkpoint(results, status="completed")
            
        print(f"\n✅ 第二阶段（文章纯文本详情）提取完毕！")
        print(f"   最终带有正文的数据已保存至: {full_output_file}")

        try:
            run_analysis_report(nickname, full_output_file, results, run_output_dir, settings)
        except KeyboardInterrupt:
            print(f"\n[!] 已停止分析报告生成，保留当前输出目录: {run_output_dir}")
        except Exception as e:
            print(f"[!] 分析报告生成失败，正文结果已保留: {e}")


    return all_articles


def main():
    parser = argparse.ArgumentParser(description="微信公众号文章爬虫")
    parser.add_argument(
        "--credentials",
        type=str,
        help='凭证 JSON，格式: \'{"cookie":"...","token":"..."}\'',
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.json",
        help="配置文件路径 (默认: config.json)",
    )
    parser.add_argument(
        "--nickname",
        type=str,
        default=None,
        help="直接指定公众号名称（覆盖 config.json）",
    )
    parser.add_argument(
        "--fakeid",
        type=str,
        default=None,
        help="公众号的 fakeid（纯数字，固定不变，提供后跳过搜索）",
    )
    parser.add_argument(
        "--biz",
        type=str,
        default=None,
        help="公众号的 biz 参数（如 MzU1NDk2MzQyNg==，从文章URL中获取）",
    )
    parser.add_argument(
        "--target",
        type=int,
        default=None,
        help="只抓取 config 中的第 N 个目标 (从 0 开始)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        dest="max_articles",
        help="最多抓取的文章数量",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="只抓取此日期之后的文章，格式: YYYY-MM-DD（如 2026-01-01）",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="在无头模式下启动登录浏览器（适合云服务器无界面环境）",
    )
    parser.add_argument(
        "--relogin",
        "-r",
        action="store_true",
        help="忽略已保存凭证，直接启动浏览器重新扫码登录",
    )
    parser.add_argument(
        "--content-workers",
        "--workers",
        "-w",
        type=int,
        default=None,
        dest="content_workers",
        help="正文抓取并发数（默认读取 config；未配置时为 16）",
    )
    parser.add_argument(
        "--content-proxy",
        "--proxy",
        "-p",
        action="append",
        default=None,
        dest="content_proxy",
        help="正文抓取代理，可重复传入，也支持逗号/分号分隔；支持 http://、https://、socks5://、socks5h://",
    )
    parser.add_argument(
        "--content-proxy-file",
        "--proxy-file",
        "-pf",
        type=str,
        default=None,
        dest="content_proxy_file",
        help="正文抓取代理文件，每行一个代理",
    )
    parser.add_argument(
        "--content-delay",
        "-cd",
        type=float,
        default=None,
        dest="content_delay",
        help="正文抓取并发批次间隔秒数；并发时建议 0~1",
    )
    parser.add_argument(
        "--list-delay",
        "-ld",
        type=float,
        default=None,
        dest="list_delay",
        help="文章列表翻页间隔秒数；不建议低于 1，过快容易触发风控",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="快捷加速模式：正文至少 16 线程、正文批次间隔 0 秒、列表间隔 1 秒",
    )
    parser.add_argument(
        "--cooldown",
        "-fc",
        type=float,
        default=None,
        help="列表接口触发频控后的基础冷却秒数（默认 60）",
    )
    parser.add_argument(
        "--skip-analysis",
        action="store_true",
        help="爬完正文后不生成资产/图片分析报告",
    )
    parser.add_argument(
        "--analysis-workers",
        type=int,
        default=None,
        help="分析报告重抓页面并发数（默认跟随正文并发）",
    )
    args = parser.parse_args()

    # 加载配置
    config = load_config(args.config)
    settings = config.get("crawl_settings", {})
    if args.fast:
        settings["content_workers"] = max(16, int(settings.get("content_workers", 16) or 16))
        settings.setdefault("content_delay_seconds", 0)
        settings.setdefault("delay_seconds", 1)
    if args.content_workers is not None:
        settings["content_workers"] = args.content_workers
    if args.content_proxy is not None:
        settings["content_proxies"] = args.content_proxy
    if args.content_proxy_file is not None:
        settings["content_proxy_file"] = args.content_proxy_file
    if args.content_delay is not None:
        settings["content_delay_seconds"] = args.content_delay
    if args.list_delay is not None:
        settings["delay_seconds"] = args.list_delay
    if args.cooldown is not None:
        settings["list_freq_cooldown_seconds"] = args.cooldown
    if args.skip_analysis:
        settings["analyze_after_crawl"] = False
    if args.analysis_workers is not None:
        settings["analysis_workers"] = args.analysis_workers
    targets = config.get("targets", [])

    # 命令行指定的 nickname/fakeid 优先
    if args.nickname or args.fakeid or args.biz:
        targets = [{"nickname": args.nickname or "未知", "fakeid": args.fakeid or args.biz}]
    elif args.target is not None:
        targets = [targets[args.target]]

    if not targets:
        print("[✗] 未指定目标公众号")
        print("    用法: python crawler.py --nickname 公众号名称")
        print("    或者在 config.json 中配置 targets")
        sys.exit(1)

    # 获取凭证
    if args.credentials:
        data = json.loads(args.credentials)
        cookie, token = data["cookie"], data["token"]
        save_credentials(cookie, token)
    elif args.relogin:
        cookie, token = get_credentials_auto(headless=args.headless)
    else:
        cookie, token = load_credentials()
        if not cookie or not token:
            cookie, token = get_credentials_smart(headless=args.headless)

    # 解析 since 日期
    since_date = None
    if args.since:
        try:
            since_date = datetime.strptime(args.since, "%Y-%m-%d")
            print(f"📅 时间过滤: 只抓取 {args.since} 之后的文章")
        except ValueError:
            print(f"[✗] 日期格式错误: {args.since}，请使用 YYYY-MM-DD 格式")
            sys.exit(1)

    # 抓取
    for target in targets:
        crawl_account(
            cookie=cookie,
            token=token,
            nickname=target["nickname"],
            settings=settings,
            fakeid=target.get("fakeid"),
            max_articles=args.max_articles,
            since_date=since_date,
        )


if __name__ == "__main__":
    main()

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
import warnings
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace

from fetch_content import fetch_all_content

warnings.filterwarnings("ignore", message="urllib3 .* doesn't match a supported version.*")

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


def is_credential_error(error):
    """判断是否为 Cookie/Token 失效或权限异常。"""
    text = str(error).lower()
    keywords = (
        "cookie",
        "token",
        "登录",
        "登陆",
        "重新输入",
        "invalid",
        "unauthorized",
        "forbidden",
        "ret=200003",
        "ret=200004",
        "ret=200005",
        "ret=200023",
    )
    return any(keyword in text for keyword in keywords)


def save_json_atomic(path, data):
    """原子写入 JSON，避免中断时留下半截文件。"""
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(temp_path, path)


def load_json_file(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def safe_output_name(value):
    return (value or "unknown").replace("/", "_").replace(" ", "_")


def is_invalid_nickname_input(value):
    value = (value or "").strip()
    return not value or value.isdigit()


def find_exact_official_account(info, nickname):
    nickname = (nickname or "").strip()
    for item in info or []:
        if (item.get("nickname") or "").strip() == nickname:
            return item
    return None


def print_official_account_candidates(info):
    if not info:
        return
    print("  搜索到了这些候选，但没有精确匹配：")
    for index, item in enumerate(info[:10], 1):
        print(f"    {index}. {item.get('nickname', '')}")


def article_url_value(article):
    url = article.get("link") or article.get("url") or article.get("content_url") or article.get("source_url") or ""
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url


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


def find_latest_run_dir(output_dir, safe_name):
    if not os.path.isdir(output_dir):
        return None
    prefix = f"{safe_name}_"
    candidates = []
    for name in os.listdir(output_dir):
        path = os.path.join(output_dir, name)
        if not os.path.isdir(path) or not name.startswith(prefix):
            continue
        if os.path.exists(os.path.join(path, "article_list.json")) or os.path.exists(os.path.join(path, "article_full.json")):
            candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda path: os.path.getmtime(path))


def normalize_checkpoint_data(data, status="completed"):
    if isinstance(data, dict):
        articles = data.get("articles", [])
        normalized = dict(data)
        normalized.setdefault("status", status)
        normalized.setdefault("total", len(articles))
        normalized.setdefault("next_begin", len(articles))
        return normalized
    if isinstance(data, list):
        return {
            "account": "unknown",
            "total": len(data),
            "status": status,
            "next_begin": len(data),
            "crawled_at": datetime.now().isoformat(),
            "articles": data,
        }
    return data


def find_latest_legacy_json(output_dir, candidate_names, full=False):
    if not os.path.isdir(output_dir):
        return None
    matches = []
    for name in os.listdir(output_dir):
        path = os.path.join(output_dir, name)
        if not os.path.isfile(path) or not name.endswith(".json"):
            continue
        for candidate_name in candidate_names:
            if not candidate_name:
                continue
            if full and name.startswith(f"{candidate_name}_full_"):
                matches.append(path)
            elif not full and name.startswith(f"{candidate_name}_") and "_full_" not in name:
                matches.append(path)
    if not matches:
        return None
    return max(matches, key=lambda path: os.path.getmtime(path))


def migrate_legacy_checkpoints(output_dir, safe_name, candidate_names):
    candidate_names = [name for name in [safe_name, *(candidate_names or [])] if name]
    legacy_list = find_latest_legacy_json(output_dir, candidate_names, full=False)
    legacy_full = find_latest_legacy_json(output_dir, candidate_names, full=True)
    if not legacy_list and not legacy_full:
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(output_dir, f"{safe_name}_resume_{timestamp}")
    os.makedirs(run_dir, exist_ok=True)
    list_source = legacy_list or legacy_full
    if list_source:
        save_json_atomic(
            os.path.join(run_dir, "article_list.json"),
            normalize_checkpoint_data(load_json_file(list_source), status="completed"),
        )
    if legacy_full:
        save_json_atomic(
            os.path.join(run_dir, "article_full.json"),
            normalize_checkpoint_data(load_json_file(legacy_full), status="completed"),
        )
    print(f"↩️  已迁移旧版进度到: {run_dir}")
    return run_dir


def choose_run_output_dir(output_dir, safe_name, settings, fallback_names=None):
    if settings.get("resume", True) and not settings.get("new_run", False):
        candidate_names = [safe_name, *(fallback_names or [])]
        latest_dirs = [
            find_latest_run_dir(output_dir, candidate_name)
            for candidate_name in candidate_names
            if candidate_name
        ]
        latest_dirs = [path for path in latest_dirs if path]
        latest_dir = max(latest_dirs, key=lambda path: os.path.getmtime(path)) if latest_dirs else None
        if latest_dir:
            print(f"↩️  发现历史进度，自动续跑: {latest_dir}")
            return latest_dir
        migrated_dir = migrate_legacy_checkpoints(output_dir, safe_name, candidate_names)
        if migrated_dir:
            return migrated_dir

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(output_dir, f"{safe_name}_{timestamp}")


def has_reusable_list_checkpoint(target, settings, max_articles=None):
    if not settings.get("resume", True) or settings.get("new_run", False):
        return False
    output_dir = settings.get("output_dir", "output")
    safe_name = safe_output_name(target.get("output_name") or target.get("alias") or target.get("nickname"))
    fallback_names = [
        safe_output_name(target.get("nickname")),
        safe_output_name(target.get("alias")),
        safe_output_name(target.get("output_name")),
    ]
    latest_dirs = [
        find_latest_run_dir(output_dir, candidate_name)
        for candidate_name in [safe_name, *fallback_names]
        if candidate_name
    ]
    latest_dirs = [path for path in latest_dirs if path]
    latest_dir = max(latest_dirs, key=lambda path: os.path.getmtime(path)) if latest_dirs else None
    if not latest_dir:
        legacy_list = find_latest_legacy_json(output_dir, [safe_name, *fallback_names], full=False)
        legacy_full = find_latest_legacy_json(output_dir, [safe_name, *fallback_names], full=True)
        if legacy_list or legacy_full:
            return True
    if not latest_dir:
        return False
    checkpoint = load_json_file(os.path.join(latest_dir, "article_list.json"))
    if not isinstance(checkpoint, dict) or not checkpoint.get("articles"):
        return False
    if checkpoint.get("status") == "completed":
        return True
    return bool(max_articles and len(checkpoint.get("articles", [])) >= max_articles)


def list_task_dirs(output_dir="output"):
    if not os.path.isdir(output_dir):
        return []
    rows = []
    for path in Path(output_dir).iterdir():
        if not path.is_dir():
            continue
        list_file = path / "article_list.json"
        full_file = path / "article_full.json"
        if not list_file.exists() and not full_file.exists():
            continue
        list_data = load_json_file(str(list_file)) if list_file.exists() else {}
        full_data = load_json_file(str(full_file)) if full_file.exists() else {}
        articles = []
        if isinstance(full_data, dict) and full_data.get("articles"):
            articles = full_data.get("articles", [])
        elif isinstance(list_data, dict) and list_data.get("articles"):
            articles = list_data.get("articles", [])
        rows.append(
            {
                "path": path,
                "name": path.name,
                "mtime": path.stat().st_mtime,
                "account": (full_data or list_data or {}).get("account", ""),
                "list_status": (list_data or {}).get("status", "missing") if list_file.exists() else "missing",
                "full_status": (full_data or {}).get("status", "missing") if full_file.exists() else "missing",
                "articles": len(articles),
                "has_report": (path / "分析报告.md").exists(),
            }
        )
    return sorted(rows, key=lambda item: item["mtime"], reverse=True)


def choose_task_dir(output_dir="output"):
    rows = list_task_dirs(output_dir)
    if not rows:
        print(f"[!] 没有发现任务目录: {output_dir}")
        return None

    print("\n可用历史任务：")
    for index, item in enumerate(rows[:20], 1):
        report_label = "有报告" if item["has_report"] else "无报告"
        print(
            f"  {index}. {item['name']} | {item['articles']}篇 | "
            f"列表:{item['list_status']} 正文:{item['full_status']} | {report_label}"
        )

    while True:
        value = input("选择任务编号（回车=1，q=返回）：").strip().lower()
        if not value:
            return rows[0]["path"]
        if value == "q":
            return None
        if value.isdigit() and 1 <= int(value) <= min(len(rows), 20):
            return rows[int(value) - 1]["path"]
        print("请输入有效编号。")


def run_report_for_task(task_dir, settings):
    task_dir = Path(task_dir)
    full_file = task_dir / "article_full.json"
    list_file = task_dir / "article_list.json"
    source_file = full_file if full_file.exists() else list_file
    data = load_json_file(str(source_file))
    if not isinstance(data, dict) or not data.get("articles"):
        print(f"[x] 任务没有可分析的文章: {task_dir}")
        return None
    nickname = data.get("account") or task_dir.name
    return run_analysis_report(nickname, str(source_file), data.get("articles", []), str(task_dir), settings)


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


def merge_content_results(all_articles, existing_results, fetched_results):
    fetched_by_url = {article_url_value(item): item for item in fetched_results if article_url_value(item)}
    existing_by_url = {article_url_value(item): item for item in existing_results if article_url_value(item)}
    merged = []
    for article in all_articles:
        key = article_url_value(article)
        item = fetched_by_url.get(key) or existing_by_url.get(key)
        if not item:
            item = next(
                (
                    candidate for candidate in fetched_results
                    if article_url_value(candidate).split("#", 1)[0] == key.split("#", 1)[0]
                ),
                None,
            )
        if item:
            merged.append(item)
    return merged


def merge_pending_content_results(all_articles, existing_results, pending_articles, fetched_results):
    existing_by_url = {article_url_value(item): item for item in existing_results if article_url_value(item)}
    fetched_by_url = {}
    for article, result in zip(pending_articles, fetched_results):
        merged_result = dict(result)
        source_url = article_url_value(article)
        if source_url:
            merged_result.setdefault("source_url", source_url)
            if not merged_result.get("url") or "mp.weixin.qq.com/s" not in str(merged_result.get("url")):
                merged_result["url"] = source_url
        merged_result.setdefault("original_title", article.get("title", ""))
        fetched_by_url[source_url] = merged_result

    merged = []
    for article in all_articles:
        key = article_url_value(article)
        item = existing_by_url.get(key) or fetched_by_url.get(key)
        if item:
            merged.append(item)
    return merged


def filter_pending_content_articles(all_articles, existing_results):
    existing_by_url = {
        article_url_value(item): item
        for item in existing_results
        if article_url_value(item) and item.get("content")
    }
    pending = []
    for article in all_articles:
        key = article_url_value(article)
        if not key or key not in existing_by_url:
            pending.append(article)
    return pending


def run_analysis_report(nickname, full_output_file, articles, report_dir, settings):
    """爬取完成后自动生成资产分析报告。"""
    if not settings.get("analyze_after_crawl", True):
        return None
    if settings.get("analysis_skip_existing", True):
        required_report_files = [
            os.path.join(report_dir, "分析报告.md"),
            os.path.join(report_dir, "先看这个_总览.txt"),
            os.path.join(report_dir, "外部链接_精简.txt"),
            os.path.join(report_dir, "图片资源", "本地图片清单.txt"),
            os.path.join(report_dir, "图片资源", "疑似二维码小程序码图片.txt"),
            os.path.join(report_dir, "图片资源", "标准二维码解码结果.txt"),
        ]
        if all(os.path.exists(path) for path in required_report_files):
            print(f"✅ 分析报告已存在，跳过第三阶段: {report_dir}")
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
        download_images=settings.get("analysis_download_images", True),
        image_download_timeout=settings.get("analysis_image_download_timeout", 20),
        image_download_max=settings.get("analysis_image_download_max"),
    )
    print(f"✅ 分析完成: 文章 {summary['articles']} / 资源 {summary['assets']} / 图片 {summary['images']}")
    return summary


def crawl_account(cookie, token, nickname, settings, fakeid=None, max_articles=None, since_date=None, output_name=None):
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
    safe_name = safe_output_name(nickname)
    run_output_dir = choose_run_output_dir(
        output_dir,
        safe_name,
        settings,
        fallback_names=[safe_output_name(output_name), safe_output_name(nickname)],
    )
    os.makedirs(run_output_dir, exist_ok=True)
    output_file = os.path.join(run_output_dir, "article_list.json")
    full_output_file = os.path.join(run_output_dir, "article_full.json")
    list_checkpoint = load_json_file(output_file)
    full_checkpoint = load_json_file(full_output_file)

    print(f"\n{'='*50}")
    print(f"开始抓取公众号: {nickname}")
    print(f"{'='*50}")

    list_checkpoint_completed = (
        isinstance(list_checkpoint, dict)
        and list_checkpoint.get("articles")
        and (
            list_checkpoint.get("status") == "completed"
            or (max_articles and len(list_checkpoint.get("articles", [])) >= max_articles)
        )
    )

    if list_checkpoint_completed:
        print("✅ 已有完整文章列表，跳过微信列表接口")
        first_page_data = {"app_msg_list": list_checkpoint.get("articles", [])[:batch_size]}
        first_page_articles = first_page_data["app_msg_list"]
        articles_sum = list_checkpoint.get("reported_total") or len(list_checkpoint.get("articles", []))
        crawl_total = min(max_articles, len(list_checkpoint.get("articles", []))) if max_articles else len(list_checkpoint.get("articles", []))
    else:
        paw = PublicAccountsWeb(cookie=cookie, token=token)

        # 1. 确定 fakeid
        if fakeid:
            # 手动提供了 fakeid，跳过搜索
            print(f"  使用提供的 FakeID: {fakeid}")
        else:
            # 通过 nickname 搜索 fakeid
            try:
                info = paw.official_info(nickname)
                found = find_exact_official_account(info, nickname)
                if found:
                    fakeid = found['fakeid']
                    print(f"  公众号: {found['nickname']}")
                    print(f"  FakeID: {fakeid}")
                    print(f"  [提示] 下次可在 config.json 中填入 fakeid 跳过搜索")
                else:
                    print(f"[✗] 未找到精确匹配的公众号: {nickname}")
                    print_official_account_candidates(info)
                    print("[!] 为避免抓错公众号，已停止。请重新输入完整公众号名称。")
                    return []
            except Exception as e:
                print(f"[✗] 查询公众号失败: {e}")
                if is_credential_error(e):
                    print("[!] Cookie/Token 可能已过期，将尝试重新登录")
                    return {"status": "credential_expired", "message": str(e)}
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
                if is_credential_error(e):
                    return {"status": "credential_expired", "message": str(e)}
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

    # 3. 循环翻页获取全部文章；如果存在 checkpoint，则自动续跑
    all_articles = []
    article_keys = set()
    failed_count = 0
    freq_retry_count = 0
    stalled_pages = 0
    reached_date_limit = False
    interrupted = False
    since_ts = since_date.timestamp() if since_date else None

    begin = 0
    list_finished = False
    if isinstance(list_checkpoint, dict) and list_checkpoint.get("articles"):
        all_articles = list_checkpoint.get("articles", [])
        article_keys = {get_article_key(article) for article in all_articles}
        begin = int(list_checkpoint.get("next_begin") or len(all_articles) or 0)
        list_status = list_checkpoint.get("status", "")
        if max_articles and len(all_articles) >= max_articles:
            all_articles = all_articles[:max_articles]
            list_finished = True
        elif list_status == "completed":
            list_finished = True
        print(f"↩️  已载入列表进度: {len(all_articles)} 篇，状态 {list_status or 'unknown'}，下次偏移 {begin}")

    if not all_articles:
        save_json_atomic(
            output_file,
            build_article_list_result(nickname, all_articles, "in_progress", articles_sum, 0),
        )
    print(f"💾 列表会边抓边保存到: {output_file}")

    try:
        while not list_finished:
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
        existing_content_results = []
        content_finished = False
        if isinstance(full_checkpoint, dict) and full_checkpoint.get("articles"):
            existing_content_results = full_checkpoint.get("articles", [])
            content_status = full_checkpoint.get("status", "")
            content_finished = content_status == "completed" and len(existing_content_results) >= len(all_articles)
            print(
                f"↩️  已载入正文进度: {len(existing_content_results)} / {len(all_articles)}，状态 {content_status or 'unknown'}"
            )

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

        if content_finished:
            results = existing_content_results
            print(f"✅ 正文已完成，跳过第二阶段: {full_output_file}")
        else:
            pending_articles = filter_pending_content_articles(all_articles, existing_content_results)
            if existing_content_results:
                save_content_checkpoint(existing_content_results, status="in_progress")
            else:
                save_content_checkpoint([], status="in_progress")
            print(f"💾 正文会边抓边保存到: {full_output_file}")
            print(f"📖 待补正文: {len(pending_articles)} / {len(all_articles)} 篇")
            try:
                fetched_results = fetch_all_content(
                    pending_articles,
                    max_articles=len(pending_articles),
                    delay=settings.get("content_delay_seconds", 2),
                    timeout=settings.get("content_timeout", 20),
                    max_retries=settings.get("content_max_retries", 3),
                    progress_callback=lambda partial: save_content_checkpoint(
                        merge_pending_content_results(all_articles, existing_content_results, pending_articles, partial),
                        status="in_progress",
                    ),
                    workers=settings.get("content_workers", 16),
                    proxies=settings.get("content_proxies"),
                    proxy_file=settings.get("content_proxy_file"),
                    adaptive=settings.get("content_adaptive", True),
                    error_threshold=settings.get("content_error_threshold", 0.5),
                    min_workers=settings.get("content_min_workers", 1),
                )
                results = merge_pending_content_results(all_articles, existing_content_results, pending_articles, fetched_results)
                if not results and fetched_results:
                    print("[!] 结果合并为空，已回退为本次抓取结果，避免生成空报告")
                    results = fetched_results
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


def apply_cli_overrides(args, settings):
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
    if args.new_run or args.no_resume:
        settings["new_run"] = True
        settings["resume"] = False
    return settings


def select_targets(args, config):
    targets = config.get("targets", [])
    if args.nickname or args.fakeid or args.biz:
        return [{"nickname": args.nickname or "未知", "fakeid": args.fakeid or args.biz}]
    if args.alias:
        matched_targets = [
            target for target in targets
            if args.alias in {
                str(target.get("alias", "")),
                str(target.get("output_name", "")),
                str(target.get("name", "")),
            }
        ]
        if not matched_targets:
            print(f"[✗] 未找到别名: {args.alias}")
            print("    请在 config.json 的 targets 里配置 alias，例如: {\"nickname\":\"公众号名\", \"alias\":\"by\"}")
            sys.exit(1)
        return matched_targets
    if args.target is not None:
        return [targets[args.target]]
    return targets


def parse_since_date(since):
    if not since:
        return None
    try:
        since_date = datetime.strptime(since, "%Y-%m-%d")
        print(f"📅 时间过滤: 只抓取 {since} 之后的文章")
        return since_date
    except ValueError:
        print(f"[✗] 日期格式错误: {since}，请使用 YYYY-MM-DD 格式")
        sys.exit(1)


def get_runtime_credentials(args, targets, settings):
    needs_credentials = bool(args.relogin or args.credentials)
    if not needs_credentials:
        needs_credentials = any(
            not has_reusable_list_checkpoint(target, settings, args.max_articles)
            for target in targets
        )

    if not needs_credentials:
        print("↩️  检测到完整列表 checkpoint，本次续跑无需登录凭证")
        return None, None
    if args.credentials:
        data = json.loads(args.credentials)
        cookie, token = data["cookie"], data["token"]
        save_credentials(cookie, token)
        return cookie, token
    if args.relogin:
        return get_credentials_auto(headless=args.headless)

    cookie, token = load_credentials()
    if not cookie or not token:
        cookie, token = get_credentials_smart(headless=args.headless)
    return cookie, token


def should_auto_relogin(args, cookie, token):
    return bool(cookie and token and not args.credentials)


def run_crawl_with_args(args, config, settings):
    targets = select_targets(args, config)
    if not targets:
        print("[✗] 未指定目标公众号")
        print("    用法: python crawler.py --nickname 公众号名称")
        print("    或者在 config.json 中配置 targets")
        return 1

    if not (args.nickname or args.fakeid or args.biz or args.alias or args.target is not None) and len(targets) == 1:
        target = targets[0]
        alias_label = target.get("alias") or target.get("output_name")
        if alias_label:
            print(f"ℹ️  使用 config.json 默认目标：{target['nickname']}（等同于 -a {alias_label}）")
        else:
            print(f"ℹ️  使用 config.json 默认目标：{target['nickname']}")

    since_date = parse_since_date(args.since)
    cookie, token = get_runtime_credentials(args, targets, settings)

    retried_login = False
    for target in targets:
        while True:
            result = crawl_account(
                cookie=cookie,
                token=token,
                nickname=target["nickname"],
                settings=settings,
                fakeid=target.get("fakeid"),
                max_articles=args.max_articles,
                since_date=since_date,
                output_name=target.get("output_name") or target.get("alias"),
            )
            if not (isinstance(result, dict) and result.get("status") == "credential_expired"):
                break
            if retried_login or not should_auto_relogin(args, cookie, token):
                print("[✗] 自动重新登录后仍失败，请手动检查公众号名称或网络环境")
                break
            retried_login = True
            print("\n🔐 登录态已失效，自动打开浏览器重新扫码获取 Cookie/Token...")
            cookie, token = get_credentials_auto(headless=args.headless)
            print("🔁 新凭证已保存，正在自动重试当前任务...")
    return 0


def interactive_menu(args, config, settings):
    targets = config.get("targets", [])
    default_target = targets[0] if targets else {}
    alias_label = default_target.get("alias") or default_target.get("output_name") or "默认目标"
    output_dir = settings.get("output_dir", "output")

    while True:
        print("\n请选择操作：")
        print(f"  1. 继续上次任务 / 默认抓取（{alias_label}，自动断点续跑）")
        print("  2. 输入公众号名称，重新开始一个新任务")
        print("  3. 只读取历史任务并生成/补全分析报告")
        print("  4. 查看历史任务目录")
        print("  5. 查看帮助")
        print("  0. 退出")
        choice = input("输入编号（回车=1）：").strip()
        if not choice:
            choice = "1"

        if choice == "0":
            return 0
        if choice == "1":
            args.new_run = False
            args.no_resume = False
            return run_crawl_with_args(args, config, settings)
        if choice == "2":
            nickname = input("请输入公众号名称（需精确匹配）：").strip()
            if is_invalid_nickname_input(nickname):
                print("[!] 请输入完整公众号名称，不能只输入编号或纯数字。")
                continue
            args.nickname = nickname
            args.alias = None
            args.target = None
            args.new_run = True
            args.no_resume = True
            settings["new_run"] = True
            settings["resume"] = False
            return run_crawl_with_args(args, config, settings)
        if choice == "3":
            task_dir = choose_task_dir(output_dir)
            if task_dir:
                settings["analysis_skip_existing"] = False
                run_report_for_task(task_dir, settings)
            continue
        if choice == "4":
            rows = list_task_dirs(output_dir)
            if not rows:
                print(f"[!] 没有发现任务目录: {output_dir}")
            else:
                for index, item in enumerate(rows[:20], 1):
                    report_label = "有报告" if item["has_report"] else "无报告"
                    print(
                        f"  {index}. {item['name']} | {item['articles']}篇 | "
                        f"列表:{item['list_status']} 正文:{item['full_status']} | {report_label}"
                    )
            continue
        if choice == "5":
            print("运行 python .\\crawler.py --help 查看完整参数说明。")
            continue
        print("请输入 0-5 之间的编号。")


def main():
    parser = argparse.ArgumentParser(
        prog="crawler.py",
        description=(
            "微信公众号文章爬虫：抓文章列表、抓正文、下载图片、生成资产分析报告。\n"
            "不输入任何目标参数时，会使用 config.json 中的默认目标；当前等同于 -a by。"
        ),
        epilog=(
            "常用示例：\n"
            "  python .\\crawler.py\n"
            "      进入交互菜单；选 2 时直接输入公众号名称并新建任务\n\n"
            "  python .\\crawler.py -a by\n"
            "      使用短别名 by 抓取，默认自动续跑旧进度\n\n"
            "  python .\\crawler.py --nickname \"公众号名称\"\n"
            "      不改 config.json，临时输入公众号名称抓取\n\n"
            "  python .\\crawler.py -a by --max 20\n"
            "      只处理最新 20 篇\n\n"
            "  python .\\crawler.py -a by --workers 32 --proxy-file proxies.txt\n"
            "      32 线程并使用代理池\n\n"
            "  python .\\crawler.py -a by --new-run\n"
            "      不复用旧进度，重新开一个输出目录；若凭证过期会自动打开浏览器重新扫码\n\n"
            "输出目录：\n"
            "  output/公众号名_时间戳/，优先看 先看这个_总览.txt、外部链接_精简.txt、图片资源/本地图片/、疑似二维码小程序码图片.txt。"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=False,
    )
    parser._optionals.title = "选项"
    parser.add_argument("-h", "--help", action="help", help="显示此帮助信息并退出")
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
        help="直接指定公众号名称（覆盖 config.json，不用改配置文件）",
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
        "--alias",
        "-a",
        type=str,
        default=None,
        help="按 config 中的短别名抓取目标，例如 by",
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
        help="忽略已保存凭证，直接启动浏览器重新扫码登录；普通运行遇到凭证过期也会自动触发",
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
    parser.add_argument(
        "--new-run",
        action="store_true",
        help="不复用历史进度，强制创建新的时间戳输出目录",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="关闭自动续跑，等同于 --new-run",
    )
    parser.add_argument(
        "--menu",
        action="store_true",
        help="强制进入交互菜单",
    )
    parser.add_argument(
        "--no-menu",
        action="store_true",
        help="不进入菜单，直接按参数执行；适合脚本/定时任务",
    )
    args = parser.parse_args()

    # 加载配置
    config = load_config(args.config)
    settings = config.get("crawl_settings", {})
    settings = apply_cli_overrides(args, settings)

    has_direct_args = any(
        [
            args.nickname,
            args.fakeid,
            args.biz,
            args.alias,
            args.target is not None,
            args.max_articles is not None,
            args.since,
            args.relogin,
            args.credentials,
            args.new_run,
            args.no_resume,
            args.no_menu,
        ]
    )
    if args.menu or (not args.no_menu and not has_direct_args):
        return interactive_menu(args, config, settings)
    return run_crawl_with_args(args, config, settings)


if __name__ == "__main__":
    main()

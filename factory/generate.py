#!/usr/bin/env python3
"""壁纸工厂：按日期从风格库轮换 4 个风格，调用 Agnes 图像 API 生成新壁纸，
并把 index.html 更新为最新 16 张。

API key 从本机凭据文件读取，绝不写入仓库。风格提示词存放在独立的
factory/styles.json 中，按 YYYYMMDD 日期整数每天轮换 4 个互不重复的风格，
同一天多次运行结果一致。生成结果保存到 img/，index.html 中的 masonry
区块与 JS images 数组按文件修改时间倒序重建。

用法见 factory/README.md。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from html import escape, unescape
from pathlib import Path

FACTORY_DIR = Path(__file__).resolve().parent
REPO_ROOT = FACTORY_DIR.parent
IMG_DIR = REPO_ROOT / "img"
INDEX_PATH = REPO_ROOT / "index.html"
STYLES_PATH = FACTORY_DIR / "styles.json"
DEFAULT_CREDENTIALS = Path("C:/Users/1/.dsh/.credentials.yaml")

API_URL = "https://apihub.agnes-ai.com/v1/images/generations"
MODEL = "agnes-image-2.1-flash"
GALLERY_SIZE = 16
MAX_RETRIES = 5
HTTP_TIMEOUT = 300

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
MASONRY_RE = re.compile(
    r'(<div class="masonry" id="masonry">\n)(.*?)(\n    </div>)', re.DOTALL
)
IMAGES_JS_RE = re.compile(
    r"(        const images = \[)(.*?)(        \];\n)", re.DOTALL
)
EXISTING_IMG_RE = re.compile(r'<img\s+src="img/([^"/]+)"\s+alt="([^"]*)"')


class FatalError(RuntimeError):
    """不可重试的错误：凭据、额度、响应结构或本地文件问题。"""


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# 凭据与风格库
# ---------------------------------------------------------------------------

def read_api_key(path: Path) -> str:
    """从 YAML 凭据文件中读取 AGNES_API_KEY（也允许环境变量优先）。"""
    env_key = os.environ.get("AGNES_API_KEY", "").strip()
    if env_key:
        return env_key

    if not path.is_file():
        raise FatalError(f"凭据文件不存在：{path}")

    pattern = re.compile(
        r"^\s*AGNES_API_KEY\s*:\s*[\"']?([^\"'\s#]+)[\"']?\s*(?:#.*)?$"
    )
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1)
    raise FatalError(f"凭据文件中没有找到 AGNES_API_KEY：{path}")


def load_styles() -> list[dict]:
    """读取并校验 factory/styles.json；每条风格包含非空的 name 与 prompt。"""
    if not STYLES_PATH.is_file():
        raise FatalError(f"风格库不存在：{STYLES_PATH}")
    try:
        styles = json.loads(STYLES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FatalError(f"styles.json 不是合法 JSON：{exc}") from exc

    if not isinstance(styles, list) or not styles:
        raise FatalError("styles.json 必须是非空数组")

    seen: set[str] = set()
    for index, item in enumerate(styles, 1):
        if not isinstance(item, dict):
            raise FatalError(f"styles.json 第 {index} 项必须是对象")
        for field in ("name", "prompt"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise FatalError(f"styles.json 第 {index} 项缺少 {field}")
        name = item["name"].strip()
        if name in seen:
            raise FatalError(f"styles.json 第 {index} 项风格名重复：{name}")
        if re.search(r'[\\/:*?"<>|]', name):
            raise FatalError(
                f"styles.json 第 {index} 项风格名不能包含路径或文件名非法字符：{name}"
            )
        seen.add(name)
        item["name"] = name
        item["prompt"] = item["prompt"].strip()
    return styles


def select_styles(styles: list[dict], day: date, count: int) -> list[dict]:
    """按日历日期轮换风格库，选出 count 个互不重复的风格。

    每天在风格库上的起点前进一位（date.toordinal），所以只要 count 小于
    风格库条目数，相邻两天选中的组合就一定不同；YYYYMMDD 整数同时作为随机
    种子，决定当天风格的展示顺序。同一日期多次运行结果一致。
    """
    if count < 1:
        raise FatalError("风格数量必须大于等于 1")
    if count > len(styles):
        raise FatalError(
            f"风格库只有 {len(styles)} 个风格，无法选出 {count} 个互不重复的风格"
        )
    seed = int(day.strftime("%Y%m%d"))
    start = day.toordinal() % len(styles)
    picked = [styles[(start + offset) % len(styles)] for offset in range(count)]
    random.Random(seed).shuffle(picked)
    return picked


# ---------------------------------------------------------------------------
# Agnes API
# ---------------------------------------------------------------------------

def parse_retry_after(value: str | None) -> int | None:
    try:
        seconds = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(seconds, 0)


def post_generation(api_key: str, prompt: str, size: str) -> dict:
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "n": 1,
        "size": size,
        "response_format": "url",
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "wallpaper-factory/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        body = response.read()
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        preview = body[:300].decode("utf-8", "replace")
        raise FatalError(f"API 返回的不是 JSON（前 300 字节）：{preview}") from exc


def request_generation(api_key: str, prompt: str, size: str, interval: float) -> dict:
    """请求生成一张图；429/5xx/网络错误自动重试，402/401 立即失败。"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return post_generation(api_key, prompt, size)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code == 402:
                raise FatalError(
                    "HTTP 402：Agnes 额度或付费权限不足。未修改 index.html，旧站保持可用。"
                    f" 响应：{detail}"
                ) from exc
            if exc.code in (401, 403):
                raise FatalError(
                    f"HTTP {exc.code}：API key 无效或没有权限。响应：{detail}"
                ) from exc
            if exc.code == 429:
                wait = parse_retry_after(exc.headers.get("Retry-After")) or int(interval)
                wait = max(wait, 20)
                if attempt == MAX_RETRIES:
                    raise FatalError(
                        f"HTTP 429：触发限流，已重试 {MAX_RETRIES} 次。"
                        "未修改 index.html，旧站保持可用。"
                    ) from exc
                log(f"  触发限流（429），{wait} 秒后重试（{attempt}/{MAX_RETRIES}）")
                time.sleep(wait)
                continue
            if exc.code >= 500:
                wait = min(30 * 2 ** (attempt - 1), 300)
                if attempt == MAX_RETRIES:
                    raise FatalError(
                        f"HTTP {exc.code}：API 服务异常，已重试 {MAX_RETRIES} 次。"
                        f" 响应：{detail}"
                    ) from exc
                log(f"  API 服务异常（{exc.code}），{wait} 秒后重试（{attempt}/{MAX_RETRIES}）")
                time.sleep(wait)
                continue
            raise FatalError(f"HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            wait = min(10 * 2 ** (attempt - 1), 180)
            if attempt == MAX_RETRIES:
                raise FatalError(
                    f"网络请求失败，已重试 {MAX_RETRIES} 次：{exc}"
                ) from exc
            log(f"  网络异常（{exc}），{wait} 秒后重试（{attempt}/{MAX_RETRIES}）")
            time.sleep(wait)
    raise FatalError("生成请求失败")  # 理论上不可达


def download_image(url: str, attempts: int = 3) -> bytes:
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "wallpaper-factory/1.0"}
            )
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == attempts:
                raise
            time.sleep(5 * attempt)
    raise FatalError("图片下载失败")  # 理论上不可达


def extract_image_bytes(payload: dict) -> tuple[bytes, str]:
    """从 API 响应中取出图片。实测返回 data[0].url，这里同时兼容 b64_json。"""
    if not isinstance(payload, dict):
        raise FatalError("API 响应不是 JSON 对象")
    items = payload.get("data")
    if not isinstance(items, list) or not items:
        raise FatalError(f"API 响应缺少 data 数组，顶层字段：{sorted(payload)}")

    item = items[0]
    if not isinstance(item, dict):
        raise FatalError(f"data[0] 不是对象：{type(item).__name__}")

    b64 = item.get("b64_json")
    if isinstance(b64, str) and b64.strip():
        try:
            return base64.b64decode(b64), "b64_json"
        except (ValueError, TypeError) as exc:
            raise FatalError(f"b64_json 解码失败：{exc}") from exc

    url = item.get("url")
    if isinstance(url, str) and url.strip():
        try:
            return download_image(url), "url"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FatalError(f"图片下载失败：{exc}") from exc

    raise FatalError(
        "响应里既没有 url 也没有 b64_json，data[0] 字段：" + str(sorted(item))
    )


def ensure_pillow() -> None:
    """在调用 API 前确认 Pillow 可用，避免白白消耗一次生成额度。"""
    try:
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise FatalError(
            "需要 Pillow 把 API 返回的 PNG 转为 JPEG，请先执行：pip install Pillow"
        ) from exc


def save_as_jpeg(raw: bytes, destination: Path) -> tuple[int, int]:
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise FatalError(
            "需要 Pillow 把 API 返回的 PNG 转为 JPEG，请先执行：pip install Pillow"
        ) from exc

    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            ):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                output = background
            else:
                output = image.convert("RGB")
            width, height = output.size
            output.save(
                destination, "JPEG", quality=92, optimize=True, progressive=True
            )
    except FatalError:
        raise
    except Exception as exc:
        raise FatalError(f"图片转换/保存失败：{exc}") from exc
    return width, height


def build_image_path(style_name: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{style_name}"
    candidate = IMG_DIR / f"{base}.jpg"
    suffix = 2
    while candidate.exists():
        candidate = IMG_DIR / f"{base}-{suffix}.jpg"
        suffix += 1
    return candidate


# ---------------------------------------------------------------------------
# index.html 重建
# ---------------------------------------------------------------------------

def collect_gallery() -> list[Path]:
    files = [
        p
        for p in IMG_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    ]
    files.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    return files[:GALLERY_SIZE]


def build_title_map(styles: list[dict], html_text: str) -> dict[str, str]:
    """风格名映射表；同时保留 index.html 里已有图片的 alt 标题。"""
    titles = {item["name"]: item["name"] for item in styles}
    for name, alt in EXISTING_IMG_RE.findall(html_text):
        titles.setdefault(name, unescape(alt))
    return titles


def title_for(name: str, styles: list[dict], titles: dict[str, str]) -> str:
    if name in titles:
        return titles[name]

    stem = Path(name).stem
    style_name = next(
        (
            item["name"]
            for item in sorted(styles, key=lambda s: len(s["name"]), reverse=True)
            if stem.endswith("-" + item["name"])
        ),
        None,
    )
    if style_name:
        return style_name

    match = re.match(r"^\d{8}-\d{6}-(.+)$", stem)
    label = match.group(1) if match else stem
    return label.replace("-", " ").replace("_", " ").title()


def render_masonry(files: list[Path], styles: list[dict], titles: dict[str, str]) -> str:
    entries = []
    for index, path in enumerate(files):
        title = title_for(path.name, styles, titles)
        entries.append(
            f'        <div class="item" onclick="openViewer({index})">\n'
            f'            <img src="img/{path.name}" alt="{escape(title, quote=True)}">\n'
            f"            <div class=\"overlay\">{escape(title, quote=True)}</div>\n"
            f"        </div>"
        )
    return "\n".join(entries)


def js_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def render_images_js(files: list[Path], styles: list[dict], titles: dict[str, str]) -> str:
    entries = []
    for path in files:
        title = title_for(path.name, styles, titles)
        entries.append(
            f"            {{ src: 'img/{js_string(path.name)}', "
            f"title: '{js_string(title)}' }},"
        )
    return "\n".join(entries)


def rebuild_index(styles: list[dict]) -> list[Path]:
    if not IMG_DIR.is_dir():
        raise FatalError(f"图片目录不存在：{IMG_DIR}")
    files = collect_gallery()
    if not files:
        raise FatalError(f"{IMG_DIR} 下没有可用的壁纸图片")

    raw = INDEX_PATH.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    html_text = raw.decode("utf-8")
    if newline == "\r\n":
        html_text = html_text.replace("\r\n", "\n")
    if not MASONRY_RE.search(html_text):
        raise FatalError("index.html 中找不到 masonry 区块（结构可能已变化）")
    if not IMAGES_JS_RE.search(html_text):
        raise FatalError("index.html 中找不到 JS images 数组（结构可能已变化）")

    titles = build_title_map(styles, html_text)
    masonry = render_masonry(files, styles, titles)
    images_js = render_images_js(files, styles, titles)

    new_html = MASONRY_RE.sub(
        lambda match: match.group(1) + masonry + match.group(3),
        html_text,
        count=1,
    )
    new_html = IMAGES_JS_RE.sub(
        lambda match: match.group(1) + "\n" + images_js + "\n" + match.group(3),
        new_html,
        count=1,
    )

    item_count = new_html.count('class="item"')
    js_count = new_html.count("{ src: 'img/")
    expected = len(files)
    if item_count != expected or js_count != expected:
        raise FatalError(
            f"index.html 重建校验失败：masonry={item_count}，JS={js_count}，预期={expected}"
        )

    temp_path = INDEX_PATH.with_name(INDEX_PATH.name + ".tmp")
    temp_path.write_bytes(new_html.replace("\n", newline).encode("utf-8"))
    os.replace(temp_path, INDEX_PATH)
    return files


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_day(value: str) -> date:
    """把 YYYYMMDD 解析为 date；非法日期由 argparse 直接报错。"""
    if not re.fullmatch(r"\d{8}", value):
        raise argparse.ArgumentTypeError("日期格式应为 YYYYMMDD，例如 20261103")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"不是有效日期：{value}") from exc


def print_dry_run(day: date, selected: list[dict]) -> None:
    """只打印当天选中的风格名与提示词，不触发任何 API 调用。"""
    log(f"{day.strftime('%Y%m%d')} 选中的 {len(selected)} 个风格（--dry-run，不调用 API、不生成图片）：")
    for index, item in enumerate(selected, 1):
        log(f"{index}. {item['name']}")
        log(f"   {item['prompt']}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按日期风格轮换调用 Agnes 生成壁纸，并把 index.html 更新为最新 16 张。"
    )
    parser.add_argument(
        "--count", type=int, default=4, help="本次生成的图片数量，默认 4"
    )
    parser.add_argument(
        "--date",
        type=parse_day,
        default=None,
        help="覆盖轮换日期（YYYYMMDD），默认今天；同一天结果可重现",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=65.0,
        help="每次 API 请求之间的等待秒数，默认 65（免费档约 1 请求/分钟）",
    )
    parser.add_argument(
        "--size", default="1024x1536", help="请求的图片尺寸，默认 1024x1536"
    )
    parser.add_argument(
        "--credentials",
        default=str(DEFAULT_CREDENTIALS),
        help="凭据文件路径，默认 C:/Users/1/.dsh/.credentials.yaml",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印当天选中的风格名与提示词，不调用 API、不生成图片",
    )
    parser.add_argument(
        "--rebuild-only",
        action="store_true",
        help="不调用 API，只按 img/ 文件修改时间重建 index.html",
    )
    args = parser.parse_args(argv)
    if args.count < 1:
        parser.error("--count 必须大于等于 1")
    if args.interval < 0:
        parser.error("--interval 不能为负数")
    if args.dry_run and args.rebuild_only:
        parser.error("--dry-run 与 --rebuild-only 不能同时使用")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    day = args.date or date.today()

    try:
        styles = load_styles()
    except FatalError as exc:
        log(f"无法读取风格库：{exc}")
        return 1

    if args.dry_run:
        try:
            selected = select_styles(styles, day, args.count)
        except FatalError as exc:
            log(f"无法选择风格：{exc}")
            return 1
        print_dry_run(day, selected)
        return 0

    if args.rebuild_only:
        try:
            files = rebuild_index(styles)
        except FatalError as exc:
            log(f"重建失败：{exc}")
            return 1
        log(f"已按最新文件重建 index.html，共 {len(files)} 张")
        return 0

    try:
        selected = select_styles(styles, day, args.count)
    except FatalError as exc:
        log(f"无法选择风格：{exc}")
        return 1

    try:
        ensure_pillow()
        api_key = read_api_key(Path(args.credentials))
    except FatalError as exc:
        log(f"无法开始生成：{exc}")
        return 1

    saved: list[Path] = []
    log(
        f"开始生成 {len(selected)} 张壁纸（{day.strftime('%Y%m%d')} 轮换），"
        f"模型 {MODEL}，串行请求间隔 {args.interval:g} 秒"
    )

    try:
        for index, item in enumerate(selected, 1):
            if index > 1:
                time.sleep(args.interval)
            log(f"[{index}/{len(selected)}] {item['name']}")
            payload = request_generation(api_key, item["prompt"], args.size, args.interval)
            raw, source = extract_image_bytes(payload)
            destination = build_image_path(item["name"])
            width, height = save_as_jpeg(raw, destination)
            saved.append(destination)
            log(
                f"  已保存 {destination.relative_to(REPO_ROOT)} "
                f"({width}x{height}，响应字段 {source})"
            )
    except FatalError as exc:
        log(f"生成中止：{exc}")
        if saved:
            log(
                "本轮已保存的图片保留在 img/ 中，但 index.html 未修改，旧站保持原样。"
            )
        else:
            log("index.html 未修改，旧站保持原样。")
        return 1
    except KeyboardInterrupt:
        log("已中断，index.html 未修改。")
        return 130

    try:
        files = rebuild_index(styles)
    except FatalError as exc:
        log(f"生成成功，但 index.html 重建失败：{exc}")
        return 1

    log(f"完成：新增 {len(saved)} 张，index.html 现展示最新 {len(files)} 张")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""壁纸工厂：按日期从风格库轮换 4 个风格，调用 Agnes 图像 API 生成新壁纸，
并重建站点页面。

API key 从本机凭据文件读取，绝不写入仓库。风格提示词存放在独立的
factory/styles.json 中，按 YYYYMMDD 日期整数每天轮换 4 个互不重复的风格，
同一天多次运行结果一致。生成结果保存到 img/。

每次成功重建都会更新三个文件：

- ``index.html``：最新 16 张瀑布流 +「最后更新 / 共 N 张」+ 归档入口；
- ``archive.html``：``img/`` 全量归档网格页，复用全屏查看器；
- ``status.json``：最近一次成功重建时间、图片总数、最近一批生成的文件名。

新增图片按文件名里的时间戳倒序排列；没有时间戳的旧站图片排在后面，
标题保存在 factory/titles.json。用法见 factory/README.md。
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
ARCHIVE_PATH = REPO_ROOT / "archive.html"
STATUS_PATH = REPO_ROOT / "status.json"
TITLES_PATH = FACTORY_DIR / "titles.json"
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
META_RE = re.compile(
    r"(<!-- archive-meta:start -->\n)(.*?)(\n\s*<!-- archive-meta:end -->)", re.DOTALL
)
TIMESTAMP_RE = re.compile(r"^(\d{8})-(\d{6})-")


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


def load_legacy_titles() -> dict[str, str]:
    """旧站图片没有提示词 slug，标题保存在 factory/titles.json（文件名 -> 标题）。"""
    if not TITLES_PATH.is_file():
        return {}
    try:
        data = json.loads(TITLES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FatalError(f"titles.json 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise FatalError("titles.json 必须是对象：文件名 -> 标题")
    titles: dict[str, str] = {}
    for name, title in data.items():
        if not isinstance(name, str) or not isinstance(title, str) or not title.strip():
            raise FatalError("titles.json 的键和值都必须是非空字符串")
        titles[name] = title.strip()
    return titles


def read_previous_status() -> dict:
    if not STATUS_PATH.is_file():
        return {}
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def previous_batch() -> list[str]:
    """--build-only 不生成新图片，沿用上一次成功生图记录的批次。"""
    batch = read_previous_status().get("last_batch")
    if isinstance(batch, list):
        return [name for name in batch if isinstance(name, str)]
    return []


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
# 站点重建（index.html / archive.html / status.json）
# ---------------------------------------------------------------------------

ARCHIVE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>全部壁纸 · AI壁纸精选</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body {
            background: #f5f5f5;
            font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
            min-height: 100vh;
            padding-bottom: 32px;
        }
        /* 顶部标题栏 */
        .archive-head {
            position: sticky;
            top: 0;
            z-index: 100;
            background: #fff;
            padding: 12px 16px;
            display: flex;
            align-items: center;
            gap: 12px;
            border-bottom: 1px solid #eee;
        }
        .back-link {
            color: #333;
            font-size: 14px;
            font-weight: 600;
            text-decoration: none;
            white-space: nowrap;
        }
        .archive-title { font-size: 17px; font-weight: 600; flex: 1; }
        .archive-meta {
            padding: 10px 16px 0;
            color: #666;
            font-size: 13px;
        }
        /* 全量归档网格 */
        .archive-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 8px;
            padding: 10px 8px 0;
            align-items: start;
        }
        @media (min-width: 640px) {
            .archive-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; padding: 12px; }
        }
        @media (min-width: 1024px) {
            .archive-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
        }
        .grid-item {
            border-radius: 12px;
            overflow: hidden;
            cursor: pointer;
            position: relative;
            background: #e0e0e0;
        }
        .grid-item img {
            width: 100%;
            display: block;
            aspect-ratio: 2 / 3;
            object-fit: cover;
            transition: transform 0.3s;
        }
        .grid-item:active img { transform: scale(0.98); }
        .grid-item .caption {
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            padding: 24px 10px 8px;
            background: linear-gradient(transparent, rgba(0,0,0,0.65));
            color: #fff;
            font-size: 12px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        /* 全屏查看器（复用 index.html 现有样式） */
        .viewer {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: #000;
            z-index: 200;
            flex-direction: column;
        }
        .viewer.show { display: flex; }
        .viewer .top-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 16px;
            color: #fff;
        }
        .viewer .close-btn {
            width: 32px;
            height: 32px;
            background: rgba(255,255,255,0.2);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
        }
        .viewer .img-wrap {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 10px;
        }
        .viewer .img-wrap img {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
            border-radius: 8px;
        }
        .viewer .bottom-bar {
            padding: 16px;
            display: flex;
            justify-content: center;
            gap: 20px;
        }
        .viewer .action-btn {
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 4px;
            color: #fff;
            font-size: 12px;
            cursor: pointer;
        }
        .viewer .action-btn svg { width: 28px; height: 28px; }
    </style>
</head>
<body>
    <div class="archive-head">
        <a class="back-link" href="index.html">← 返回首页</a>
        <span class="archive-title">全部壁纸</span>
    </div>

    <p class="archive-meta">@@META@@</p>

    <div class="archive-grid" id="archiveGrid">
@@GRID@@
    </div>

    <!-- 全屏查看器 -->
    <div class="viewer" id="viewer">
        <div class="top-bar">
            <div class="close-btn" onclick="closeViewer()">✕</div>
            <span id="imgTitle"></span>
            <div style="width:32px"></div>
        </div>
        <div class="img-wrap">
            <img id="viewerImg" src="" alt="">
        </div>
        <div class="bottom-bar">
            <div class="action-btn" onclick="prevImg()">
                <svg viewBox="0 0 24 24" fill="currentColor"><path d="M15.41 16.59L10.83 12l4.58-4.59L14 6l-6 6 6 6 1.41-1.41z"/></svg>
                <span>上一张</span>
            </div>
            <div class="action-btn" onclick="saveImg()">
                <svg viewBox="0 0 24 24" fill="currentColor"><path d="M19 12v7H5v-7H3v7c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2v-7h-2zm-6 .67l2.59-2.58L17 11.5l-5 5-5-5 1.41-1.41L11 12.67V3h2v9.67z"/></svg>
                <span>保存</span>
            </div>
            <div class="action-btn" onclick="nextImg()">
                <svg viewBox="0 0 24 24" fill="currentColor"><path d="M8.59 16.59L13.17 12 8.59 7.41 10 6l6 6-6 6-1.41-1.41z"/></svg>
                <span>下一张</span>
            </div>
        </div>
    </div>

    <script>
        let currentIndex = 0;
        const images = [
@@IMAGES@@
        ];

        function showImage(index) {
            currentIndex = (index + images.length) % images.length;
            document.getElementById('viewerImg').src = images[currentIndex].src;
            document.getElementById('imgTitle').textContent = images[currentIndex].title;
        }

        function openViewer(index) {
            showImage(index);
            document.getElementById('viewer').classList.add('show');
        }

        function closeViewer() {
            document.getElementById('viewer').classList.remove('show');
        }

        function nextImg() { showImage(currentIndex + 1); }

        function prevImg() { showImage(currentIndex - 1); }

        async function saveImg() {
            const { src, title } = images[currentIndex];
            try {
                const resp = await fetch(src);
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                const blob = await resp.blob();
                const url = URL.createObjectURL(blob);
                const link = document.createElement('a');
                link.href = url;
                link.download = title + '.jpg';
                document.body.appendChild(link);
                link.click();
                link.remove();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
            } catch (err) {
                alert('保存失败，请稍后重试');
            }
        }
    </script>
</body>
</html>
"""


def image_sort_key(path: Path) -> tuple[int, datetime, str]:
    """生成图按文件名时间戳倒序；没有时间戳的旧站图片排在后面，按文件名倒序。"""
    match = TIMESTAMP_RE.match(path.name)
    if match:
        try:
            stamp = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            stamp = None
        if stamp is not None:
            return (1, stamp, path.name)
    return (0, datetime.min, path.name)


def list_all_images() -> list[Path]:
    if not IMG_DIR.is_dir():
        raise FatalError(f"图片目录不存在：{IMG_DIR}")
    files = [
        p
        for p in IMG_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    ]
    files.sort(key=image_sort_key, reverse=True)
    return files


def build_title_map(
    styles: list[dict], html_text: str, legacy_titles: dict[str, str] | None = None
) -> dict[str, str]:
    """风格名映射表；同时保留 index.html 与 titles.json 里的标题。"""
    titles = {item["name"]: item["name"] for item in styles}
    for name, alt in EXISTING_IMG_RE.findall(html_text):
        titles.setdefault(name, unescape(alt))
    for name, title in (legacy_titles or {}).items():
        titles.setdefault(name, title)
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


def meta_text(total: int, moment: datetime) -> str:
    return f"最后更新 {moment.strftime('%Y-%m-%d')} · 共 {total} 张"


def render_index_meta(total: int, moment: datetime) -> str:
    return (
        f'        <span class="meta-text">{meta_text(total, moment)}</span>\n'
        f'        <a class="meta-link" href="archive.html">查看全部归档 →</a>'
    )


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


def render_archive_grid(
    files: list[Path], styles: list[dict], titles: dict[str, str]
) -> str:
    entries = []
    for index, path in enumerate(files):
        title = title_for(path.name, styles, titles)
        entries.append(
            f'        <div class="grid-item" onclick="openViewer({index})">\n'
            f'            <img src="img/{path.name}" alt="{escape(title, quote=True)}"'
            f' loading="lazy">\n'
            f'            <div class="caption">{escape(title)}</div>\n'
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


def render_archive_html(
    files: list[Path],
    styles: list[dict],
    titles: dict[str, str],
    total: int,
    moment: datetime,
) -> str:
    return (
        ARCHIVE_TEMPLATE.replace("@@META@@", meta_text(total, moment))
        .replace("@@GRID@@", render_archive_grid(files, styles, titles))
        .replace("@@IMAGES@@", render_images_js(files, styles, titles))
    )


def write_text(path: Path, text: str, newline: str = "\n") -> None:
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    os.replace(temp_path, path)


def rebuild_site(
    styles: list[dict], batch: list[str] | None = None
) -> tuple[list[Path], int]:
    """重建 index.html、archive.html 与 status.json；失败时抛 FatalError。"""
    all_files = list_all_images()
    if not all_files:
        raise FatalError(f"{IMG_DIR} 下没有可用的壁纸图片")
    files = all_files[:GALLERY_SIZE]
    total = len(all_files)

    raw = INDEX_PATH.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    html_text = raw.decode("utf-8")
    if newline == "\r\n":
        html_text = html_text.replace("\r\n", "\n")
    for pattern, label in (
        (MASONRY_RE, "masonry 区块"),
        (IMAGES_JS_RE, "JS images 数组"),
        (META_RE, "归档状态区块"),
    ):
        if not pattern.search(html_text):
            raise FatalError(f"index.html 中找不到{label}（结构可能已变化）")

    titles = build_title_map(styles, html_text, load_legacy_titles())
    moment = datetime.now().astimezone().replace(microsecond=0)

    new_html = MASONRY_RE.sub(
        lambda match: match.group(1) + render_masonry(files, styles, titles) + match.group(3),
        html_text,
        count=1,
    )
    new_html = IMAGES_JS_RE.sub(
        lambda match: match.group(1)
        + "\n"
        + render_images_js(files, styles, titles)
        + "\n"
        + match.group(3),
        new_html,
        count=1,
    )
    new_html = META_RE.sub(
        lambda match: match.group(1) + render_index_meta(total, moment) + match.group(3),
        new_html,
        count=1,
    )

    item_count = new_html.count('class="item"')
    js_count = new_html.count("{ src: 'img/")
    if item_count != len(files) or js_count != len(files):
        raise FatalError(
            f"index.html 重建校验失败：masonry={item_count}，JS={js_count}，预期={len(files)}"
        )

    archive_html = render_archive_html(all_files, styles, titles, total, moment)
    grid_count = archive_html.count('class="grid-item"')
    archive_js_count = archive_html.count("{ src: 'img/")
    if grid_count != total or archive_js_count != total:
        raise FatalError(
            f"archive.html 重建校验失败：网格={grid_count}，JS={archive_js_count}，预期={total}"
        )

    batch_names = previous_batch() if batch is None else [Path(name).name for name in batch]
    status_text = (
        json.dumps(
            {
                "last_success": moment.isoformat(),
                "total": total,
                "last_batch": batch_names,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )

    write_text(INDEX_PATH, new_html, newline)
    write_text(ARCHIVE_PATH, archive_html, newline)
    write_text(STATUS_PATH, status_text)
    return files, total


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
        description=(
            "按日期风格轮换调用 Agnes 生成壁纸，并重建 index.html、archive.html "
            "与 status.json。"
        )
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
        "--build-only",
        "--rebuild-only",
        dest="build_only",
        action="store_true",
        help=(
            "不调用 API，只用 img/ 现有图片重建 index.html、archive.html 与 "
            "status.json（--rebuild-only 为兼容旧用法保留的别名）"
        ),
    )
    args = parser.parse_args(argv)
    if args.count < 1:
        parser.error("--count 必须大于等于 1")
    if args.interval < 0:
        parser.error("--interval 不能为负数")
    if args.dry_run and args.build_only:
        parser.error("--dry-run 与 --build-only 不能同时使用")
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

    if args.build_only:
        try:
            files, total = rebuild_site(styles)
        except FatalError as exc:
            log(f"重建失败：{exc}")
            return 1
        log(
            f"已重建 index.html（最新 {len(files)} 张）、"
            f"archive.html（全部 {total} 张）与 status.json"
        )
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
                "本轮已保存的图片保留在 img/ 中，但页面未修改，旧站保持原样。"
            )
        else:
            log("页面未修改，旧站保持原样。")
        return 1
    except KeyboardInterrupt:
        log("已中断，页面未修改。")
        return 130

    try:
        files, total = rebuild_site(styles, [path.name for path in saved])
    except FatalError as exc:
        log(f"生成成功，但页面重建失败：{exc}")
        return 1

    log(
        f"完成：新增 {len(saved)} 张，首页展示最新 {len(files)} 张，"
        f"归档共 {total} 张"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

# 壁纸工厂（Agnes 每日生图管线）

`factory/generate.py` 按 `YYYYMMDD` 日期从独立风格库 `factory/styles.json`
轮换 4 个互不重复的风格，调用 Agnes 图像 API 生成竖版壁纸，保存到仓库的
`img/`，并重建站点页面：

- `index.html`：瀑布流与 JS `images` 数组为**最新的 16 张**，并显示
  「最后更新 <日期> · 共 N 张」和「查看全部归档」入口；
- `archive.html`：**全量归档页**，网格展示 `img/` 下所有壁纸，点击复用全屏查看器；
- `status.json`：每日产线状态，含最近成功时间、图片总数、最近一批文件名。

同一天多次运行选中的风格一致，跨日自动轮换；站点样式与结构保持不变，
没有任何新增依赖。

## 依赖

- Python 3.10+（只用标准库发起请求）
- [Pillow](https://pypi.org/project/Pillow/)：API 返回 PNG，需要转成 `img/*.jpg`

```bash
pip install Pillow
```

## 凭据

从 `C:/Users/1/.dsh/.credentials.yaml` 读取，行格式：

```yaml
AGNES_API_KEY: "sk-..."
```

也可以临时用环境变量 `AGNES_API_KEY` 覆盖。API key 只在本机读取，
不会写进代码、日志或提交到仓库。

## 用法

```bash
# 默认按今天生成 4 张（串行，约 4-5 分钟）
python factory/generate.py

# 只看今天选中哪 4 个风格及其提示词（不调用 API、不生成图片）
python factory/generate.py --dry-run

# 指定日期查看/生成（YYYYMMDD），便于测试轮换或补生成
python factory/generate.py --dry-run --date 20261103
python factory/generate.py --date 20261103

# 生成 6 张（数量不能超过风格库条目数）
python factory/generate.py --count 6

# 不调用 API，只用 img/ 现有图片重建 index.html、archive.html 与 status.json
python factory/generate.py --build-only
```

参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--count` | `4` | 本次生成数量，不能超过风格库条目数 |
| `--date` | 今天 | 覆盖轮换日期（`YYYYMMDD`）；同一天结果可重现 |
| `--dry-run` | 关闭 | 只打印当天选中的风格名与提示词，不调用 API、不生成图片 |
| `--interval` | `65` | 两次 API 请求之间的等待秒数；免费档约 1 请求/分钟 |
| `--size` | `1024x1536` | 请求的尺寸（实际输出尺寸以 API 返回为准） |
| `--credentials` | `C:/Users/1/.dsh/.credentials.yaml` | 凭据文件路径 |
| `--build-only` | 关闭 | 跳过 API，只重建三个页面产物；`--rebuild-only` 是兼容旧用法保留的别名 |

## 风格库与每日轮换

生图提示词集中在 `factory/styles.json`，只用标准库 `json` 解析，不引入新依赖。
每条风格包含 `name` 与 `prompt`：

```json
{ "name": "林深见鹿", "prompt": "English wallpaper prompt ..." }
```

- 库里现有 16 个风格；`name` 需唯一，且不能包含 `/ \ : * ? " < > |` 等
  路径或文件名非法字符（它会进入图片文件名）。
- 每天从风格库中轮换选出 4 个互不重复的风格（默认 `--count 4`）：
  轮换起点按日历日推进，`YYYYMMDD` 整数同时作为随机种子决定当天展示顺序。
- 同一日期多次运行结果一致；跨日自动轮换。只要 `--count` 小于风格库条目数，
  相邻两天就不会选中同一组风格。
- 用 `--dry-run` 预览当天选择，用 `--date YYYYMMDD` 覆盖日期。

## 产物

- 图片：`img/<时间戳>-<风格名>.jpg`，例如 `img/20261104-120000-林深见鹿.jpg`
  - 时间戳为本地时间 `YYYYMMDD-HHMMSS`，重复运行不会覆盖旧图；
  - 已存在的图片不会被删除或修改。
- `index.html`：只重写 masonry 区块、JS `images` 数组和归档状态区块
  （`archive-meta:start/end` 之间），取 `img/` 下最新 16 张。
  - 生成图的标题即 `styles.json` 的风格名；旧站图片的标题从当前 `index.html`
    的 `alt` 属性或 `factory/titles.json` 保留。
- `archive.html`：`img/` 全部图片的网格归档页（2 列，宽屏自动 3/4 列）。
  - 排序：文件名带 `YYYYMMDD-HHMMSS` 时间戳的图片按时间倒序；没有时间戳的
    旧站图片排在后面，按文件名倒序。结果不依赖文件修改时间（克隆后 mtime 会变）。
  - 点击任意缩略图打开与 `index.html` 相同的全屏查看器，支持上一张/下一张/保存。
- `status.json`：

  ```json
  {
    "last_success": "2026-11-03T23:34:18+08:00",
    "total": 21,
    "last_batch": ["20261103-183839-forest-deer.jpg", "..."]
  }
  ```

  - `last_success`：最近一次成功重建页面（含 `--build-only`）的本地时间（ISO 8601）；
  - `total`：`img/` 下壁纸总数，与 `archive.html` 的图片数量一致；
  - `last_batch`：最近一次生图批次保存的文件名；`--build-only` 不生成新图，
    沿用上一次记录（没有记录时为 `[]`）。
  - 生图或重建失败时不会写入，文件保持上一次成功时的内容。
- `factory/titles.json`：旧站图片（文件名不含时间戳/风格名）的中文标题表，
  新增这类图片时把 `文件名 -> 标题` 加进去即可。

## 失败行为

- **402（额度/付费受限）**：立即停止，不修改页面，旧站保持可用，退出码 1。
- **429（限流）**：按 `Retry-After` 或 `--interval` 等待后重试，最多 5 次；
  仍失败则不修改页面，旧站保持可用，退出码 1。
- **401/403（key 无效或无权限）**：立即停止，不修改页面。
- **5xx / 网络错误**：指数退避重试，最多 5 次。
- 批量中途失败时，已成功保存的图片会留在 `img/`，但本轮不改页面和 `status.json`；
  下次成功运行会自动纳入最新图片。

## 实测响应结构

2026-11 实测 `POST https://apihub.agnes-ai.com/v1/images/generations`
（`model=agnes-image-2.1-flash`）返回：

```json
{
  "data": [{ "url": "https://.../output_xxx.png", "b64_json": "", "revised_prompt": "" }],
  "created": 1793701952,
  "task_id": "task_..."
}
```

脚本优先使用非空的 `b64_json`，否则下载 `data[0].url`，两者都兼容。
返回的 PNG 会用 Pillow 转成 JPEG 后保存。

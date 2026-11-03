# 壁纸工厂（Agnes 每日生图管线）

`factory/generate.py` 调用 Agnes 图像 API 生成竖版壁纸，保存到仓库的 `img/`，
并把 `index.html` 的瀑布流与 JS `images` 数组替换为**按文件修改时间倒序的最新 16 张**。
站点样式与结构保持不变。

## 依赖

- Python 3.9+（只用标准库发起请求）
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
# 默认生成 4 张（串行，约 4-5 分钟）
python factory/generate.py

# 生成 6 张
python factory/generate.py --count 6

# 只重建 index.html，不调用 API（本地验证/手工补图后使用）
python factory/generate.py --rebuild-only
```

参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--count` | `4` | 本次生成数量 |
| `--interval` | `65` | 两次 API 请求之间的等待秒数；免费档约 1 请求/分钟 |
| `--size` | `1024x1536` | 请求的尺寸（实际输出尺寸以 API 返回为准） |
| `--credentials` | `C:/Users/1/.dsh/.credentials.yaml` | 凭据文件路径 |
| `--rebuild-only` | 关闭 | 跳过 API，只重建 `index.html` |

主题从 `factory/prompts.json` 随机抽取。每一项包含：

```json
{ "slug": "aurora-veil", "title": "极光帷幕", "prompt": "English wallpaper prompt ..." }
```

新增主题时保持 `slug` 只含小写字母、数字和连字符（它会进入文件名）。

## 产物

- 图片：`img/<时间戳>-<slug>.jpg`，例如 `img/20261103-183500-aurora-veil.jpg`
  - 时间戳为本地时间 `YYYYMMDD-HHMMSS`，重复运行不会覆盖旧图；
  - 已存在的图片不会被删除或修改。
- `index.html`：只重写 masonry 区块和 JS `images` 数组，取 `img/` 下最新 16 张
  （修改时间倒序；时间相同时按文件名倒序，保证结果稳定）。
  - 生成图的中文标题来自 `prompts.json`；已有图片的标题从当前 `index.html` 的
    `alt` 属性保留。

## 失败行为

- **402（额度/付费受限）**：立即停止，不修改 `index.html`，旧站保持可用，退出码 1。
- **429（限流）**：按 `Retry-After` 或 `--interval` 等待后重试，最多 5 次；
  仍失败则不修改 `index.html`，旧站保持可用，退出码 1。
- **401/403（key 无效或无权限）**：立即停止，不修改 `index.html`。
- **5xx / 网络错误**：指数退避重试，最多 5 次。
- 批量中途失败时，已成功保存的图片会留在 `img/`，但本轮不改 `index.html`；
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

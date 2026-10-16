# 图文宣传素材

三张 1080 × 1440 PNG，按以下顺序组成一次图文：

1. `01-cover.png`：使用场景与项目名称。
2. `02-product.png`：顶部胶囊和缓存查看机制。
3. `03-open-source.png`：开源仓库与使用条件。

`douyin-copy.md` 提供可复制的标题、正文和话题。`contact-sheet.png` 仅供快速检查，不用于上传。素材未实际发布。

## 内容来源与边界

- 功能和适用条件依据本仓库 README、`docs/widget.md`、`docs/ui-design.md`。
- 第 1 张使用 CSS 绘制场景示意；第 2 张使用测试中生成的合成演示。它们不包含真实会话或工作项目内容。
- `assets/collapsed.png`、`assets/expanded.png` 是 `tests/render_native_preview.py` 调用产品绘制器生成的合成素材副本，不代表高 DPI、多屏、悬停或全屏交互已通过真实环境验收。
- 宣传不承诺完全本地、离线运行、免费无限使用或固定效率提升。生成摘要需要在线模型和可用额度。

## 重新导出

海报使用 HTML/CSS 排版，源码为 `posters.html`。渲染器只读取本目录中的 HTML 和合成 PNG，不读取运行数据库或真实会话。

需要更新界面演示时，在 Windows 上使用测试预览入口，生成 2 倍逻辑尺寸的透明 PNG：

```powershell
python -X utf8 tests/render_native_preview.py --output evidence/native-ui --scale 2
Copy-Item evidence/native-ui/collapsed.png docs/publicity/assets/collapsed.png
Copy-Item evidence/native-ui/expanded-new.png docs/publicity/assets/expanded.png
```

需要 Node.js 和 Playwright。可复用已有环境；在临时目录安装 `playwright` 时，通过 `NODE_PATH` 指向其 `node_modules`，无需把宣传依赖加入产品运行依赖。默认使用 Playwright 已安装的 Chromium，也可通过环境变量选择已安装的浏览器：

```powershell
# 在仓库根目录执行；本机已安装 Edge 时可直接使用。
$env:PLAYWRIGHT_CHANNEL = 'msedge'
node docs/publicity/render.cjs
```

如果浏览器不在默认位置，可用 `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` 指定可执行文件。输出使用本机中文字体；本次使用 Microsoft YaHei UI，换电脑后应重新检查换行和视觉效果。

修改配图后运行渲染器，并查看三张导出图，确认中文无截断、内容完整、页码正确，且开源状态和使用条件仍准确。

## 本次检查

- 三张 PNG 的文件头尺寸均为 1080 × 1440；总览为 3240 × 1440。
- 渲染器确认两张合成素材完整加载，主要内容没有超出画布。
- 已逐张查看导出图，检查中文、使用条件、图片缩放与留白。
- 这是静态宣传素材检查，不替代小窗在目标电脑上的交互验收，也不代表已发布到抖音。

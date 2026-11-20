# ujshot

iOS 模拟器界面走查自动化：**点进去 → 截图 → 抓 AX 树 → OCR 识别文案 → 出一份 HTML 图册**，一条命令。

```bash
ujshot run --flow smoke --open
```

它不做「像素级设计稿比对」（那是另一类工具），它回答的是三个更朴素的问题：

1. 这些界面**点得进去**吗？（链路能不能走通）
2. 进去以后**显示的是不是该显示的东西**？（`expect` / `expectAbsent` 文案断言）
3. 点完**有没有反应**？（相邻两屏像素差 < 0.5% 就判 `no-visible-change`）

因为判定是机器做的，它可以直接当 CI 关卡用：任一屏不达标就 `exit 1`。

---

## 装

需要 macOS + Xcode（模拟器）+ [axe](https://github.com/cameroncooke/AXe)。

```bash
brew install cameroncooke/axe/axe     # AX 树 / 点击 / 滑动
xcode-select --install                # 如果还没装命令行工具

git clone <this repo> ~/dev/ujshot
pipx install -e ~/dev/ujshot          # 或者：pip install -e ~/dev/ujshot[diff]
```

`[diff]` 会装 Pillow（像素差判定）。不装也能跑，只是跳过 diff 那一项。

```bash
ujshot doctor        # 自检：xcrun / axe / OCR / Pillow / 配置
ujshot devices       # 列已启动的模拟器
```

OCR 用的是系统 Vision，首次运行会把自带的 Swift 小工具编译到
`~/Library/Caches/ujshot/`（不用手动做任何事）。

---

## 用

### 1. 在任何项目里初始化

```bash
cd /path/to/your-app
ujshot init
```

生成 `ujshot.toml` + `flows/smoke.json`。`bundle_id` 会尝试从 `project.yml` 里猜，
猜不到就写 `com.example.app`——**这是唯一必填项**。

```toml
[app]
bundle_id = "com.example.app"
state_path = ""                 # 可选：种子数据写进容器的哪个文件

[device]
name = "iPhone 18 Pro Max"
appearance = "dark"

[paths]
out = "ujshot-out"
flow_dir = "flows"

[board]
script = ""                     # 可选：外部评审板脚本（见下）
port = 8766
```

`ujshot.toml` 会从当前目录**向上查找**，所以在子目录里跑也能命中。

### 2. 写一条流程

`flows/smoke.json`：

```json
{
  "name": "smoke",
  "note": "最小冒烟：三个 Tab + 一个模块",
  "startTab": "tab.home",
  "steps": [
    { "op": "shot", "id": "01-home", "title": "首页", "expect": ["首页"] },
    { "op": "tap", "label": "设置", "wait": 2.2 },
    { "op": "shot", "id": "02-settings", "title": "设置页",
      "expect": ["设置"], "expectAbsent": ["TODO"] }
  ]
}
```

### 3. 跑

```bash
ujshot run --flow smoke --open
```

产物在 `ujshot-out/<时间戳>-smoke/`：

```
index.html      图册：每屏缩略图 + diff% + AX 节点数 + 判定
steps.json      每步回执（含 OCR 原文、判定、耗时相关字段）
screens/*.png   原图
trees/*.json    AX 树原文
ocr/*.json      OCR 行 + 归一化坐标
```

---

## 流程文件参考

### 顶层字段

| 字段 | 作用 |
|---|---|
| `name` | 流程名，也是默认输出目录后缀 |
| `note` | 说明，会显示在图册顶部 |
| `startTab` | 启动后先点这个 identifier（如 `tab.home`） |
| `launch.restart` | 是否重启 App（默认 `true`） |
| `launch.args` | 传给 App 的启动参数 |
| `beforeMap` | `{shotId: 改前图文件名}`，灌评审板时用 |
| `steps` | 步骤数组 |

### 步骤

| `op` | 必填 | 可选 | 说明 |
|---|---|---|---|
| `shot` | `id` | `title` `note` `parent` `expect` `expectAbsent` | 截一屏并判定。`id` 同时是文件名，必须唯一 |
| `tap` | 见下 | `wait` `dangerous` `optional` | `id` / `label` / `x`+`y` 任一定位方式 |
| `ensure` | `text` | `then` `retries` `wait` | 确保文案在屏幕上；不在就点 `then` 里的目标重试 |
| `swipe` | — | `from` `to` `wait` | 纵向滑，`from`/`to` 是屏高比例（0.75 → 0.32） |
| `type` | `text` | — | 往焦点输入，**仅 ASCII** |
| `clipboard` | `text` | `paste` | 走剪贴板 + Cmd+V，中文用这个 |
| `key` | `code` | `modifiers` | HID 键码；`modifiers: [227]` 是 Cmd |
| `wait` | — | `seconds` | 硬等 |
| `expect` | `text` | — | 断言当前屏出现这些文案 |
| `launch` | — | `args` | 重启 App |
| `back` | — | — | 点 `BackButton`，找不到就左缘右滑 |

**`tap` 的定位顺序**（每一步都试，命中即停）：

```
identifier 全等 → identifier 前缀 → label 全等 → label 包含 → OCR 坐标兜底
```

OCR 兜底不是锦上添花：`axe describe-ui` 在部分模拟器上会偶发只返回应用根节点，
这时所有 id / label 都查不到，只有 OCR 坐标还能点。所以**流程里写 `label` 最稳，
坐标最不稳**（换机型就废）。

**`dangerous`**：`tap` 的标签命中删除 / 清空 / 退出这类词时会被跳过，并在回执里
记 `skipped`。走查不该改用户数据。

**`optional`**：目标不存在不算失败（用于"有就关掉"的收尾动作）。

---

## 判定

| `verdict` | 含义 | 算失败 |
|---|---|---|
| `ok` | 截图成功、`expect` 全中、`expectAbsent` 全无、与上屏有明显差异 | 否 |
| `no-visible-change` | 与上一屏像素差 < 0.5%（点了没反应） | 是 |
| `expect-missing` | `expect` 里有词没在 OCR 里出现 | 是 |
| `unexpected-text` | `expectAbsent` 里的词出现了 | 是 |
| `ocr-unavailable` | 要断言文案但 OCR 跑不起来 | 是（**不假装通过**） |
| `skipped` | 危险点击被跳过 | 否 |
| `optional-missing` | 可选目标不存在 | 否 |

`expectAbsent` 是这套工具最实用的一个字段。典型用法：中文界面里不该出现 ISO
货币代码。

```json
{ "op": "shot", "id": "03-currency", "expect": ["设置"], "expectAbsent": ["CNY", "USD"] }
```

同一套断言也能离线跑在**任何已有截图**上（不起模拟器）：

```bash
ujshot check --image old-shot.png --expect 设置 --must-not CNY,USD
# {"verdict":"unexpected-text","leaked":["CNY"],...}   退出码 1
```

改前/改后对比时特别好用：改前那张应该是 `unexpected-text`，改后应该是 `ok`。

---

## 代码里用

```python
from ujshot import config, flow, runner
from ujshot import device as dev

cfg = config.load(".")
fl = flow.load("flows/smoke.json")
axe = dev.which_axe()
udid = dev.resolve_device(cfg.device)
dev.ensure_booted(udid)

r = runner.Runner(axe, udid, fl, outdir="ujshot-out/manual",
                  bundle=cfg.bundle_id).run()
print(r.ok, len(r.shots), r.anomalies)
```

---

## 版本号

图册和评审板都会记录**这批截图是哪个版本拍的**。读的是模拟器上那份包的
`Info.plist`（`CFBundleShortVersionString` + `CFBundleVersion`），不是仓库里的配置——
因为「本机改了但没出包」时这两个数字不一样，证据必须写实际跑起来的那份。

```bash
ujshot run --flow smoke --open
# 输出：版本 0.1.0 (237)
```

- 图册顶部会显示；`steps.json` 的 `meta.version` 也有一份
- 灌评审板时自动写进 `<board>/versions.json`（`--board-as-after` 就记到 `after`，否则 `before`）
- 读不到 Info.plist 就留空并在图册标「未标版本」，**不猜**

单独补标 / 改标（比如从别处导入了一批旧图）：

```bash
ujshot board --set-version "0.1.0 (233)" --side before --into out/board
ujshot board --set-version "0.1.0 (236)" --side after --image old.png --into out/board
```

`versions.json` 是**旁挂文件**，和 `annotations.json` 分开：清空批注不会把版本信息一起抹掉。

```json
{
  "before": "0.1.0 (233)",
  "after": "0.1.0 (237)",
  "overrides": { "before": {}, "after": { "old.png": "0.1.0 (236)" } }
}
```

`overrides` 里放单张例外，整批标签不受影响。

## 评审板（可选）

图册是**只读**的。如果需要人**在截图上框选批注、把问题回传**，`ujshot` 能喂任何
遵守下面目录约定的板子：

```
<board-dir>/                  # 改前图
<board-dir>/after/            # 改后图（同名才能并排对比）
<board-dir>/annotations.json  # {"version":1,"annotations":[...]}
```

```bash
ujshot run --flow smoke \
  --board-dir out/board --board-as-after \
  --board-baseline out/old-screens \
  --board-script ~/dev/your-board.py --serve
```

| 参数 | 作用 |
|---|---|
| `--board-dir` | 把截图灌进板子目录 |
| `--board-as-after` | 进 `after/`（改名后图） |
| `--board-baseline` | 改前图目录，按流程的 `beforeMap` 配对 |
| `--board-script` | 板子程序；`--serve` 时用它起服务 |
| `--serve` | 起板子并打印地址 |

自动批注带 `source: "ujshot"`，**重跑只替换自己那批，不动人的批注**。

反向读人标的：

```bash
ujshot board --read out/board --status rejected
# [{id, image, target, status, text, rejectReason, box:{x,y,w,h}}, ...]
```

`box` 是归一化坐标（0…1），拿来定位足够。

### 迁移/合并另一个板子

```bash
# 从磁盘（板子没在跑）
ujshot board --import old-board --into out/board

# 从正在跑的板子导入：带上浏览器里尚未保存的批注
ujshot board --import-url http://127.0.0.1:8765 --into out/board \
  --push http://127.0.0.1:8766
```

做的是：复制 `<源>/` 与 `<源>/after/` 的全部图片（同名不覆盖），把人工批注并入
目标 `annotations.json`，`--push` 再让已打开的板子立刻刷新。

| 行为 | 说明 |
|---|---|
| 批注 id 冲突 | 两个板子都从 `U1` 起，同 id 但内容不同 → 重命名为 `U11@<源目录名>`；**不静默覆盖** |
| 同图同文案同坐标 | 视为重复，默认跳过（`--prefer src` 可改为用来源的） |
| 来源标记 | 每条导入补 `importedFrom: <源目录名>`，板子里看得到出处 |
| 图片 | 已存在就跳过，不覆盖目标板已经看过的图 |

`--import-url` 比 `--import` 更准：`/api/state` 返回的是页面当前内容，磁盘上的
`annotations.json` 只有已落盘的。

---

## 已知限制

- **只有 macOS。** 依赖 `xcrun simctl` 和 Vision，没法跨平台。
- **`type` 只支持 ASCII。** 中文/emoji 用 `clipboard` op（模拟器不装中文键盘）。
- **首次启动的系统授权弹窗**（麦克风、相册等）会影响首屏。`run` 启动后会尝试点
  「允许 / 使用App时允许」；新类型的弹窗需要自己加一句 `tap`。
- **编辑类断言不可行。** 这是截图工具，不是 XCUITest：它证明"屏幕上是什么"，
  不证明"数据库里写了什么"。数据正确性请用单测。
- **OCR 会认错字。** 断言文案要选稳定的短词（两三个字的名词），别断言整句；容错靠
  `check --expect` 一次给多个词。

## 为什么这样设计

来自 2026-11-20 一次真实的 48 屏走查，当时踩到的坑直接决定了现在的形状：

| 踩到的坑 | 现在的做法 |
|---|---|
| 一次性脚本，换人就重写 | 流程是 JSON，加场景只写数据 |
| `axe describe-ui` 偶发只返回根节点 | 每次查询都重试，且永远留 OCR 坐标兜底 |
| `axe screenshot` 返回过期帧 | 截图统一走 `xcrun simctl io screenshot` |
| 点了个控件没反应，图看着却"正常" | 相邻两屏像素差 < 0.5% 判 `no-visible-change` |
| 只看图不知道文案对不对 | OCR + `expect` / `expectAbsent` 机器判定 |
| OCR 挂了悄悄通过 | 单独一个 `ocr-unavailable` 判定，算失败 |
| 走查脚本把种子数据改花 | 危险标签默认跳过（`dangerous`） |
| 已知缺陷让流程整条卡死 | `ensure` op：断言 + 补点，穿过缺陷继续走 |

## 许可

MIT。见 [LICENSE](LICENSE)。

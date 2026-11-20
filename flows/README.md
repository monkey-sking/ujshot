# 流程样例

`ujshot run --flow <名字>` 会先看当前目录，再看这里。所以流程文件名就是「流程名」。

两条现成的：

| 文件 | 用途 |
|---|---|
| `smoke.json` | 最小冒烟：Tab 能切、模块能进。换了新构建先跑这个 |
| `currency.json` | 文案断言样例：中文界面不该出现 ISO 代码 |

`currency.json` 演示 `expectAbsent`：断言中文界面里不出现 `CNY` / `USD` / `HKD`。
里面的 `tap` 目标（「币种」）是示例名字，换项目要改成自己的入口文案。

两条都是**模板**，不是给某个 App 定制的；跑之前先按自己的界面改 `expect` / `label`。

```json
{ "op": "shot", "id": "03-currency", "expect": ["设置"], "expectAbsent": ["CNY", "USD"] }
```

加新流程直接复制一个改，或者：

```bash
ujshot init --name my-flow
```

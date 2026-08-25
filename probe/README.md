# 探针（Probe）

在写阶段 0 之前，先花最小成本回答两个可能否决 World 选型的问题。
探针的产物同时是阶段 0 的回放 fixture（见 [../docs/03-设计方案.md](../docs/03-设计方案.md) §6）。

| 问题 | 由谁回答 |
|---|---|
| 这个世界够不够有心跳？ | `collect.py` |
| 面对一天上百条变化，能不能只挑出该理会的那几条？ | `judge.py` |

## 用法

```bash
uv venv --python 3.12
uv pip install --python .venv/bin/python pyyaml jsonschema

.venv/bin/python probe/collect.py 7      # 录制 7 天信号流 → fixtures/signals.json
.venv/bin/python -u probe/judge.py       # 全量 Intake 判断 → fixtures/intake.jsonl
.venv/bin/python probe/judge.py 120      # 限量试跑，控制成本
.venv/bin/python probe/judge.py --report # 只出报告，不再调用 API

# 长跑脱离会话，避免被清掉
nohup .venv/bin/python -u probe/judge.py > fixtures/judge.log 2>&1 &
```

⚠️ **一律用 `.venv/bin/python` 显式调用，不要 `source activate` + `python`。**
本机 zsh 里 `python` 被 alias 到 `python3.12`，alias 优先级高于 activate 改的 PATH，
会静默绕过 venv 跑到全局解释器上去——全局恰好装了同名包时，这个错误不会报错。

## 文件

| 文件 | 作用 |
|---|---|
| `sources.yaml` | 关切声明 + 订阅源配置 |
| `ds.py` | DeepSeek 结构化调用封装：**校验—重试层** |
| `collect.py` | 拉取外生信号，分页、去重、按天分桶 |
| `judge.py` | 按关切声明跑 Intake 判断，实测忽略率 |

## `ds.py` 为什么存在

三条实测约束（[../docs/05-模型与运行环境.md](../docs/05-模型与运行环境.md)）：

1. 严格 JSON Schema 不可用 → schema 必须自己校验
2. thinking 模式不支持强制 `tool_choice` → 只能 `auto`，模型可能压根不调用工具
3. 推理 token 吃 `max_tokens` → 耗尽时 `content` **静默返回空，不报错**

所以每次调用都要检查：`finish_reason != "length"`、确实产生了 `tool_calls`、
`arguments` 是合法 JSON、通过 jsonschema 校验。失败把原因回灌重试，上限 3 次。

**三次失败抛 `StructuredCallFailed`，不静默兜底。** 失败率本身是实验数据：
某一步骤失败率偏高说明 prompt 或 schema 有问题，用默认值糊过去会污染 30 天结果。

## 两条设计约束在代码里的落点

- **不静默跳过失败**：`collect.py` 的 `failures` 列表会写进 fixture 并打印
- **前缀逐字节稳定**：`judge.py` 的 `system_prompt()` 不含时间戳与随机内容（缓存命中差 30 倍）
- **增量落盘 + 可续跑**：`judge.py` 每批立即追加 `intake.jsonl`，启动时跳过已判断的 id

最后一条是花钱买的教训：最初只在结束时写一次结果，后台进程被清掉后
18 批、约 720 条判断、$0.46 全丢，而每次调用都追加的 `usage.jsonl` 一条没少。
已升格为设计原则，见 [../docs/03-设计方案.md](../docs/03-设计方案.md) §3.2。

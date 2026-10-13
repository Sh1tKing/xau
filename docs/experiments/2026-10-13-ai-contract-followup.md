# 2026-10-13 15:12 AI-2 计量超预算与 15:16 AI-1 意见重复

本次仅修改黄金请求配置校验、输出说明和诊断。实际 `.env`、三盘定义、质量分值、入场硬限制、护盘和交易进程保持原状。没有请求付费 AI、连接交易终端或发送通知。

## 已同步的原始证据

两轮服务器代码签名都是 `3a0be9be65927595`，已经加载上次修复，不能再归因于旧代码。原始正文和成功 POST 对应的证据目录归档在 `tests/fixtures/ai_followup_20261013_151602.json`，没有保存密钥或私有思考正文。

| 时间 / 角色 | 实际行为 | 结论 |
| --- | --- | --- |
| 15:12:47 / AI-2 Grok 4.7 | 103.36 秒完成，`end_turn`；实发 `max_tokens=4096`、`thinking=disabled`，配置 `low` 但实际 effort 省略；报告输出 9847、正文 198 字符、1 个 thinking 块，没有思考 token 分项 | 超客户端生成预算；关闭思考和档位映射存在明确冲突，实际上游档位及预算参数语义尚未确认 |
| 15:16:02 / AI-1 Claude 主路 | 完整 BUY JSON，输出 3379，其中明确报告思考 2114、其余输出 1265；JSON Schema 形状有效 | 同一反向意见重复归属，属于语义契约错误，非截断或缺少 JSON |

AI-1 同一根 M15 `1791885600` 阴线同时出现于：

- `/quality_assessment/dimensions/structure/reservations/0`
- `/quality_assessment/dimensions/counter_evidence/observations/0`

两者都是 `cause=opposing_structure`、`event_group=null`，解析后的引用都是 `/m15_bars/31`。现有 v3 明确要求意见唯一归属，包括轻微观察，因此拒绝正确。`observations` 本身没有额外分值，“重复扣分”旧通知没有指出首次条目，容易让人误解。行情事实可出现在不同维度的 `reason/evidence_refs` 中；不能复制成两条意见。

本轮本地 45 分是自主分析的参考分，不能把它当成这次 BUY 拒绝的原因。原回复仍不产生有效质量分或订单，也不重新付费审核。

## Grok 参数与生成预算的区别

Grok 4.5/4.6/4.7 官方明确不能关闭思考，未指定 effort 默认 high。此前用 `disabled` 省略配置的 `low`，不能达到关闭 Grok 思考的目的。[xAI 思考参数说明](https://docs.x.ai/developers/model-capabilities/text/reasoning)

Grok Responses 的 `max_output_tokens` 只约束可见答案，思考和函数调用 token 不计入该上限。若中转将 Messages 转成这个接口，即使正文未超限，总生成计量仍可能超过 4096。[xAI 输出上限说明](https://docs.x.ai/developers/model-capabilities/text/best-practices)

这是可能的转换解释，不是 hapiopen 已确认采用 Responses 的证据。当前没有可靠思考分项，不能把 9847 全认作思考、确认实际使用 high，或断言中转完全漏传了上限。客户端暂停超预算路线可以避免继续付费，不能撤回这一轮费用，也不能替供应商实现思考硬预算。

## 修改后的行为

- `xauusd_fib_volume_pa_ai3.py:request_reasoning_configuration_error/call_chat` 对官方明确的三个 Grok ID 检查 `disabled/between_tools` 冲突：在行情刷新、分析检查点消费和 HTTP 前停止该路线，转已有独立备用；没有可用备用则报配置错误。不按任意别名猜能力，不自动删除用户的思考开关，也不切换协议或模型。
- 已配置的 Grok effort 被接口拒绝，或兼容缓存会省略它时，不删除档位再回落到默认 high。可用备用仍按自身参数处理。仅格式字段拒绝仍保留 effort、token 上限、逐 POST 新行情和原共享 5 次 HTTP 预算。
- 超计量和截断通知区分“客户端总生成预算”与“供应商生成参数语义”，原暂停、截断拒绝和费用保护继续执行。usage 增加 `reasoning_capability_version=ai-reasoning-capability-v1` 与预算解释字段，未知思考分项保留未知。
- `strategies/quality.py` 的生成说明要求先确定意见唯一归属，加入本次 M15 反向观察的具体例子；同源说明用于系统指令、rubric 和 Schema 描述。主脚本报错同时列出首次/重复位置与 cause。没有放松去重、改数组、改等级、补 JSON、提高质量分或付费修卡。

如果继续用 Grok 主路，用户自行设置并重启：

```dotenv
FIB_PA_AI2_THINKING=
FIB_PA_AI2_EFFORT=low
```

备用的 `THINKING` 按其实际模型能力设置，不能因为主路改空白就删除备用已有的独立 `disabled`。当前部署已有 DeepSeek 备用；保留原配置也会跳过冲突 Grok 主路并尝试该备用。`low` 仍有思考，不能承诺总生成量一定小于 4096。

## 验证与交付

新增 15 项离线回归，覆盖原始坏卡拒绝/不修改、独立编写的唯一归属样例、引用顺序与事件去重、Schema 描述、Grok 主备配置冲突、HTTP 前拦截、实际 low 字段、档位兼容缓存、格式降级保留档位及 9847 的费用保护。唯一归属新样例仍按五个 adequate 得 80 分；这是测试编写的新回复，不是旧回复修复或真实成交证明。

根目录黄金 578 项通过（64.734 秒），两个自检通过，策略 CLI 指纹仍为 `0a16c78772e79b466eceb7b150182da1c7787eb015157d893f93261e1394ae89`，回放帮助及编译通过。测试隔离状态，禁止未 mock 的 MT5/HTTP；实际配置和既有状态哈希未变化。

部署目录同样 578 项通过（54.726 秒）、两个自检通过。交付代码签名 `3b0b84a4a9ddc7e1`；122 个清单文件中 118 个与根目录源文件逐字节匹配，其余 4 个为部署启动/依赖文件。实际两个 `.env` 哈希均与修前一致。结果及逐文件 SHA 见 `server_deploy/MANIFEST.json`。

用户需更新 AI 主脚本和完整 `strategies` 包并自行重启；正在运行的旧进程不会加载本次修复。新增说明不保证供应商档位/预算映射已正确生效，也不保证模型不再产生语义错误。

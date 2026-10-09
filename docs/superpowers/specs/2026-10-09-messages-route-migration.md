# 三AI主备统一Messages

2026-10-09用户要求将本地与部署配置全部改为Messages。范围是现有三AI调用、主备配置、共用URL默认值及部署文件；不变更策略、模型、密钥、档位、超时、生成额度和交易门槛。

## 路由与契约

中转站现有Claude、Grok、GPT路线使用 `https://hapiopen.cc/v1/messages`。DeepSeek官网路线使用 `https://api.deepseek.com/anthropic/v1/messages`，继续使用原 `deepseek-flash` / `deepseek-v4-pro` 模型与官网密钥。空白备用URL继承已更新主路，完全相同的主备仍不算独立备用。

本地 `.env` 和 `server_deploy/.env` 逐项保留非URL字段。`GPTSTORE_BASE_URL`、`DEEPSEEK_BASE_URL` 与已填写的 `AI{1,2,3}[_BACKUP]_URL` 均改为Messages，未填写备用字段不补造新通道。黄金入口使用角色URL及GPTSTORE默认URL；不启动其它旧交易脚本。

`xauusd_fib_volume_pa_ai3.py` 保留按URL选择协议，默认入口改为Messages。Messages发送顶层system、user消息、max_tokens、anthropic-version及x-api-key，并保留中转站要求的Bearer认证。GPT模型在Messages上也使用max_tokens，不套用Chat的max_completion_tokens/reasoning_effort。

官方 [Anthropic兼容文档](https://api-docs.deepseek.com/zh-cn/guides/anthropic_api/) 明确DeepSeek的output_config只支持effort，不支持format。`messages_supports_schema()`仅针对官方主机与完整Messages路径关闭Schema参数，不按模型名猜测其它中转站能力；用量如实记录prompt模式，Python JSON语法、质量卡及各角色策略契约照常验证。其它路线保持优先Schema及明确400后的兼容恢复。

不删除历史Chat兼容代码；当前实际主备配置均为Messages。保留5次共享预算、读超时默认不向同通道补发、每POST刷新行情、AI-1/2共享360秒、AI-3接管220秒，以及截断/超上限/格式错误拒绝。格式错误不猜补、不付费重审，不将NO/REJECT改成交易。

## 验证范围

使用现有密钥，对6组不同URL/模型/密钥组合发送小型连通请求，全部取得HTTP200、原模型名、Messages正文、end_turn及完整JSON：

| 路线 | 模型 | 单次完成耗时 |
| --- | --- | --- |
| 中转AI-1主路 | claude-sonnet-5-5 | 2.61秒 |
| 中转AI-1备用 | gpt-6.1-sol | 40.69秒 |
| 部署中转AI-2主路 | grok-4.7 | 2.94秒 |
| 本地中转AI-2主路 | grok-4.6 | 5.31秒 |
| 官网AI-2备用 | deepseek-v4-pro | 1.88秒 |
| 官网AI-3主路 | deepseek-flash | 0.78秒 |

仅探测请求使用low档位、512 token上限；GPT第一次30秒读超时，另一次以128 token上限/90秒等待取得上述结果，首次用量未知。探测不提交交易行情或账户信息、不自动重试、不写交易状态；上述耗时不是生产配置下完整审核的时延保证。真实配置的effort、timeout和MAX_TOKENS均未改写。

新增8项协议回归，覆盖官网认证与Schema能力、GPT/Grok原生字段、主备刷新及共享预算、官网读超时不重复、坏JSON不修补、示例路由。原AI-3刷新测试固定测试入口，不再依赖私人.env的协议或假设user消息总在索引1。离线验证覆盖168项测试及两个内置自检；未连接MT5、未下单、未启动或重启交易。服务器需同步代码和私有.env后自行重启。

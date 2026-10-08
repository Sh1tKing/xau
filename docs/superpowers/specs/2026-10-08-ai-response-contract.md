# AI 双协议与 JSON 响应契约

用户要求同时修复 Claude 接入与 JSON 问题。保留本地粗筛、AI-1 自主提案、AI-2 审核、AI-3 护盘、质量卡、所有交易硬限制和实时行情刷新。用户自行将 AI 门槛暂调65验证流程；本次不改置信度、模型、思考档位、超时或密钥。

## 故障依据

10:25 请求46.42秒返回HTTP200、finish_reason=stop、output2580，原文少一个外层右括号且wait_basis嵌入quality_assessment；不是超时或被标记的token截断。另两份无有效JSON缺原始回执，不推定原因相同。18:53截图8328输出/184秒与18:50主请求高度时间匹配，但缺request_id；客户端60秒读超时，备用11.92秒完成。发送4096与账单8328不一致，无法只凭截图断定中转忽略上限或全部是思考token。

## 实现规则

1. 按每条主备URL路径选择协议，不自动按模型名改写URL。`/messages`用顶层system/user、anthropic-version、max_tokens及原生text/stop_reason/usage；其余保留Chat形状。Claude主路配置到Messages，现有GPT备用保留Chat。
2. 每角色定义完整JSON Schema，AI-1五维评分、reservation、顶层wait_basis均明确；补完整可解析模板，示例不作为市场结论。原生Messages发送output_config.format/effort，Chat发送response_format/reasoning_effort。
3. 第三方是否透传结构化输出未知。明确400拒绝可选字段才在原总请求预算内降级，兼容结果仅缓存到该URL/模型，支持的档位与结构约束尽量保留。Messages拒绝必填max_tokens只切独立备用或失败，不能取消上限。每次恢复仍调用行情构建回调。
4. 正文解析拒绝语法不完整、重复键、非有限数值、多个对象以及顶层数组，不从残缺正文中挑嵌套对象，不补括号/移动字段。语法错误附行列、顶层wait_basis嵌错明确说明；记录原文与usage诊断，不因格式错误付费重审。既有质量、证据指针及最终价格检查继续执行。
5. 识别两协议截断、拒绝及非最终结束；回执报告output超过发送上限时明确失败且不补发。检测不是服务端硬限实现，不能阻止客户端超时后上游继续计费，亦不证明第三方参数已生效。
6. usage新增protocol、response_contract_version、json_mode_sent、json_error和实际发送档位。Messages输入中的非缓存/缓存读/缓存写分别记录，仅数据完整时相加，未知保持null。

## 验证与部署

离线HTTP模拟覆盖协议切换、Schema与effort兼容、刷新与预算、必填上限、截断、超限以及历史格式失效。交易策略不改，不运行MT5回测或启动交易，不发送真实付费探测。同步部署AI脚本及README/MANIFEST；服务器只需覆盖脚本并调整AI1_URL，保留自己的.env其他值（包括临时65）和日志后自行重启。启动检查 `AI协议与JSON=response-contract-v1`。离线结果不保证服务器上游时延、格式或收益。

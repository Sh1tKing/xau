# 2026-10-13 黄金契约与输出预算复核

同步日志截至 14:06:29。这次区分了坏回复、正常观望、达到上限和供应商报告超限，不能统称“JSON错误”或“网络超时”。

## 日志证据

| 时间 | 路线与结果 | 结论 |
| --- | --- | --- |
| 10:07:52 | AI-2 主路 Grok，Messages，发送 4096、thinking=disabled、effort 省略；返回 end_turn、output_tokens=6672 | 已发送上限，但上游报告超过上限。可见正文169字符、1个思考块，未报告思考token，不能按块数或字符数推算计费组成。 |
| 10:13:37 | AI-1 备用 GPT，HTTP 200 / end_turn / Schema；adequate 的 structure 缺 reservations、observations | 完整可解析 JSON，但不符合声明的必填字段。原动作no，不能替模型补空数组后放行。 |
| 10:17:35、10:22:28、10:37:23、10:42:22 | AI-1 备用 GPT，HTTP 200 / end_turn / Schema；可见正文末尾缺闭合括号 | 不符合声明的 JSON 格式，输出1371/1153/1114/1163，均小于4096。没有证据说明由token上限截断；不能确认是模型生成还是中转转换损坏。 |
| 10:46:05 | AI-2 原超限配置被跳过，未发HTTP | 原费用隔离有效，没有再次付费请求同配置。 |
| 12:51:42 后 | 服务器加载 dae6854a989295fc；15个完整AI1结果均为watch | 这15次没有契约错误。后段明确引用EMA偏离1.51–1.66ATR、缺触发或目标空间，不应按格式修复改为入场。 |
| 14:06:28 | AI-1 主路 Claude，medium / provider_default，86.61秒，finish=max_tokens；output=4096，thinking_tokens=3623 | 达到已发送的上限，没有越过上限，也没有HTTP读超时。原归一化漏读了原始usage中的思考计量。 |

全日记录90个AI1结果：watch 68、review_pending 15、python_rejected 2、contract_invalid 5。重启前后序号会重新计数，不能只按decision_id后缀或截图排列匹配请求。

14:06 原始数字为：input_tokens=4，cache_creation_input_tokens=31441，cache_read_input_tokens=0，output_tokens=4096，output_tokens_details.thinking_tokens=3623。按Messages计量，总输入31445；报告的非思考输出差额为473。该差额来自供应商报告，不是本地对3270字符可见正文的分词结果，也不是独立获验的费用账单。没有报告思考计量的其它轮次仍记未知。

Anthropic官方说明，max_tokens约束整体输出，effort是分配思考的软指导，思考计量来自usage.output_tokens_details.thinking_tokens，不能把medium或low当作固定思考token硬预算。[思考控制与成本](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost)、[结构化输出与截断](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)。本项目仍用用户配置的中转模型别名；官方参数语义不证明中转端实现正确。

## 本次修改

运行版本标记为ai-output-reliability-v2，源码签名3a0be9be65927595。

1. AI脚本归一化Messages的思考token和具体来源。数字必须有效且非负；多个计量冲突或思考大于总输出时标记reasoning_usage_inconsistent，不生成非思考差额。缺报不猜，私有思考正文不落盘。截断通知同时显示报告的思考与非思考输出，保留拒绝及不付费重审。
2. 对本次请求实际生成的Schema做只读形状诊断，检查JSON、对象必填字段、额外字段、类型、枚举、数组和本地引用。usage新增response_shape_status、response_shape_error及schema_enforcement。Schema已发送但回复不符合时记violated；符合时仅记no_violation_observed，不能宣称上游保证执行Schema。提示词模式记not_requested，截断/网络失败记unverified。这个小检查器只覆盖项目生成的Schema子集，不替代完整JSON Schema实现。
3. quality.py在各等级Schema分支明示意见数组；adequate/weak/missing两数组不可省略，weak/missing的独立扣分意见仍至少一条。模板把wait_basis放在顶层、quality_assessment之前，并说明末尾闭合层级。entry-quality-v3版本、等级分值、观察原因、引用隔离和全部旧契约处理保持原规则。仅改变请求的格式说明，不改写历史回复。
4. AI-1新增独立FIB_PA_AI1_THINKING及FIB_PA_AI1_BACKUP_THINKING：空白保留接口默认，备用空白继承主路，disabled显式发thinking.type=disabled并省略effort。AI1还可明确选between_tools：仅Messages，保留low/medium/high或默认effort，本项目不传tools，适用于支持该模式的无前置思考请求。协议或档位不合法在POST前拒绝；400拒绝开关时只用已有独立备用或结束，不能偷偷取消/转换开关。AI2/3仍只接受原空白或disabled，不因AI1选项改变。保留每POST新报价、总共5次HTTP、相同通道读超时默认不补发和截断不重审。

形状合法仍须通过原证据隔离、质量、交易策略和Python实时风险门；新诊断本身不下单、不更改no，也不凭Schema错误触发付费重审。所有H1/SL8/RR1.5/M5极值保护/EMA1.5ATR/.01单仓、三盘定义及护盘规则未改。

## 输出预算如何调整

实际本机及部署.env保持用户原配置。新增开关写在.env.example，但不会自动关闭AI1的思考。

继续使用思考时，可以自行降低AI1档位以比较时延和决策质量；low仍可能用满4096，不能保证留出完整JSON。

原生Sonnet 5.5拒绝disabled（400），关闭前置思考应使用between_tools并保持effort不高于high；无工具时只返回文字。这与Sonnet 5等接受disabled的模型不同。[官方型号配置表](https://platform.claude.com/docs/en/build-with-claude/thinking#configuring-thinking)。不能只凭中转别名推断它遵守原生实现，不能把disabled当作所有模型通用保证。

备用空白会继承主路模式。明确关闭前置思考前，应确认主备路线都支持所选模式，或分别填写供应商明确支持的设置。只有主备均确认支持disabled时，以下配置才适用：

```dotenv
FIB_PA_AI1_THINKING=disabled
FIB_PA_AI1_BACKUP_THINKING=
```

若主路确实对应支持between_tools的Sonnet 5.5，可以把主路值明确设为between_tools；备用需按自身接口支持情况独立配置，不盲目继承。只关闭备用时主路留空、备用填写其支持的模式。关闭思考可能影响复杂方案判断，必须看新的真实回复与交易记录；本轮没有实际模型质量验证。不将原模型、档位、超时或上限改成其它值，不按模型名称推断开关是否生效。

AI2报告6672而发送4096属于供应商控制/转换/计量待核查，本地已有隔离只能阻止后续付费请求，不能撤销已发生的费用。返回的思考块不够证明具体思考token数量。不能靠再提高上限、删除上限或放行残缺JSON宣称修复这类问题。重启会按原设计清除内存中的隔离；供应商未修正同配置时，重启后仍可能再次发生一次超限。

## 离线验证与部署

- 真实失败夹具tests/fixtures/ai_response_failures_20261013.json只保存可见决策与原数字usage，不含密钥、账户、私有思考正文或完整日志。
- 新增15项回归。首批11项在旧代码上失败4项、错误11个（含子测试），2项原拒绝路径已通过；兼容计量来源检查先发现2个子测试失败，再修正来源字段名称；between_tools支持新增回归先失败1项、错误1项，完成后15项通过。
- 最终源码与部署副本各563项通过，用时64.302/64.325秒；两内置自检通过，策略CLI通过且定义指纹仍为0a16c78772e79b466eceb7b150182da1c7787eb015157d893f93261e1394ae89。
- 测试禁止未mock的HTTP/MT5调用并隔离状态文件，实际配置与既有持仓/事件状态哈希保持；未启动交易、请求真实AI或发送飞书消息。

已同步既有server_deploy源码及MANIFEST；部署副本的完整测试与最终源码提交以MANIFEST的output_reliability_revision及source_commit为准。服务器需更新xauusd_fib_volume_pa_ai3.py及完整strategies包，再由用户重启；保留实际.env、日志和状态文件。新增开关只有实际.env明确填写才改变运行行为。离线测试验证格式、诊断和保护路径，不证明供应商今后不会生成坏回复或忽略参数。

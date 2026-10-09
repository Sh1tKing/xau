# AI2 完整审核超时修复

## 已复现的问题

用户提供的部署日志覆盖到北京时间2026-10-08 11:30:40。只分析11:26修正配置后的轮次，不把早先手填模型字段错误作为本次原因。

AI1 Claude成功不代表AI2 Grok可在同样时间内完成：主路URL相同，模型、API key和等待时间不同；同轮总输入字符量仅相差0.61%。日志中主路40.5秒读超时后确实切到了DeepSeek官网，备用40.3秒读超时。

最新完整AI2输入复放包含1107字符规则、18596字符市场审核数据，未调用MT5或下单。单次本机结果：

| 模型/档位 | 完成时间 | 结果 |
| --- | --- | --- |
| Grok 4.7 / medium | 85.7秒 | 有效审核JSON，REJECT |
| DeepSeek V4 Pro / medium | 44.6秒 | 0.47秒收到200响应头，4096输出token全用于思考，length结束且最终JSON为空 |
| DeepSeek V4 Pro / low | 13.8秒 | 有效审核JSON，REJECT |

DeepSeek官方将medium映射到high，默认开启思考。只延长40秒等待仍可能收到截断回复；此前小请求连通探测没有验证完整审核的思考额度。

## 本轮实现

- AI2主路等待默认90秒，同步本地与部署.env；当前主路模型、URL、密钥及medium档位保留。
- 部署中已配置的DeepSeek备用effort改low，独立等待40秒，生成上限仍4096。
- 新增可选`FIB_PA_AI{1,2,3}_BACKUP_TIMEOUT`。正整数秒生效，未写/空白/非法/非正值继承对应主路；超时配置不参与独立备用判定。
- requests响应hook在读取完整正文之前保存HTTP状态、请求ID和响应头耗时。正文读超时仍记录已收到的响应头事实，usage未知保持null。
- 用量新增read_timeout_sec、headers_elapsed_sec、body_complete；启动日志显示主备等待秒数，成本控制版本cost-latency-v2。

本地没有独立AI2备用，保持备用URL/密钥/模型/effort空白；只预留备用等待40秒，不能因此产生一个新的模型通道。生产.env只改上述指定字段，保留其他字段字节与账号/密钥配置。

## 保留的约束

AI1自主提案、AI2独立审核最终Python价格、AI3护盘及本地策略都保留。AI1/AI2共享360秒过期检查、HTTP总预算5、读超时默认不重复补发均保留。正常REJECT、截断或过期结果不触发换模型重审，不能因为修好通讯就强行开仓。

90秒是单次读取等待，requests标量timeout同时用于连接等待，并非整个分析任务的总期限。该角色备用可独立40秒，无需把全部模型都延到90秒。请求返回时仍检查任务是否有效。

## 验证

`tests/test_ai_cost_control.py`增加主备90/40切换、空/非法备用等待继承、只有等待项不创建备用以及HTTP200响应头后正文超时的回归。原实现对主备时间及响应头保留两项回归失败，修复实现通过。71项离线测试和2项内置自检通过，现有交易日志与状态哈希保持不变。修复后的call_chat直接调用DeepSeek备用，约19.8秒返回完整REJECT；Grok主路90秒单次复核仍超时，不能声称已消除主路服务抖动。另以真实自动链路验证：主路90.6秒超时后切备用，备用17.6秒返回完整REJECT，整轮110.2秒；主备事件具有同一call_id，分别记录90/40秒等待及备用HTTP200/响应头0.19秒/正文完整。数据保存在`xau_fib_pa_logs/ai2_fix_chain_verification_20261008.json`，不执行该历史审核的订单。

真实完整快照验证仅用诊断副本，主路90秒、备用low/40秒各实际POST一次，不下单、不写交易用量或状态、不启动或重启交易进程。单次本机成功不保证服务器时延分布、交易收益或该历史提案可开仓。用户需要自行重启服务器进程加载更新。

## 官方依据

- [DeepSeek思考模式与档位映射](https://api-docs.deepseek.com/guides/thinking_mode/)
- [Requests读取等待](https://requests.readthedocs.io/en/latest/user/advanced/#timeouts)
- [Requests响应正文读取异常转换](https://requests.readthedocs.io/en/latest/_modules/requests/models/)

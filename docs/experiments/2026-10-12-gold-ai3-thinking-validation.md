# 黄金AI-3思考开关与输出诊断

2026-10-12，按用户“相关问题xauusd那边应该也有，一并更改”同步币种版已完成的请求层修复。黄金AI-3此前只发送effort档位，没有显式关闭扩展思考的配置；截断通知也未显示实际发送参数和可见输出形状。`low`不作为关闭思考的判据；这次增加可选开关及可核查诊断，不改变黄金交易规则。

## 行为

`xauusd_fib_volume_pa_ai3.py`新增`xau-ai3-thinking-v1`，配置和规则如下：

| 配置 | 行为 |
| --- | --- |
| `FIB_PA_AI3_THINKING`空白/未配置 | 保留供应商默认及原effort兼容路径 |
| `FIB_PA_AI3_THINKING=disabled` | AI-3发送顶层`thinking={"type":"disabled"}`，省略effort，仍保留输出上限及JSON契约 |
| `FIB_PA_AI3_BACKUP_THINKING`空白 | 继承主路开关，不创建新备用通道 |
| 仅备用设置`disabled` | 主路行为不变，切至已有独立备用后使用关闭开关 |
| `low`/`enabled`/无效值作为开关 | 在POST前拒绝配置；effort字段与此开关分开 |
| HTTP400明确不支持thinking | 只切已有独立备用或结束；不删除开关重新付费 |

按URL决定Messages/Chat；未自动迁移协议或更换供应商。关闭思考后其它中转仍保留Schema格式，官方DeepSeek路线仍用提示词JSON及严格Python校验，不发送不支持的format。格式兼容恢复不取消开关或输出上限。主备和兼容请求继续共用5次预算，超时策略、每POST刷新行情、任务过期检查、截断/正常持有不重审保持。

请求准备日志及`ai_usage`记录配置effort与实际发送effort、thinking开关、输出上限和行情刷新耗时。响应仅统计可见正文字符数及thinking/redacted_thinking块数量，不保存私有思考正文。缺失reasoning用量保持未知。输入量不占输出上限；一次请求的总量、输出与可见JSON长度也不能相互替代。详细截断错误给出实际cap、requested/sent effort、thinking、供应商报告输出、正文字符与思考块数，本地护盘继续。

协议依据：[DeepSeek思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)、[Anthropic兼容接口](https://api-docs.deepseek.com/zh-cn/guides/anthropic_api/)、[Chat输出参数](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/)。其它中转需自行核对实际参数支持；本次没有付费请求证明供应商已执行开关，也不承诺不会再截断或超时。

## 验证

先加入11项来自币种修复的离线回归：旧黄金代码出现5个失败、4个错误，证实显式开关缺失及截断诊断不足。修复后加入2项黄金边界回归，合计13项新增测试。

- 黄金完整`tests/`：416项通过（44.585秒），无跳过；涵盖原入场质量、AI-2审核、AI-3冻结契约、策略重载及成交风险门。
- 两个主脚本`--self-test`、`python -m strategies.check`和回放`--help`通过；使用NullHandler隔离导入日志，不启动交易。
- 思考开关回归覆盖Messages/Chat、主路/备用、默认保留、拒绝参数、格式兼容、非法配置、截断立即停止及AI-1/2不受影响。
- 4072输出截断模拟保留实际4072上限/low档位，识别16字符未完整正文及1个思考块，只发1次请求；没有把上限提高到无边界或强行修补JSON。
- 缺失/异常Messages内容形状安全返回空正文；隐藏与redacted思考只计数量，不写其原文。单独备用思考设置不会生成新的服务路线。
- `server_deploy`副本的42项成本/传输测试、23项协议/JSON测试、两自检、策略CLI和回放help全部通过；部署策略指纹与根目录一致，为`0a16c78772e79b466eceb7b150182da1c7787eb015157d893f93261e1394ae89`。黄金复制清单共91个文件，逐项字节SHA256核对。
- 545个已存在的配置/日志/状态/缓存及币种副本文件在测试和部署同步前后哈希一致；币种源码与私人配置没有被黄金同步覆盖。

所有HTTP响应和交易数据均为离线模拟。黄金策略及本地交易/回放代码、质量门槛、H1/SL8/RR1.5/M5保护/追价/单仓等规则未改，不因此重新解释历史回测收益。

## 部署与配置

根目录和`server_deploy/.env`现有AI-3为`medium`/2048，未配置思考开关；本次保留实际配置，不声称已关闭。需要启用时按[部署说明](../deployment/README.md)设置运行目录的开关，并由用户重启。币种`crypto_demo/.env`与币种源码保持原样。

发现`server_deploy`包含旧币种README/AGENTS/manifest及被币种版本覆盖的共享策略文件。本次直接同步黄金脚本、黄金策略/测试和部署说明，黄金入口及复制清单以新`MANIFEST.json`为准；遗留币种文件不作为黄金发布内容。私人`.env`、日志、事件状态、AI3上下文和缓存不覆盖。根目录本地master提交，本轮未推送远程，也未启动模拟或实盘进程。

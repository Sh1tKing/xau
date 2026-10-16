# AI-1 固定五项归属与观望依赖引用

用户授权：2026-10-17「已同步日志，又有契约错误」「修复」。本轮基线本地master `b4ae4c7`，交易源码仍对应`9c945de`及签名`6cb0c8c06dbf87e0`。

## 实际失败

| 北京时间 | decision | 供应商路线 | 完整回复中的问题 |
|---|---|---|---|
| 02:02:30 | 1792184400-32 | 主路Claude读超时后，备用GPT成功返回 | 原动作buy；量能意见cause=volume正确，但dimension=volume错误，固定五项没有这个维度 |
| 03:40:26 | 1792190400-52 | 主路Claude | 原动作no；保护阻断，target.reason说未评估，target.evidence_refs却为空 |

两轮都为`end_turn`，用量记录`schema_enforcement=violated`。请求Schema原本已经限定五个dimension及引用`minItems=1`，不是漏发这两个限制。Python原样拒绝正确；这次不能保证模型或中转以后永不违反Schema，不能靠客户端推断供应商严格执行。

真实成功POST的压缩行情、原引用目录和可见正文分别保存为`tests/fixtures/ai1_quality_20261017_020230.json`、`ai1_quality_20261017_034026.json`。前者使用02:02:07备用刷新输入，不借用02:00的超时快照；后者保留原Markdown围栏文本并通过既有JSON解析规则读取。无密钥、认证头、宿主证明或私有思考。

## 修复内容

保持`entry-quality-v5`及`ai1-bound-severity-v1`的字段和20/16/8/0评分语义，增加生成说明版本`ai1-quality-shape-v1`。统一Schema描述、系统指令、rubric和模板：

- dimension只可选structure、trigger、protection、target、counter_evidence，明确volume是cause。归属仍由真实事实决定，不把所有量能意见强制搬到counter_evidence。
- buy/sell/no均填写五项，每项、每条意见及等待条件的引用非空。保护先阻断时，target可明确“依赖保护、尚未评估”，引用实际报价/保护/风险限额作为依赖证据，保持no，不伪造目标、RR或另一条重复阻断。
- 当前rubric的反证维度说明不再混入旧版“放observations/reservations、评weak”的生成指令，统一使用issues/kind/severity，等级仍由Python生成。
- 每次实际POST从本次真实目录生成格式示例，量能引用只用自己的候选组，保护依赖使用BUY Ask或SELL Bid及本候选保护事实/公共风险限额。示例仅帮助表达，不能当成行情意见或下单依据；缺失的量能不拿其他候选补齐。
- 动态短ID示例放请求末尾`quality_reference_examples`，不插进稳定rubric缓存前缀。`citations.py`将该格式元数据和原rubric/目录一样排除于来源hash，防止短ID与scope循环依赖；它不是允许的证据根。真实报价变化仍会使旧目录失效。
- 意见归属错误诊断同时给出实际dimension/kind和固定五项，便于区分错误字段及原因；不改坏回复本身。

原两张坏卡仍拒绝且无质量分，不补引用、不推断或改写归属、不付费重审。独立写出的合法观望卡仍no；同样的blocking若被改为交易仍拒绝。本轮没有把03:40的保护超限观望转为开仓，也没有实施前一轮H1复盘中的候选设计。

AI2/AI3及HTTP配置、思考档位/上限/超时/门槛不变；原双向方向许可、真实报价、SL8/RR1.5、最近三M5保护、新闻休息、离散profit-floor-v3阶梯和存量仓冻结规则不变。实际.env、日志、状态、crypto和local_guard_demo不改。

## 验证

11项新离线回归覆盖真实主备坏卡、准确错误诊断、Schema/模板/rubric一致、当前ID及候选隔离、缺量不补、刷新不复用旧ID、BUY/SELL镜像、元数据hash隔离、真实报价篡改仍拒绝、合法volume意见与阻断观望。首次在原实现上10项有2失败/7错误；实现后通过。新增缓存前缀位置/来源hash回归在修改前再次失败，补齐后11项通过。旧评分卡及新合法卡均保持原声明不变。

本轮新验证结果：

| 检查 | 根目录 | server_deploy |
|---|---:|---:|
| 完整离线回归 | 919通过，0失败/错误，182.689秒 | 919通过，0失败/错误，184.224秒 |
| 两入口自检 | 2通过 | 2通过 |
| 策略CLI | 通过，三盘仍v3/原指纹 | 通过，同指纹 |
| 声明Python文件语法 | 99通过 | 99通过 |

完整命令为`python -X utf8 docs/experiments/verify_gold_profit_guard.py --verbosity 1`，部署增加`--root server_deploy`。所有验证禁止真实HTTP/socket/MT5/订单/飞书，并隔离日志/GuardStore。全量过程中出现一个原本不存在的0字节日期日志，确认无内容且不在原文件清单后移除，未删改任何原有日志；最终613个原有配置、日志、状态及独立样本文件的SHA全部一致。没有启动交易进程。

AST比对确认trader、backtest、profit_guard、guardian_risk、guardian_review、direction、lifecycle、news_calendar及三盘定义共11个模块完全不变。AI脚本仅有请求格式示例/准备、rubric和模板四个函数变化，交易执行、质量归一化、HTTP/模型重试与AI3策略函数不变。新评分解码只补准确归属诊断，不自动修卡或改变评分。

两张实际POST重新生成目录后，所有原事实和候选不变、引用scope及ID与原目录完全相同；请求JSON各增加1246字符，其中动态示例477字符。这是字符度量，不能等同于token或费用，也不能声称此次降低了单次输入成本；未付费请求模型测量遵约率。动态短ID在目录之后，不提前破坏稳定rubric前缀。交付204个声明文件，代码签名`ab05cc1ca77f75fa`，最终来源提交和逐字节SHA以MANIFEST为准。

部署交付为本地`server_deploy`的三个黄金脚本和完整strategies；用户保留服务器.env及全部状态文件，覆盖源码后自行重启单实例。本轮只提交本地master，不推送远程。

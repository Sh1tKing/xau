# 黄金AI-1评分契约v3修复

## 问题及原始证据

16:23:12、decision `1791804000-34` 的原始回复完整，但 `structure.level=weak` 与 `reservation.cause=shallow_retrace` 冲突；理由是“价格位于区间低位且远离EMA21”。浅回撤只能是轻微意见，远离EMA应按执行限制分析。该轮Claude主路120秒读超时，备用GPT完整返回，错误来自备用回复。原Schema将level和cause独立枚举，允许Python随后拒绝的组合。

测试夹具 `tests/fixtures/ai1_quality_20261012_162312.json` 保存精确的成功POST紧凑行情、证据目录和原始回复，只含市场及响应事实，不含账户或密钥。目录与归档逐项一致。旧坏卡仍拒绝，不补写原因、升级等级或复活历史订单。

## 行为变化

`strategies/quality.py` 是版本、五项分值、原因分类及等级关联的共同来源，供AI-1提示词、rubric、响应Schema和Python校验使用。版本升级 `entry-quality-v3`，仍为strong20/adequate16/weak8/missing0，不是胜率。

- strong只填level/reason/evidence_refs，不含意见数组。
- adequate/weak/missing另外填reservations和observations；weak/missing必须有至少一条对应扣分理由。程度仅由维度level决定，不恢复v1重复填写ID、归属及severity的方式。
- 普通量与浅回撤仅进入observations，无额外扣分、不能独立支撑weak/missing或重大反证观望。真实重大结构问题与轻微观察可以同时存在，维度仍保留其weak等级及8分，不自动抬分。
- 强制所有观察绑定实际成功POST证据；去重包含扣分理由及观察，跨候选、旧快照ID和不存在路径继续拒绝。硬限制、缺数据等不能藏在observations中。
- 原因说明明确浅回撤不包括深度下行或远离EMA；M15暂反向不自动等于硬伤。事实与分类语义仍需要模型正确分析，客户端不按关键词改写原因。
- 有效v1/v2卡按原版本严格处理，不转换成v3；v1也不能把普通量/浅回撤声明material/blocking。错误消息显示维度路径、实际cause和level，通知追加评分版本。
- 契约失败仍为contract_invalid、无质量分、不审核或下单，不增加付费重审。可以独立记录已确认的H1方向或当前EMA距离，不能拿风险诊断替代评分。
- 日志、持仓保存及重启恢复保留原始观察和入场评分版本，AI-2/AI-3分工及已开仓策略契约不变。加载代码签名包含新quality模块。

Schema以一个 `$defs` 复用嵌套anyOf分支，避免if/then、数组maxItems等兼容性问题；强等级直接省略意见字段，不用复杂空数组枚举。[Anthropic结构化输出文档](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)列明anyOf及minItems 0/1等子集限制。这里只验证Schema和模拟协议链路，中转忽略或降级Schema时Python仍严格验证，未发送付费请求证明供应商支持。

## 验证

`tests/test_ai_quality_contract_v3.py` 覆盖真实错误、所有等级/原因/数组组合的Schema与Python一致性、重大与轻微混写、BUY/SELL、旧版兼容、证据隔离、独立风险诊断，以及三盘双向实际AI-1→AI-2→模拟执行与持仓落盘/恢复。主备超时后用成功请求目录；坏卡只有一次请求、不进入AI-2。全部HTTP及MT5订单调用为mock。

将Schema临时退回独立枚举并去掉最低扣分理由条数，针对性测试明确失败；恢复新Schema后通过。测试脚本不修改生产文件。可选离线测试依赖在 `docs/deployment/requirements-test.txt`，运行交易不依赖jsonschema。

- 新增19项回归通过，包含72组等级/原因/数组组合，以及三盘双向与主备切换的完整实际代码链路。
- 根目录完整黄金回归466项通过（49.695秒）；部署按MANIFEST列出的28个黄金测试模块独立加载，466项通过（52.328秒）。
- 根目录和部署的两个主脚本自检及策略CLI均通过。三盘启用项与指纹 `0a16c78772e79b466eceb7b150182da1c7787eb015157d893f93261e1394ae89` 不变。
- 根目录及部署实际.env的SHA256前后一致；部署102个清单文件的精确字节SHA256均匹配。
- 原始报错轮的v2卡仍拒绝，并明确定位structure/reservations/0、shallow_retrace、weak；同样错误组合在v3 Schema也拒绝。独立诊断得到实时请求报价偏离2.35ATR，硬门保留。
- 加载源码签名 `341dc430f017a660`，评分版本 `entry-quality-v3`；未调用付费模型、连接交易终端或启动交易。工程验证不证明实际供应商永不产生错误、成交频率或收益。

## 部署及限制

直接修改根目录原文件并同步既有 `server_deploy/`，必须更新AI脚本与完整strategies包（新增quality.py）。保留服务器自己的.env、日志及状态，由用户自行重启；启动评分版本应为entry-quality-v3，具体源码签名和文件哈希见MANIFEST.json。

本轮未改变实际.env、门槛、模型、档位、超时、本地策略、H1、SL8、RR1.5、M5保护及EMA1.5ATR。原报错轮实时BUY Ask距EMA约2.35ATR仍超1.5，修复协议不能使该轮变成合法订单。币种与独立本地样本未修改，也未启动交易。

# 黄金AI-1证据引用契约修复

2026-10-12，依据已同步的黄金 `ai_decisions/2026-10-12.jsonl` 与运行日志排查。46次AI-1返回中，30次为 `contract_invalid`、15次正常观望、1次待AI-2审核。不能把30次格式失败解释为策略主动观望；这次修复请求/响应契约，未修改策略、阈值或成交条件。

## 原因与取舍

10:30:58引用的 `/opportunities/0/strategy_evidence/buy/evidence/bar_behavior/volume` 不存在。真实 `volume` 与 `bar_behavior` 同级。其它错误包括漏写 `/evidence` 的 `position`/`recent_extremes`，以及将 `target_max_reference_r` 写在候选根对象而非候选 `strategy_contract` 中。

客户端也存在两项问题：静态rubric仍展示候选池压缩后已删除的 `/local_plan_feasibility` 与赢家 `computed_evidence` 别名；候选隔离白名单遗漏实际共享字段 `/m15_ema21_distance_atr` 和公共风险规则。仅在Python发现错误后猜测改写Pointer，会将真实跨候选错误和拼写错误混在一起，违背原证据隔离契约。本次不采用这种修补方式。

## 新行为

- `strategies/citations.py` 在压缩后的真实输入上生成 `evidence_catalog`。每组声明路径前缀、ID到后缀的映射及候选ID/方向归属；短ID绑定输入摘要。共享组只提供实际行情与明确公共风险字段，目标参数使用各自候选契约。
- AI-1复制目录ID，避免自行拼接深层路径。Python验证目录与快照一致，再按明确声明映射成原Pointer，继续验证路径存在、候选/方向隔离及完整质量卡。`wait_basis`、维度和保留意见均用同一目录。
- 每次实际POST均同时刷新行情、目录、角色提示词与动态Schema。兼容重发、主备切换都使用新目录，回复只能按成功POST的目录解码；AI-2保留原候选稀疏索引、固定SL/TP和实时报价审核。
- Schema枚举实际候选ID和证据ID，以 `$defs/$ref` 复用维度及引用声明，避免在各维度重复列出整份目录。格式兼容降级仍发送目录和提示词，Python严格校验不降级。
- 不恢复赢家别名，不猜测修复历史坏路径，不重审正常观望、错误契约或截断输出。有效旧Pointer保持原语义，非法转义、缺失路径、空引用数组、旧快照ID、跨候选及隐藏宿主字段继续拒绝。字段层级错误保留明确诊断。
- `ai1_input`记录 `citation_version` 和精确目录，原始完整行情记录仍保留；启动日志显示 `AI-1证据引用=ai1-citations-v1`。

[Anthropic结构化输出文档](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)及[OpenAI结构化输出文档](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat)说明本地Schema引用的用法。本次验证了标准Schema语法，未发送真实请求证明中转的实际支持情况；原格式兼容与严格本地校验继续适用。

## 验证结果

新增 `tests/test_ai_citations.py` 11项回归，先在原实现上观察到共享字段误拒绝、非法转义以及缺失目录等失败，再修复。覆盖用户报错的同级volume、position、目标契约，NO与保留意见评分，未知/旧快照/跨候选ID，目录篡改，400格式兼容刷新，以及实际Messages请求→ID归一化→AI-2审核→模拟执行调用的完整路径。所有HTTP和交易调用均为mock。

- 黄金完整 `python -m unittest discover -s tests -p 'test_*.py'`：**427项通过，46.935秒**。
- 独立JSON Schema校验：动态Schema合法，完整有效卡通过，未知ID被枚举拒绝。
- 根目录及部署副本的两个主脚本自检、策略CLI通过，策略指纹仍为 `0a16c78772e79b466eceb7b150182da1c7787eb015157d893f93261e1394ae89`。仅运行离线入口，未连接MT5、启动交易或调用付费模型。
- 部署按 `MANIFEST.json` 的黄金tests清单加载：**427项通过，42.962秒**。对整个混合目录进行普通discovery时另有3个旧币种测试因缺少 `crypto_demo` 包而导入失败；这些旧文件不在黄金复制/测试清单内，原地保留，未称整份混合目录测试通过。
- 开工保存的76个私有配置/策略定义/币种及独立样本源码，与部署20个既有日志/状态文件，修改、测试和同步前后字节哈希一致。
- 按每个结果之前同decision_id的最后请求快照离线复核46条历史原始回复：契约错误从30降为19，解除11次共享事实误拒绝；剩余5次路径不存在、13次证据隔离错误、1次保留意见字段错误继续拒绝。没有自动改写旧错误，所有原动作保持不变。此复核未重新请求AI或执行历史订单。
- 目录为137–153个ID；原始已收盘K线和候选事实不删减。最近8根各周期K线提供单根目录，较早K线仍在输入中，可用整个数组证据ID引用。
- 历史46份请求实际紧凑序列化：正文字符平均31120→37676（+21.31%）；包含系统提示词和Schema的字符量平均增加17.06%。原Schema5425字符，新Schema5120–5420字符。字符量不等于token或美元成本，不能宣称成本不变；本次没有更换模型/档位、提高输出上限或增加重审。

本轮不会改变AI-1原有观望结论，也不保证模型从此绝无契约失败、一定开仓或收益改善。真实部署效果需看重启后的新日志，重点区分 `contract_invalid`、`watch` 和Python风控拒绝。

## 部署

直接修改本地原文件，并同步既有 `server_deploy/`，无额外修复包。必须同步AI脚本及完整 `strategies/`（包含新增 `citations.py`），服务器进程由用户自行重启。复制清单、提交与逐文件哈希见 `server_deploy/MANIFEST.json`；运行目录自己的 `.env`、日志、ledger和AI-3持仓状态保留。币种与独立本地样本不在本次修改范围内。

# MT5 作为期权方向研究唯一默认行情源

用户更正：“不需要通过币安请求分析，还是使用mt5来获取k线”。此决定覆盖同日期权初版对 Binance 指数/K线/期权链的运行依赖。用户仍手动交易，不接交易 API；不改黄金、crypto、local 交易系统或部署。

## 数据与边界

新增 `options_analysis/mt5_market.py`。CLI 扫描和行情诊断只构造 MT5Market，不构造 BinanceMarket，没有自动回退。只连接已运行的 Windows terminal64.exe，多个终端要求专用路径；连接前再次核对运行清单。不登录、下单、读取持仓或修改交易开关，只读取行情及 server/company 来源命名空间，不保存登录号、余额或完整 account_info。

专用配置明确指定 BTC/ETH/XAU 品种、原始时间偏移和初始化超时。价位基于 MT5 Bid，保留 Ask 和 tick 时间；Binance 指数基差未知。元数据基币须对应 BTC/XBT、ETH、XAU/GOLD 且利润货币 USD/USDT，不猜后缀或代币替代品。

读取原生 30m/1h/2h/4h/6h/12h/1d，shift=1、每周期180根、至少100根预热，剔除换算后未闭合柱。原始时间减配置偏移，fresh tick 和短周期共同检查。H4/D1 保留经纪商原生相位，历史缺口记录、不填补、不猜原因，短周期最近三根须连续；高周期背景最多七天内，短周期须最近闭合。MACD/MA/ATR/支点公式保持，tick_volume 只代表报价次数，real_volume零不等于真实零成交。短 ID 目录包含模式、来源、tick 与期权数据缺失事实，压缩输入无损保留 tick/real量。

快照 analysis_mode=underlying_research，source_identity/source_id 与全事实摘要共同绑定。source_id 包含终端路径摘要、服务器/公司、精确品种及元数据、时间偏移。采集结束、AI2前后与通知前核对来源及新报价，不套用旧服务器或 Binance 价位。

## 分析

保留模型连接、超时、主备策略、每天240 POST、15分钟扫描、严格 JSON/scope/引用及飞书持久去重。没有期权链时主分析可给 CONSIDER 标的计划，Python 核对方向、当前 Bid 触发、失效、目标、新闻和行情。独立 AI2 审核同一固定计划，APPROVE 仅输出 research，不生成期权候选或调用 Black-Scholes。

审核前后 Bid 均须在原触发范围、相对原快照不超过0.25原30m ATR。卡片最多600秒，不跨下一原生30m闭合或新闻边界。通知列出 MT5 来源、时间、Bid/Ask、主周期、依据、反证、触发/失效/目标/兑现时间和风险；明确实际期权、到期、权利金、IV、Greeks、成本/盈亏和可交易性未知，由用户在币安核对。

原 BinanceMarket、定价模块与测试保留历史兼容，当前命令没有选择路径，不作为 MT5 故障兜底。新闻仍复用纯公共组件及独立缓存。无需 MT5 交易账户凭据。

## 人工持仓

研究卡录入成交须用户显式提供 symbol/side/strike/expiry/unit/quantity/premium/fee/time，绑定通过复核的研究编号及原 source_id，元数据标 user_entered_not_exchange_verified，不冒充交易所数量/单位验证。side 须匹配原研究，实际结算仅用录入的价格、数量、乘数和两边费用。

AI3 只依据冻结计划、同来源 MT5 结构和用户到期资料，期权现值/浮盈未知，不自动平仓。旧无 source_id 或来源不同的仓不迁移价位，只提示核对，不阻塞新研究；到期提醒可在行情失败时独立运行。

## 验证

离线 mock MT5 原生数据、完整主分析/独立审核 HTTP 契约、通知、人工持仓、来源变化、缺口、偏移及前后报价；无终端先于 initialize/AI/飞书诊断失败。保留原52项回归。本机没有运行 MT5，不能宣称真实行情已经验证或持续扫描已启动。

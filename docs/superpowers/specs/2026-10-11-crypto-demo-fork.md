# BTC / ETH 三AI模拟副本

用户要求先学习 BTC、ETH 日内打法，再复制现有框架、尝试并推送另一分支。本轮按当前 MT5 场景实现独立 CFD DEMO 副本；不接交易所账户，不启动交易，不证明盈利。黄金运行代码、配置、策略、部署与状态不改。

## 研究与策略取舍

BTC / ETH 原生市场连续交易，但波动与流动性有时段差异，经纪商 CFD 可能有自己的休市。周末不固定拒绝，只看真实新 tick、完整闭合 K 线与价差。保留 H1 方向许可、M15 结构及风险、M5 进场事实。AI-1 自主选趋势回撤、压缩后突破回踩、扫回确认或其他有真实收盘证据的日内方案；菜单不是固定检测器门槛。无需本地先达到分数或固定形态。H4 背景，不恢复 D1/H4 硬方向门。ETH 不能仅因为 BTC 上涨而开多；没有 BTC 同期数据时联动未知。

第一版每次选择 BTC 或 ETH，一个账户只允许一个副本运行、最多一个账户持仓（包括手动/黄金仓）。不做双币同时持仓、补仓、网格、止损反手，也不依赖几十秒延迟做秒级剥头皮。建议与黄金使用不同模拟账户/终端，黄金进程不受副本锁约束。BTC 参考 3R / 90 分钟，ETH 参考 2.5R / 60 分钟；冷却均 60 秒，参数来源为所选定义，持仓冻结，不能在公共提示词写死。

来源：
- Kraken 日内交易、24/7、费用与滑点：https://www.kraken.com/learn/what-is-crypto-day-trading
- BTC/ETH 波动与流动性的周期性研究：https://arxiv.org/abs/2109.12142
- Coinbase 相关性历史研究（非当前相关系数）：https://www.coinbase.com/institutional/research-insights/research/monthly-outlook/monthly-outlook-june-2024
- Binance 对收盘突破、假突破和回踩的解释：https://www.binance.bh/en/academy/glossary/breakout
- MT5 合约规格：https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py
- MT5 账户货币损益：https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcprofit_py

以上支持研究思路与执行要求，不证明任何本项目参数的优势。

## Global Constraints

- 独立副本全部位于 crypto_demo/；用户明确要求复制代码。复制 7f3df37 的已验证黄金三AI主脚本及 strategies 到副本后适配，记录来源；不运行/导入根目录黄金主脚本，不读根目录 .env。
- 发布分支 codex/crypto-demo，以干净 codex/publish-master 的 00376b31e5b1c0d6b6ac02a408174a276df30898 为父系；绝不推送本地 master 的旧历史。只公开代码/示例/文档/测试，不提交真实 .env、日志、状态、缓存或 server_deploy。
- 保留 AI-1 自主提案、AI-2 固定 SL/TP 对新报价审核、AI-3 原持仓契约护盘、严格 JSON/质量卡、定义指纹重载、逐 POST 刷新、五次共用 HTTP 预算、读超时不重复补发、检查点去重与预提交 token，不把 no/REJECT 改交易。
- 默认观察模式关闭 AI 请求与下单；显式 dry-run 可请求 AI 但不能下单；显式 demo 才可模拟下单。交易和持仓修改/关闭均强制 DEMO，未知账户拒绝。任何模式不自动启动本轮交易。
- 每次启动选择 BTC 或 ETH，精确配置其 MT5 symbol（可有后缀），不能猜合约。进程持有同账户跨副本的 OS 文件锁，崩溃自动释放，实例冲突退出；账号/服务器绑定日志状态防止混用。所有新增入场检查账户全部持仓，未知/已有持仓在 HTTP 前停止；持仓护盘只操作自己 magic/symbol 的票。
- 每 POST / 送单 / 改止损 / 平仓检查账户 login/server 仍与锁和配置一致；切换到另一个 DEMO 账户也必须停止，不能沿用旧账户状态。observe/dry-run 禁止开仓、平仓和改止损。
- 实际送单前先原子保存全账户待确认 token/请求；None、超时、部分成交或未知回执不能因为重启换币而绕过原待确认状态。只凭精确 token/symbol/magic/账户的持仓或成交/明确拒单证据解决，不猜最新票；无法确定时两币均停止新付费入场与送单，持仓护盘继续。
- 账户仅 USD；风险预算为 min(8 USD, 8% * min(实际正净值, 100 USD))，不按上涨余额无限放大。按 order_calc_profit 当前合约、volume_min/max/step 向下取整手数，手续费配置与不利滑点预留计入预算；最小手数也超预算则拒绝，禁止裁短结构止损。无效/非有限/缺失的报价、规格、损益、保证金全部拒绝；下单前用 order_calc_margin + order_check 验证。
- 手数同时受风险和可用保证金90%限制；风险最大手数超保证金但较小合法手数可行时应缩小手数，只在最小手数也放不下或估算不可信时拒绝。下单前精确复算所选手数，不为凑保证金裁 SL。
- 价格距离上限不是黄金的 8 美元：BTC 为 min(2.5 * M15 ATR, 参考价 * 1.5%)，ETH 为 min(2.5 * M15 ATR, 参考价 * 2%)；至少 0.6 ATR 生成宽度、保护近三根 M5 极值，缓冲 max(0.15 ATR, 2 * tick_size)，不得硬缩到上限。实时 RR >= 1.5，EMA21 距离 <= 1.5 ATR，止损触发侧余量 >= 2 * buffer，价差 <= 0.25 M15 ATR。价格按照 broker tick_size 安全量化，不能写死两位小数；合法 lot 不会上取到超预算。
- 同一已定稿 SL/TP 固定到 AI-2 与提交；每次请求和提交重新取真实行情，动态风控上限/合约规格有变化可以拒绝，不能偷偷移动 SL/TP。原持仓期限/打法保存，定义重载不改变旧仓期限。
- 副本 CRYPTO_SERVER_UTC_OFFSET_HOURS 显式声明原始 tick/K线时钟的UTC偏移（默认0，按经纪商实际配置）；真实 quote 的 tick_time-offset*3600 必须接近当前UTC。不能四舍五入 tick 年龄推断偏移，否则整小时旧报价会被误认成新报价；旧价拒绝、配置+3小时真实新价可验证。
- 已配置的往返手续费/不利滑点计入现金预算及净收益风险比；估计净 RR < 1.5 时拒绝。未取得经纪商费用不能称零费用或将交易所费用说成当前 CFD 费用，快照说明成本假设。
- MT5 tick_volume 只标经纪商代理，资金费率、OI、交易所成交量、订单簿、清算及新闻无数据时未知，不能借现货数据冒充 CFD 可执行报价。
- 离线验证只用 mock 合约/AI 或公开行情，不登录 MT5、不发付费 AI、不下单。明确区分纯本地策略、代码通路、真实 AI 结果。部署使用完整独立目录，由用户自行配置并启动。

## Task 1: 完整副本及币种适配

工作区 C:/Users/10057/.codex/worktrees/crypto-demo/MYQ，仅写 crypto_demo/ 及本任务私有报告。控制器已准备从黄金复制的骨架。实现者可改 crypto_demo 内复制脚本/策略辅助函数，组织新增小模块，不改根目录黄金内容。公共 import 必须隔离，建议副本作为独立脚本目录运行、测试在子进程中运行；不能复用 sys.modules 里根目录 strategies。

新增 run.py 为唯一操作入口（--asset BTC|ETH、--mode observe|dry-run|demo、--symbol 可选覆盖、--check、--self-test、--help）；离线 check/help/self-test 不需要真实账户/密钥且不能初始化 MT5。副本 .env.example 只含空凭据与示例，FIB_PA_* 可保留与三AI兼容但实际配置仅副本 .env。日志为副本 crypto_logs/<账户标识>/<asset>，不能落根黄金目录。BTC/ETH 定义放 strategies/definitions/btc 和 eth，所有现有 session 都可分析；按所选 asset 选择 registry，不让两资产定义同时成为候选。新增 profiles 及 prompt 清除黄金 8美元/.01手、亚欧美强制路由等遗留业务限制，仍用当前事实。

price/risk 主实现独立 risk.py：纯数学价格/步长与预算函数可离线调用，broker sizing 通过显式传入 mt5 接口/账户/规格；接入 copied trader 和 AI prompt/rules/feasibility，确保真实 place_order 路径执行，不只新写一个未被调用的函数。移除黄金绝对缓冲/下限/round(...,2)在活跃交易及护盘路径的影响；保留可复用算法，旧固定定义不启用。默认 8 USD 是账户估计亏损上限，不是 BTC/ETH 的价差上限。API/key 不打印明文。

回放入口复制 crypto_backtest.py；黄金 USD per price/100 contract size不得沿用。若本轮不能可信接入实际 CFD 的逐笔回放，明确禁止直接运行旧回放并提供纯 CSV 的研究回放接口，不能留下一个看似能用实际算错盈亏的入口。支持 --help 离线。控制器另行做公开行情诊断，不要求选择历史盈利策略作为启用前置。

测试至少覆盖 BTC/ETH 买卖结构止损的不同量级，非 0.01 的 tick_size，最小手数不可行、向下量化、实际现金预算、保证金不足、已仓/未知/非DEMO、观察模式不付费/不送单、跨实例锁、完整 mock AI-1→AI-2→提交及 AI-3 冻结原契约，逐次报价刷新与固定 SL/TP。测试只用占位凭据/mock。复制现有重点 JSON/protocol/预算回归并在副本运行适用部分，证明未破坏已有链路。

完成 focused + 全部 crypto tests、check/help，提交本工作分支一句中文行为说明，写 .superpowers/sdd/crypto-task-1-report.md（实现、命令/结果、文件、concerns），返回简短状态。不要推送或修改 root master，控制器审核后完成本地备份和发布。

## 验收与发布

控制器进行任务规格/质量复审和整分支独立复审，必要修补后单独完整离线验证。公开行情诊断记录采样窗口/成本假设/延迟与未知，不宣称模拟 CFD/AI 盈利。本地 master 仅追加 crypto_demo/ 和相关研究/设计记录；公开分支保持干净父系并推送 codex/crypto-demo，校验远程 SHA。研究、使用说明、测试结果与 manifest 一起交付；不启动交易。

## 2026-10-11 单次下单入口补充

用户要求自己决定并下单、让已运行的AI3护盘，飞书不标注手动来源；随后明确客户端只实现下单，不启动另一套护盘。新增`manual_order.py`一次提交及等待回执；已有`run.py`持有账号锁、执行订单及原护盘。客户端读取本目录配置以匹配账户/币种/symbol/magic，不导入MT5、主脚本、requests或启动loop。首次同步后用户重启原主程序一次。

`manual_orders.py`提供同目录日志下文件队列，原子写入，控制器每2秒心跳，60秒会话绑定有效期。主进程在派发新AI1前消费；已有AI入场任务先结束，不抢占/并发送单，等待期间也拒绝过期请求。先将命令持久领取，再由原执行链路冻结完整人类入场上下文及event/token/account绑定。已领取文件不自动重放，未知订单回执继续使用原账户级pending latch。重启只精确恢复原仓信息，不重新执行旧会话指令。

人类决策绕过AI1/AI2，内部source为manual，与自主AI1提案单独验证，不伪造quality card、闭合触发或检查点。仍验证USD DEMO/空账户/新报价、H1许可、最新闭合M15/H1/M5、三M5极值缓冲、.6ATR宽度、价格上限/RR/EMA/点差/现金成本/保证金及broker规则。指定SL/TP固定不改，手数可省略沿用现金计算；指定手数须精确合法且低于风险和保证金允许值，不隐式缩量。原资产定义只提供入场时冻结的期限/冷却，AI3用原人类理由、失效及目标依据；未给结构明示未知，公共M1/M5护盘规则保留。通知沿用正常开平仓标题，无入场渠道标记，不显示虚构AI评分。实际私有配置、原自动策略、黄金文件保持，不生成修复包或启动进程。

# XAUUSD 三AI交易系统

AI-1新请求使用`entry-quality-v5`：每个独立问题只声明一次归属和严重程度，由Python生成五项等级与总分，避免等级与意见清单脱节。20/16/8/0、实际质量门槛和入场风控保持；旧版本坏卡仍拒绝。此次按用户要求保留下面的`profit-floor-v3`阶梯护盘，没有加入平滑过渡。见[契约设计](docs/superpowers/specs/2026-10-16-ai1-bound-quality.md)。

重大美国事件（非农、CPI/PPI、GDP/PCE、FOMC决议/发布会/纪要）默认在发布时间前后各15分钟停止新开仓，已有仓位继续锁盈和AI-3护仓。夏令时自动处理，相接窗口合并；日历不可用时单独记录原因，窗口结束后重新分析行情。见[新闻休息设计](docs/superpowers/specs/2026-10-14-news-entry-pause.md)。

默认启用亚、欧、美三盘自主AI粗筛，仅允许MT5模拟账户。本地整理位置、趋势、波动和原始价格行为，不要求先命中固定形态或本地分数过线；AI-1结合分盘参考自主选择打法、提出方案或观望，AI-2审核固定价格和当次报价，AI-3按原入场理由护盘。H1许可方向，M15提供ATR和EMA追价限制，Python执行止损、盈亏比、M5极值保护和持仓限制。

开仓通知分别显示“AI-1质量分（所选BUY/SELL方案）”“本地BUY粗筛分”和“本地SELL粗筛分”。AI质量分是所选方案的质量评分，不是胜率；本地多空分用于行情粗筛，不表示AI对两个方向的评分。自主模式的审核通知标明本地阈值仅供参考，平仓通知沿用开仓时记录的评分标签；缺失历史评分继续显示未知。

新订单使用`profit-floor-v3`：持续确认净浮盈2/4/6/10/15账户币种时，增加0.20/1/2/4/6的现金保护底线，仍留原风险四分之一、三倍点差、1.00价格单位及broker要求的回撤空间，未到下一档不追小波动。普通分级跟随继续完整M5 ATR空间，SL只收紧。AI-3结合原理由和闭合M5/M15/H1复核SL与TP，TP变更绑定成功请求和原持仓，延长TP须实际SL已扣费保本。已有仓（含旧v3阶梯）保留完整冻结规则。见[设计与执行边界](docs/superpowers/specs/2026-10-15-stepped-profit-and-ai3-target-review.md)；预计锁盈不是保证成交额，纯本地回放不证明含AI组合收益。

- `xauusd_fib_volume_pa_ai3.py`：三AI运行入口。
- `xauusd_fib_volume_pa_trader.py`：本地策略、下单与护盘。
- `xauusd_fib_pa_backtest.py`：本地策略历史回放，不调用AI、不下单。
- [策略文件说明](docs/strategies/README.md)：修改 `strategies/definitions/` 的策略内容，框架自动发现并绑定同版本评分、证据与AI规则。
- [部署说明](docs/deployment/README.md)：Windows、Python 3.12、MT5终端与依赖。
- [入场契约与边界设计](docs/superpowers/specs/2026-10-09-entry-gate-reliability.md)：最新评分和实时执行规则。
- [三盘自主AI模拟策略](docs/superpowers/specs/2026-10-10-adaptive-session-demo.md)：当前默认模式、分盘参考及AI请求条件。
- [三盘流程验证](docs/experiments/2026-10-10-adaptive-session-validation.md)：完整模拟AI链路、边界回归及缓存候选覆盖。
- [接入后的实际回放验证](docs/experiments/2026-10-09-session-retest-integration-validation.md)：本地10笔、净+0.30美元，后段仍亏，研究收益不能沿用。
- [黄金多策略研究与模拟比较](docs/experiments/2026-10-09-gold-strategy-research.md)：短线、中线及激进候选的独立离线实验；优先候选现已按[接入设计](docs/superpowers/specs/2026-10-09-session-retest-integration.md)加入模拟盘框架。

新三盘参考目标上限3R、止损不超过8美元，持仓90分钟进入AI-3复核，时间本身不再强制平仓；原SLTP和实时单向锁盈持续保护。每个分析检查点最多一次下单尝试，平仓后冷却60秒。普通已收盘M5可发起分析，检查点不是交易触发成立；AI仍须给真实证据并通过质量门槛。北京时间分段：亚盘07–15、欧盘15–20，美盘20–次日07（包含重叠和尾段），不冒称交易所开盘或自动夏令时分段。旧固定定义保留但默认关闭，非自主固定事件仓仍按原硬期限。原契约和时间写入 `xau_fib_pa_logs/strategy_event_state.json`，部署迁移同一账户时保留该文件、`guardian_profit_state.json`及AI持仓上下文。时间复核兼容已有自主仓，见[设计](docs/superpowers/specs/2026-10-14-holding-review.md)。

另有两个默认关闭的日内研究定义：`region_retest`（区域突破回踩）与 `region_rejection`（区域触碰拒绝）。它们在欧美盘中使用新形成的已确认M15区域、首次M5事件及独立保护锚，目标上限2.5R、最多120分钟、平仓冷却10分钟。参数属于本项目假设，不是视频作者算法的复现；构建与离线结果见[验证记录](docs/experiments/2026-10-10-intraday-build-validation.md)和[设计](docs/superpowers/specs/2026-10-10-intraday-region-opportunities-draft.md)。

将 `.env.example` 复制为本机 `.env` 并填写自己的配置；示例值不代表当前服务器配置。密钥、账户、日志与持仓状态不在公开仓库中。框架代码或配置修改需要自行重启；加载新框架后，策略定义保存将在下一次评估/请求刷新时自动检查。

三AI主备配置统一使用Messages：中转站 `/v1/messages`，DeepSeek官网 `/anthropic/v1/messages`。DeepSeek官方只发送effort、使用提示词JSON与Python严格校验；其它Messages路线优先Schema。模型、档位和超时按实际 `.env`，切换协议不保证消除超时。参见 [协议迁移记录](docs/superpowers/specs/2026-10-09-messages-route-migration.md)。

离线验证（不连接MT5，不请求模型）：

```powershell
py -3.12 -m strategies.check
py -3.12 docs/experiments/verify_gold_profit_guard.py
py -3.12 xauusd_fib_volume_pa_trader.py --self-test
py -3.12 xauusd_fib_volume_pa_ai3.py --self-test
```

离线通过说明规则和程序路径按设计工作，不代表中转服务时延、真实成交或策略收益已验证。

2026-10-15三盘采用[双向短线许可](docs/superpowers/specs/2026-10-15-bidirectional-tactical-permission.md)：H1真实背景与闭合M15结构分开，有证明时可评估逆H1的BUY或SELL；失效前不只凭慢H1反复试原方向。仍须保护真实规范支点及近三M5，SL8/RR1.5/追价等硬门保留。每轮一次AI1请求；持仓继续原AI3与锁盈规则。完整更新三个黄金脚本和strategies后自行重启，实际.env无需改写。离线测试与历史候选诊断不代表新增交易收益，见[验证记录](docs/experiments/2026-10-15-bidirectional-validation.md)。

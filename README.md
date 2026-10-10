# XAUUSD 三AI交易系统

当前默认只启用研究报告优先候选“欧美时段突破回踩”，仅允许MT5模拟账户。伦敦与纽约当地08:00–08:30建箱，08:30–11:00等待首次突破后的首次M5回踩确认，自动处理夏令时。H1许可方向，M15提供ATR和EMA追价限制。本地策略粗筛，AI-1自主提出方案，AI-2审核最终价格，AI-3持仓护盘；Python执行止损、盈亏比、追价和持仓限制。

- `xauusd_fib_volume_pa_ai3.py`：三AI运行入口。
- `xauusd_fib_volume_pa_trader.py`：本地策略、下单与护盘。
- `xauusd_fib_pa_backtest.py`：本地策略历史回放，不调用AI、不下单。
- [策略文件说明](docs/strategies/README.md)：修改 `strategies/definitions/` 的策略内容，框架自动发现并绑定同版本评分、证据与AI规则。
- [部署说明](docs/deployment/README.md)：Windows、Python 3.12、MT5终端与依赖。
- [入场契约与边界设计](docs/superpowers/specs/2026-10-09-entry-gate-reliability.md)：最新评分和实时执行规则。
- [接入后的实际回放验证](docs/experiments/2026-10-09-session-retest-integration-validation.md)：本地10笔、净+0.30美元，后段仍亏，研究收益不能沿用。
- [黄金多策略研究与模拟比较](docs/experiments/2026-10-09-gold-strategy-research.md)：短线、中线及激进候选的独立离线实验；优先候选现已按[接入设计](docs/superpowers/specs/2026-10-09-session-retest-integration.md)加入模拟盘框架。

新候选参考目标上限3R、止损不超过8美元、最多持有180分钟，每个当地日期窗口最多一次下单尝试，平仓后冷却10分钟。旧三策略保留但默认关闭。事件和期限写入 `xau_fib_pa_logs/strategy_event_state.json`，部署迁移同一账户时保留该文件及AI持仓上下文。

另有两个默认关闭的日内研究定义：`region_retest`（区域突破回踩）与 `region_rejection`（区域触碰拒绝）。它们在欧美盘中使用新形成的已确认M15区域、首次M5事件及独立保护锚，目标上限2.5R、最多120分钟、平仓冷却10分钟。参数属于本项目假设，不是视频作者算法的复现；构建与离线结果见[验证记录](docs/experiments/2026-10-10-intraday-build-validation.md)和[设计](docs/superpowers/specs/2026-10-10-intraday-region-opportunities-draft.md)。

将 `.env.example` 复制为本机 `.env` 并填写自己的配置；示例值不代表当前服务器配置。密钥、账户、日志与持仓状态不在公开仓库中。框架代码或配置修改需要自行重启；加载新框架后，策略定义保存将在下一次评估/请求刷新时自动检查。

三AI主备配置统一使用Messages：中转站 `/v1/messages`，DeepSeek官网 `/anthropic/v1/messages`。DeepSeek官方只发送effort、使用提示词JSON与Python严格校验；其它Messages路线优先Schema。模型、档位和超时按实际 `.env`，切换协议不保证消除超时。参见 [协议迁移记录](docs/superpowers/specs/2026-10-09-messages-route-migration.md)。

离线验证（不连接MT5，不请求模型）：

```powershell
py -3.12 -m strategies.check
py -3.12 -m unittest discover -s tests -q
py -3.12 xauusd_fib_volume_pa_trader.py --self-test
py -3.12 xauusd_fib_volume_pa_ai3.py --self-test
```

离线通过说明规则和程序路径按设计工作，不代表中转服务时延、真实成交或策略收益已验证。

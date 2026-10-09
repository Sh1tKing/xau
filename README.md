# XAUUSD 三AI交易系统

H1判断方向，M15提供斐波那契、量能与价格行为，M5确认进场时机。本地策略粗筛，AI-1自主提出方案，AI-2审核最终价格，AI-3持仓护盘；Python执行止损、盈亏比、追价和持仓限制。

- `xauusd_fib_volume_pa_ai3.py`：三AI运行入口。
- `xauusd_fib_volume_pa_trader.py`：本地策略、下单与护盘。
- `xauusd_fib_pa_backtest.py`：本地策略历史回放，不调用AI、不下单。
- [策略文件说明](docs/strategies/README.md)：修改 `strategies/definitions/` 的策略内容，框架自动发现并绑定同版本评分、证据与AI规则。
- [部署说明](docs/deployment/README.md)：Windows、Python 3.12、MT5终端与依赖。
- [入场契约与边界设计](docs/superpowers/specs/2026-10-09-entry-gate-reliability.md)：最新评分和实时执行规则。
- [黄金多策略研究与模拟比较](docs/experiments/2026-10-09-gold-strategy-research.md)：短线、中线及激进候选的独立离线实验，不接入交易账户。

将 `.env.example` 复制为本机 `.env` 并填写自己的配置；示例值不代表当前服务器配置。密钥、账户、日志与持仓状态不在公开仓库中。框架代码或配置修改需要自行重启；加载新框架后，策略定义保存将在下一次评估/请求刷新时自动检查。

离线验证（不连接MT5，不请求模型）：

```powershell
py -3.12 -m strategies.check
py -3.12 -m unittest discover -s tests -q
py -3.12 xauusd_fib_volume_pa_trader.py --self-test
py -3.12 xauusd_fib_volume_pa_ai3.py --self-test
```

离线通过说明规则和程序路径按设计工作，不代表中转服务时延、真实成交或策略收益已验证。

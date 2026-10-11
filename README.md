# BTC / ETH 三AI模拟试验分支

本分支 `codex/crypto-demo` 的新入口在 [crypto_demo](crypto_demo/README.md)。副本使用 MT5 CFD 模拟账户，BTC/ETH 各有可编辑自主策略、合约手数与现金风险检查；完整目录可独立复制部署。每次选择一个币运行，不自动启动交易。

先阅读 [研究与公开行情诊断](docs/experiments/2026-10-11-crypto-strategy-research.md) 和 [设计约束](docs/superpowers/specs/2026-10-11-crypto-demo-fork.md)。现货探针/离线AI通路不代表CFD成交频率或收益已经验证。

以下黄金文件是原公开分支基线；本次复制来源为本地已验证的黄金新版，黄金运行系统另行保留。

## 原 XAUUSD 三AI交易系统

H1判断方向，M15提供斐波那契、量能与价格行为，M5确认进场时机。本地策略粗筛，AI-1自主提出方案，AI-2审核最终价格，AI-3持仓护盘；Python执行止损、盈亏比、追价和持仓限制。

- `xauusd_fib_volume_pa_ai3.py`：三AI运行入口。
- `xauusd_fib_volume_pa_trader.py`：本地策略、下单与护盘。
- `xauusd_fib_pa_backtest.py`：本地策略历史回放，不调用AI、不下单。
- [部署说明](docs/deployment/README.md)：Windows、Python 3.12、MT5终端与依赖。
- [入场契约与边界设计](docs/superpowers/specs/2026-10-09-entry-gate-reliability.md)：最新评分和实时执行规则。

将 `.env.example` 复制为本机 `.env` 并填写自己的配置；示例值不代表当前服务器配置。密钥、账户、日志与持仓状态不在公开仓库中。进程启动后修改代码或配置，需要自行重启才能加载。

离线验证（不连接MT5，不请求模型）：

```powershell
py -3.12 -m unittest discover -s tests -q
py -3.12 xauusd_fib_volume_pa_trader.py --self-test
py -3.12 xauusd_fib_volume_pa_ai3.py --self-test
```

离线通过说明规则和程序路径按设计工作，不代表中转服务时延、真实成交或策略收益已验证。

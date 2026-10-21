# 2026-10-21 期权分析服务器部署准备与安装验证

用户要求给出服务器运行步骤，未要求推送远程或代为启动。当前期权版本在本地 master，远程公开黄金快照不含 options_analysis。指南明确使用源码包，不能用一句 git pull 冒充已发布；也不直接推送含旧密钥历史的 master。

## 修改

- `options_analysis/DEPLOY_WINDOWS.md`：源码、独立私有配置与停机状态迁移，Windows x64/Python/桌面MT5，同用户终端、真实品种、时间偏移与七周期历史，分离行情/新闻/模型/飞书验证，单轮禁通知、持续运行与用户会话要求，以及更新保留账本。
- `options_analysis/requirements.txt`：新增 `requests>=2.32.3,<3`。实际调用链 `options_analysis.news -> strategies.news_calendar` 原来需要 requests，旧清单仅含 MetaTrader5/tzdata，会在干净环境导入新闻时缺包。
- README 指向部署步骤；`.gitignore` 忽略独立交付与构建目录，AGENTS 记录当前部署边界。
- 不修改算法、MT5初始化方式、行情源、阈值、模型、预算、实际.env、交易脚本或运行状态。

## 干净环境验证

使用 Python 3.12.5 x64 新建 `options_analysis/deployment_packages/_build/.venv`。安装前 `find_spec('requests')` 为 False，未继承全局 site-packages。按当前 requirements 安装成功，实际得到：

```text
MetaTrader5 5.0.6231
requests 2.34.2
tzdata 2026.5
numpy 2.5.4
```

这些是本次解析安装的版本，不宣称未来最新版或所有版本均兼容。`pip check` 返回 `No broken requirements found`。导入 `MetaTrader5`、`requests`、`options_analysis.news.NewsSource` 和 `ZoneInfo('America/New_York')` 成功，没有连接终端、刷新新闻或发送模型/飞书请求。

在此新解释器运行：

```text
python -X utf8 -m unittest discover -s options_analysis/tests -p 'test_*.py' -q
Ran 81 tests in 7.579s
OK
```

测试使用临时数据库与合成 MT5/模型/通知传输；证明独立安装和离线代码契约，不证明服务器实际行情、模型或飞书已通。

## 交付规则

源码 ZIP 从本次源提交生成，只包含 `options_analysis/`、完整 `strategies/` 和本次公开说明，随后加入 `DEPLOYMENT_MANIFEST.json`，记录源提交、源树、逐文件SHA256和包验证信息。私有.env、runtime、.git、虚拟环境、日志、黄金主程序与原有未跟踪文件不打入包。交付前须重新解压验证文件摘要及独立导入/演示，具体包名和哈希以实际产物为准；二进制包不进入 Git。

单独的期权 `.env` 由用户私下复制。迁移 runtime 先停止旧扫描，SQLite WAL/SHM 如仍存在一起复制；保留预算、通知去重和人工持仓，不能删库刷新额度。服务器路径/品种后缀/原始时间偏移须实测核对，不把本机3小时配置当已验证。

## 边界

无远程推送、服务器登录或实际部署；未运行连续扫描、终端初始化、真实AI/飞书或模拟/实盘进程。源代码仅支持本机Windows桌面MT5只读采集，未实现Linux行情桥接。现有黄金/crypto/local配置与两个黄金主脚本SHA256与前次记录一致。

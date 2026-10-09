# 三AI黄金策略服务器部署

此部署包的代码提交以 `MANIFEST.json` 为准，证据版本 `entry-evidence-v5`，评分版本 `entry-quality-v2`，行情刷新版本 `request-freshness-v2`，JSON契约版本 `response-contract-v2`。
运行入口是 `xauusd_fib_volume_pa_ai3.py`，它会导入同目录的 `xauusd_fib_volume_pa_trader.py`。

若从GitHub源码部署：将这两个脚本及 `docs/deployment` 下的 `requirements.txt`、`install.cmd`、`start.cmd` 放在同一目录，再将 `.env.example` 复制为 `.env` 并填写自己的账户及接口配置。公开仓库不包含私人 `server_deploy` 目录、实际 `.env`、日志或持仓状态。下述“部署包”指本机生成的私人目录。

## 服务器环境

- Windows 64位服务器，安装 Python 3.12 64位（包含 Python Launcher，即 `py` 命令）。依赖版本与打包机器一致；不是Linux原生部署包。
- 安装经纪商的 MetaTrader 5 桌面终端，在运行脚本的同一Windows用户下打开终端，确认服务器、账户和交易品种可用，并允许外部Python自动交易。
- 能访问经纪商、配置中的AI接口及飞书接口。首次安装依赖需要能访问Python包下载源。

## 部署步骤

1. 将整个 `server_deploy` 文件夹复制到服务器固定目录，例如 `C:\MYQ`。保持文件同级放置。
2. 检查 `.env` 中的账户、经纪商服务器、品种、AI接口和交易开关是否适用于目标服务器。包中带的是打包时部署配置，实际使用以该文件为准。
3. 双击 `install.cmd`，安装依赖到该文件夹的 `.venv`。安装脚本不会启动交易。
4. 打开MT5终端。迁移同一账户前，先在原机器停止旧交易脚本，避免两台机器同时管理同一账户。
5. 双击 `start.cmd` 启动三AI策略。该操作会按 `.env` 的交易开关运行，可能实际下单；只运行一个实例。
6. 查看启动输出：证据版本应为 `entry-evidence-v5`、质量评分应为 `entry-quality-v2`、行情刷新应为 `request-freshness-v2`。门槛以实际 `.env` 为准，AI分比较五项质量总分（非胜率），本地55仍是本地评分门槛。日志写入 `xau_fib_pa_logs`，跨日后第一条记录会按服务器本地日期创建对应日期日志；每次AI结果可查看质量明细、状态与观望依据。更新后需自行重启加载，旧进程不会自动换代码或配置。

## 2026-10-09 入场可靠性更新

评分卡将保留意见嵌入唯一所属维度，Python按模型声明的等级生成ID、归属与程度，避免重复填写产生矛盾。真实证据引用、同一原因去重、已有触发与阻断条件仍严格校验；坏卡不会自动修成交易，也不会付费重审。日志的 `status` 区分 `contract_invalid`、`watch`、`python_rejected`、`review_pending`，契约错误通知明确为输出无效。

0.6ATR/0.8美元继续用于M15参考价下生成止损；实时成交检查改为止损触发侧BUY Bid/SELL Ask至少剩余 `2*stop_buffer(atr)`（现有参数下1.2–2美元），并考虑点差、止损8美元上限、RR1.5、EMA21追价及旧K线过期。价格向有利方向变化不再仅因低于生成宽度而拒绝，贴近止损仍拒绝。AI-2与实际下单使用同一检查。详见入场可靠性v2设计；本次不修改实际配置和本地策略。

Claude原生接口按URL的 `/v1/messages` 路径选择Messages协议，GPT备用可继续使用 `/v1/chat/completions`，各路线独立适配。三角色优先Schema，接口明确不支持才降级；max_tokens、截断拒绝与5次总请求预算保持。新格式减少冗余声明，仍不能保证中转或模型永不返回无效引用。

退出脚本使用控制台 `Ctrl+C`。此包是前台运行方式，没有注册Windows服务或开机任务。关闭控制台、注销Windows用户或服务器重启会中断运行，需自行重新启动。

## AI备用接口

`.env`已预留三套备用配置。将编号替换为1、2、3即可分别配置对应角色：

```dotenv
AI1_BACKUP_URL=
AI1_BACKUP_KEY=
FIB_PA_AI1_BACKUP_MODEL=
FIB_PA_AI1_BACKUP_EFFORT=
FIB_PA_AI1_BACKUP_TIMEOUT=
```

空白或未写的项逐项继承对应主接口的URL、密钥、模型、推理档位和超时。备用超时须为正整数秒，空白或无效值沿用主路；单独填写超时不会创建新备用通道。全部留空时仍使用原配置。备用中转站可能需要填写它自己的密钥和支持的模型名。

2026-10-08完整AI-2审核复放发现：Grok主路约85.7秒才返回，DeepSeek V4 Pro的`medium`实际映射为`high`，一轮4096输出token全部用于思考、没有最终JSON。仅提高超时仍可能得到截断。当前AI-2主路读取等待改为90秒，已配置的DeepSeek备用改用`low`并独立等待40秒：

```dotenv
FIB_PA_AI2_TIMEOUT=90
FIB_PA_AI2_BACKUP_EFFORT=low
FIB_PA_AI2_BACKUP_TIMEOUT=40
```

这是该部署配置的修复值，不强制改写所有供应商的思考档位。没有独立备用时不要仅填一个不同effort来虚构备用。完整快照本机单次对照中，DeepSeek `low`约13.8秒返回完整审核，修复后的call_chat复核约19.8秒也返回完整审核；Grok主路90秒仍可能超时，因此修复验收关注自动备用接手，不能保证主路不再超时或服务器长期时延。AI-1与AI-2共享360秒接管期限不变，过期回复仍丢弃，正常REJECT与截断均不换模型重审。

每次分析先请求主接口，主接口发生超时、连接失败、HTTP错误、非JSON响应或可见输出为空时切换独立备用。主备以及参数兼容恢复合计最多请求 `FIB_PA_AI_HTTP_RETRIES` 次（默认5）。读超时后，上游可能仍在生成并计费，因此默认不向同一通道重复补发；有独立备用则切换一次，两边都读超时就结束该轮。连接失败/连接超时仍可在总预算内重试，网络重试间隔2秒。正常观望/REJECT不换模型重审。启动日志显示备用是否独立；切换日志显示primary/backup、模型和总请求序号，不显示密钥。两条通道都不可用时开仓仍会拒绝，AI-3仍由本地护盘和原服务器止损保护。

编辑配置后需自行重启进程才能加载。将更新后的脚本复制到服务器，同时将这些备用配置项加入服务器实际使用的 `.env`，保留服务器自己的主接口、账户及其他参数。

## AI用量与成本控制

启动日志应显示 `AI成本控制=cost-latency-v2`，备用配置行同时显示主路及备用等待秒数。关键证据、完整K线窗口、实际配置的质量门槛和Python风控保留；输入只移除与已序列化OHLC计算结果一致的重复candle_parts、完全重复的元数据和AI-2无需重评的评分卡，来源精度更高的影线数值仍保留。规则与评分标准提前排列以便缓存。三角色要求简短JSON解释，不复述全部行情。

```dotenv
FIB_PA_AI_READ_TIMEOUT_RETRIES=0
FIB_PA_AI1_MAX_TOKENS=4096
FIB_PA_AI2_MAX_TOKENS=4096
FIB_PA_AI3_MAX_TOKENS=2048
```

`READ_TIMEOUT_RETRIES`为每通道读超时后的额外补发次数，0代表不重复同一慢请求；仍受HTTP总预算限制。减少重复请求意味着主线路较慢且没有备用时，本轮会较早结束，不再靠第2至5次同线路补发等回复。备用项留空仍没有新服务可用。

Chat Completions使用 `reasoning_effort`。GPT/o系列用 `max_completion_tokens`，其余兼容模型先用 `max_tokens`；接口明确拒绝该上限字段时最多改用另一字段，始终保留上限。拒绝推理参数时可去掉该参数恢复，并在进程内记住兼容结果。不同中转如何映射思考参数及计费需要看实际usage，不能仅凭HTTP200认定推理档位生效。

回复结束原因是 `length`/`max_tokens` 时视为截断，即使可解析JSON也不执行、不另换模型重审。日志会提示调整对应角色上限；上限在部分模型上包含思考token，因此不要仅按可见JSON长度继续压低。

每次实际HTTP请求均写入 `xau_fib_pa_logs/ai_usage/YYYY-MM-DD.jsonl`，包括角色、call_id、decision_id、trigger、主备、请求/回复ID、耗时、上限、发送档位及输入/输出/思考/缓存读写token。v2另记`read_timeout_sec`、`headers_elapsed_sec`和`body_complete`；收到响应头后正文超时仍保留实际HTTP状态和请求ID，不把已连接误写成完全不可达。未知字段为null，读超时费用未知，绝不记作免费；缓存未命中与缓存写入分开，不重复累计provider已含缓存的输入。美元费用留空，实际费用以中转账单为准。迟到的过期回复也记用量后丢弃，不下单。

`trigger=startup/M15/M5/M1`分别表示启动立即评估、M15主评估、满足本地条件的M5复核、持仓M1护盘；同call_id的不同attempt是重试，AI-2沿用本轮入场decision_id。AI-2请求快照另记 `ai2_input`，便于与账单和最终方案核对。以上用量日志不包含密钥。

## 文件说明

- 两个 `.py`：三AI与本地策略/交易/护盘模块。
- `.env`：当前实际配置，含密钥和账户信息，仅私下传输保存，不提交Git或公开上传。
- `requirements.txt`：当前已安装的三个直接依赖版本。
- `install.cmd` / `start.cmd`：安装与启动入口。
- `MANIFEST.json`：打包时间、源代码版本、脚本SHA256，便于检查是否复制完整。

不需要复制历史日志、回测结果、其他交易脚本、`.env.example` 或本机虚拟环境。
本次打包时未发现 `xau_fib_pa_logs/ai3_position_state.json`，因此未包含持仓理由状态。如果正式迁移时已有该文件，应在旧脚本完全停止后，将最新文件复制到新机器同名目录；缺少入场历史时AI-3会将相关上下文标记为未知。

本地已核对脚本复制一致和依赖元数据；服务器安装、MT5连接、AI连通性与交易执行需要在目标机器确认。本次打包没有启动或重启任何交易进程。

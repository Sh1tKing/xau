# 入场执行区间与评分契约 v2

用户授权全面修复并发布到 `https://github.com/Sh1tKing/xau.git`。保留本地粗筛、AI-1自主提案、AI-2独立审核、AI-3护盘；本次不改本地策略、账户、模型、档位、实际 `.env`、HTTP预算或超时。

## 问题证据

10月9日00:00至06:56的43轮决策中，12轮主备读超时、8轮正文JSON合法但评分卡语义不合法、23轮有效no，AI-2请求0。31份结果都绑定成功请求快照。6份快照的固定本地方案已通过实时RR、止损上限、触发侧缓冲与追价，却因0.6ATR最低距离失败；其中5份还同时存在质量卡错误，不能重复计数。

## 实时执行边界

原 `request-freshness-v1` 将 `entry_risk_limits(atr)` 的生成最小宽度也用作实时剩余距离，SELL报价变高或BUY报价变低时，RR改善也会被拦截。本次明确调整这条策略取舍：

- 止损生成与Python定稿仍以已收盘M15参考价执行0.6ATR/0.8美元的原下限，结构止损和1.8R目标上限不变。
- 成交检查使用实际止损触发侧：BUY看Bid、SELL看Ask，至少剩余 `2 * stop_buffer(atr)`，即现有参数下1.2–2美元。一份原结构缓冲加一份成交等待缓冲；这是保守的工程取舍，不是经历史收益标定的最优值。
- 允许成交侧距离下限为上述余量加当前点差；最大距离仍为8，固定SL/TP下实时RR仍至少1.5。区间不移动SL/TP，不给RR添加宽松容差。EMA21追价和收盘K线过期检查保持。
- 同一函数供AI-1输入事实、AI-2每次POST前、下单tick前检查共用。报价必须有限、正数且Ask不低于Bid。

在归档成功请求报价上，01:00、01:17、02:26、03:26、03:31的上述区间问题解除；00:56虽有11R的账面比值，触发侧仅剩1.01美元，仍拒绝。此前595017736秒损案例及BUY镜像也必须拒绝。只说明边界算术变化，不将这些快照当作已批准或应成交订单。

## 评分契约

新请求使用 `entry-quality-v2`：

```json
{
  "quality_assessment": {
    "version": "entry-quality-v2",
    "direction": "buy",
    "dimensions": {
      "target": {
        "level": "adequate",
        "reason": "目标证据结论",
        "evidence_refs": ["/computed_evidence/obstacles/buy/0"],
        "reservations": [
          {
            "cause": "target_uncertainty",
            "event_group": "实际来源事件",
            "reason": "本项轻微意见",
            "evidence_refs": ["/computed_evidence/obstacles/buy/0"]
          }
        ]
      }
    }
  },
  "wait_basis": []
}
```

上例仅展示层级，实际必须有全部五个维度与完整交易字段，不能照抄路径或事件。每项仍由模型判断strong20/adequate16/weak8/missing0，Python汇总，不猜胜率、不输出自由confidence。

保留意见直接放在唯一归属维度内，模型不再重复输出ID、dimension、severity或reservation_ids。level一次声明程度：adequate中的保留是minor、weak中的保留是material、missing中的保留是blocking。strong不得有保留；weak/missing必须有保留。Python按公开契约生成一致的ID、归属和程度供现有日志/持仓上下文使用，这是新格式解码，不是修补旧模型结果。

继续严格检查真实JSON Pointer、同cause/event去重、已有触发不可说缺失、blocking不可提交易、no必须有wait_basis。普通量/浅回撤不能通过weak/missing声明成重大硬伤。错误明确到证据路径；列表索引遵循JSON Pointer，不接受Python负索引或前导零索引。旧v1卡保留原严格检查并标为v1，8份历史坏卡不自动恢复、不改动作、不额外付费重审。

提示词同步说明：`local_plan_feasibility`只评价原本地方案，不是自主AI方案的全局否决；该字段位于输入顶层。给出Python定稿的生成下限和1.8R目标上限，避免以随后被截短的目标声称实时RR通过。执行空间问题归protection，实时RR归target，不能抹去已存在触发。重要障碍仍须有来源，不能为出单删除真实反向证据。

## 可观测性与上线

`ai1_result.status`区分 `contract_invalid`、`watch`、`python_rejected`、`review_pending`。契约错误通知明确为输出无效并保留原始动作；正常no保持原样。请求异常与行情停止另有快照。版本为 `entry-quality-v2` / `request-freshness-v2` / `response-contract-v2`。

部署只同步脚本、说明与MANIFEST；实际 `.env`、日志及持仓状态保留。进程由用户自行重启，代理不启动交易。不保证供应商严格执行Schema或永不返回坏引用，也不保证机会、成交或收益增加。

验证结果：116项离线回归、两项内置自检和编译检查通过。原31份结果复放仍为23份有效v1卡、8份无效卡严格拒绝；19份有本地计划的快照中，新执行算术为7份通过、12份拒绝，其中5份仅生成宽度误用造成的问题解除，贴止损的00:56仍拒绝。未请求真实模型。验证前后53个实际配置、交易日志及状态文件SHA256一致。

## Git发布

本地master历史包含无关脚本的硬编码密钥，不能直接上传该历史。保留本地历史，在 `codex/publish-master` 建立黄金相关文件的干净快照，推送该分支到远程master；后续发布以远程公开提交为父提交。公开文件限README、AGENTS、忽略规则、示例配置、三个黄金脚本、tests和docs。实际.env、server_deploy、日志、其他交易脚本及.claude不上传。

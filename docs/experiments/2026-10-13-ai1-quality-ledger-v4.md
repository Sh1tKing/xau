# 2026-10-13 AI-1统一意见清单

## 最新同步日志

最新文件截至16:46:02，16:05启动及用量记录代码签名均为`3b0b84a4a9ddc7e1`，服务器已加载上次修复。

16:46这轮`decision_id=1791891900-8`，Claude主路HTTP成功，耗时59.97秒、`finish_reason=end_turn`，output=3559，其中明确报告thinking=2167。JSON完整，Schema字段形状有效，失败来自语义去重：structure/observations/0与counter_evidence/observations/0均为`cause=opposing_structure`、`event_group=null`，实际引用集合相同（`/m15_bars/31`与`/patterns/M15`），描述时间1791891000的同一根M15阴线。省略实体长度不构成新事件。轻微观察无额外分值，但旧v3要求意见唯一归属，因此不是校验误拒。上次提示词未阻止逐维度复制，仅增加同类句子不足以作为本次修复。

原回复是`action=no`，trigger=missing，另外四项adequate，理由缺少多头收盘触发。旧坏卡继续拒绝且无质量分。测试单独编写的新v4同事实、同等级卡仍64分、watch，不能说这轮应开单。

16:30已出现80分BUY提案，AI2备用DeepSeek约4秒审核通过，16:30:59实际开仓0.01手。16:16另有AI1截断：output=4096、thinking=3333、非思考输出763。这是另一类失败，本次不改变用户档位/上限，也不声称统一意见清单能解决思考预算不足。

## 新协议

新请求使用`entry-quality-v4`，五个维度统一只有`level/reason/evidence_refs`，所有意见集中在`quality_assessment.issues`中声明一次：

```json
{
  "dimension": "counter_evidence",
  "kind": "observation",
  "cause": "opposing_structure",
  "event_group": null,
  "reason": "本次真实M15反向信息尚非确认反转",
  "evidence_refs": ["本次证据目录中的实际ID"]
}
```

`dimension`是唯一归属，`kind`为reservation或observation。没有意见写issues=[]；不在五项放重复的reservations/observations数组。同事实可在多个维度理由里提及；清单仍可保留同cause的独立事件，没有把原因强行固定到某维度或限制每类仅一次观察。

`strategies/quality.py`集中声明新版本、Schema、提示词、rubric及v4解码。`xauusd_fib_volume_pa_ai3.py`统一使用，并按模型声明的清单生成现有内部原因ID/程度；新仓只保存单份v4清单并恢复，不保存旧格式校验投影；旧仓不重建。AI2/AI3实际HTTP继续按原逻辑省略完整评分卡。该处理是明确新协议的解码，不能用于改写历史坏卡。

以下规则保留：

- 分值20/16/8/0；strong不含意见；weak/missing各须本维度reservation，不能借用别项理由撑扣分。
- observation永远minor、无额外扣分；普通量/浅回撤仅观察，硬限制/必要缺失仅reservation，blocking不能与交易动作并存。
- 同cause/event去重、实际证据存在、同候选/方向隔离、成功POST快照引用与材料观望检查。
- JSON无效或截断不补字段、不搬数组、不删重复、不改等级、不付费重审；no不转交易。
- H1许可、SL8、RR1.5、近3根M5保护、EMA追价、实时报价、0.01单仓、执行消费及AI角色分工。

v1/v2/v3继续按原版严格兼容：合法旧回复保持原版，旧坏回复仍错误。生产HTTP始终请求v4；显式v3 Schema入口用于历史离线复核。

Schema不能单独表达全部跨条目的语义唯一性。若模型在统一清单再次重复，Python仍拒绝，没有自动整理功能；没有付费供应商验证，不能保证不再出现任何契约错误。

## 体积及验证

对同一16:46档案，紧凑JSON字符量：

| 内容 | 上次构建 | 新构建 |
| --- | ---: | ---: |
| 响应Schema | 8075 | 6588 |
| 公共AI1系统说明 | 5342 | 5157 |
| 层级模板 | 1162 | 994 |

Schema减少18.4%。字符不是实际token/费用，不证明时延或收益；三盘附加说明及实际用量须用新服务器记录复核。

原始失败夹具`tests/fixtures/ai1_quality_20261013_164602.json`保存原回复、原错误、准确目录及源签名，没有密钥/私有思考正文。新增`tests/test_ai_quality_ledger_v4.py`18项，覆盖旧坏卡不改写、新格式同等级watch、重复/归属/程度/隔离/Schema诊断、三盘买卖完整AI1→AI2→持仓保存恢复、主备刷新及错误卡不重审。历史v3测试显式请求其自身Schema，继续验证原规则。

根目录完整596项通过（67.248秒），两项内置自检、策略CLI、回放help通过；策略指纹仍`0a16c78772e79b466eceb7b150182da1c7787eb015157d893f93261e1394ae89`。验证禁止未模拟MT5/HTTP并隔离状态，实际.env及既有状态哈希不变。部署白名单35个黄金测试模块的596项也通过（67.573秒），部署两项内置自检通过，125个文件哈希及121份源码一致；编译、实际.env哈希及原状态检查通过。

## 使用更新

同步既有server_deploy白名单，替换主脚本和完整strategies包后用户自行重启，无需改.env。新代码签名`646d4ac9a9e63755`，评分版本`entry-quality-v4`；文件哈希及提交以MANIFEST为准。不启动交易、不发送实际通知、不主动推送远程。

# 具身数据采购组合分析台

研究团队可用本项目比较候选具身数据包对客户任务的分层覆盖、重复样本、采购金额和授权边界。运行仅依赖 Python 3.11 标准库与本地 SQLite；不会访问机器人、外部报价接口或在线数据源。

先用命令 python -m embodied_portfolio demo --workspace fixtures 执行合成数据演示。演示导入两份候选包与一份采购需求，找出符合失败样本覆盖且预算可承受的组合，审批分析后验证冻结快照。演示中的供应商、价格与样本均为合成值。

自有数据可用 python -m embodied_portfolio --db plan.sqlite3 import requirement requirement.json 和 import offer offer.json 登记。再执行 plan REQUIREMENT_KEY 查看可行组合、缺口和包级排除原因。approve ANALYSIS_ID INPUT_DIGEST 只批准当前需求和报价版本下的可行分析；目录发生修订后，应重新分析。verify ANALYSIS_ID 校验冻结输入与报告摘要；analysis、history、versions 可查看旧版本。gaps、marginal、frontier 支持覆盖缺口和边际价值查看。

候选包声明采集路径、供应商、机型、场景、动作阶段、结果类别、样本数量及可选的逐条样本标识。已给出的标识用于精确去重；没有标识的数量不会被猜测为与另一包相同。报价支持逐条、逐小时与包价，金额按给出的数量计算到分。许可可对客户有限制，限制外的报价不可进入组合。需求声明预算、可用采集路径、各分层最低数量及是否必需。系统不会把失败或人工干预归入成功样本。

分析结果包含所选包、成本、逐层覆盖、重复证据、候选过滤原因和替代组合。每次分析绑定明确的需求与包版本；审批后的报告不因日后报价变化而改写。输入清单使用 UTF-8 JSON，错误会返回具体代码。

## 上游来源证据与独立来源保障

供应商名称不同不等于来源分散。`source` 文档记录“某个包由哪支上游采集队提供”的证据，包含依据类型（`contract`、`invoice`、`audit`、`interview`、`disclosure`）、证据强度（`verified` 或 `reported`）、参考编号与观察日期，并有 `valid_from`/`valid_until` 有效期。示例：

```json
{"key": "upstream_2026", "title": "上游采集队关系证据",
 "claims": [{"package": "remote_01", "collector": "field_crew_alpha", "basis": "contract",
             "strength": "verified", "reference": "采购合同附件A", "observed_on": "2026-10-01"}],
 "valid_from": "2026-10-01", "valid_until": "2027-09-30", "notes": "尽调记录"}
```

需求可在目标分层上声明 `minimum_independent_sources`。`plan` 与 `compare-packages` 会把两种口径分开报告：

- **样本数覆盖**：`coverage`、`coverage_only_selection`/`coverage_only_cost`，即只按数量与去重选出的结果。
- **独立来源保障**：`source_assurance`，按证据把包归并到上游采集队，统计真正独立的来源数。

判定规则：无有效证据的包为 `unconfirmed`，有效期外为 `expired`/`not_yet_valid`，同一有效窗口内被指向两个采集队为 `disputed`；这几种状态都不算独立来源。只重复他人样本标识的包不构成独立贡献者。报告同时给出 `shared_source_groups`（包装在不同供应商下但同属一个采集队）与 `provider_diversity_misleading`，避免仅凭供应商数量宣称已分散。

为降低相关失效风险而引入独立来源后的成本口径写在 `independent_alternative` 与 `assurance_premium`：前者是相对只按覆盖选出的组合，满足来源保障的最低成本组合、额外成本与剩余预算；`within_budget` 直接回答是否仍在预算内。若找不到满足保障的组合，会给出 `available: false` 与原因。

`as_of` 决定用哪些证据版本评估时效，默认今天，可用 `plan REQUIREMENT --as-of 2026-11-17` 固定。证据文档与需求、报价一并冻结为分析输入：关系证据被更正后必须重新分析，旧草稿不能批准；旧批准方案保留当时的证据修订与结论，`audit` 会用当时的证据重算并单独标出 `evidence_current: false`，不会用新证据改写历史，也不会因供应商名称不同而声称来源已分散。`sourcing REQUIREMENT [--as-of DATE]` 只读查看当前证据下的来源保障与时效。

其他可用命令：

可用 `batch delivery.json` 原子导入一批文档，文件顶层为 `documents` 数组，每项含 `kind`（offer、requirement 或 source）和 `body`；整批先通过验证再提交，重复内容不会产生新修订。`compare-versions offer PACKAGE 1 2` 比较两次报价或样本清单的具体变化。

分析前可用 `readiness REQUIREMENT` 和 `brief REQUIREMENT` 查看路径分布、许可限制、分层供给、已知重复以及逐包价格。`budgets REQUIREMENT 140 150 180`、`targets REQUIREMENT SCENARIO|STAGE|OUTCOME|ROBOT 3 4 5`、`withdrawal REQUIREMENT PACKAGE`、`price-scenario REQUIREMENT PACKAGE PRICE` 只做临时情景推演，不修订目录。`compare-packages REQUIREMENT PACKAGE_A PACKAGE_A,PACKAGE_B` 对照人工指定方案的覆盖、成本、许可和供应商支出。

`audit ANALYSIS_ID` 使用分析时绑定的输入版本重新计算历史选择，检查来源摘要和报告摘要；`compare EARLIER_ID LATER_ID` 展示同一需求两次分析的包版本、来源证据版本、选择、成本、来源保障与覆盖变化。后续目录修订不会让旧的有效快照失真，但会使旧草稿不能直接批准。

业务测试：python -m unittest discover -s tests -v。

语法检查：python -m compileall -q embodied_portfolio。

运行验收：python -m embodied_portfolio demo --workspace fixtures。

来源证据验收：python -m embodied_portfolio demo-sourcing --workspace fixtures_sourcing --as-of 2026-11-17。该演示先展示两家供应商共用一个上游采集队、仅按覆盖选出的组合并不满足独立来源保障，再给出独立组合的成本与剩余预算；随后更正证据并重新分析，同时验证旧批准方案仍按当时的供应链依据可审计。

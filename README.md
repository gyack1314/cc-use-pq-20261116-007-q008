# 具身数据采购组合分析台

研究团队可用本项目比较候选具身数据包对客户任务的分层覆盖、重复样本、采购金额和授权边界。运行仅依赖 Python 3.11 标准库与本地 SQLite；不会访问机器人、外部报价接口或在线数据源。

先用命令 python -m embodied_portfolio demo --workspace fixtures 执行合成数据演示。演示导入两份候选包与一份采购需求，找出符合失败样本覆盖且预算可承受的组合，审批分析后验证冻结快照。演示中的供应商、价格与样本均为合成值。

自有数据可用 python -m embodied_portfolio --db plan.sqlite3 import requirement requirement.json 和 import offer offer.json 登记。再执行 plan REQUIREMENT_KEY 查看可行组合、缺口和包级排除原因。approve ANALYSIS_ID INPUT_DIGEST 只批准当前需求和报价版本下的可行分析；目录发生修订后，应重新分析。verify ANALYSIS_ID 校验冻结输入与报告摘要；analysis、history、versions 可查看旧版本。gaps、marginal、frontier 支持覆盖缺口和边际价值查看。

候选包声明采集路径、供应商、机型、场景、动作阶段、结果类别、样本数量及可选的逐条样本标识。已给出的标识用于精确去重；没有标识的数量不会被猜测为与另一包相同。报价支持逐条、逐小时与包价，金额按给出的数量计算到分。许可可对客户有限制，限制外的报价不可进入组合。需求声明预算、可用采集路径、各分层最低数量及是否必需。系统不会把失败或人工干预归入成功样本。

分析结果包含所选包、成本、逐层覆盖、重复证据、候选过滤原因和替代组合。每次分析绑定明确的需求与包版本；审批后的报告不因日后报价变化而改写。输入清单使用 UTF-8 JSON，错误会返回具体代码。

可用 `batch delivery.json` 原子导入一批文档，文件顶层为 `documents` 数组，每项含 `kind`（offer 或 requirement）和 `body`；整批先通过验证再提交，重复内容不会产生新修订。`compare-versions offer PACKAGE 1 2` 比较两次报价或样本清单的具体变化。

分析前可用 `readiness REQUIREMENT` 和 `brief REQUIREMENT` 查看路径分布、许可限制、分层供给、已知重复以及逐包价格。`budgets REQUIREMENT 140 150 180`、`targets REQUIREMENT SCENARIO|STAGE|OUTCOME|ROBOT 3 4 5`、`withdrawal REQUIREMENT PACKAGE`、`price-scenario REQUIREMENT PACKAGE PRICE` 只做临时情景推演，不修订目录。`compare-packages REQUIREMENT PACKAGE_A PACKAGE_A,PACKAGE_B` 对照人工指定方案的覆盖、成本、许可和供应商支出。

`audit ANALYSIS_ID` 使用分析时绑定的输入版本重新计算历史选择，检查来源摘要和报告摘要；`compare EARLIER_ID LATER_ID` 展示同一需求两次分析的包版本、选择、成本和覆盖变化。后续目录修订不会让旧的有效快照失真，但会使旧草稿不能直接批准。

业务测试：python -m unittest discover -s tests -v。

语法检查：python -m compileall -q embodied_portfolio。

运行验收：python -m embodied_portfolio demo --workspace fixtures。

# 建模库调研补充：吸收验证方法，暂不扩张选型

核验日期：2026-09-13。范围：用户提供的 penaltyblog / sports-betting / footBayes 调研，与当前 goalx 实现的差异。只读源码和官方资料；未安装、升级、运行新模型或修改产品。

## 结论

penaltyblog 不是待引入的新选型，goalx 已用它拟合 Dixon–Coles。新材料最值得吸收的是去水敏感性分析、依赖升级回归，以及更完整的模型工件身份；不应据此再引入回测框架或增加模型清单。

## 已实现，应保留

- `apps/backend/src/goalx_backend/dc_model.py` 已调用 `DixonColesGoalModel`，按联赛拟合，保存参数、训练窗口、半衰期与数据指纹。`score_matrix.py` 已从参数复建比分矩阵和国内玩法视图，`test_score_matrix.py:65` 有与 penaltyblog 输出的交叉验证。附件关于“适配层 + 参数工件 + 一致概率底座”的建议已经部分实现，不需要重新开发。
- `odds_math.py` 已自写 multiplicative 和 Shin；市场基准并非缺失。`backtest.py` 已有按时间的回测、参数记录以及投入加权 ROI。建议先修可靠性与口径，保留现有国内业务边界。

## 应吸收的增量

### 1. 将版本差异与升级审查写实

本项目 `uv.lock:1331` 锁定 **1.6.2**，依赖声明为 `penaltyblog>=1.6.2`；官方 PyPI 当前为 **1.12.1，2026-09-10 发布，Python >=3.10**。附件的最新版信息核验成立，但“latest 文档支持”不能直接等于当前安装版本支持。[PyPI](https://pypi.org/project/penaltyblog/)

官方 1.9.0 新增由 lambdas 创建 DC 矩阵的 `create_dixon_coles_grid()`，也修复亚洲让球符号及大小球四分之一盘口；1.10.0 加入批量预测，1.11.0 加入中立场选项。项目当前自己生成矩阵和国内整数三向让球，不宜据此断言这些上游盘口错误已影响当前 had 流程，也不应仅为新功能立即升级。[官方更新日志](https://penaltyblog.readthedocs.io/en/latest/changelog/index.html)

建议将升级作为一个有边界的维护实验：用固定训练数据比较参数、矩阵、had 概率和历史实验指标；确认差异原因后再改变锁文件。已有矩阵交叉测试应沿用，但它只与当前装载的库比对，不能独自证明跨版本结果没有变化。

### 2. 去水敏感性比另一个模型更便宜、更贴近陈盘假设

官方 `calculate_implied()` 支持 multiplicative、Shin、Power 等，返回 margin、method 和方法参数。[官方赔率文档](https://penaltyblog.readthedocs.io/en/latest/implied/implied.html)

建议先离线用同一批完整同公司 1X2 报价，交叉核验现有 Shin，并比较三种方法下候选 EV 的正负与排序是否稳定。不要事后挑最赚钱的方法，也不要把三种输出当成统计置信区间。不必立刻替换目前几十行纯函数。

另一个当前代码带出的具体问题：`consensus_odds()` 是按选项先平均赔率，再去水。建议比较“每家公司完整三项先去水，再聚合概率”，并约束报价时间与公司集合；否则三项来自不同公司集合，合成出的向量难以解释。此项是基于本地代码的工程建议，不是声称某聚合方法必然更准确。

### 3. 工件身份需要包含方法与版本，而不只是数据

`DCArtifact` 有数据指纹和半衰期，却未记录 penaltyblog 版本；`run_filename()` 只按联赛和数据指纹命名，`save_run()` 直接覆盖。相同数据改变半衰期、bootstrap 配置或依赖版本，会写向同一路径，旧实验难以追溯。建议小范围补齐：数据指纹 + 训练配置 + 库/实现版本构成 run 身份，旧 run 不被新配置覆盖。这比增加模型注册平台简单，也更直接支持附件强调的可复现升级。

### 4. 官方确有 Backtest，但不因此替换现有引擎

附件纠正“penaltyblog 没有回测”是对的：官方有 `Backtest`、训练回调、lookback、账户与 Kelly 示例。该示例不是中国竞彩票据引擎；它让策略回调访问含最终赛果的 fixture，因此资料时点隔离仍由使用者负责。[官方 Backtest 示例](https://penaltyblog.readthedocs.io/en/latest/backtest/backtest.html)

吸收其最小研究示例即可。不能假设它自动处理竞彩停售、有效购买、2串1、无效腿、奖金封顶、开奖更正或报价时间边界。官方示例的 ROI 与本项目投入加权 ROI 也应先核对分母再比较。没有必要在已有引擎旁长期维护第二套账本和回测口径。

## 暂缓引入

| 项目 | 官方核实 | 对 goalx 的建议 |
| --- | --- | --- |
| sports-betting | 官方确有统计/赔率来源组合、scikit-learn Bettor、时间序列回测和 MCP/CLI；还含执行工具。[仓库](https://github.com/georgedouzas/sports-betting) | 借鉴来源明确、研究结果可复用即可。当前引入会与现有采集、模型、回测和 Agent 接口重叠。MCP 便利性不解决国内报价及结算问题。 |
| footBayes | 官方支持静态/动态球队实力、贝叶斯推断、后验检查及样本外比较；2.0 起需 R + cmdstanr + CmdStan。[仓库](https://github.com/LeoEgidi/footBayes) | 留作模型误差明确后的离线对照。先问是球队实力动态变化还是参数不确定性真的导致了已观察的失败；无此证据先不加语言和编译链。 |

补充：新版 penaltyblog 本身也已有 Bayesian / Hierarchical Bayesian，因此“需要贝叶斯”不自动意味着必须引入 footBayes；这只说明以后应按研究问题评估，当前也不建议提前比较两套贝叶斯实现。[官方更新日志](https://penaltyblog.readthedocs.io/en/latest/changelog/index.html)

## 最小吸收顺序

1. 保留现有 penaltyblog 适配和国内业务实现；修旧审视报告中的可信度问题。
2. 补齐模型工件身份，离线做去水方法敏感性与当前实现交叉核验。
3. 有明确收益时才进行一次受控 penaltyblog 升级实验。
4. sports-betting、footBayes、更多玩法与模型留待实际缺口出现；不列为首版必经步骤。

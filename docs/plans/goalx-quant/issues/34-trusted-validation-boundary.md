# 修正验证统计与赛前证据边界

Type: impl
Status: resolved
Assignee: ZCode
Blocked by: 35

前序说明：票33已使正式收益曲线、已结数与CLV过滤purchased=1。此处仍需完成paper/live分组、注/腿去重、赛前证据与未知不通过等验收，不重复实现该基础过滤。

交接修订：先合并“补齐报价观测、匹配与可购买状态”，再消费其真实证据类型/查询，避免两套时间与资格定义。样本集合、Bet/BetSlip/腿计数及单关/串关CLV口径见[实施安排](../../trusted-paper-handoff.md)。概率评分覆盖冻结比赛集合（含未下注比赛），不只评分已购买样本。内部可分多个垂直切片PR交付。

## 范围

看板只表达已验证事实，承接Forecast/回测/指标/CLV旧票；与[报价观测](35-trusted-quote-evidence.md)协调时间字段，先以隔离样本验证，真实验收在运行票。

- 无复核=未评估，整赛季独立显示；历史回测不能充当前瞻skill。
- 正式前瞻集合固定策略/模型版本、锁定时点和纳入规则；paper/live/未购隔离，Bet/腿/场次数分列，滚动窗口用唯一正式纸面Bet。
- CLV只纳赛前完整有效报价，源时间未知/迟到/不合格均排除并报告分母。单关与2串1分开；串关用票级联合概率并标独立性假设，腿级仅诊断，不复制票级profit做独立回归。
- Forecast生成及训练窗严格早于kickoff；历史replay另标，旧资格未知不倒填。
- 修复同串关按玩法重复汇总，明确投入加权ROI与每注收益率均值。稳定排序；相关性未处理的p值可先隐藏/标探索性，不堆复杂检验。
- 历史基准按源/日期质检，复查2025-07-23后PSC，形成分期/来源对照新run；保留旧run，不无条件改用另一源。合成价实验不能称真实陈盘回放。
- 工件身份含数据+训练配置+实现/依赖版本，防覆盖；不升级penaltyblog。bootstrap显示有效样本与名义水平，未验证覆盖不得称校准成功。

## 验收

1. 空库未知项不通过；100张2串1的200腿显示100票，不满足200注；混合mode和未购不污染纸面。
2. 整赛季或复核缺失，即使其他数值达标仍未完成；不降低既定阈值、不做真钱放行，不阻止真实历史记账。
3. 已结赛前Forecast生成前瞻评分；最新历史run不能改变前瞻成绩；赛后预测、未来工件、迟到closing、重复同场均有测试。
4. had-only每玩法去重汇总等于overall；相同输入可重现，flat与加权收益清楚。
5. 基准报告包含时期/来源/数量/缺失/差异，旧run仍可查，统计限制明确。
6. 同数据不同half-life/seed/实现版本生成不同工件身份；旧预测可追溯。覆盖未验收明确显示。

## 验证与交付

固定时钟、隔离数据做服务/API/指标回归，不能只断言字段存在或和为1；契约同步及task check。历史对照保留输入标识和参数，不覆盖旧结果。进一步统计放行裁决留后续，不以本票代替长期验证。

## Answer（2026-09-13）

已实现，分支 `feat/trusted-validation-boundary`；本地 `task check` exit 0（后端 236 测试含覆盖率门禁、web 全套、契约一致性），发布 [PR #7](https://github.com/cosmos-arc/goalx/pull/7)，已由用户合并，合并提交 `9c21816`；CI 15 项全绿。

验收对照：

1. 空库全部条件不通过（`test_validation_progress_empty_state`）；2串1 两腿计 1 票不冒充 2 注（`test_parlay_ticket_level_clv_with_independence_flag` 的 n_bets/denominator）；同决策重试/拆分金额去重后唯一注计数（`test_decision_identity_dedup_for_denominator`）；paper/live/未购互不污染（mode 隔离计数 + 曲线按 mode，`test_api.py` live 场景）。
2. 复核无记录=未评估、整赛季独立显示未完成——两条件结构上不可真空通过（`test_api_validation.py`）；未降低阈值、未做真钱放行、不阻止真实历史记账（live 回录路径未动）。
3. 前瞻评分集合只收 issued_at 严格早于 kickoff 的冻结 Forecast（`test_forward_validation.py`）；进度 skill 不读最新回测 run（回测卡明确标注"回测"）；赛后预测只计 replay、预测时点之后的市场观测不用、同场多条赛前预测冻结最新一条、无基准计入排除分母，均有测试。
4. CLV closing 必须实际开赛前观测（`test_late_closing_observation_excluded`，按 observed_at 源解释）；单关/2串1 × paper/live 分组；串关票级联合概率+独立性声明，腿级不进回归；串关玩法重复汇总修复、had-only 去重汇总==overall（`test_parlay_same_market_not_double_counted`）；flat/加权 ROI 分列（`test_dual_roi_reporting`）；dm_p 标探索性。
5. 基准报告按 2025-07-23 切期分列数量/缺失/overround/两源 Shin 差异（`test_baseline_quality_report_periods_and_diffs`）；`baseline-compare` 生成 psc/avgc 对照新 run 且旧 run 保留（`test_run_baseline_comparison_creates_new_runs_and_keeps_old`）；合成价 run 标 `price_model=simulated_jc`。
6. 工件身份=数据+half-life+seed+实现/依赖版本（`test_artifact_id_distinguishes_config_and_versions`）；文件名按身份内容寻址不互相覆盖、payload 带 artifact_id 可追溯（`test_run_filename_uses_artifact_id_no_overwrite`）；bootstrap 标名义水平与有效样本数（`test_payload_carries_artifact_identity_and_ci_metadata`）；覆盖未验收不称校准成功。

消费 35 号票证据：CLV/前瞻均通过 `quote_evidence.effective_observed_at` 按源解释观测时间，未另建定义。边界：复核条件待 M3；整赛季验收待 37 运行协议；前瞻市场基准是评分对照（无新鲜度窗）与投注判定口径分开。后继票 36 从本 PR 合并后的 main 开始。

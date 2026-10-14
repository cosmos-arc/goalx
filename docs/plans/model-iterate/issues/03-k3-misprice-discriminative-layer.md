# 03 K3 错价桶判别层：8 桶特征第二阶段调整 + walk-forward 验证

Status: resolved
Spec: `.scratch/model-iterate/spec.md` S3
Blocked by: 02

## 任务

- **判别模块** `modelling/residual_layer.py`：以 signal-attribution
  8 桶特征（ah/ou 线漂移、相位计数、临场位移、水位）为输入的第二
  阶段残差调整（logistic 三向，或特征→调整量的收缩回归；模型形态
  由实现者定，参数量小、正则化）；
- **walk-forward 验证**：同窗内拟合、出窗评估（十年循环内嵌），
  对照 K2 融合输出：判别层增量 skill/log-score/EV 实现率；
- **对照报告**（并入 model_layers）：特征重要性/系数表（n 与效应量
  随行，探索性声明同票 21 先例）；
- **裁决材料**：有增量→接引擎缺省开的建议；无增量→诚实留空结论
  （这本身是答案），汇总进程序收官。

## 不变量与人裁决项

- 特征全为 gold 既有 PIT 列（不新建特征）；出窗评估严格（无窗内
  评分混入）；正则化防过拟合（8 桶效应 1-2pp 量级，极易被噪声
  吃掉——报告须带与随机特征对照的安慰剂检验）；
- 多重比较诚实：桶级结论带 n 与效应量，探索性声明；
- 人裁决项：判别层是否进缺省管线（门=报告+用户点头）。

## Out of scope

- 新特征工程（xG/GBM stacking 留下一迭代）；AH/OU 侧判别。

## Answer

2026-10-14 交付（分支 feat/residual-layer-k3-20261014，PR #147，
三提交，合 main 9cd0230）：

- **模块** `modelling/residual_layer.py`：带偏置 softmax（logit=
  log p_fused+W·x）+L2 岭罚确定性梯度下降；FeatureTuple 八特征；
  placebo_entries 互素步长置换；权重轨迹入 run summary 系数面。
  引擎管线 calibrate→fuse→residual（判别史存判别前三向）。
- **十年终审（run=10 resid / run=11 placebo vs 基线 9，L2 修正后
  定版）**：
  - **判别层无实质增量——留空**：skill −0.0060→−0.0066（resid）vs
    −0.0072（placebo）双臂持平；系数 mean|w|≈0.016 极小；
  - 下注面与安慰剂分离的微弱正贡献（ROI −12.2% vs −18.3%，Δ6.1pp）
    但绝对值≈−haircut 且预测面无支撑——**不接缺省管线**，引擎保留
    可选配置；
  - signal-attribution 8 桶 |t|≥2 在出窗终审下不构成可用判别信号
    （1-2pp 效应被噪声+岭罚压平，票面预警兑现）。
- **review 战果**：双轴抓到 L2 缩放真 bug（罚项被 /n 稀释至 5e-4，
  修正后重跑两变体定版数字）；correctness 三修（placebo gcd-7 非双
  射+全长度性质测试 / NaN 三层 isfinite 自防御 / 安慰剂系数面标注）；
  940 tests 全绿。
- 人裁决项（是否接缺省）→ 结论=不接（报告+数字在此，留待下一迭代
  方向裁决时复核）。

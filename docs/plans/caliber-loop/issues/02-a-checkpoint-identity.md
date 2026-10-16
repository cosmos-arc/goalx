# 02 A3 抽取 checkpoint 复合键与 lastModified 版本分组

Status: resolved（PR #156 合 main b5c4fc3，2026-10-16）
Spec: `.scratch/caliber-loop/spec.md` A 相
Blocked by: （无）

## 任务

- **复合键**：`data/ingest/guardian_silver.py` checkpoint 键从
  article_sid 升级为 `article_sid + fixture sid + body_hash +
  extractor 版本`。一稿命中多场是链接层设计（各对独立抽取，提示词
  含具体场次两队），复合键消除断点重跑时 A 场结果记到 B 场的交叉
  污染；旧 checkpoint（article_sid 键）不迁移、按新键重抽（存量
  90 篇，版本号变更自然触发）。
- **版本分组**：候选过滤增加 lastModified 维度——bronze payload 已
  存 `lastModified`；分组为 strict-PIT（lastModified < kickoff）与
  版本不可证（lastModified ≥ kickoff 或缺失）两组，silver 事件行
  带组标记；报告侧（01 号票口径）分组统计，不可证组不与严格组
  混合下结论。
- 熔断/重放回归：一稿两场+中途断点重放的测试（断言两场各自落对、
  无交叉）。

## 不变量与人裁决项

- checkpoint 条目自描述（键内含版本与 hash，可从文件直接判断可否
  复用）；webPublicationDate 早于开球仍是最小门槛（PIT 过滤不动），
  lastModified 只加严不放宽；
- 人裁决项：无。

## 验收标准

- [ ] checkpoint 键=article+fixture+body_hash+extractor 版本，条目自描述
- [ ] 一稿两场+中途断点重放回归测试：两场各自落对、无交叉污染
- [ ] strict-PIT / 版本不可证分组落 silver 事件行标记
- [ ] 存量 90 篇按新键重抽（版本号变更触发）；webPublicationDate 门槛不动，lastModified 只加严

## Answer（2026-10-16）

- 重抽口径：版本 1→2 触发存量 101 条 legacy checkpoint 在下一次 pilot 跑
  自然全量重抽；回填停待票 07（用户 2026-10-14 裁决），本票只交付机制，
  重抽随 07 有界扩样一次完成，不烧双份 LLM 费。
- correctness 加固（超出票面）：`_parse_instant` tz-strict（naive 时间戳
  保守降级）——消除 naive lastModified/webPublicationDate 在预筛
  TypeError 崩溃的毒丸行（与 main 既有 pub 比较同族地雷一并根因修）。
- 组视图披露口径：`event_by_pit_group` 按全体侧样本（匹配前）构建——
  strict 存在但全被对照匹配剔除时仍可见；主结论面维持 matched 池。

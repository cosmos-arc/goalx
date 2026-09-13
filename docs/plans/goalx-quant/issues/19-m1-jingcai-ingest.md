# 19 [M1-2] 竞彩采集 Prefect flow

Type: impl
Status: resolved
Blocked by: 37

## 范围

把票 08 原型（`.scratch/goalx-quant/prototype/pipeline_prototype.py`）的 sporttery 部分产品化：Prefect flow 定时拉取 `getMatchCalculatorV1.qry`（全部 5 玩法 poolCode，须 Referer 头），解析入 fixtures/OddsSnapshot（append-only，保留 `updateDate/updateTime` 调盘时点）；重试+礼貌限速；businessDate≠matchDate 处理；DrawResult（官方开奖）导入留接口（M1-6 接）。凭据读 `.env`。

## 验收

- Prefect deployment 连续运行 ≥3 天无人工干预，快照时点覆盖每日竞彩销售期。
- 数据可查：任意场次的全部玩法赔率时序。

## Answer

2026-09-13 实施完成。`ingest/sporttery.py`：httpx 带 Referer 拉取
getMatchCalculatorV1.qry（5 玩法全量），解析纯函数（crs s02s01→2:1、
s1sh→h_other；ttg s0..s7；hafu 两字母；hhad goalLine 入 meta），
调盘时点 updateDate/updateTime→UTC 随快照落库；businessDate≠matchDate
已按北京日期自然键处理。Prefect flow `jingcai-snapshot` + CLI
`task ingest-jingcai`。实测：39 场/2085 快照，重复采集 dup 吸收，
`GET /api/v1/fixtures/{id}/odds` 可查全部玩法时序。
「deployment 连续 ≥3 天」为运维验收项，flows 已就绪待部署。

## 当前验收修订（2026-09-13）

代码组件已完成；连续至少3个目标销售日的部署/实采验收未完成。

原答案保留为历史，不代表当前通过。依赖票完成后，本票只核对剩余验收并链接证据，不重复开发；不满足仍保持未验收。当前计划见[Spec v1.1](../spec.md)，后继工作见[可信纸面闭环验收](37-trusted-paper-operational-acceptance.md)。

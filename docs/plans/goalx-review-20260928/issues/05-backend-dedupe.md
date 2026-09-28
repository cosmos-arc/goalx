# 05 backend：重复代码三件套收敛

Status: resolved（PR #98，2026-09-28）

## 现状（复验坐实）

1. **共识→Shin→EV 组装双份**：`data/today.py:139-158` vs `api/fixtures.py:317-349`
   同构（today.py:144 与 fixtures.py:325 同句 `om.shin_implied(consensus)`）。
   fixtures.py:276 docstring 自述的不变量「同 fixture 两页不出现两个共识」目前
   只靠两边手工同步维持。
2. **_triple 双胞胎**：`evaluation/drift_replay.py:75 _triple` 与
   `evaluation/pool_replay.py:73 _odds_triple` 逐字节相同的行→三元组守卫。
3. **0.65 硬编码**：`api/pool.py:347` `0.65 / share_prod`，权威常量
   `POOL_RETURN_RATE` 在 `data/pool.py:40`（同文件 :390 已用对）。——若票 02
   先行则随票 02 搭车，本票剩前两件。

另记（低优先，可并入后续触点）：Chrome UA 串 ×12、
`httpx.Client(verify=srct.browser_ssl_context())` tasks.py×7/cli.py×8——等下一
次 collector 改动时顺手收敛，不单开。

## 修复

1 抽共享 helper（落 data 域，两处调用）；2 合一（evaluation 内共享小函数）。

## 不变量与人裁决项

- 行为零变化，纯抽取；验收：两页共识输出对同一 fixture 逐字段一致（现有测试
  或补一个对拍断言）。

## Comments

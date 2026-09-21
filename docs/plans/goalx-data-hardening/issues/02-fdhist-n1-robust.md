# N1 进默认导入 + 单文件失败不中断

Type: impl
Status: resolved
Blocked by: none

## 问题

- N1 荷甲不在 FD_COMPETITIONS（M2 手动加训），2627 新赛季数据永远不进（模型停在 2026-05-17）。
- `import_history` 的 `raise_for_status()` 让任一 (season, competition) 404 中断整个导入——N1 2627 文件若未发布会炸全量导入。

## 范围

1. FD_COMPETITIONS 增补 N1
2. 根修：单文件拉取失败（404/超时）→ 跳过+计数+warning，不中断其余文件；统计加 failed_files 计数
3. 单测：一个文件 404 时其余文件照常入库
4. 实跑 ingest-hist 验证 N1 2627 行数（若源文件确实未发布，如实记录 skipped）

## 不做

- 不加重试/退避（polite 限速已有；失败下次幂等重跑即可）

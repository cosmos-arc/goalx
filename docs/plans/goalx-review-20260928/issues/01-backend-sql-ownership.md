# 01 backend：ledger_audit 跨包裸 SQL + sql_ownership 守护测试盲区

Status: resolved（PR #95，2026-09-28）

## 现状

`apps/backend/src/goalx_backend/betting/ledger_audit.py:84-87`：

```python
for table in ("draw_result_revisions", "settlement_revisions"):
    history[table] = [
        dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")
    ]
```

betting 包直读 `draw_result_revisions`（data 域表，见 docs/db-and-domains.md），
违反 ADR-0008「他包不走裸 SQL」。

更糟的是守护测试 `tests/unit/test_sql_ownership.py` **对此天生失明**：表名藏在
普通字符串元组里经 f-string 拼进 SQL，不落在 SQL 字面量上，字面量扫描抓不到。

## 修复

1. revisions 读取改走表主（data 域暴露只读函数；settlement_revisions 若归
   betting 自有则可留包内——以 db-and-domains.md 权属为准）。
2. 守护测试补盲区：f-string/字符串变量拼接的表名也要纳入扫描（如禁止
   `execute(f"...{var}...")` 形态或建立表名→包的静态白名单交叉检查），让这类
   绕行在 CI 红灯而非靠人眼。

## 不变量与人裁决项

- 人裁决：audit 需要的 revisions 数据由 data 包暴露函数返回，还是 audit 模块
  整体挪到 data 域？（推荐前者，audit 的"审计 betting"语义留在 betting。）
- 验收：test_sql_ownership 能抓到修复前的写法（用旧代码形态写一个红灯
  用例）；`task test` 绿。

## Comments

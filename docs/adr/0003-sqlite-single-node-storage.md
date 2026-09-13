# SQLite 单机存储（WAL）

单用户本地 Mac 部署、数据量为万级场次×时点快照（每年 GB 级以下），SQLite（WAL 模式）足以覆盖 Prefect 任务与 FastAPI API 的并发读写，且零运维。不选 Postgres：常驻数据库服务的运维成本在当前规模下没有回报。SQL 层保持标准方言，未来上云/多用户时可平移 Postgres。

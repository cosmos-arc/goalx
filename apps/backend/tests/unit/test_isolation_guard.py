"""测试隔离守卫（report-audit-20261104 票 10①）。

默认约束在 tests/conftest.py（Settings 路径默认值钉进会话临时目录）；
本文件钉死该约束的效果——部分构造的 Settings 从隔离 CWD 跑，库文件只
出现在指定目录（goals4 测试污染真实库前科的防线，不靠逐测试补）。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from goalx_backend.config import Settings
from goalx_backend.db import connect, migrate
from goalx_backend.main import create_app


def test_partial_settings_do_not_touch_cwd(tmp_path: Path, monkeypatch) -> None:
    """只传 db_path 的 Settings（test_api_validation.py:260 复现载荷）：
    研究库/corpus 等默认走会话隔离目录，CWD 零落盘。"""
    monkeypatch.chdir(tmp_path)  # 隔离 CWD：默认值若仍相对 CWD 会在此暴露
    settings = Settings(db_path=tmp_path / "biz.db")
    assert settings.research_db_path == Path(os.environ["GOALX_RESEARCH_DB_PATH"])
    assert settings.corpus_root == Path(os.environ["GOALX_CORPUS_ROOT"])
    # 复现原路径：validation/progress 打开研究库（曾自动迁移 CWD 默认库）
    conn = connect(settings.db_path)
    migrate(conn)  # 业务库就绪（progress 另读 review_items）
    conn.close()
    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/api/v1/validation/progress")
        assert response.status_code == 200
    assert not (tmp_path / "data").exists()  # CWD 零落盘
    assert settings.research_db_path.exists()  # 库文件只在指定隔离目录


def test_bare_settings_defaults_isolated() -> None:
    """裸 Settings() 的全部存储默认值都在会话隔离目录下（含 corpus 不落
    $HOME/goalx-data）。"""
    settings = Settings()
    root = Path(os.environ["GOALX_CORPUS_ROOT"]).parent
    for path in (
        settings.db_path,
        settings.research_db_path,
        settings.observations_dir,
        settings.models_dir,
        settings.corpus_root,
    ):
        assert root in path.parents

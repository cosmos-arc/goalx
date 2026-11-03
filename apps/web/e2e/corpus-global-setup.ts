import { execSync } from "node:child_process";
import { rmSync } from "node:fs";
import path from "node:path";

/**
 * corpus 主链 e2e 全局准备（boundary-hardening 票 06）：
 * 清旧库/旧语料树 → 迁移空 schema → CLI e2e-corpus 生成四场制式夹具
 * （源T 页→bronze→silver→corpus.duckdb + 运行面）。
 *
 * 与 loop 夹具（seed-demo 演示数据）分离：本套件证明真实 corpus 读径。
 */
export const corpusDbPath = path.join(import.meta.dirname, "../test-results/corpus-e2e.db");
export const corpusRootPath = path.join(import.meta.dirname, "../test-results/corpus-e2e-tree");

export default function globalSetup(): void {
	for (const suffix of ["", "-wal", "-shm"]) {
		rmSync(`${corpusDbPath}${suffix}`, { force: true });
	}
	rmSync(corpusRootPath, { recursive: true, force: true });
	const env = {
		...process.env,
		GOALX_DB_PATH: corpusDbPath,
		GOALX_CORPUS_ROOT: corpusRootPath,
	};
	execSync("uv run --no-sync python -m goalx_backend.cli migrate", {
		cwd: path.resolve(import.meta.dirname, "../../.."),
		env,
		encoding: "utf8",
		stdio: "pipe",
	});
	execSync("uv run --no-sync python -m goalx_backend.cli e2e-corpus", {
		cwd: path.resolve(import.meta.dirname, "../../.."),
		env,
		encoding: "utf8",
		stdio: "pipe",
	});
}

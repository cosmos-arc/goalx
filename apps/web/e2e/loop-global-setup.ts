import { execSync } from "node:child_process";
import { rmSync } from "node:fs";
import path from "node:path";

/**
 * 纸面闭环 e2e 全局准备：清掉旧库并迁移空 schema（票 36 隔离环境）。
 */
export default function globalSetup(): void {
	const dbPath = path.join(import.meta.dirname, "../test-results/loop-e2e.db");
	// 票 07②：验证页回测读面走研究库（playwright.loop.config 同名文件）
	const researchDbPath = path.join(import.meta.dirname, "../test-results/loop-e2e-research.db");
	for (const suffix of ["", "-wal", "-shm"]) {
		rmSync(`${dbPath}${suffix}`, { force: true });
		rmSync(`${researchDbPath}${suffix}`, { force: true });
	}
	execSync("uv run --no-sync python -m goalx_backend.cli migrate", {
		cwd: path.resolve(import.meta.dirname, "../../.."),
		env: { ...process.env, GOALX_DB_PATH: dbPath },
		encoding: "utf8",
		stdio: "pipe",
	});
}

import { createHash } from "node:crypto";
import { EventEmitter } from "node:events";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import { describe, expect, it } from "vitest";
import {
	assertSandboxTarget,
	buildExportArgs,
	buildRunArgs,
	DOCKER_DOWN_HINT,
	describeFailure,
	ENGINE_MISSING,
	engineEnv,
	findingFilePath,
	MODEL_API_KEY_ENV,
	MODEL_HTTP_HEADERS_ENV,
	narrateEvent,
	parseEngineLine,
	pathUnderRoot,
	readCandidateForApproval,
	resolveEngineBinary,
	resolveSandboxInputs,
	runEngine,
	siblingManifest,
} from "../src/core/patchcage-sandbox.ts";

describe("reviewed candidate", () => {
	it("pins preview and export to the verified digest", () => {
		const dir = mkdtempSync(join(tmpdir(), "patchcage-review-"));
		try {
			const patch = "--- a/file\n+++ b/file\n";
			const digest = createHash("sha256").update(patch).digest("hex");
			writeFileSync(join(dir, "candidate.patch"), patch);
			expect(readCandidateForApproval(dir, digest)).toBe(patch);
			expect(buildExportArgs(dir, "/out", digest)).toContain(digest);
			writeFileSync(join(dir, "candidate.patch"), "changed");
			expect(() => readCandidateForApproval(dir, digest)).toThrow(/changed/);
			expect(() => readCandidateForApproval(dir, undefined)).toThrow(/digest/);
		} finally {
			rmSync(dir, { recursive: true, force: true });
		}
	});
});

describe("resolveSandboxInputs", () => {
	const root = "/repo";
	it("auto-picks the only manifests/*.finding.yml and its sibling manifest", () => {
		const result = resolveSandboxInputs(root, undefined, () => ["a.yml", "a.finding.yml", "README.md"]);
		expect(result).toEqual({
			ok: true,
			finding: join(root, "manifests", "a.finding.yml"),
			manifest: join(root, "manifests", "a.yml"),
		});
	});
	it("fails closed with zero or multiple findings", () => {
		expect(resolveSandboxInputs(root, undefined, () => []).ok).toBe(false);
		expect(resolveSandboxInputs(root, undefined, () => ["a.finding.yml", "b.finding.yml"]).ok).toBe(false);
	});
	it("resolves an explicit relative finding against the repo root", () => {
		const result = resolveSandboxInputs(root, "manifests/x.finding.json", () => []);
		expect(result).toEqual({
			ok: true,
			finding: join(root, "manifests", "x.finding.json"),
			manifest: join(root, "manifests", "x.json"),
		});
	});
	it("rejects a finding without the .finding stem", () => {
		expect(resolveSandboxInputs(root, "manifests/x.yml", () => []).ok).toBe(false);
		expect(siblingManifest("/m/x.yml")).toBeUndefined();
	});
});

describe("resolveEngineBinary", () => {
	it("prefers PATCHCAGE_ENGINE, else PATH lookup by name", () => {
		expect(resolveEngineBinary({ PATCHCAGE_ENGINE: "/opt/pc/bin/patchcage-engine" })).toBe(
			"/opt/pc/bin/patchcage-engine",
		);
		expect(resolveEngineBinary({ PATCHCAGE_ENGINE: "  " })).toBe("patchcage-engine");
		expect(resolveEngineBinary({})).toBe("patchcage-engine");
	});
});

describe("engineEnv", () => {
	it("passes the key only as PATCHCAGE_MODEL_API_KEY and strips a stale one", () => {
		expect(engineEnv({ HOME: "/h" }, "sk-x")[MODEL_API_KEY_ENV]).toBe("sk-x");
		expect(engineEnv({ HOME: "/h", [MODEL_API_KEY_ENV]: "old" }, undefined)[MODEL_API_KEY_ENV]).toBeUndefined();
	});
	it("strips parent credentials and sets the child key plus extra headers", () => {
		const env = engineEnv(
			{
				HOME: "/h",
				PATH: "/bin",
				OPENAI_API_KEY: "leak-openai",
				GH_TOKEN: "leak-gh",
				GITHUB_TOKEN: "leak-github",
				[MODEL_API_KEY_ENV]: "old-key",
				[MODEL_HTTP_HEADERS_ENV]: '{"Authorization":"Bearer old"}',
			},
			"sk-child",
			{ "api-key": "azure" },
		);
		expect(env.HOME).toBe("/h");
		expect(env.PATH).toBe("/bin");
		expect(env.OPENAI_API_KEY).toBeUndefined();
		expect(env.GH_TOKEN).toBeUndefined();
		expect(env.GITHUB_TOKEN).toBeUndefined();
		expect(env[MODEL_API_KEY_ENV]).toBe("sk-child");
		expect(JSON.parse(env[MODEL_HTTP_HEADERS_ENV]!)).toEqual({ "api-key": "azure" });
	});
});

describe("findingFilePath / pathUnderRoot / assertSandboxTarget", () => {
	it("reads file_path from JSON or a YAML line", () => {
		expect(findingFilePath('{"file_path": "src/app.py"}')).toBe("src/app.py");
		expect(findingFilePath("file_path: src/demo_app/search.py\n")).toBe("src/demo_app/search.py");
		expect(findingFilePath('file_path: "src/app.py"  # comment')).toBe("src/app.py");
		expect(findingFilePath("file_path: |\n  folded")).toBeUndefined();
		expect(findingFilePath("{}")).toBeUndefined();
	});
	it("rejects absolute, empty, and escaping paths", () => {
		expect(pathUnderRoot("/repo", "src/app.py")).toBe(join("/repo", "src/app.py"));
		expect(pathUnderRoot("/repo", "/etc/passwd")).toBeUndefined();
		expect(pathUnderRoot("/repo", "../etc/passwd")).toBeUndefined();
		expect(pathUnderRoot("/repo", "")).toBeUndefined();
	});
	it("fails closed when the finding, path, or target file is missing", () => {
		const files = new Set(["/repo/manifests/a.finding.yml", "/repo/manifests/a.yml", "/repo/src/app.py"]);
		const io = {
			exists: (p: string) => files.has(p),
			readFile: () => "file_path: src/app.py\n",
		};
		expect(assertSandboxTarget("/repo", "/repo/manifests/a.finding.yml", "/repo/manifests/a.yml", io)).toEqual({
			ok: true,
		});
		expect(assertSandboxTarget("/repo", "/missing.yml", "/repo/manifests/a.yml", io).ok).toBe(false);
		expect(
			assertSandboxTarget("/repo", "/repo/manifests/a.finding.yml", "/repo/manifests/a.yml", {
				...io,
				readFile: () => "title: no path\n",
			}),
		).toMatchObject({ ok: false, error: expect.stringMatching(/no file_path/) });
		expect(
			assertSandboxTarget("/repo", "/repo/manifests/a.finding.yml", "/repo/manifests/a.yml", {
				...io,
				readFile: () => "file_path: ../etc/passwd\n",
			}),
		).toMatchObject({ ok: false, error: expect.stringMatching(/outside/) });
		expect(
			assertSandboxTarget("/repo", "/repo/manifests/a.finding.yml", "/repo/manifests/a.yml", {
				...io,
				readFile: () => "file_path: src/missing.py\n",
			}),
		).toMatchObject({ ok: false, error: expect.stringMatching(/create_demo_repo/) });
	});
});

describe("parseEngineLine / narrateEvent", () => {
	it("parses engine JSON and ignores noise", () => {
		expect(parseEngineLine("not json")).toBeUndefined();
		expect(parseEngineLine('{"nope":1}')).toBeUndefined();
		const event = parseEngineLine(
			'{"sequence":1,"event_type":"phase","phase":"snapshot_ready","payload":{"phase":"snapshot_ready"}}',
		);
		expect(narrateEvent(event!)).toBe("sandbox: snapshot_ready");
	});
	it("narrates checks and results", () => {
		expect(
			narrateEvent({ event_type: "check_result", payload: { name: "unit", status: "passed", summary: "ok" } }),
		).toBe("check unit: passed — ok");
		expect(narrateEvent({ event_type: "result", status: "awaiting_approval", detail: "d" })).toBe(
			"result: awaiting_approval — d",
		);
		expect(narrateEvent({ event_type: "tool_call" })).toBeUndefined();
	});
});

describe("args", () => {
	it("builds run/export argv the engine accepts", () => {
		expect(
			buildRunArgs({ repo: "r", manifest: "m", finding: "f", runDir: "d", modelEndpoint: "u", modelId: "id" }),
		).toEqual([
			"run",
			"--repo",
			"r",
			"--manifest",
			"m",
			"--finding",
			"f",
			"--run-dir",
			"d",
			"--model-endpoint",
			"u",
			"--model-id",
			"id",
		]);
		expect(buildExportArgs("d", "o")).toEqual(["export", "--run", "d", "--out", "o"]);
	});
});

function fakeChild() {
	const child = new EventEmitter() as EventEmitter & {
		stdout: PassThrough;
		stderr: PassThrough;
		kill: (sig: string) => boolean;
		killed: string[];
	};
	child.stdout = new PassThrough();
	child.stderr = new PassThrough();
	child.killed = [];
	child.kill = (sig: string) => {
		child.killed.push(sig);
		return true;
	};
	return child;
}

describe("runEngine", () => {
	it("streams JSON lines across chunk boundaries and captures the result", async () => {
		const child = fakeChild();
		const events: string[] = [];
		const promise = runEngine({
			bin: "x",
			args: [],
			cwd: "/",
			env: {},
			onEvent: (e) => events.push(e.event_type),
			spawnImpl: (() => child) as never,
		});
		child.stdout.write('{"event_type":"phase","pha');
		child.stdout.write('se":"created","payload":{}}\n{"event_type":"result","status":"awaiting_approval"}');
		child.stderr.write("diag\n");
		child.emit("close", 0, null);
		const outcome = await promise;
		expect(events).toEqual(["phase", "result"]);
		expect(outcome.result?.status).toBe("awaiting_approval");
		expect(outcome.stderr).toBe("diag\n");
	});
	it("SIGINTs the child on abort", async () => {
		const child = fakeChild();
		const abort = new AbortController();
		const promise = runEngine({
			bin: "x",
			args: [],
			cwd: "/",
			env: {},
			onEvent: () => {},
			signal: abort.signal,
			spawnImpl: (() => child) as never,
		});
		abort.abort();
		child.emit("close", 130, null);
		await promise;
		expect(child.killed).toEqual(["SIGINT"]);
	});
	it("SIGINTs immediately when aborted before spawn and calls onSpawn", async () => {
		const child = fakeChild();
		const abort = new AbortController();
		abort.abort();
		const spawned: unknown[] = [];
		const promise = runEngine({
			bin: "x",
			args: [],
			cwd: "/",
			env: {},
			onEvent: () => {},
			signal: abort.signal,
			onSpawn: (c) => spawned.push(c),
			spawnImpl: (() => child) as never,
		});
		child.emit("close", 130, null);
		await promise;
		expect(child.killed).toEqual(["SIGINT"]);
		expect(spawned).toHaveLength(1);
	});
	it("reports a missing engine binary without throwing", async () => {
		const child = fakeChild();
		const promise = runEngine({
			bin: "x",
			args: [],
			cwd: "/",
			env: {},
			onEvent: () => {},
			spawnImpl: (() => child) as never,
		});
		const err = Object.assign(new Error("spawn x ENOENT"), { code: "ENOENT" });
		child.emit("error", err);
		const outcome = await promise;
		expect(outcome.code).toBeNull();
		expect(outcome.stderr).toBe(ENGINE_MISSING);
	});
});

describe("describeFailure", () => {
	it("adds the Docker hint only when stderr says the daemon is down", () => {
		const down = describeFailure({ code: 1, signal: null, stderr: "docker daemon unavailable: boom\n" });
		expect(down).toContain(DOCKER_DOWN_HINT);
		const other = describeFailure({
			code: 1,
			signal: null,
			stderr: "x",
			result: { event_type: "result", status: "verification_failed" },
		});
		expect(other).toContain("verification_failed");
		expect(other).not.toContain(DOCKER_DOWN_HINT);
	});
});

import { EventEmitter } from "node:events";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import { describe, expect, it } from "vitest";
import {
	buildExportArgs,
	buildRunArgs,
	DOCKER_DOWN_HINT,
	describeFailure,
	ENGINE_MISSING,
	engineEnv,
	MODEL_API_KEY_ENV,
	narrateEvent,
	parseEngineLine,
	resolveEngineBinary,
	resolveSandboxInputs,
	runEngine,
	siblingManifest,
} from "../src/core/patchcage-sandbox.ts";

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

/**
 * `/sandbox` (Phase 6): drive `patchcage-engine` as a subprocess.
 *
 * The chat agent stays unsandboxed. This module never runs tools itself; it
 * spawns the Python engine, narrates its JSON-lines events, and lets the caller
 * gate `export` on human approval. All parsing here is pure so it can be unit
 * tested without Docker.
 */

import { type ChildProcess, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { closeSync, existsSync, fstatSync, openSync, readdirSync, readFileSync, readSync } from "node:fs";
import { basename, dirname, isAbsolute, join, resolve, sep } from "node:path";

export const ENGINE_ENV = "PATCHCAGE_ENGINE";
export const ENGINE_BIN = "patchcage-engine";
export const MODEL_API_KEY_ENV = "PATCHCAGE_MODEL_API_KEY";
export const MODEL_HTTP_HEADERS_ENV = "PATCHCAGE_MODEL_HTTP_HEADERS";
export const EXPORTS_DIR = join(".patchcage", "exports");

/** Read exactly the candidate the engine verified, with a bounded allocation. */
export function readCandidateForApproval(runDir: string, digest: string | null | undefined): string {
	if (!digest) throw new Error("Engine result has no candidate digest");
	const fd = openSync(join(runDir, "candidate.patch"), "r");
	try {
		const stat = fstatSync(fd);
		if (!stat.isFile() || stat.size > 1_000_000) throw new Error("Candidate is not a reviewable patch file");
		const buffer = Buffer.alloc(stat.size + 1);
		const length = readSync(fd, buffer, 0, buffer.length, null);
		const bytes = buffer.subarray(0, length);
		if (createHash("sha256").update(bytes).digest("hex") !== digest) {
			throw new Error("Candidate changed after verification; refusing approval");
		}
		return bytes.toString("utf8");
	} finally {
		closeSync(fd);
	}
}

export const SANDBOX_USAGE =
	"Usage: /sandbox [finding.yml]. The manifest is the sibling file without `.finding` (X.finding.yml → X.yml). Without an argument, exactly one manifests/*.finding.yml must exist under the repo root.";

export const ENGINE_MISSING = `${ENGINE_BIN} not found. Install the PatchCage engine (pip install -e . in the PatchCage repo) or set ${ENGINE_ENV} to the executable.`;

export const DOCKER_DOWN_HINT =
	"Docker is required for /sandbox. Start the daemon and retry; the unsandboxed agent is unaffected.";

/** Resolve finding + manifest paths. Pure; `readdir` is injectable for tests. */
export function resolveSandboxInputs(
	repoRoot: string,
	findingArg: string | undefined,
	readdir: (dir: string) => string[] = safeReaddir,
): { ok: true; finding: string; manifest: string } | { ok: false; error: string } {
	let finding: string;
	if (findingArg?.trim()) {
		finding = isAbsolute(findingArg) ? findingArg : resolve(repoRoot, findingArg.trim());
	} else {
		const dir = join(repoRoot, "manifests");
		const candidates = readdir(dir).filter((name) => /\.finding\.(ya?ml|json)$/.test(name));
		if (candidates.length !== 1) {
			return {
				ok: false,
				error: `${candidates.length === 0 ? "No" : "Multiple"} manifests/*.finding.yml under ${repoRoot}. ${SANDBOX_USAGE}`,
			};
		}
		finding = join(dir, candidates[0]);
	}
	const manifest = siblingManifest(finding);
	if (!manifest) return { ok: false, error: `Finding must be named X.finding.{yml,yaml,json}: ${finding}` };
	return { ok: true, finding, manifest };
}

/** `file_path` from a finding JSON object or a YAML `file_path:` line. No YAML parser. */
export function findingFilePath(contents: string): string | undefined {
	const trimmed = contents.trim();
	if (trimmed.startsWith("{")) {
		try {
			const parsed = JSON.parse(trimmed) as { file_path?: unknown };
			if (typeof parsed.file_path === "string") {
				const path = parsed.file_path.trim();
				return path || undefined;
			}
		} catch {
			return undefined;
		}
		return undefined;
	}
	for (const line of contents.split(/\r?\n/)) {
		const match = /^\s*file_path:\s*(?:["']([^"']+)["']|(\S+))\s*(?:#.*)?$/.exec(line);
		if (!match) continue;
		const path = (match[1] ?? match[2]).trim();
		if (path && path !== "|" && path !== ">") return path;
	}
	return undefined;
}

/** Resolve `rel` under `root`; undefined if absolute, empty, or it escapes `root`. */
export function pathUnderRoot(root: string, rel: string): string | undefined {
	if (!rel || isAbsolute(rel) || rel.includes("\0")) return undefined;
	const rootAbs = resolve(root);
	const abs = resolve(rootAbs, rel);
	const prefix = rootAbs.endsWith(sep) ? rootAbs : rootAbs + sep;
	if (abs === rootAbs || abs.startsWith(prefix)) return abs;
	return undefined;
}

export function assertSandboxTarget(
	repoRoot: string,
	finding: string,
	manifest: string,
	io?: {
		exists?: (path: string) => boolean;
		readFile?: (path: string) => string;
	},
): { ok: true } | { ok: false; error: string } {
	const exists = io?.exists ?? existsSync;
	const readFile = io?.readFile ?? ((path: string) => readFileSync(path, "utf8"));
	if (!exists(finding)) return { ok: false, error: `Finding not found: ${finding}` };
	if (!exists(manifest)) return { ok: false, error: `Manifest not found: ${manifest}` };
	let contents: string;
	try {
		contents = readFile(finding);
	} catch {
		return { ok: false, error: `Finding not found: ${finding}` };
	}
	const rel = findingFilePath(contents);
	if (!rel) return { ok: false, error: `Finding has no file_path: ${finding}` };
	const abs = pathUnderRoot(repoRoot, rel);
	if (!abs) {
		return { ok: false, error: `Finding file_path ${rel} is outside ${repoRoot}` };
	}
	if (!exists(abs)) {
		return {
			ok: false,
			error: `Finding file_path ${rel} is not in ${repoRoot}. /sandbox snapshots the current git root. For the Flask demo: python scripts/create_demo_repo.py <dir> && cd <dir>, then /sandbox.`,
		};
	}
	return { ok: true };
}

export function siblingManifest(finding: string): string | undefined {
	const match = /^(.*)\.finding(\.(?:ya?ml|json))$/.exec(basename(finding));
	if (!match) return undefined;
	return join(dirname(finding), `${match[1]}${match[2]}`);
}

function safeReaddir(dir: string): string[] {
	try {
		return readdirSync(dir);
	} catch {
		return [];
	}
}

/** `PATCHCAGE_ENGINE` wins; otherwise rely on PATH. No `python -m` fallback (fail closed). */
export function resolveEngineBinary(env: NodeJS.ProcessEnv = process.env): string {
	const override = env[ENGINE_ENV]?.trim();
	return override || ENGINE_BIN;
}

export interface EngineEvent {
	event_type: string;
	phase?: string;
	payload?: Record<string, unknown>;
	status?: string;
	run_dir?: string;
	out_dir?: string;
	diff_ref?: string | null;
	detail?: string;
	checks?: Array<{ name: string; status: string; summary: string }>;
}

/** Parse one stdout line. Non-JSON lines are returned as `undefined` (engine keeps diagnostics on stderr). */
export function parseEngineLine(line: string): EngineEvent | undefined {
	const trimmed = line.trim();
	if (!trimmed.startsWith("{")) return undefined;
	try {
		const parsed = JSON.parse(trimmed) as unknown;
		if (parsed && typeof parsed === "object" && typeof (parsed as EngineEvent).event_type === "string") {
			return parsed as EngineEvent;
		}
	} catch {
		// fall through
	}
	return undefined;
}

/** Human line for the TUI, or `undefined` for events we do not narrate. */
export function narrateEvent(event: EngineEvent): string | undefined {
	switch (event.event_type) {
		case "phase":
			return `sandbox: ${String(event.payload?.phase ?? event.phase ?? "?")}`;
		case "check_result": {
			const p = event.payload ?? {};
			return `check ${String(p.name)}: ${String(p.status)} — ${String(p.summary ?? "")}`.trimEnd();
		}
		case "run_finished":
			return `sandbox finished: ${String(event.payload?.detail ?? "")}`.trimEnd();
		case "result":
			return `result: ${String(event.status)}${event.detail ? ` — ${event.detail}` : ""}`;
		default:
			return undefined;
	}
}

export interface EngineRunOutcome {
	code: number | null;
	signal: NodeJS.Signals | null;
	result?: EngineEvent;
	stderr: string;
}

export interface EngineRunOptions {
	bin: string;
	args: string[];
	cwd: string;
	env: NodeJS.ProcessEnv;
	onEvent: (event: EngineEvent) => void;
	signal?: AbortSignal;
	spawnImpl?: typeof spawn;
	onSpawn?: (child: ChildProcess) => void;
}

/** Spawn the engine, stream JSON lines, and resolve with the final `result` event. */
export function runEngine(options: EngineRunOptions): Promise<EngineRunOutcome> {
	const spawner = options.spawnImpl ?? spawn;
	return new Promise((resolvePromise) => {
		const child: ChildProcess = spawner(options.bin, options.args, {
			cwd: options.cwd,
			env: options.env,
			stdio: ["ignore", "pipe", "pipe"],
		});
		let buffer = "";
		let stderr = "";
		let result: EngineEvent | undefined;
		const feed = (chunk: string) => {
			buffer += chunk;
			let nl = buffer.indexOf("\n");
			while (nl >= 0) {
				const event = parseEngineLine(buffer.slice(0, nl));
				buffer = buffer.slice(nl + 1);
				if (event) {
					if (event.event_type === "result") result = event;
					options.onEvent(event);
				}
				nl = buffer.indexOf("\n");
			}
		};
		child.stdout?.setEncoding("utf-8").on("data", feed);
		child.stderr?.setEncoding("utf-8").on("data", (chunk: string) => {
			stderr += chunk;
		});
		const onAbort = () => child.kill("SIGINT");
		options.signal?.addEventListener("abort", onAbort, { once: true });
		if (options.signal?.aborted) onAbort();
		options.onSpawn?.(child);
		child.on("error", (error: NodeJS.ErrnoException) => {
			options.signal?.removeEventListener("abort", onAbort);
			stderr += error.code === "ENOENT" ? ENGINE_MISSING : error.message;
			resolvePromise({ code: null, signal: null, stderr });
		});
		child.on("close", (code, signal) => {
			options.signal?.removeEventListener("abort", onAbort);
			if (buffer) feed("\n");
			resolvePromise({ code, signal, result, stderr });
		});
	});
}

export function buildRunArgs(input: {
	repo: string;
	manifest: string;
	finding: string;
	runDir: string;
	modelEndpoint: string;
	modelId: string;
}): string[] {
	return [
		"run",
		"--repo",
		input.repo,
		"--manifest",
		input.manifest,
		"--finding",
		input.finding,
		"--run-dir",
		input.runDir,
		"--model-endpoint",
		input.modelEndpoint,
		"--model-id",
		input.modelId,
	];
}

export function buildExportArgs(runDir: string, outDir: string, digest?: string): string[] {
	return ["export", "--run", runDir, "--out", outDir, ...(digest ? ["--expected-sha256", digest] : [])];
}

/** True for parent env keys that must not reach the engine child. */
export function isCredentialEnvKey(key: string): boolean {
	const upper = key.toUpperCase();
	if (upper === MODEL_API_KEY_ENV || upper === MODEL_HTTP_HEADERS_ENV) return true;
	if (upper === "AWS_SECRET_ACCESS_KEY" || upper === "AWS_SESSION_TOKEN") return true;
	if (/API[_-]?KEY/.test(upper)) return true;
	if (/(^|_)(TOKEN|SECRET|PASSWORD|PASSPHRASE)$/.test(upper)) return true;
	if (upper.includes("_SECRET_")) return true;
	return false;
}

/**
 * Child env: inherit PATH/HOME/DOCKER_*, strip credential keys, then set the
 * model key only as PATCHCAGE_MODEL_API_KEY (and optional extra headers).
 * Never returned to the UI.
 */
export function engineEnv(
	base: NodeJS.ProcessEnv,
	apiKey: string | undefined,
	extraHeaders?: Record<string, string>,
): NodeJS.ProcessEnv {
	const env: NodeJS.ProcessEnv = {};
	for (const [key, value] of Object.entries(base)) {
		if (value === undefined || isCredentialEnvKey(key)) continue;
		env[key] = value;
	}
	if (apiKey) env[MODEL_API_KEY_ENV] = apiKey;
	if (extraHeaders && Object.keys(extraHeaders).length > 0) {
		env[MODEL_HTTP_HEADERS_ENV] = JSON.stringify(extraHeaders);
	}
	return env;
}

/** Docker-down detection from engine stderr; used only to add a hint, never to change behavior. */
export function isDockerDown(stderr: string): boolean {
	return /docker (daemon unavailable|error)/i.test(stderr);
}

export function describeFailure(outcome: EngineRunOutcome): string {
	const tail = outcome.stderr.trim().split("\n").filter(Boolean).slice(-3).join("\n");
	const head = outcome.result
		? `sandbox run ended: ${outcome.result.status}`
		: `${ENGINE_BIN} exited ${outcome.code ?? outcome.signal ?? "abnormally"}`;
	const hint = isDockerDown(outcome.stderr) ? `\n${DOCKER_DOWN_HINT}` : "";
	return tail ? `${head}\n${tail}${hint}` : `${head}${hint}`;
}

export function isRepoRoot(dir: string): boolean {
	return existsSync(join(dir, ".git"));
}

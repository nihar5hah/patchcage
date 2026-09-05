import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, rename, unlink, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { ModelConfig, type ModelsJsonProvider } from "./model-config.ts";

export type DisclosureDecision = "allow" | "need-interactive-prompt" | "fail-closed";

export const UNSANDBOXED_DISCLOSURE_NOTICE = `This PatchCage agent session is not sandboxed. It can read, write, and run
commands with your user permissions. Use /sandbox to run a finding through the
Docker cage instead; only that path is verified and export-gated.

Continue only if you accept that.`;

export const UNSANDBOXED_DISCLOSURE_FAIL_CLOSED =
	"This PatchCage agent session is not sandboxed and can use tools with your user permissions. Acknowledge once in interactive mode, or pass --ack-unsandboxed (this run only) or --no-tools.";

export const UNSANDBOXED_DISCLOSURE_DECLINED = "Declined unsandboxed agent mode. Exiting.";

export const PATCHCAGE_SYSTEM_APPENDIX = `You are running as PatchCage's coding agent in unsandboxed agent mode.
This session is not sandboxed. Do not claim your own tools are sandboxed. The user can run /sandbox to send a finding through the PatchCage Docker cage; you cannot invoke it.

Authorized defensive security work only: review findings, explain issues, harden code, and write PoCs the user requested for systems they own or are authorized to test. Refuse criminal activity, unauthorized access, and exploitation of third-party systems.`;

export const LOCAL_PROBE_BASE_URLS = [
	"http://127.0.0.1:11434/v1",
	"http://127.0.0.1:8080/v1",
	"http://127.0.0.1:8000/v1",
] as const;

export type LocalPresetId = "ollama" | "llamacpp" | "vllm";

export const LOCAL_MODEL_PRESETS: Record<
	LocalPresetId,
	{ providerId: string; baseUrl: string; apiKey: string; label: string }
> = {
	ollama: {
		providerId: "ollama",
		baseUrl: "http://127.0.0.1:11434/v1",
		apiKey: "ollama",
		label: "Ollama (127.0.0.1:11434)",
	},
	llamacpp: {
		providerId: "llamacpp",
		baseUrl: "http://127.0.0.1:8080/v1",
		apiKey: "llamacpp",
		label: "llama.cpp (127.0.0.1:8080)",
	},
	vllm: {
		providerId: "vllm",
		baseUrl: "http://127.0.0.1:8000/v1",
		apiKey: "vllm",
		label: "vLLM (127.0.0.1:8000)",
	},
};

export const HOSTED_PROVIDER_ID = "openai-compat";

const LOCAL_COMPAT = { supportsDeveloperRole: false, supportsReasoningEffort: false };

export function decideUnsandboxedDisclosure(input: {
	acknowledged: boolean;
	toolsEnabled: boolean;
	interactive: boolean;
	ackThisRun: boolean;
}): DisclosureDecision {
	if (!input.toolsEnabled) return "allow";
	if (input.acknowledged || input.ackThisRun) return "allow";
	if (input.interactive) return "need-interactive-prompt";
	return "fail-closed";
}

export function shouldSkipUnsandboxedDisclosure(parsed: {
	help?: boolean;
	listModels?: string | true;
	version?: boolean;
}): boolean {
	return parsed.help === true || parsed.listModels !== undefined || parsed.version === true;
}

export function withBundledPromptTemplates(
	existing: string[] | undefined,
	bundledDir: string,
	enabled: boolean,
): string[] | undefined {
	if (!enabled || !existsSync(bundledDir)) return existing;
	return [...(existing ?? []), bundledDir];
}

export function normalizeHostedApiKeyRef(input: string): { ok: true; value: string } | { ok: false; error: string } {
	const trimmed = input.trim();
	if (!trimmed) return { ok: false, error: "Environment variable name is required" };
	if (/\s/.test(trimmed) || trimmed.toLowerCase().startsWith("sk-")) {
		return { ok: false, error: "Paste the environment variable name, not an API key" };
	}
	const name = trimmed.startsWith("$") ? trimmed.slice(1) : trimmed;
	if (!/^[A-Z_][A-Z0-9_]*$/.test(name)) {
		return { ok: false, error: "Environment variable name must match [A-Z_][A-Z0-9_]*" };
	}
	return { ok: true, value: `$${name}` };
}

export function validateHostedBaseUrl(input: string): { ok: true; url: string } | { ok: false; error: string } {
	const trimmed = input.trim();
	if (!trimmed) return { ok: false, error: "Base URL is required" };
	let parsed: URL;
	try {
		parsed = new URL(trimmed);
	} catch {
		return { ok: false, error: "Base URL must be a valid http(s) URL" };
	}
	if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
		return { ok: false, error: "Base URL must be http or https" };
	}
	if (parsed.username || parsed.password) {
		return { ok: false, error: "Base URL must not include credentials" };
	}
	return { ok: true, url: trimmed };
}

export function normalizeLocalBaseUrl(baseUrl: string): string {
	return baseUrl.replace(/\/+$/, "");
}

export function isAllowlistedLocalBaseUrl(baseUrl: string): boolean {
	return (LOCAL_PROBE_BASE_URLS as readonly string[]).includes(normalizeLocalBaseUrl(baseUrl));
}

export async function probeLocalModels(baseUrl: string): Promise<string[]> {
	const normalized = normalizeLocalBaseUrl(baseUrl);
	if (!isAllowlistedLocalBaseUrl(normalized)) return [];
	try {
		const response = await fetch(`${normalized}/models`, { signal: AbortSignal.timeout(1000) });
		if (!response.ok) return [];
		const body = (await response.json()) as { data?: Array<{ id?: unknown }> };
		if (!Array.isArray(body.data)) return [];
		return body.data.map((item) => item.id).filter((id): id is string => typeof id === "string" && id.length > 0);
	} catch {
		return [];
	}
}

export function localPresetProvider(
	preset: (typeof LOCAL_MODEL_PRESETS)[LocalPresetId],
	modelId: string,
): ModelsJsonProvider {
	return {
		baseUrl: preset.baseUrl,
		api: "openai-completions",
		apiKey: preset.apiKey,
		compat: LOCAL_COMPAT,
		models: [{ id: modelId }],
	};
}

export function hostedPresetProvider(baseUrl: string, apiKeyRef: string, modelId: string): ModelsJsonProvider {
	return {
		baseUrl,
		api: "openai-completions",
		apiKey: apiKeyRef,
		models: [{ id: modelId }],
	};
}

export function mergeProviderIntoConfig(
	existing: ModelsJsonProvider | undefined,
	incoming: ModelsJsonProvider,
): ModelsJsonProvider {
	const byId = new Map((existing?.models ?? []).map((model) => [model.id, model]));
	for (const model of incoming.models ?? []) {
		byId.set(model.id, model);
	}
	return {
		...existing,
		...incoming,
		models: [...byId.values()],
	};
}

export async function mergeAndWriteModelsJson(
	path: string,
	providerId: string,
	incoming: ModelsJsonProvider,
): Promise<{ ok: true } | { ok: false; error: string }> {
	const loaded = await ModelConfig.load(path);
	const loadError = loaded.getError();
	if (loadError) return { ok: false, error: loadError };

	const providers: Record<string, ModelsJsonProvider> = {};
	for (const id of loaded.getProviderIds()) {
		const provider = loaded.getProvider(id);
		if (provider) providers[id] = structuredClone(provider);
	}
	providers[providerId] = mergeProviderIntoConfig(providers[providerId], incoming);

	try {
		await writeModelsJsonAtomic(path, { providers });
		return { ok: true };
	} catch (error) {
		return { ok: false, error: error instanceof Error ? error.message : String(error) };
	}
}

async function writeModelsJsonAtomic(
	path: string,
	data: { providers: Record<string, ModelsJsonProvider> },
): Promise<void> {
	const dir = dirname(path);
	await mkdir(dir, { recursive: true });
	const tmp = join(dir, `.models.json.${randomBytes(8).toString("hex")}.tmp`);
	await writeFile(tmp, `${JSON.stringify(data, null, 2)}\n`, "utf-8");
	try {
		await rename(tmp, path);
	} catch (error) {
		await unlink(tmp).catch(() => {});
		throw error;
	}
}

export interface PresetSession {
	modelRuntime: {
		refresh(options: { allowNetwork: boolean; providers?: string[] }): Promise<unknown>;
		getModel(providerId: string, modelId: string): unknown;
	};
	setModel(model: unknown, options?: { persist?: boolean }): Promise<void>;
}

export async function applyPresetToSession(session: PresetSession, providerId: string, modelId: string): Promise<void> {
	await session.modelRuntime.refresh({ allowNetwork: false, providers: [providerId] });
	const model = session.modelRuntime.getModel(providerId, modelId);
	if (!model) {
		throw new Error(`Model ${providerId}/${modelId} not found after writing models.json`);
	}
	await session.setModel(model, { persist: true });
}

export type ModelOnboardingResult =
	| { kind: "applied"; providerId: string; modelId: string }
	| { kind: "skip"; persist: boolean }
	| { kind: "error"; error: string };

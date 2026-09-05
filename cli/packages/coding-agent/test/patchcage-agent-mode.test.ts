import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import * as fsPromises from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("node:fs/promises", async (importOriginal) => {
	const actual = await importOriginal<typeof import("node:fs/promises")>();
	return {
		...actual,
		rename: vi.fn(actual.rename),
	};
});

import {
	applyPresetToSession,
	decideUnsandboxedDisclosure,
	hostedPresetProvider,
	LOCAL_MODEL_PRESETS,
	localPresetProvider,
	mergeAndWriteModelsJson,
	normalizeHostedApiKeyRef,
	PATCHCAGE_SYSTEM_APPENDIX,
	probeLocalModels,
	shouldSkipUnsandboxedDisclosure,
	validateHostedBaseUrl,
	withBundledPromptTemplates,
} from "../src/core/patchcage-agent-mode.ts";
import { InMemorySettingsStorage, SettingsManager } from "../src/core/settings-manager.ts";

describe("decideUnsandboxedDisclosure", () => {
	it("does not let project config acknowledge host tool access", () => {
		const storage = new InMemorySettingsStorage();
		storage.withLock("project", () => JSON.stringify({ unsandboxedDisclosureAcknowledged: true }));
		const settings = SettingsManager.fromStorage(storage);
		expect(settings.getUnsandboxedDisclosureAcknowledged()).toBe(false);
	});
	it("allows after a saved ack", () => {
		expect(
			decideUnsandboxedDisclosure({
				acknowledged: true,
				toolsEnabled: true,
				interactive: true,
				ackThisRun: false,
			}),
		).toBe("allow");
	});

	it("prompts once in interactive mode when tools are on", () => {
		expect(
			decideUnsandboxedDisclosure({
				acknowledged: false,
				toolsEnabled: true,
				interactive: true,
				ackThisRun: false,
			}),
		).toBe("need-interactive-prompt");
	});

	it("fail-closes non-interactive tool runs without prior ack", () => {
		expect(
			decideUnsandboxedDisclosure({
				acknowledged: false,
				toolsEnabled: true,
				interactive: false,
				ackThisRun: false,
			}),
		).toBe("fail-closed");
	});

	it("allows --no-tools without ack", () => {
		expect(
			decideUnsandboxedDisclosure({
				acknowledged: false,
				toolsEnabled: false,
				interactive: false,
				ackThisRun: false,
			}),
		).toBe("allow");
	});

	it("allows --ack-unsandboxed for this run only", () => {
		expect(
			decideUnsandboxedDisclosure({
				acknowledged: false,
				toolsEnabled: true,
				interactive: false,
				ackThisRun: true,
			}),
		).toBe("allow");
	});
});

describe("shouldSkipUnsandboxedDisclosure", () => {
	it("skips --help, --list-models, and --version", () => {
		expect(shouldSkipUnsandboxedDisclosure({ help: true })).toBe(true);
		expect(shouldSkipUnsandboxedDisclosure({ listModels: true })).toBe(true);
		expect(shouldSkipUnsandboxedDisclosure({ listModels: "gpt" })).toBe(true);
		expect(shouldSkipUnsandboxedDisclosure({ version: true })).toBe(true);
		expect(shouldSkipUnsandboxedDisclosure({})).toBe(false);
	});
});

describe("withBundledPromptTemplates", () => {
	it("appends bundled last so user/project/cli win", () => {
		const bundled = mkdtempSync(join(tmpdir(), "patchcage-prompts-"));
		expect(withBundledPromptTemplates(["/user", "/cli"], bundled, true)).toEqual(["/user", "/cli", bundled]);
	});

	it("does not append when templates are disabled", () => {
		expect(withBundledPromptTemplates(["/cli"], "/bundled", false)).toEqual(["/cli"]);
	});

	it("does not append a missing bundled directory", () => {
		expect(withBundledPromptTemplates(["/cli"], join(tmpdir(), "patchcage-missing-prompts"), true)).toEqual(["/cli"]);
	});
});

describe("validateHostedBaseUrl", () => {
	it("accepts http(s) URLs and rejects credentials", () => {
		expect(validateHostedBaseUrl("https://api.example.com/v1")).toEqual({
			ok: true,
			url: "https://api.example.com/v1",
		});
		expect(validateHostedBaseUrl("ftp://api.example.com").ok).toBe(false);
		expect(validateHostedBaseUrl("https://user:secret@api.example.com/v1").ok).toBe(false);
	});
});

describe("PATCHCAGE_SYSTEM_APPENDIX", () => {
	it("states unsandboxed defensive-only agent mode", () => {
		expect(PATCHCAGE_SYSTEM_APPENDIX).toContain("not sandboxed");
		expect(PATCHCAGE_SYSTEM_APPENDIX).toContain("/sandbox");
		expect(PATCHCAGE_SYSTEM_APPENDIX).toContain("Authorized defensive");
		expect(PATCHCAGE_SYSTEM_APPENDIX).toContain("unauthorized access");
	});
});

describe("applyPresetToSession", () => {
	it("refreshes offline then persists the selected model", async () => {
		const model = { id: "llama3" };
		const refresh = vi.fn(async () => {});
		const getModel = vi.fn(() => model);
		const setModel = vi.fn(async () => {});
		await applyPresetToSession({ modelRuntime: { refresh, getModel }, setModel }, "ollama", "llama3");
		expect(refresh).toHaveBeenCalledWith({ allowNetwork: false, providers: ["ollama"] });
		expect(getModel).toHaveBeenCalledWith("ollama", "llama3");
		expect(setModel).toHaveBeenCalledWith(model, { persist: true });
	});
});

describe("normalizeHostedApiKeyRef", () => {
	it("stores $ENV_NAME and rejects pasted secrets", () => {
		expect(normalizeHostedApiKeyRef("OPENAI_API_KEY")).toEqual({ ok: true, value: "$OPENAI_API_KEY" });
		expect(normalizeHostedApiKeyRef("$OPENAI_API_KEY")).toEqual({ ok: true, value: "$OPENAI_API_KEY" });
		expect(normalizeHostedApiKeyRef("sk-live-secret").ok).toBe(false);
		expect(normalizeHostedApiKeyRef("openai-api-key").ok).toBe(false);
	});
});

describe("mergeAndWriteModelsJson", () => {
	it("merges a model into an existing provider without dropping others", async () => {
		const dir = mkdtempSync(join(tmpdir(), "patchcage-models-"));
		const path = join(dir, "models.json");
		writeFileSync(
			path,
			JSON.stringify({
				providers: {
					ollama: {
						baseUrl: "http://127.0.0.1:11434/v1",
						api: "openai-completions",
						apiKey: "ollama",
						models: [{ id: "keep-me" }],
					},
					other: {
						baseUrl: "http://127.0.0.1:8000/v1",
						api: "openai-completions",
						apiKey: "vllm",
						models: [{ id: "untouched" }],
					},
				},
			}),
		);

		const result = await mergeAndWriteModelsJson(
			path,
			"ollama",
			localPresetProvider(LOCAL_MODEL_PRESETS.ollama, "new-tag"),
		);
		expect(result).toEqual({ ok: true });

		const written = JSON.parse(readFileSync(path, "utf-8")) as {
			providers: Record<string, { models: Array<{ id: string }>; apiKey: string }>;
		};
		expect(written.providers.other.models.map((m) => m.id)).toEqual(["untouched"]);
		expect(written.providers.ollama.models.map((m) => m.id).sort()).toEqual(["keep-me", "new-tag"]);
	});

	it("refuses to clobber a malformed models.json", async () => {
		const dir = mkdtempSync(join(tmpdir(), "patchcage-models-"));
		const path = join(dir, "models.json");
		writeFileSync(path, "{");
		const result = await mergeAndWriteModelsJson(
			path,
			"ollama",
			localPresetProvider(LOCAL_MODEL_PRESETS.ollama, "llama3"),
		);
		expect(result.ok).toBe(false);
		expect(readFileSync(path, "utf-8")).toBe("{");
	});

	it("writes hosted env refs, never a pasted secret", async () => {
		const dir = mkdtempSync(join(tmpdir(), "patchcage-models-"));
		const path = join(dir, "models.json");
		const ref = normalizeHostedApiKeyRef("OPENAI_API_KEY");
		if (!ref.ok) throw new Error(ref.error);
		const result = await mergeAndWriteModelsJson(
			path,
			"openai-compat",
			hostedPresetProvider("https://api.example.com/v1", ref.value, "gpt-4o"),
		);
		expect(result).toEqual({ ok: true });
		const raw = readFileSync(path, "utf-8");
		expect(raw).toContain("$OPENAI_API_KEY");
		expect(raw).not.toContain("sk-");
	});

	it.each(["EEXIST", "EPERM"])("preserves the destination when rename fails with %s", async (code) => {
		const dir = mkdtempSync(join(tmpdir(), "patchcage-models-"));
		const path = join(dir, "models.json");
		writeFileSync(path, `${JSON.stringify({ providers: {} }, null, 2)}\n`);
		vi.mocked(fsPromises.rename).mockClear();
		vi.mocked(fsPromises.rename).mockImplementationOnce(async () => {
			const err = new Error("exists") as NodeJS.ErrnoException;
			err.code = code;
			throw err;
		});
		const result = await mergeAndWriteModelsJson(
			path,
			"ollama",
			localPresetProvider(LOCAL_MODEL_PRESETS.ollama, "llama3"),
		);
		expect(result.ok).toBe(false);
		expect(fsPromises.rename).toHaveBeenCalledTimes(1);
		const written = JSON.parse(readFileSync(path, "utf-8")) as {
			providers: Record<string, { models: Array<{ id: string }> }>;
		};
		expect(written.providers).toEqual({});
	});
});

describe("probeLocalModels", () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it("does not fetch non-allowlisted URLs including localhost", async () => {
		const fetchMock = vi.fn();
		vi.stubGlobal("fetch", fetchMock);
		expect(await probeLocalModels("http://localhost:11434/v1")).toEqual([]);
		expect(await probeLocalModels("http://evil.example/v1")).toEqual([]);
		expect(fetchMock).not.toHaveBeenCalled();
	});
});

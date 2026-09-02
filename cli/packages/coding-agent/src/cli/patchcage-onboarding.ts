import {
	HOSTED_PROVIDER_ID,
	hostedPresetProvider,
	LOCAL_MODEL_PRESETS,
	type LocalPresetId,
	localPresetProvider,
	type ModelOnboardingResult,
	mergeAndWriteModelsJson,
	normalizeHostedApiKeyRef,
	probeLocalModels,
	UNSANDBOXED_DISCLOSURE_NOTICE,
	validateHostedBaseUrl,
} from "../core/patchcage-agent-mode.ts";
import type { SettingsManager } from "../core/settings-manager.ts";
import { showStartupInput, showStartupSelector } from "./startup-ui.ts";

export async function promptUnsandboxedDisclosure(settingsManager: SettingsManager): Promise<boolean> {
	const choice = await showStartupSelector(settingsManager, UNSANDBOXED_DISCLOSURE_NOTICE, [
		{ label: "Continue", value: true },
		{ label: "Decline", value: false },
	]);
	return choice === true;
}

export async function runModelPresetOnboarding(
	settingsManager: SettingsManager,
	options: { persistSkip: boolean; modelsPath: string },
): Promise<ModelOnboardingResult> {
	const choice = await showStartupSelector<LocalPresetId | "hosted" | "skip">(
		settingsManager,
		"No usable model yet. Choose a preset:",
		[
			{ label: LOCAL_MODEL_PRESETS.ollama.label, value: "ollama" },
			{ label: LOCAL_MODEL_PRESETS.llamacpp.label, value: "llamacpp" },
			{ label: LOCAL_MODEL_PRESETS.vllm.label, value: "vllm" },
			{ label: "Hosted OpenAI-compatible", value: "hosted" },
			{ label: "Skip for now", value: "skip" },
		],
	);
	if (choice === undefined) return { kind: "skip", persist: false };
	if (choice === "skip") return { kind: "skip", persist: options.persistSkip };

	if (choice === "hosted") {
		return writeHostedPreset(settingsManager, options.modelsPath);
	}
	return writeLocalPreset(settingsManager, choice, options.modelsPath);
}

async function writeLocalPreset(
	settingsManager: SettingsManager,
	presetId: LocalPresetId,
	modelsPath: string,
): Promise<ModelOnboardingResult> {
	const preset = LOCAL_MODEL_PRESETS[presetId];
	const probed = await probeLocalModels(preset.baseUrl);
	const modelId =
		probed.length > 0
			? await showStartupSelector(
					settingsManager,
					`Select a model on ${preset.label}:`,
					probed.map((id) => ({ label: id, value: id })),
				)
			: await showStartupInput(settingsManager, `Model id for ${preset.label}`, "llama3");
	if (!modelId?.trim()) return { kind: "skip", persist: false };

	const write = await mergeAndWriteModelsJson(
		modelsPath,
		preset.providerId,
		localPresetProvider(preset, modelId.trim()),
	);
	if (!write.ok) return { kind: "error", error: write.error };
	return { kind: "applied", providerId: preset.providerId, modelId: modelId.trim() };
}

async function writeHostedPreset(settingsManager: SettingsManager, modelsPath: string): Promise<ModelOnboardingResult> {
	const baseRaw = await showStartupInput(settingsManager, "OpenAI-compatible base URL", "https://api.example.com/v1");
	if (baseRaw === undefined) return { kind: "skip", persist: false };
	const base = validateHostedBaseUrl(baseRaw);
	if (!base.ok) return { kind: "error", error: base.error };

	const envRaw = await showStartupInput(settingsManager, "API key environment variable name", "OPENAI_API_KEY");
	if (envRaw === undefined) return { kind: "skip", persist: false };
	const envVar = normalizeHostedApiKeyRef(envRaw);
	if (!envVar.ok) return { kind: "error", error: envVar.error };

	const modelId = await showStartupInput(settingsManager, "Model id", "gpt-4o-mini");
	if (!modelId?.trim()) return { kind: "skip", persist: false };

	const write = await mergeAndWriteModelsJson(
		modelsPath,
		HOSTED_PROVIDER_ID,
		hostedPresetProvider(base.url, envVar.value, modelId.trim()),
	);
	if (!write.ok) return { kind: "error", error: write.error };
	return { kind: "applied", providerId: HOSTED_PROVIDER_ID, modelId: modelId.trim() };
}

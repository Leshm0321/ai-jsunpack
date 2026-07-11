import type { SettingsSection } from "./routes";
import type { TranslationKey } from "./i18n";

export type SettingSource = "built-in" | "file" | "environment" | "system" | "project" | "job";

export interface EffectiveConfigResponse {
  config?: Record<string, unknown>;
  effective?: Record<string, unknown>;
  lockedFields?: string[];
  restartRequired?: boolean;
  restartRequiredFields?: string[];
  sources?: Record<string, SettingSource | string>;
  fingerprint?: string;
}

export interface RuntimeSettingsResponse {
  settings?: Record<string, unknown>;
  values?: Record<string, unknown>;
  revision?: number;
  lockedFields?: string[];
  sources?: Record<string, SettingSource | string>;
}

export interface ProviderReadinessItem {
  id?: string;
  mode: "cloud" | "local" | "desensitized" | string;
  provider: string;
  model: string | null;
  endpointType: string;
  credentialConfigured: boolean;
  status: "ready" | "misconfigured" | "unavailable" | "unknown" | string;
  detail?: string;
  checkedAt?: string;
  issues?: string[];
  secretRefConfigured?: boolean;
}

export type ProviderReadinessResponse = ProviderReadinessItem[] | {
  providers?: ProviderReadinessItem[];
  cloud?: Partial<ProviderReadinessItem>;
  local?: Partial<ProviderReadinessItem>;
  desensitized?: Partial<ProviderReadinessItem>;
};

export type SettingsFieldKind = "boolean" | "number" | "select" | "text" | "textarea";

export interface SettingsFieldDefinition {
  descriptionKey: TranslationKey;
  key: string;
  kind: SettingsFieldKind;
  labelKey: TranslationKey;
  max?: number;
  min?: number;
  options?: Array<{ labelKey: TranslationKey; value: string }>;
  section: SettingsSection;
}

export const settingsFields: SettingsFieldDefinition[] = [
  {
    section: "ai",
    key: "ai.cloud.provider",
    labelKey: "settings.field.ai.cloud.provider.label",
    descriptionKey: "settings.field.ai.cloud.provider.description",
    kind: "text"
  },
  {
    section: "ai",
    key: "ai.cloud.model",
    labelKey: "settings.field.ai.cloud.model.label",
    descriptionKey: "settings.field.ai.cloud.model.description",
    kind: "text"
  },
  {
    section: "ai",
    key: "ai.cloud.baseUrl",
    labelKey: "settings.field.ai.cloud.baseUrl.label",
    descriptionKey: "settings.field.ai.cloud.baseUrl.description",
    kind: "text"
  },
  {
    section: "ai",
    key: "ai.cloud.apiKeySecretRef",
    labelKey: "settings.field.ai.cloud.secretRef.label",
    descriptionKey: "settings.field.ai.cloud.secretRef.description",
    kind: "text"
  },
  {
    section: "ai",
    key: "ai.local.provider",
    labelKey: "settings.field.ai.local.provider.label",
    descriptionKey: "settings.field.ai.local.provider.description",
    kind: "text"
  },
  {
    section: "ai",
    key: "ai.local.model",
    labelKey: "settings.field.ai.local.model.label",
    descriptionKey: "settings.field.ai.local.model.description",
    kind: "text"
  },
  {
    section: "ai",
    key: "ai.local.baseUrl",
    labelKey: "settings.field.ai.local.baseUrl.label",
    descriptionKey: "settings.field.ai.local.baseUrl.description",
    kind: "text"
  },
  {
    section: "ai",
    key: "ai.local.apiKeySecretRef",
    labelKey: "settings.field.ai.local.secretRef.label",
    descriptionKey: "settings.field.ai.local.secretRef.description",
    kind: "text"
  },
  {
    section: "agents",
    key: "agents.maxParallel",
    labelKey: "settings.field.agents.maxParallel.label",
    descriptionKey: "settings.field.agents.maxParallel.description",
    kind: "number",
    min: 1,
    max: 10
  },
  {
    section: "agents",
    key: "agents.enabled",
    labelKey: "settings.field.agents.enabled.label",
    descriptionKey: "settings.field.agents.enabled.description",
    kind: "boolean"
  },
  {
    section: "agents",
    key: "agents.contextBudget",
    labelKey: "settings.field.agents.contextBudget.label",
    descriptionKey: "settings.field.agents.contextBudget.description",
    kind: "number",
    min: 1000,
    max: 1000000
  },
  {
    section: "validation",
    key: "validation.minimumConfidence",
    labelKey: "settings.field.validation.minimumConfidence.label",
    descriptionKey: "settings.field.validation.minimumConfidence.description",
    kind: "number",
    min: 0,
    max: 1
  },
  {
    section: "validation",
    key: "validation.runTypecheck",
    labelKey: "settings.field.validation.runTypecheck.label",
    descriptionKey: "settings.field.validation.runTypecheck.description",
    kind: "boolean"
  },
  {
    section: "validation",
    key: "validation.runRuntimeCompare",
    labelKey: "settings.field.validation.runRuntimeCompare.label",
    descriptionKey: "settings.field.validation.runRuntimeCompare.description",
    kind: "boolean"
  }
];

export function settingsValues(response: RuntimeSettingsResponse | null): Record<string, unknown> {
  return flattenObject(response?.settings ?? response?.values ?? {});
}

export function effectiveValues(response: EffectiveConfigResponse | null): Record<string, unknown> {
  return flattenObject(response?.effective ?? response?.config ?? {});
}

export function normalizeProviderReadiness(response: ProviderReadinessResponse | null): ProviderReadinessItem[] {
  if (!response) {
    return [];
  }
  if (Array.isArray(response)) {
    return response;
  }
  if (Array.isArray(response.providers)) {
    return response.providers;
  }
  return (["cloud", "local", "desensitized"] as const).flatMap((mode) => {
    const item = response[mode];
    if (!item) {
      return [];
    }
    return [{
      id: item.id ?? mode,
      mode: item.mode ?? mode,
      provider: item.provider ?? "",
      model: item.model ?? null,
      endpointType: item.endpointType ?? "",
      credentialConfigured: item.credentialConfigured ?? false,
      status: item.status ?? "unknown",
      detail: item.detail,
      checkedAt: item.checkedAt
    }];
  });
}

export function nestedSettings(values: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [path, value] of Object.entries(values)) {
    const parts = path.split(".");
    let target = result;
    parts.forEach((part, index) => {
      if (index === parts.length - 1) {
        target[part] = value;
      } else {
        const next = target[part];
        if (!next || typeof next !== "object" || Array.isArray(next)) {
          target[part] = {};
        }
        target = target[part] as Record<string, unknown>;
      }
    });
  }
  return result;
}

function flattenObject(value: Record<string, unknown>, prefix = ""): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, child] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (child && typeof child === "object" && !Array.isArray(child)) {
      Object.assign(result, flattenObject(child as Record<string, unknown>, path));
    } else {
      result[path] = child;
    }
  }
  return result;
}

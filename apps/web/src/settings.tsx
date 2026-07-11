import { useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent } from "react";
import {
  Binary,
  Bot,
  Braces,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  FileCog,
  LockKeyhole,
  RefreshCw,
  Save,
  Settings2,
  ShieldCheck,
  SlidersHorizontal
} from "lucide-react";
import {
  API_PROJECT_ID,
  fetchEffectiveConfig,
  fetchProjectSettings,
  fetchProviderReadiness,
  fetchSystemSettings,
  updateProjectSettings,
  updateSystemSettings
} from "./api";
import { useLocalization } from "./i18n";
import type { TranslationKey } from "./i18n";
import {
  useActiveNavMotion,
  useApplicationMotion,
  useApplicationScrollMotion,
  useMetricMotion
} from "./app-motion";
import type { AppRoute, ParsedAppRoute, SettingsSection } from "./routes";
import {
  effectiveValues,
  nestedSettings,
  normalizeProviderReadiness,
  settingsFields,
  settingsValues
} from "./settings-types";
import type {
  EffectiveConfigResponse,
  ProviderReadinessResponse,
  RuntimeSettingsResponse,
  SettingsFieldDefinition
} from "./settings-types";
import { LanguageToggle } from "./workbench-shell";

interface SettingsCenterProps {
  onNavigate: (route: AppRoute) => void;
  route: Extract<ParsedAppRoute, { kind: "settings" }>;
}

const sectionMeta: Record<SettingsSection, { descriptionKey: TranslationKey; icon: typeof Settings2; labelKey: TranslationKey }> = {
  general: { labelKey: "settings.section.general.label", descriptionKey: "settings.section.general.description", icon: Settings2 },
  ai: { labelKey: "settings.section.ai.label", descriptionKey: "settings.section.ai.description", icon: Bot },
  agents: { labelKey: "settings.section.agents.label", descriptionKey: "settings.section.agents.description", icon: SlidersHorizontal },
  security: { labelKey: "settings.section.security.label", descriptionKey: "settings.section.security.description", icon: ShieldCheck },
  validation: { labelKey: "settings.section.validation.label", descriptionKey: "settings.section.validation.description", icon: CheckCircle2 }
};

export function SettingsCenter({ onNavigate, route }: SettingsCenterProps) {
  const { language, setLanguage, t } = useLocalization();
  const rootRef = useRef<HTMLDivElement>(null);
  const [effective, setEffective] = useState<EffectiveConfigResponse | null>(null);
  const [settings, setSettings] = useState<RuntimeSettingsResponse | null>(null);
  const [readiness, setReadiness] = useState<ProviderReadinessResponse | null>(null);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<string | null>(null);
  const projectId = route.projectId;
  const scopeLabel = projectId ? `${t("settings.projectScope")} ${projectId}` : t("settings.system");

  const load = async () => {
    setLoading(true);
    setLoadError(null);
    const settingsRequest = projectId ? fetchProjectSettings(projectId) : fetchSystemSettings();
    const [effectiveResult, settingsResult, readinessResult] = await Promise.allSettled([
      fetchEffectiveConfig(),
      settingsRequest,
      fetchProviderReadiness()
    ]);
    if (effectiveResult.status === "fulfilled") {
      setEffective(effectiveResult.value);
    }
    if (settingsResult.status === "fulfilled") {
      setSettings(settingsResult.value);
      setDraft(settingsValues(settingsResult.value));
    }
    if (readinessResult.status === "fulfilled") {
      setReadiness(readinessResult.value);
    }
    const failures = [effectiveResult, settingsResult, readinessResult]
      .filter((result): result is PromiseRejectedResult => result.status === "rejected")
      .map((result) => result.reason instanceof Error ? result.reason.message : String(result.reason));
    setLoadError(failures.length ? failures.join(" | ") : null);
    setLoading(false);
  };

  useEffect(() => {
    void load();
  }, [projectId]);

  const handleSave = async () => {
    setSaving(true);
    setSaveStatus(null);
    try {
      const expectedRevision = settings?.revision ?? 0;
      const updated = projectId
        ? await updateProjectSettings(projectId, nestedSettings(draft), expectedRevision)
        : await updateSystemSettings(nestedSettings(draft), expectedRevision);
      setSettings(updated);
      setDraft(settingsValues(updated));
      setSaveStatus(t("settings.saved"));
    } catch (error) {
      setSaveStatus(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  };

  const fields = projectId ? settingsFields : settingsFields.filter((field) => field.section === route.section);
  const lockedFields = new Set([...(effective?.lockedFields ?? []), ...(settings?.lockedFields ?? [])]);
  const providers = normalizeProviderReadiness(readiness);
  const staticConfig = effectiveValues(effective);
  const selectedMeta = sectionMeta[route.section];
  const motionKey = `${projectId ?? "system"}:${route.section}:${language}`;
  const metricKey = `${settings?.revision ?? 0}:${providers.map((provider) => provider.status).join(":")}`;
  useApplicationMotion(rootRef, [motionKey]);
  useApplicationScrollMotion(rootRef, [motionKey]);
  useActiveNavMotion(rootRef, projectId ? `project:${projectId}` : route.section);
  useMetricMotion(rootRef, metricKey);

  return (
    <div className="application-frame" ref={rootRef}>
      <header className="application-topbar">
        <button className="brand brand-button" type="button" onClick={() => onNavigate("/")} aria-label={t("site.aria.home")}>
          <span className="brand-mark"><Binary size={18} aria-hidden="true" /></span>
          <span>AI JS Unpack</span>
        </button>
        <div className="application-topbar-actions">
          <button className="secondary-action compact" type="button" onClick={() => onNavigate("/workbench/new")}>{t("workbench.label")}</button>
          <LanguageToggle language={language} onLanguageChange={setLanguage} />
        </div>
      </header>
      <div className="application-body">
        <aside className="application-sidebar settings-sidebar" aria-label={t("app.aria.settingsNav")}>
          <span className="sidebar-active-indicator" aria-hidden="true" />
          <div className="sidebar-context">
            <span>{t("settings.configuration")}</span>
            <strong>{scopeLabel}</strong>
          </div>
          <button
            className={projectId === API_PROJECT_ID ? "sidebar-link active" : "sidebar-link"}
            type="button"
            onClick={() => onNavigate(`/projects/${encodeURIComponent(API_PROJECT_ID)}/settings`)}
          >
            <FileCog size={17} aria-hidden="true" />
            <span><strong>{t("settings.project")}</strong><small>{API_PROJECT_ID}</small></span>
            <ChevronRight size={15} aria-hidden="true" />
          </button>
          <nav className="sidebar-nav">
            {(Object.entries(sectionMeta) as Array<[SettingsSection, (typeof sectionMeta)[SettingsSection]]>).map(([section, meta]) => {
              const Icon = meta.icon;
              return (
                <button
                  aria-current={!projectId && route.section === section ? "page" : undefined}
                  className={!projectId && route.section === section ? "sidebar-link active" : "sidebar-link"}
                  key={section}
                  type="button"
                  onClick={() => onNavigate(`/settings/${section}`)}
                >
                  <Icon size={17} aria-hidden="true" />
                  <span><strong>{t(meta.labelKey)}</strong><small>{t(meta.descriptionKey)}</small></span>
                  <ChevronRight size={15} aria-hidden="true" />
                </button>
              );
            })}
          </nav>
          <div className="sidebar-note">
            <FileCog size={17} aria-hidden="true" />
            <span>{t("settings.startupSourceNote")}</span>
          </div>
        </aside>

        <main className="application-content settings-content">
          <div className="page-heading settings-page-heading">
            <div>
              <p className="panel-kicker">{scopeLabel} - {t("settings.scopeSuffix")}</p>
              <h1>{projectId ? t("settings.project") : t(selectedMeta.labelKey)}</h1>
              <p>{t(selectedMeta.descriptionKey)} {t("settings.secretNotice")}</p>
            </div>
            <div className="page-actions">
              <button className="secondary-action compact" type="button" disabled={loading} onClick={() => void load()}>
                <RefreshCw size={16} aria-hidden="true" /> {t("action.refresh")}
              </button>
              <button className="primary-action compact" type="button" disabled={loading || saving || fields.length === 0} onClick={() => void handleSave()}>
                <Save size={16} aria-hidden="true" /> {saving ? t("settings.saving") : t("settings.saveRevision")}
              </button>
            </div>
          </div>

          {loadError ? (
            <div className="settings-notice warning" role="status">
              <CircleAlert size={18} aria-hidden="true" />
              <div><strong>{t("settings.apiUnavailable")}</strong><span>{loadError}</span></div>
            </div>
          ) : null}
          {saveStatus ? <div className="settings-notice" role="status"><CheckCircle2 size={18} aria-hidden="true" /><span>{saveStatus}</span></div> : null}

          {route.section === "ai" ? <ProviderReadiness providers={providers} loading={loading} /> : null}

          {fields.length ? <section className="settings-section" aria-labelledby="runtime-settings-title">
            <div className="settings-section-heading">
              <div><h2 id="runtime-settings-title">{t("settings.application.title")}</h2><p>{t("settings.application.description")}</p></div>
              <span className="source-badge">{projectId ? t("settings.projectScope") : t("settings.system")}</span>
            </div>
            <div className="settings-form-grid">
              {fields.map((field) => (
                <SettingsField
                  field={field}
                  key={field.key}
                  locked={lockedFields.has(field.key)}
                  source={settings?.sources?.[field.key] ?? effective?.sources?.[field.key]}
                  value={draft[field.key]}
                  onChange={(value) => setDraft((current) => ({ ...current, [field.key]: value }))}
                />
              ))}
            </div>
          </section> : (
            <section className="settings-section settings-empty-section">
              <div className="settings-empty"><LockKeyhole size={18} aria-hidden="true" /><span>{t("settings.startupOnly")}</span></div>
            </section>
          )}

          {route.section === "general" || route.section === "security" ? (
            <section className="settings-section" aria-labelledby="startup-config-title">
              <div className="settings-section-heading">
                <div><h2 id="startup-config-title">{t("settings.effective.title")}</h2><p>{t("settings.effective.description")}</p></div>
                <span className="source-badge restart"><RefreshCw size={13} aria-hidden="true" /> {t("settings.restartRequired")}</span>
              </div>
              <div className="effective-config-list">
                {Object.entries(staticConfig).slice(0, 24).map(([key, value]) => (
                  <div key={key}><span>{key}</span><code>{safeConfigValue(key, value, t)}</code><small>{localizeSource(effective?.sources?.[key], t, "settings.fileDefaultSource")}</small></div>
                ))}
                {!loading && Object.keys(staticConfig).length === 0 ? (
                  <div className="settings-empty"><Braces size={18} aria-hidden="true" /><span>{t("settings.noEffectiveConfig")}</span></div>
                ) : null}
              </div>
            </section>
          ) : null}
        </main>
      </div>
    </div>
  );
}

function SettingsField({
  field,
  locked,
  onChange,
  source,
  value
}: {
  field: SettingsFieldDefinition;
  locked: boolean;
  onChange: (value: unknown) => void;
  source?: string;
  value: unknown;
}) {
  const { t } = useLocalization();
  const id = `setting-${field.key.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  const stringValue = Array.isArray(value) ? value.join("\n") : typeof value === "string" || typeof value === "number" ? String(value) : "";
  const commonProps = { id, disabled: locked, name: field.key };
  let control;
  if (field.kind === "boolean") {
    control = (
      <button
        aria-pressed={Boolean(value)}
        className={Boolean(value) ? "settings-toggle active" : "settings-toggle"}
        disabled={locked}
        id={id}
        type="button"
        onClick={() => onChange(!Boolean(value))}
      ><span aria-hidden="true" />{Boolean(value) ? t("settings.enabled") : t("settings.disabled")}</button>
    );
  } else if (field.kind === "select") {
    control = <select {...commonProps} value={stringValue} onChange={(event) => onChange(event.currentTarget.value)}>{field.options?.map((option) => <option key={option.value} value={option.value}>{t(option.labelKey)}</option>)}</select>;
  } else if (field.kind === "textarea") {
    control = <textarea {...commonProps} rows={4} value={stringValue} onChange={(event) => onChange(event.currentTarget.value.split(/\r?\n/).filter(Boolean))} />;
  } else {
    control = (
      <input
        {...commonProps}
        max={field.max}
        min={field.min}
        type={field.kind === "number" ? "number" : "text"}
        value={stringValue}
        onChange={(event: ChangeEvent<HTMLInputElement>) => onChange(field.kind === "number" ? event.currentTarget.valueAsNumber : event.currentTarget.value)}
      />
    );
  }
  return (
    <div className="settings-field">
      <div className="settings-field-label">
        <label htmlFor={id}>{t(field.labelKey)}</label>
        <div>{locked ? <span className="source-badge locked"><LockKeyhole size={12} aria-hidden="true" /> {t("settings.locked")}</span> : null}<span className="source-badge">{localizeSource(source, t, "settings.defaultSource")}</span></div>
      </div>
      <p>{t(field.descriptionKey)}</p>
      {control}
    </div>
  );
}

function ProviderReadiness({ providers, loading }: { providers: ReturnType<typeof normalizeProviderReadiness>; loading: boolean }) {
  const { t } = useLocalization();
  return (
    <section className="settings-section readiness-section" aria-labelledby="provider-readiness-title">
      <div className="settings-section-heading"><div><h2 id="provider-readiness-title">{t("settings.provider.title")}</h2><p>{t("settings.provider.description")}</p></div></div>
      <div className="readiness-grid">
        {providers.map((provider) => (
          <div className="readiness-card" key={provider.id ?? provider.mode}>
            <div><Bot size={18} aria-hidden="true" /><strong>{localizeProviderMode(provider.mode, t)}</strong><span className={`readiness-status motion-metric-value ${provider.status}`}>{localizeProviderStatus(provider.status, t)}</span></div>
            <dl><dt>{t("settings.provider.provider")}</dt><dd>{provider.provider || t("settings.provider.notConfigured")}</dd><dt>{t("settings.provider.model")}</dt><dd>{provider.model || t("settings.provider.notConfigured")}</dd><dt>{t("settings.provider.endpoint")}</dt><dd>{provider.endpointType || t("settings.provider.notConfigured")}</dd><dt>{t("settings.provider.credential")}</dt><dd>{provider.credentialConfigured ? t("settings.provider.configured") : t("settings.provider.missing")}</dd></dl>
            {provider.detail || provider.issues?.length ? <p>{provider.detail ?? provider.issues?.join("; ")}</p> : null}
          </div>
        ))}
        {!loading && providers.length === 0 ? <div className="settings-empty"><CircleAlert size={18} aria-hidden="true" /><span>{t("settings.provider.noData")}</span></div> : null}
      </div>
    </section>
  );
}

function safeConfigValue(key: string, value: unknown, t: (key: TranslationKey) => string): string {
  if (/token|password|secret|credential|api.?key/i.test(key)) {
    return value ? t("settings.provider.configured") : t("settings.provider.notConfigured");
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}

function localizeProviderMode(mode: string, t: (key: TranslationKey) => string): string {
  const keys: Record<string, TranslationKey> = {
    cloud: "settings.provider.mode.cloud",
    local: "settings.provider.mode.local",
    desensitized: "settings.provider.mode.desensitized"
  };
  return keys[mode] ? t(keys[mode]) : mode;
}

function localizeProviderStatus(status: string, t: (key: TranslationKey) => string): string {
  const keys: Record<string, TranslationKey> = {
    ready: "settings.provider.status.ready",
    misconfigured: "settings.provider.status.misconfigured",
    unavailable: "settings.provider.status.unavailable",
    unknown: "settings.provider.status.unknown"
  };
  return keys[status] ? t(keys[status]) : status;
}

function localizeSource(source: string | undefined, t: (key: TranslationKey) => string, fallbackKey: TranslationKey): string {
  const keys: Partial<Record<string, TranslationKey>> = {
    "built-in": "settings.defaultSource",
    system: "settings.system",
    project: "settings.projectScope"
  };
  return source ? (keys[source] ? t(keys[source]!) : source) : t(fallbackKey);
}

export const marketingRoutes = ["/", "/workflow", "/evidence", "/runtime"] as const;
export const workbenchSections = ["overview", "artifacts", "evidence", "agents", "runtime", "audit"] as const;
export const settingsSections = ["general", "ai", "agents", "security", "validation"] as const;

export type MarketingRoute = (typeof marketingRoutes)[number];
export type WorkbenchSection = (typeof workbenchSections)[number];
export type SettingsSection = (typeof settingsSections)[number];
export type WorkbenchRoute = "/workbench/new" | `/workbench/${string}/${WorkbenchSection}`;
export type SettingsRoute = `/settings/${SettingsSection}` | `/projects/${string}/settings`;
export type AppRoute = MarketingRoute | WorkbenchRoute | SettingsRoute;

export type ParsedAppRoute =
  | { kind: "marketing"; path: MarketingRoute }
  | { kind: "workbench-new"; path: "/workbench/new" }
  | { kind: "workbench"; jobId: string; path: WorkbenchRoute; section: WorkbenchSection }
  | { kind: "settings"; path: SettingsRoute; projectId: string | null; section: SettingsSection };

const marketingRouteSet = new Set<string>(marketingRoutes);
const workbenchSectionSet = new Set<string>(workbenchSections);
const settingsSectionSet = new Set<string>(settingsSections);

export function toAppRoute(pathname: string): AppRoute {
  const normalized = pathname.replace(/\/+$/, "") || "/";
  if (normalized === "/workbench" || normalized === "/workbench/new") {
    return "/workbench/new";
  }
  if (marketingRouteSet.has(normalized)) {
    return normalized as MarketingRoute;
  }

  const workbenchMatch = normalized.match(/^\/workbench\/([^/]+)\/([^/]+)$/);
  if (workbenchMatch && workbenchSectionSet.has(workbenchMatch[2])) {
    return normalized as WorkbenchRoute;
  }

  const settingsMatch = normalized.match(/^\/settings\/([^/]+)$/);
  if (settingsMatch && settingsSectionSet.has(settingsMatch[1])) {
    return normalized as SettingsRoute;
  }

  if (/^\/projects\/[^/]+\/settings$/.test(normalized)) {
    return normalized as SettingsRoute;
  }
  return "/";
}

export function parseAppRoute(pathname: string): ParsedAppRoute {
  const path = toAppRoute(pathname);
  if (marketingRouteSet.has(path)) {
    return { kind: "marketing", path: path as MarketingRoute };
  }
  if (path === "/workbench/new") {
    return { kind: "workbench-new", path };
  }

  const workbenchMatch = path.match(/^\/workbench\/([^/]+)\/([^/]+)$/);
  if (workbenchMatch) {
    return {
      kind: "workbench",
      jobId: decodeURIComponent(workbenchMatch[1]),
      path: path as WorkbenchRoute,
      section: workbenchMatch[2] as WorkbenchSection
    };
  }

  const projectMatch = path.match(/^\/projects\/([^/]+)\/settings$/);
  if (projectMatch) {
    return {
      kind: "settings",
      path: path as SettingsRoute,
      projectId: decodeURIComponent(projectMatch[1]),
      section: "general"
    };
  }
  return {
    kind: "settings",
    path: path as SettingsRoute,
    projectId: null,
    section: path.split("/").at(-1) as SettingsSection
  };
}

export function workbenchPath(jobId: string, section: WorkbenchSection): WorkbenchRoute {
  return `/workbench/${encodeURIComponent(jobId)}/${section}`;
}

export function isWorkbenchRoute(route: ParsedAppRoute): route is Extract<ParsedAppRoute, { kind: "workbench" | "workbench-new" }> {
  return route.kind === "workbench" || route.kind === "workbench-new";
}

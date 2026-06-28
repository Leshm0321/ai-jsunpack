export const appRoutes = ["/", "/workflow", "/evidence", "/runtime", "/workbench"] as const;

export type AppRoute = (typeof appRoutes)[number];

const routeSet = new Set<string>(appRoutes);

export function toAppRoute(pathname: string): AppRoute {
  const normalized = pathname.replace(/\/+$/, "") || "/";
  return routeSet.has(normalized) ? (normalized as AppRoute) : "/";
}


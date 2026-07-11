import { useEffect, useMemo, useRef, useState } from "react";
import { useGSAP } from "@gsap/react";
import { gsap } from "gsap";
import { LocalizationContext, persistPreferredLanguage, readPreferredLanguage, translate } from "./i18n";
import type { Language, LocalizationValue } from "./i18n";
import { MarketingPage } from "./marketing";
import { parseAppRoute, toAppRoute } from "./routes";
import type { AppRoute } from "./routes";
import { SettingsCenter } from "./settings";
import { AppContainer as WorkbenchContainer } from "./workbench";

gsap.registerPlugin(useGSAP);

export function AppContainer() {
  const transitionRef = useRef<HTMLDivElement>(null);
  const [language, setLanguageState] = useState<Language>(() => readPreferredLanguage());
  const [routePath, setRoutePath] = useState<AppRoute>(() => toAppRoute(window.location.pathname));
  const route = useMemo(() => parseAppRoute(routePath), [routePath]);
  const t = useMemo(() => (key: string) => translate(language, key), [language]);
  const localization = useMemo<LocalizationValue>(
    () => ({
      language,
      setLanguage: (nextLanguage) => {
        setLanguageState(nextLanguage);
        persistPreferredLanguage(nextLanguage);
      },
      t
    }),
    [language, t]
  );

  useEffect(() => {
    const handlePopState = () => setRoutePath(toAppRoute(window.location.pathname));
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const handleNavigate = (nextRoute: AppRoute) => {
    const normalized = toAppRoute(nextRoute);
    if (normalized === routePath) {
      return;
    }
    window.history.pushState(null, "", normalized);
    setRoutePath(normalized);
  };

  useGSAP(
    () => {
      const element = transitionRef.current;
      if (!element) return;
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        gsap.set(element, { autoAlpha: 0, scaleX: 0 });
        return;
      }
      const timeline = gsap.timeline();
      timeline
        .set(element, { autoAlpha: 1, scaleX: 1, transformOrigin: "right center" })
        .to(element, { scaleX: 0, duration: 0.72, ease: "power4.inOut" })
        .set(element, { autoAlpha: 0 });
      return () => timeline.kill();
    },
    { dependencies: [routePath], revertOnUpdate: true }
  );

  return (
    <LocalizationContext.Provider value={localization}>
      <div className="route-transition-layer" ref={transitionRef} aria-hidden="true" />
      {route.kind === "marketing" ? (
        <MarketingPage key={route.path} route={route.path} onNavigate={handleNavigate} />
      ) : null}
      {route.kind === "workbench" || route.kind === "workbench-new" ? (
        <WorkbenchContainer route={route} onNavigate={handleNavigate} />
      ) : null}
      {route.kind === "settings" ? <SettingsCenter route={route} onNavigate={handleNavigate} /> : null}
    </LocalizationContext.Provider>
  );
}

export default AppContainer;

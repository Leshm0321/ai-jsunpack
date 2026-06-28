import { useEffect, useMemo, useState } from "react";
import { LocalizationContext, persistPreferredLanguage, readPreferredLanguage, translate } from "./i18n";
import type { Language, LocalizationValue } from "./i18n";
import { MarketingPage } from "./marketing";
import { toAppRoute } from "./routes";
import type { AppRoute } from "./routes";
import { AppContainer as WorkbenchContainer } from "./workbench";

export function AppContainer() {
  const [language, setLanguageState] = useState<Language>(() => readPreferredLanguage());
  const [route, setRoute] = useState<AppRoute>(() => toAppRoute(window.location.pathname));
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
    const handlePopState = () => setRoute(toAppRoute(window.location.pathname));
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const handleNavigate = (nextRoute: AppRoute) => {
    if (nextRoute === route) {
      window.scrollTo({ top: 0, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
      return;
    }
    window.history.pushState(null, "", nextRoute);
    setRoute(nextRoute);
    window.scrollTo({ top: 0, behavior: "auto" });
  };

  return (
    <LocalizationContext.Provider value={localization}>
      {route === "/workbench" ? <WorkbenchContainer onNavigate={handleNavigate} /> : <MarketingPage route={route} onNavigate={handleNavigate} />}
    </LocalizationContext.Provider>
  );
}

export default AppContainer;

import { createContext, useContext } from "react";
import { en } from "./locales/en";
import { zh } from "./locales/zh";

export type Language = "en" | "zh";
type TranslationKey = keyof typeof en;
type TranslationTable = Record<TranslationKey, string>;

export interface LocalizationValue {
  language: Language;
  setLanguage: (language: Language) => void;
  t: (key: string) => string;
}

const translations = { en, zh } satisfies Record<Language, TranslationTable>;

export const LocalizationContext = createContext<LocalizationValue>({
  language: "en",
  setLanguage: () => undefined,
  t: (key) => translate("en", key)
});

const languageStorageKey = "ai-jsunpack.language.v1";

export function translate(language: Language, key: string): string {
  const localized = translations[language] as Readonly<Record<string, string>>;
  const fallback = translations.en as Readonly<Record<string, string>>;
  return localized[key] ?? fallback[key] ?? key;
}

export function readPreferredLanguage(): Language {
  if (typeof window === "undefined") {
    return "en";
  }
  try {
    const value = window.localStorage.getItem(languageStorageKey);
    return value === "zh" || value === "en" ? value : "en";
  } catch {
    return "en";
  }
}

export function persistPreferredLanguage(language: Language): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(languageStorageKey, language);
  } catch {
    // Ignore unavailable storage; the in-memory language still updates.
  }
}

export function useLocalization(): LocalizationValue {
  return useContext(LocalizationContext);
}

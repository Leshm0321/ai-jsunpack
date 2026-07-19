import { createContext, useContext } from "react";
import { en } from "./locales/en";
import { zh } from "./locales/zh";

export type Language = "en" | "zh";
export type TranslationKey = keyof typeof en;
type TranslationTable = Record<TranslationKey, string>;

export interface LocalizationValue {
  language: Language;
  setLanguage: (language: Language) => void;
  t: (key: string) => string;
}

const translations = { en, zh } satisfies Record<Language, TranslationTable>;

export const LocalizationContext = createContext<LocalizationValue>({
  language: "zh",
  setLanguage: () => undefined,
  t: (key) => translate("zh", key)
});

const languageStorageKey = "ai-jsunpack.language.v1";

export function translate(language: Language, key: string): string {
  const localized = translations[language] as Readonly<Record<string, string>>;
  const fallback = translations.en as Readonly<Record<string, string>>;
  return localized[key] ?? fallback[key] ?? key;
}

export function readPreferredLanguage(): Language {
  if (typeof window === "undefined") {
    return "zh";
  }
  try {
    const value = window.localStorage.getItem(languageStorageKey);
    return value === "zh" || value === "en" ? value : "zh";
  } catch {
    return "zh";
  }
}

export function persistPreferredLanguage(language: Language): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(languageStorageKey, language);
  } catch {
    // 忽略不可用的存储；内存中的语言状态仍会更新。
  }
}

export function useLocalization(): LocalizationValue {
  return useContext(LocalizationContext);
}

// i18next bootstrap — trilingual driver shell per Corrigendum 3, Appendix A6.
// English (default + fallback), Hindi (hi), Marathi (mr). Resource bundles are
// inlined so the PWA renders translated chrome with no network round-trip and
// keeps working offline; screen-level strings can be added incrementally.
// Mirrors web/src/i18n/index.ts (same lib stack, same localStorage key).

import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";

import en from "./locales/en.json";
import hi from "./locales/hi.json";
import mr from "./locales/mr.json";

export const SUPPORTED_LANGS = ["en", "hi", "mr"] as const;
export type LangCode = (typeof SUPPORTED_LANGS)[number];

export const LANG_LABELS: Record<LangCode, string> = {
  en: "English",
  hi: "हिन्दी",
  mr: "मराठी",
};

export const resources = {
  en: { translation: en },
  hi: { translation: hi },
  mr: { translation: mr },
} as const;

void i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources,
    fallbackLng: "en",
    supportedLngs: SUPPORTED_LANGS as unknown as string[],
    nonExplicitSupportedLngs: true,
    interpolation: { escapeValue: false }, // React already escapes
    detection: {
      order: ["localStorage", "navigator", "htmlTag"],
      caches: ["localStorage"],
      lookupLocalStorage: "jnpa-lang", // shared key with the web dashboard
    },
  });

export default i18n;

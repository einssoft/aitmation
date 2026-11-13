// Minimal, typed translation layer (ADR-0022). German is the lead file and
// defines the key set; the build script refuses incomplete languages.
// No library: a key tree, {param} interpolation and a React context.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import de from "./de.json";
import fr from "./fr.json";
import it from "./it.json";
import en from "./en.json";

export const LANGUAGES = ["de", "fr", "it", "en"] as const;
export type Language = (typeof LANGUAGES)[number];
export const LANGUAGE_NAMES: Record<Language, string> = { de: "Deutsch", fr: "Français", it: "Italiano", en: "English" };
export const LOCALES: Record<Language, string> = { de: "de-CH", fr: "fr-CH", it: "it-CH", en: "en-CH" };

type Tree = { [key: string]: string | Tree };
const dictionaries: Record<Language, Tree> = { de, fr, it, en };

function lookup(tree: Tree, key: string): string | undefined {
  let node: string | Tree | undefined = tree;
  for (const part of key.split(".")) {
    if (!node || typeof node === "string") return undefined;
    node = node[part];
  }
  return typeof node === "string" ? node : undefined;
}

export type Params = Record<string, string | number>;

export function translate(lang: Language, key: string, params?: Params): string {
  const text = lookup(dictionaries[lang], key) ?? lookup(dictionaries.de, key);
  if (text === undefined) {
    if (import.meta.env.DEV) console.warn(`[i18n] missing key ${key}`);
    return key;
  }
  return params ? text.replace(/\{(\w+)\}/g, (_, name: string) => String(params[name] ?? `{${name}}`)) : text;
}

/** A multilingual field from the core: a plain string is German. */
export type Text = string | Record<string, string>;
export function pick(text: Text | undefined | null, lang: Language): string {
  if (!text) return "";
  if (typeof text === "string") return text;
  return text[lang] ?? text.de ?? Object.values(text)[0] ?? "";
}

interface I18n {
  lang: Language;
  setLang: (lang: Language) => void;
  t: (key: string, params?: Params) => string;
  pick: (text: Text | undefined | null) => string;
  formatDate: (iso: string | null | undefined) => string;
  formatNumber: (n: number, digits?: number) => string;
}

const I18nContext = createContext<I18n | null>(null);
const STORAGE_KEY = "aitmation.lang";

function initialLanguage(): Language {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && (LANGUAGES as readonly string[]).includes(stored)) return stored as Language;
  } catch {
    /* storage unavailable */
  }
  const nav = navigator.language.slice(0, 2);
  return (LANGUAGES as readonly string[]).includes(nav) ? (nav as Language) : "de";
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Language>(initialLanguage);
  useEffect(() => {
    document.documentElement.lang = LOCALES[lang];
  }, [lang]);
  const setLang = useCallback((next: Language) => {
    setLangState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* ignore */
    }
  }, []);
  const value = useMemo<I18n>(
    () => ({
      lang,
      setLang,
      t: (key, params) => translate(lang, key, params),
      pick: (text) => pick(text, lang),
      formatDate: (iso) =>
        iso ? new Intl.DateTimeFormat(LOCALES[lang], { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso)) : "",
      formatNumber: (n, digits = 2) => new Intl.NumberFormat(LOCALES[lang], { maximumFractionDigits: digits }).format(n),
    }),
    [lang, setLang],
  );
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18n {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error("useI18n outside I18nProvider");
  return ctx;
}

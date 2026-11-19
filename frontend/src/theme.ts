/** Light / dark / system theme, remembered in this browser only. */

export type Theme = "system" | "light" | "dark";
const KEY = "carborewind.theme";

export function savedTheme(): Theme {
  try {
    const value = localStorage.getItem(KEY);
    if (value === "light" || value === "dark") return value;
  } catch {
    /* storage unavailable: follow the system */
  }
  return "system";
}

export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "system") delete root.dataset.theme;
  else root.dataset.theme = theme;
}

export function setTheme(theme: Theme): void {
  try {
    if (theme === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, theme);
  } catch {
    /* not remembered, applied all the same */
  }
  applyTheme(theme);
}

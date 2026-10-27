import {applyVoyagePrefs, loadVoyagePrefs, saveVoyagePrefs} from '@yiminlab/voyage';

export const PREFS_KEY = 'ablation.workbench.voyage';
export const MODE_KEY = 'ablation.workbench.theme';

export function modePreference() {
  try { return JSON.parse(localStorage.getItem(MODE_KEY)); }
  catch { return null; }
}

export function preferredMode() {
  const mode = modePreference();
  return ['light', 'dark'].includes(mode) ? mode : matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function appearance() {
  const mode = preferredMode();
  return {...loadVoyagePrefs({theme:'github', mode, style:'classic', tone:'quiet'}, PREFS_KEY), mode};
}

export function applyAppearance() {
  const prefs = appearance();
  applyVoyagePrefs(document.documentElement, prefs);
  document.documentElement.style.colorScheme = prefs.mode;
  saveVoyagePrefs(prefs, PREFS_KEY);
  return prefs;
}

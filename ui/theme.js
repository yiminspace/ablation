import {applyAppearance, MODE_KEY, PREFS_KEY} from './appearance';

applyAppearance();
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyAppearance);
window.addEventListener('storage', event => {
  if ([MODE_KEY, PREFS_KEY].includes(event.key)) applyAppearance();
});
window.addEventListener('ablation-theme-change', applyAppearance);

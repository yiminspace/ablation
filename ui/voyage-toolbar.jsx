import {useEffect, useRef} from 'react';
import {createRoot} from 'react-dom/client';
import {VoyageProvider, VoyageToolbar, useVoyage} from '@yiminlab/voyage/react';
import {appearance, modePreference, preferredMode, MODE_KEY, PREFS_KEY} from './appearance';

function Toolbar() {
  const {prefs, setMode, setPrefs} = useVoyage();
  const previousMode = useRef(prefs.mode);
  const externalMode = useRef(null);

  useEffect(() => {
    const synchronize = () => {
      const next = appearance();
      externalMode.current = next.mode;
      setPrefs(next);
    };
    const followSystem = () => {
      if (!['light', 'dark'].includes(modePreference())) {
        externalMode.current = preferredMode();
        setMode(externalMode.current);
      }
    };
    const storageChanged = event => { if ([MODE_KEY, PREFS_KEY].includes(event.key)) synchronize(); };
    const system = matchMedia('(prefers-color-scheme: dark)');
    window.addEventListener('ablation-theme-change', synchronize);
    window.addEventListener('storage', storageChanged);
    system.addEventListener('change', followSystem);
    return () => {
      window.removeEventListener('ablation-theme-change', synchronize);
      window.removeEventListener('storage', storageChanged);
      system.removeEventListener('change', followSystem);
    };
  }, [setMode, setPrefs]);

  useEffect(() => {
    document.documentElement.style.colorScheme = prefs.mode;
    if (previousMode.current === prefs.mode) return;
    if (externalMode.current !== prefs.mode) {
      try { localStorage.setItem(MODE_KEY, JSON.stringify(prefs.mode)); } catch {}
    }
    previousMode.current = prefs.mode;
    externalMode.current = null;
  }, [prefs.mode]);

  return <VoyageToolbar locale="zh"
    reporter={{disabled:true, title:'尚未配置问题上报'}}
    language={{disabled:true, title:'当前界面提供中文'}} />;
}

createRoot(document.getElementById('voyage-toolbar')).render(
  <VoyageProvider defaults={appearance()} storageKey={PREFS_KEY}><Toolbar /></VoyageProvider>
);

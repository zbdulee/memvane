// Dark/light theme on <html data-theme>. The choice is a per-browser
// convenience in localStorage; the page works when storage is unavailable.

import {useEffect, useState} from 'preact/hooks';

export type Theme = 'dark' | 'light';
const KEY = 'memvane-ui-theme';

function initialTheme(): Theme {
  try {
    const saved = window.localStorage.getItem(KEY);
    if (saved === 'dark' || saved === 'light') return saved;
  } catch {
    // Storage blocked: fall through to the system preference.
  }
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(initialTheme);
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);
  const toggle = () => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark';
    try {
      window.localStorage.setItem(KEY, next);
    } catch {
      // Not remembered across reloads; the toggle still applies.
    }
    setTheme(next);
  };
  return [theme, toggle];
}

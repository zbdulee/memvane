// One "해제·없는 체크아웃 보기" switch shared by the sidebar and the overview table,
// remembered in localStorage (off when storage is missing or refuses).

import {useEffect, useState} from 'preact/hooks';

const KEY = 'memvane.ui.showDormant';

function load(): boolean {
  try {
    return window.localStorage.getItem(KEY) === '1';
  } catch {
    return false;
  }
}

let current = load();
const listeners = new Set<(value: boolean) => void>();

export function setShowDormant(value: boolean): void {
  current = value;
  try {
    window.localStorage.setItem(KEY, value ? '1' : '0');
  } catch {
    // A per-viewer convenience: the switch still works for this page.
  }
  for (const listener of listeners) listener(value);
}

export function useShowDormant(): [boolean, (value: boolean) => void] {
  const [value, setValue] = useState(current);
  useEffect(() => {
    listeners.add(setValue);
    setValue(current);
    return () => {
      listeners.delete(setValue);
    };
  }, []);
  return [value, setShowDormant];
}

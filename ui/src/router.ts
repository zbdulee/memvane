// App routes live in the URL hash as `#<tab>` or `#<tab>?target=<targetId>`.

import {useEffect, useState} from 'preact/hooks';
import type {TabId, TargetId} from './api/types';

export const TABS: readonly {id: TabId; label: string}[] = [
  {id: 'overview', label: 'Overview'},
  {id: 'wiki', label: 'Wiki'},
  {id: 'recall', label: 'Recall'},
  {id: 'runtime', label: 'Runtime'},
];

export interface Route {
  tab: TabId;
  target: TargetId | null;
}

const TARGET_ID = /^[0-9a-f]{32}(:[0-9a-f]{12})?$/;

export function isTab(value: string | null): value is TabId {
  return TABS.some(tab => tab.id === value);
}

export function isTargetId(value: string | null): value is TargetId {
  return value !== null && TARGET_ID.test(value);
}

export function parseRoute(hash: string): Route {
  const raw = hash.startsWith('#') ? hash.slice(1) : hash;
  const question = raw.indexOf('?');
  const tab = question === -1 ? raw : raw.slice(0, question);
  const params = new URLSearchParams(question === -1 ? '' : raw.slice(question + 1));
  const target = params.get('target');
  return {tab: isTab(tab) ? tab : 'overview', target: isTargetId(target) ? target : null};
}

export function routeHash(route: Route): string {
  return route.target === null ? `#${route.tab}` : `#${route.tab}?target=${encodeURIComponent(route.target)}`;
}

export function navigate(route: Route): void {
  const next = routeHash(route);
  if (window.location.hash !== next) window.location.hash = next;
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  return route;
}

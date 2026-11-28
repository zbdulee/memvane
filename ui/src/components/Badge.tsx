import type {ComponentChildren} from 'preact';

export type Tone = 'green' | 'amber' | 'red' | 'blue' | 'purple' | 'muted';

export function Badge(props: {tone: Tone; mono?: boolean; title?: string; children: ComponentChildren}) {
  const className = `badge badge-${props.tone}${props.mono === true ? ' mono' : ''}`;
  return props.title === undefined
    ? <span class={className}>{props.children}</span>
    : <span class={className} title={props.title}>{props.children}</span>;
}

export function Dot(props: {tone: Tone}) {
  return <span class={`dot ${props.tone}`} aria-hidden="true" />;
}

import type { ComponentProps } from 'react';

export type ResponsesMessage = Record<string, any>;
export const cn = (...parts: unknown[]) => parts.filter(Boolean).join(' ');
export const ChevronDown = (props: ComponentProps<'svg'>) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true" {...props}><path d="m6 9 6 6 6-6" /></svg>;

export function applyRedactionMapping(text: string, mapping?: Record<string, string>): string {
  for (const [key, value] of Object.entries(mapping ?? {})) text = text.replaceAll(`[${key}]`, value);
  return text;
}

export function applyRedactionMappingToValue(value: unknown, mapping?: Record<string, string>): unknown {
  if (typeof value === 'string') return applyRedactionMapping(value, mapping);
  if (Array.isArray(value)) return value.map(item => applyRedactionMappingToValue(item, mapping));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, applyRedactionMappingToValue(item, mapping)]));
  return value;
}

// Adapted from Bifrost logDetailView.utils.ts, Apache-2.0; see bundled NOTICE.
export function isResponsesToolCallItem(type: string | undefined): boolean {
  return !!type && type.endsWith('_call');
}

export function hasNoToolArguments(args: unknown): boolean {
  if (typeof args !== 'string') return false;
  const trimmed = args.trim();
  if (!trimmed) return true;
  try {
    const parsed: unknown = JSON.parse(trimmed);
    return parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed) && Object.keys(parsed).length === 0;
  } catch {
    return false;
  }
}

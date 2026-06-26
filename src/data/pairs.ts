import { providers, type ProviderMeta } from './providers';

export const PAIRS: [string, string][] = [
  ['runpod', 'vast'],
  ['vast', 'lambda'],
  ['runpod', 'lambda'],
];

export function pairSlug([a, b]: [string, string]): string {
  return `${a}-vs-${b}`;
}

export function pairProviders([a, b]: [string, string]): readonly [ProviderMeta, ProviderMeta] {
  return [providers[a], providers[b]] as const;
}

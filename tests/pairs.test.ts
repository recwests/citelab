import { describe, it, expect } from 'vitest';
import { PAIRS, pairSlug, pairProviders } from '../src/data/pairs';

describe('pairs', () => {
  it('produces 3 unique slugs', () => {
    const slugs = PAIRS.map(pairSlug);
    expect(new Set(slugs).size).toBe(3);
  });

  it('resolves both providers for every pair', () => {
    for (const p of PAIRS) {
      const [a, b] = pairProviders(p);
      expect(a?.name).toBeTruthy();
      expect(b?.name).toBeTruthy();
    }
  });
});

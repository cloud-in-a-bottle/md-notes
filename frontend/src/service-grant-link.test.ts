import { describe, expect, it } from 'vitest';
import { parseServiceGrantLink } from './config';

describe('parseServiceGrantLink', () => {
  it('parses a consent link with a return address', () => {
    expect(parseServiceGrantLink('/service/grant', '?request=tok123&return_to=https%3A%2F%2Fapp.example.com%2Fx'))
      .toEqual({ token: 'tok123', returnTo: 'https://app.example.com/x' });
  });

  it('tolerates a missing return address', () => {
    expect(parseServiceGrantLink('/service/grant', '?request=tok123'))
      .toEqual({ token: 'tok123', returnTo: null });
  });

  it('tolerates a trailing slash', () => {
    expect(parseServiceGrantLink('/service/grant/', '?request=tok123')?.token).toBe('tok123');
  });

  it('ignores other routes', () => {
    expect(parseServiceGrantLink('/', '?request=tok123')).toBeNull();
    expect(parseServiceGrantLink('/service/grants', '?request=tok123')).toBeNull();
    expect(parseServiceGrantLink('/myvault', '')).toBeNull();
  });
});

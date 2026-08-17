import { describe, expect, it } from 'vitest';
import { parseServiceGrantLink } from './config';

describe('parseServiceGrantLink', () => {
  it('parses a consent link with a return address', () => {
    expect(parseServiceGrantLink(
      '/service/grant',
      '?consumer=notes-reader&access=read&return_to=https%3A%2F%2Fapp.example.com%2Fx',
    )).toEqual({ consumer: 'notes-reader', access: 'read', returnTo: 'https://app.example.com/x' });
  });

  it('tolerates a missing return address', () => {
    expect(parseServiceGrantLink('/service/grant', '?consumer=notes-reader&access=read'))
      .toEqual({ consumer: 'notes-reader', access: 'read', returnTo: null });
  });

  it('tolerates a trailing slash', () => {
    expect(parseServiceGrantLink('/service/grant/', '?consumer=a&access=read')?.consumer).toBe('a');
  });

  it('ignores other routes', () => {
    expect(parseServiceGrantLink('/', '?consumer=a&access=read')).toBeNull();
    expect(parseServiceGrantLink('/service/grants', '?consumer=a&access=read')).toBeNull();
    expect(parseServiceGrantLink('/myvault', '')).toBeNull();
  });
});

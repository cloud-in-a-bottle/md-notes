/**
 * The consent page's API (see server/web/api/service_grants.py). Standalone from the main client:
 * this page runs on its own, outside the editor shell and its connection tracking.
 */

import { serverUrl, type ServiceGrantLink } from '../config';
import type { Permission } from './types';

export interface GrantRequestInfo {
  /** OpenHost app name of whoever asked for access. */
  consumerName: string;
  access: Permission;
  /** Where to send the browser when we're done; null if the link didn't supply a usable one. */
  returnTo: string | null;
}

async function errorMessage(res: Response): Promise<string> {
  try {
    const body = await res.json();
    return body.detail || body.error || `Request failed (${res.status})`;
  } catch {
    return `Request failed (${res.status})`;
  }
}

/** Vet the link's parameters — the tier is real, and the return URL is inside this space. */
export async function fetchGrantRequest(link: ServiceGrantLink): Promise<GrantRequestInfo> {
  const search = new URLSearchParams({ consumer: link.consumer, access: link.access });
  if (link.returnTo) search.set('return_to', link.returnTo);
  const res = await fetch(`${serverUrl}/api/service-grants/request?${search}`);
  if (!res.ok) throw new Error(await errorMessage(res));
  return res.json();
}

/** Register the grant with OpenHost. `paths` null grants the whole vault, future files included. */
export async function approveGrant(
  link: ServiceGrantLink,
  vault: string,
  paths: string[] | null,
): Promise<void> {
  const res = await fetch(`${serverUrl}/api/service-grants/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ consumer: link.consumer, access: link.access, vault, paths }),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
}

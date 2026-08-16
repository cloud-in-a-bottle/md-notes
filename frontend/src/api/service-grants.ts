/**
 * The consent page's API (see server/web/api/service_grants.py). Standalone from the main client:
 * this page runs on its own, outside the editor shell and its connection tracking.
 */

import { serverUrl } from '../config';
import type { Permission } from './types';

export class GrantRequestExpiredError extends Error {
  constructor() {
    super('This request link is unknown or has expired. Ask the app to try again.');
  }
}

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

export async function fetchGrantRequest(token: string, returnTo: string | null): Promise<GrantRequestInfo> {
  const search = new URLSearchParams(returnTo ? { return_to: returnTo } : {});
  const res = await fetch(`${serverUrl}/api/service-grants/request/${encodeURIComponent(token)}?${search}`);
  if (res.status === 404) throw new GrantRequestExpiredError();
  if (!res.ok) throw new Error(await errorMessage(res));
  return res.json();
}

/** Register the grant with OpenHost. `paths` null grants the whole vault, future files included. */
export async function approveGrant(token: string, vault: string, paths: string[] | null): Promise<void> {
  const res = await fetch(`${serverUrl}/api/service-grants/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, vault, paths }),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
}

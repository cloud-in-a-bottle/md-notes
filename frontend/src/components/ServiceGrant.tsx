import { createResource, createSignal, For, Show, type Component } from 'solid-js';
import { listFiles, listVaults } from '../api/client';
import { approveGrant, fetchGrantRequest, GrantRequestExpiredError, type GrantRequestInfo } from '../api/service-grants';
import type { FileEntry, Vault } from '../api/types';
import type { ServiceGrantLink } from '../config';

interface Props {
  link: ServiceGrantLink;
}

const ACCESS_LABELS = { read: 'read', comment: 'read and comment on', write: 'read and edit' } as const;

interface FileRow {
  path: string;
  name: string;
  depth: number;
  isDir: boolean;
}

/** Flatten the file tree into indented rows; directories are labels, files are selectable. */
function flatten(entries: FileEntry[], depth = 0): FileRow[] {
  return entries.flatMap((entry) => [
    { path: entry.path, name: entry.name, depth, isDir: entry.type === 'dir' },
    ...(entry.type === 'dir' ? flatten(entry.children ?? [], depth + 1) : []),
  ]);
}

/**
 * Consent page for the notes service: another app asked to read notes, got a 403, and sent its user
 * here to shape a grant. The requesting app's identity comes from the server keyed by the request
 * token — never from the link — so a doctored URL can't misrepresent who is asking.
 */
export const ServiceGrant: Component<Props> = (props) => {
  const [vault, setVault] = createSignal<Vault | null>(null);
  const [selected, setSelected] = createSignal<Set<string>>(new Set());
  const [wholeVault, setWholeVault] = createSignal(true);
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal<string | null>(null);
  const [notice, setNotice] = createSignal<string | null>(null);

  const [request] = createResource(() => fetchGrantRequest(props.link.token, props.link.returnTo));
  const [vaults] = createResource(async () => (await listVaults()).filter((v) => v.owned));
  const [files] = createResource(vault, listFiles);

  function toggle(path: string) {
    const next = new Set(selected());
    if (!next.delete(path)) next.add(path);
    setSelected(next);
  }

  /** Hand the browser back to the requesting app, telling it whether to retry. */
  function finish(info: GrantRequestInfo, granted: boolean) {
    if (!info.returnTo) {
      setNotice(granted
        ? 'Access granted. Close this tab and try again in the other app.'
        : 'Nothing was shared. You can close this tab.');
      return;
    }
    const url = new URL(info.returnTo);
    url.searchParams.set('granted', granted ? '1' : '0');
    window.location.href = url.toString();
  }

  async function grant(info: GrantRequestInfo) {
    const chosen = vault();
    if (!chosen || busy()) return;
    setBusy(true);
    setError(null);
    try {
      await approveGrant(props.link.token, chosen.vault, wholeVault() ? null : [...selected()]);
      finish(info, true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function errorMessage(e: unknown): string {
    if (e instanceof GrantRequestExpiredError) return e.message;
    return String(e instanceof Error ? e.message : e);
  }

  return (
    <div class="federation-connect">
      <div class="vault-picker-card service-grant">
        <div class="vault-picker-title">Share notes with another app</div>

        <Show when={!request.loading} fallback={<p>Checking this request…</p>}>
          <Show when={!request.error} fallback={<p>{errorMessage(request.error)}</p>}>
            <Show when={request()}>
              {(info) => (
                <>
                  <p>
                    <strong class="service-grant-consumer">{info().consumerName}</strong> wants to{' '}
                    {ACCESS_LABELS[info().access]} data in your md-notes. To grant that, first pick the
                    vault you want to share.
                  </p>

                  <div class="vault-picker-list">
                    <For each={vaults() ?? []}>{(v) => (
                      <div
                        class="vault-picker-item"
                        classList={{ 'service-grant-selected': vault()?.id === v.id }}
                        data-vault={v.name}
                        onClick={() => { setVault(v); setSelected(new Set<string>()); setWholeVault(true); }}
                      >
                        <div class="vault-picker-item-info">
                          <div class="vault-picker-item-name">{v.name}</div>
                        </div>
                      </div>
                    )}</For>
                    <Show when={!vaults.loading && (vaults() ?? []).length === 0}>
                      <p>You don't have any vaults to share yet.</p>
                    </Show>
                  </div>

                  <Show when={vault()}>
                    {(chosen) => (
                      <div class="share-new-section">
                        <label class="service-grant-scope">
                          <input
                            type="radio"
                            name="scope"
                            value="vault"
                            checked={wholeVault()}
                            onChange={() => setWholeVault(true)}
                          />
                          <span>
                            Everything in <strong>{chosen().name}</strong>, including files added later
                          </span>
                        </label>
                        <label class="service-grant-scope">
                          <input
                            type="radio"
                            name="scope"
                            value="files"
                            checked={!wholeVault()}
                            onChange={() => setWholeVault(false)}
                          />
                          <span>Only the files I pick</span>
                        </label>

                        <Show when={!wholeVault()}>
                          <div class="service-grant-files">
                            <For each={flatten(files() ?? [])}>{(row) => (
                              <Show
                                when={!row.isDir}
                                fallback={
                                  <div class="service-grant-dir" style={{ 'padding-left': `${row.depth * 14}px` }}>
                                    {row.name}
                                  </div>
                                }
                              >
                                <label class="service-grant-file" style={{ 'padding-left': `${row.depth * 14}px` }}>
                                  <input
                                    type="checkbox"
                                    value={row.path}
                                    checked={selected().has(row.path)}
                                    onChange={() => toggle(row.path)}
                                  />
                                  <span>{row.name}</span>
                                </label>
                              </Show>
                            )}</For>
                            <Show when={!files.loading && (files() ?? []).length === 0}>
                              <div class="service-grant-dir">This vault has no files.</div>
                            </Show>
                          </div>
                        </Show>
                      </div>
                    )}
                  </Show>

                  <Show when={error()}>
                    <div class="share-modal-error">{error()}</div>
                  </Show>
                  <Show when={notice()}>
                    <p class="service-grant-notice">{notice()}</p>
                  </Show>

                  <div class="share-modal-buttons">
                    <button class="share-modal-btn" disabled={busy()} onClick={() => finish(info(), false)}>
                      Don't share
                    </button>
                    <button
                      class="share-modal-btn share-modal-btn-primary"
                      disabled={busy() || !vault() || (!wholeVault() && selected().size === 0)}
                      onClick={() => grant(info())}
                    >
                      {busy() ? 'Granting…' : 'Grant access'}
                    </button>
                  </div>
                </>
              )}
            </Show>
          </Show>
        </Show>
      </div>
    </div>
  );
};

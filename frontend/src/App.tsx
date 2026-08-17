import { createSignal, createResource, createEffect, onMount, onCleanup, Show, type Component } from 'solid-js';
import { createEditor, type EditorInstance } from './editor/editor';
import { getEditorKind } from './editor/editor-settings';
import {
  listVaults, createVault, deleteVault, connectVault, disconnectVault, getServerVimrc, pingHealth,
  createShareLink, listShareLinks,
  getOwnerInfo, vaultCommentsApi, shareCommentsApi,
} from './api/client';
import { ownerIdentity, shareIdentity, federatedIdentity } from './editor/comments/identity';
import { parseInviteLink, fetchShareInfo as fetchVaultShareInfo, InviteRejectedError } from './api/invites';
import { setActiveVault } from './api/vault-ops';
import {
  clearVaultCache,
  onConnectionStatus, onConnectionError, onLastSyncedAt, onIdbError,
  type AggregateConnectionStatus,
} from './editor/sync';
import { connectionState, UnauthorizedError, startHeartbeat } from './api/connection';
import {
  serverUrl, getShareUuid, getVaultNameFromUrl, getUrlHeaderAnchor, fetchShareInfo, getLoginUrl,
  getFederationInvite, getServiceGrantLink, type ShareInfo,
} from './config';
import type { Vault } from './api/types';
import { VaultPicker } from './components/VaultPicker';
import { Sidebar } from './components/sidebar/Sidebar';
import { MobileSidebar } from './components/sidebar/MobileSidebar';
import { EditorLayout, type EditorLayoutHandle } from './components/EditorLayout';
import { MobileShell } from './components/MobileShell';
import { resolveShellKind } from './app-settings';
import { ShareModal } from './components/ShareModal';
import { VaultShareModal } from './components/VaultShareModal';
import { FederationConnect } from './components/FederationConnect';
import { ServiceGrant } from './components/ServiceGrant';
import { WebSettingsModal } from './components/SettingsModal';
import { QuickOpen } from './components/QuickOpen';
import { SearchModal } from './components/SearchModal';

import DEFAULT_VIMRC from './default.vimrc?raw';

const BASE_TITLE = 'md-notes';

function fileLabel(path: string | null | undefined): string | null {
  if (!path) return null;
  const base = path.split('/').pop() || path;
  return base.replace(/\.md$/i, '');
}

/** Share URL for a doc, reusing an existing link with the right permission or creating one. */
async function shareUrlForDoc(serverPath: string, permission: 'read' | 'write'): Promise<string> {
  const links = await listShareLinks(serverPath);
  const existing = links.find((l) => l.permission === permission);
  const uuid = existing?.uuid ?? await createShareLink(serverPath, permission);
  return `${window.location.origin}/share/${uuid}`;
}

const ShareEditor: Component<{ uuid: string; info: ShareInfo }> = (props) => {
  let container!: HTMLDivElement;
  createEffect(() => {
    const name = fileLabel(props.info.doc_path);
    document.title = name ? `${name} — Shared · ${BASE_TITLE}` : `Shared · ${BASE_TITLE}`;
  });
  onMount(() => {
    // Shares are viewed by arbitrary visitors, so always use the standard editor.
    createEditor(container, {
      shareUuid: props.uuid,
      shareDocPath: props.info.doc_path,
      syncServerUrl: serverUrl,
      readOnly: props.info.permission !== 'write',
      anchorHeader: getUrlHeaderAnchor() ?? undefined,
      comments: {
        api: shareCommentsApi(props.uuid),
        identity: shareIdentity(),
        canComment: props.info.permission !== 'read',
        ui: 'panel',
      },
    });
  });
  return <div ref={container} id="editor-container" />;
};

const ShareView: Component<{ uuid: string }> = (props) => {
  const [info] = createResource(() => props.uuid, fetchShareInfo);
  return (
    <Show
      when={info()}
      fallback={
        info.error
          ? <div style={{ padding: '2rem' }}>Share link not found or revoked.</div>
          : <div style={{ padding: '2rem' }}>Loading…</div>
      }
    >
      {(data) => <ShareEditor uuid={props.uuid} info={data()} />}
    </Show>
  );
};

export const App: Component = () => {
  const shareUuid = getShareUuid();
  if (shareUuid) return <ShareView uuid={shareUuid} />;

  const federationInvite = getFederationInvite();
  if (federationInvite) return <FederationConnect invite={federationInvite} />;

  const serviceGrantLink = getServiceGrantLink();
  if (serviceGrantLink) return <ServiceGrant link={serviceGrantLink} />;

  const [activeVimrc, setActiveVimrc] = createSignal(DEFAULT_VIMRC);
  const [ownerName, setOwnerName] = createSignal('owner');
  const [editorKind, setEditorKind] = createSignal(getEditorKind());
  const [shellKind, setShellKind] = createSignal(resolveShellKind());
  const [vault, setVault] = createSignal<Vault | null>(null);
  const [vaultList, setVaultList] = createSignal<Vault[]>([]);
  const [showVaultPicker, setShowVaultPicker] = createSignal(false);
  const [booting, setBooting] = createSignal(true);
  const [currentDocPath, setCurrentDocPath] = createSignal<string | null>(null);
  const [shareModalPath, setShareModalPath] = createSignal<string | null>(null);
  const [shareVaultName, setShareVaultName] = createSignal<string | null>(null);
  const [showWebSettings, setShowWebSettings] = createSignal(false);
  const [showQuickOpen, setShowQuickOpen] = createSignal(false);
  const [showSearch, setShowSearch] = createSignal(false);
  const [syncErrorPath, setSyncErrorPath] = createSignal<string | null>(null);
  const [remoteVaultError, setRemoteVaultError] = createSignal<string | null>(null);
  const [syncStatus, setSyncStatus] = createSignal<AggregateConnectionStatus>(null);
  const [syncErrorMsg, setSyncErrorMsg] = createSignal<string | null>(null);
  const [lastSyncedAtTs, setLastSyncedAtTs] = createSignal<number | null>(null);
  const [idbError, setIdbError] = createSignal<string | null>(null);

  let layoutHandle: EditorLayoutHandle | undefined;

  createEffect(() => {
    const v = vault();
    const file = fileLabel(currentDocPath());
    if (v && file) {
      document.title = `${file} — ${v.name} · ${BASE_TITLE}`;
    } else if (v) {
      document.title = `${v.name} · ${BASE_TITLE}`;
    } else {
      document.title = BASE_TITLE;
    }
  });

  async function boot() {
    try {
      const saved = await getServerVimrc();
      if (saved) setActiveVimrc(saved);
    } catch {}
    try {
      setOwnerName((await getOwnerInfo()).displayName);
    } catch {}
    await loadWebVaults();
    setBooting(false);
    // Heartbeat keeps the connection indicator fresh while the user is idle.
    // Authed probe; failure routes to the connection-state signal but does NOT
    // trigger a redirect. Skipped while the tab is hidden.
    startHeartbeat(async () => {
      if (document.hidden) return;
      await pingHealth();
    });
  }

  async function fetchVaultListWithRetry(): Promise<Vault[]> {
    let delay = 500;
    while (true) {
      try {
        return await listVaults();
      } catch (e) {
        if (e instanceof UnauthorizedError) throw e;
        console.warn('Backend not ready, retrying:', e);
        await new Promise((r) => setTimeout(r, delay));
        delay = Math.min(delay * 2, 5000);
      }
    }
  }

  async function loadWebVaults() {
    let vaults: Vault[];
    try {
      vaults = await fetchVaultListWithRetry();
    } catch (e) {
      if (e instanceof UnauthorizedError) {
        const loginUrl = getLoginUrl();
        if (loginUrl) {
          window.location.href = loginUrl;
          return;
        }
        // No login URL configured (e.g. local dev): fall through so the user
        // sees the "Unauthorized" status indicator rather than a blank screen.
        setBooting(false);
        return;
      }
      throw e;
    }
    setVaultList(vaults);

    // Priority: URL path > sessionStorage > localStorage > VaultPicker
    const urlVault = getVaultNameFromUrl();
    if (urlVault) {
      const match = vaults.find((v) => v.name === urlVault);
      if (match) { openVault(match); return; }
      history.replaceState({}, '', '/');
    }

    try {
      const lastName =
        sessionStorage.getItem('mdnotes-last-vault') ??
        localStorage.getItem('mdnotes-last-vault');
      if (lastName) {
        const match = vaults.find((v) => v.name === lastName);
        if (match) { openVault(match); return; }
      }
    } catch {}

    setShowVaultPicker(true);
  }

  async function refreshVaultList() {
    try {
      setVaultList(await listVaults());
    } catch (e) {
      console.warn('Failed to refresh vault list:', e);
    }
  }

  function switchToVault(v: Vault) {
    if (v.name === vault()?.name) return;
    setVault(null);
    queueMicrotask(() => openVault(v));
  }

  function openVault(v: Vault) {
    setActiveVault(v);
    setVault(v);
    setShowVaultPicker(false);
    if (!v.owned) checkConnectedVault(v);
    if (v.name) {
      try {
        sessionStorage.setItem('mdnotes-last-vault', v.name);
        localStorage.setItem('mdnotes-last-vault', v.name);
      } catch {}
      history.replaceState({}, '', '/' + encodeURIComponent(v.name));
    }
    if (v.owned) {
      createVault(v.name).catch(() => {});
    }
  }

  // The sharing instance can change under us between sessions (upgraded API, revoked share), so
  // re-verify the handshake every time a connected vault is opened. Non-blocking: the vault opens
  // optimistically and a failure surfaces as a modal.
  function checkConnectedVault(v: Vault) {
    setRemoteVaultError(null);
    fetchVaultShareInfo(v.host, v.secret ?? '').catch((e) => {
      if (vault()?.id !== v.id) return;
      const host = (() => {
        try { return new URL(v.host).host; } catch { return v.host; }
      })();
      if (e instanceof InviteRejectedError) {
        setRemoteVaultError(`${host} rejected this vault's share — it may have been revoked.`);
      } else if (e instanceof TypeError) {
        // fetch network failure — the instance is unreachable; sync status already shows offline.
      } else {
        setRemoteVaultError(`Can't use this shared vault: ${e instanceof Error ? e.message : e}`);
      }
    });
  }

  function switchVault() {
    setVault(null);
    listVaults().then((vaults) => {
      setVaultList(vaults);
      setShowVaultPicker(true);
    }).catch((e) => {
      console.error('Failed to load vaults:', e);
      setVaultList([]);
      setShowVaultPicker(true);
    });
  }

  function handleFileSelect(path: string) {
    layoutHandle?.openFile(path);
  }

  function makeEditorForPath(
    path: string,
    container: HTMLElement,
    onSyncFailed: (err: Error) => void,
  ): EditorInstance {
    const v = vault();
    const mobile = shellKind() === 'mobile';
    return createEditor(container, {
      // The mobile shell always uses the mobile editor variant; the editor
      // preference setting only applies to the desktop shell.
      kind: mobile ? 'live-preview-mobile' : editorKind(),
      vimrcContent: activeVimrc(),
      touch: mobile,
      vault: v ?? undefined,
      syncFilePath: path,
      syncServerUrl: serverUrl,
      readOnly: v ? v.permission !== 'write' : false,
      // Our OpenHost username labels our cursor for other collaborators — on our own vaults and,
      // as a federated user, on connected ones.
      userName: ownerName(),
      // Per-file share links are minted by this instance, so they only exist for owned vaults.
      getShareUrl: v?.owned
        ? (permission) => shareUrlForDoc(`${v.name}/${path}`, permission)
        : undefined,
      comments: v
        ? {
            api: vaultCommentsApi(v, path),
            identity: v.owned ? ownerIdentity(ownerName()) : federatedIdentity(ownerName()),
            canComment: v.permission !== 'read',
            ui: mobile ? 'highlights' : 'panel',
          }
        : undefined,
      onSyncFailed,
    });
  }

  function handleVaultSelect(v: Vault) {
    openVault(v);
  }

  async function handleVaultAdd(name: string, path: string, sync: boolean) {
    openVault(await createVault(name));
  }

  async function handleConnectRemote(link: string) {
    const invite = parseInviteLink(link);
    if (!invite) {
      throw new Error('That doesn\'t look like an invite link — expected https://…/federation/connect?…');
    }
    // Validate straight from the browser against the sharing instance (secret + API version);
    // our own server just stores the connection record.
    const info = await fetchVaultShareInfo(invite.host, invite.secret);
    const connected = await connectVault(invite.host, info.vault, invite.secret, info.permission);
    const vaults = await listVaults();
    setVaultList(vaults);
    const match = vaults.find((v) => v.id === connected.id);
    if (match) openVault(match);
  }

  async function handleVaultRemove(v: Vault) {
    if (v.owned) await deleteVault(v.name);
    else await disconnectVault(v.id);
    await clearVaultCache(v.id);
    await loadWebVaults();
  }

  onMount(() => {
    boot().catch(console.error);

    const unsubStatus = onConnectionStatus((s) => setSyncStatus(s));
    const unsubError = onConnectionError((m) => setSyncErrorMsg(m));
    const unsubSynced = onLastSyncedAt((ts) => setLastSyncedAtTs(ts));
    const unsubIdb = onIdbError((m) => setIdbError(m));
    onCleanup(() => { unsubStatus(); unsubError(); unsubSynced(); unsubIdb(); });

    const onKey = (e: KeyboardEvent) => {
      const key = e.key.toLowerCase();
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && e.key === '\\') {
        e.preventDefault();
        layoutHandle?.splitPane();
      }
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && !e.altKey && e.code === 'Backslash') {
        e.preventDefault();
        layoutHandle?.toggleCollapseActivePane();
      }
      if (e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey && key === 'o') {
        e.preventDefault();
        e.stopPropagation();
        if (vault()) setShowQuickOpen(true);
      }
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && !e.altKey && key === 'f') {
        e.preventDefault();
        e.stopPropagation();
        if (vault()) setShowSearch(true);
      }
      if (e.ctrlKey && !e.metaKey && !e.shiftKey && key === 'h') {
        e.preventDefault();
        e.stopPropagation();
        layoutHandle?.focusGroupLeft();
      }
      if (e.ctrlKey && !e.metaKey && !e.shiftKey && key === 'l') {
        e.preventDefault();
        e.stopPropagation();
        layoutHandle?.focusGroupRight();
      }
      if (e.metaKey && e.ctrlKey && e.shiftKey && key === 'h') {
        e.preventDefault();
        e.stopPropagation();
        layoutHandle?.focusTabLeft();
      }
      if (e.metaKey && e.ctrlKey && e.shiftKey && key === 'l') {
        e.preventDefault();
        e.stopPropagation();
        layoutHandle?.focusTabRight();
      }
    };
    document.addEventListener('keydown', onKey, true);
    onCleanup(() => document.removeEventListener('keydown', onKey, true));
  });

  return (
    <>
      <Show when={booting()}>
        <div style={{ padding: '2rem', color: '#888' }}>Connecting to server…</div>
      </Show>

      <Show when={!booting() && !vault() && !showVaultPicker() && connectionState() === 'unauthorized'}>
        <div style={{ padding: '2rem', color: '#888' }}>
          Not logged in. Configure <code>OPENHOST_ZONE_DOMAIN</code> to enable automatic redirect to login.
        </div>
      </Show>

      <Show when={!booting() && showVaultPicker() && !vault()}>
        <VaultPicker
          vaults={vaultList()}
          onSelect={handleVaultSelect}
          onAdd={handleVaultAdd}
          onRemove={handleVaultRemove}
          onConnectRemote={handleConnectRemote}
        />
      </Show>

      <Show when={vault()}>
        <Show
          when={shellKind() === 'desktop'}
          fallback={
            <MobileShell
              ref={(h) => { layoutHandle = h; }}
              createEditor={makeEditorForPath}
              onActiveFileChange={setCurrentDocPath}
              onSyncFailed={(path) => setSyncErrorPath(path)}
              vaultName={vault()!.name}
              drawerContent={
                <MobileSidebar
                  vaultName={vault()!.name}
                  vaults={vaultList()}
                  isRemote={!vault()!.owned}
                  permission={vault()!.permission}
                  onSelect={handleFileSelect}
                  onDeleted={(path) => layoutHandle?.closeFilesUnder(path)}
                  onQuickOpen={() => setShowQuickOpen(true)}
                  onSearch={() => setShowSearch(true)}
                  onShare={vault()!.owned ? setShareModalPath : undefined}
                  onShareVault={vault()!.owned ? () => setShareVaultName(vault()!.name) : undefined}
                  onSwitchToVault={switchToVault}
                  onManageVaults={switchVault}
                  onRefreshVaults={refreshVaultList}
                  onSettings={() => setShowWebSettings(true)}
                  showSyncStatus={true}
                  syncStatus={syncStatus()}
                  syncErrorMsg={syncErrorMsg() ?? undefined}
                  backendStatus={connectionState()}
                  lastSyncedAt={lastSyncedAtTs()}
                  idbError={idbError()}
                  currentPath={currentDocPath()}
                />
              }
            />
          }
        >
          <Sidebar
            vaultName={vault()!.name}
            vaults={vaultList()}
            isRemote={!vault()!.owned}
            permission={vault()!.permission}
            onSelect={handleFileSelect}
            onDeleted={(path) => layoutHandle?.closeFilesUnder(path)}
            onSearch={() => setShowSearch(true)}
            onShare={vault()!.owned ? setShareModalPath : undefined}
            onShareVault={vault()!.owned ? () => setShareVaultName(vault()!.name) : undefined}
            onSwitchToVault={switchToVault}
            onManageVaults={switchVault}
            onRefreshVaults={refreshVaultList}
            onSettings={() => setShowWebSettings(true)}
            showSyncStatus={true}
            syncStatus={syncStatus()}
            syncErrorMsg={syncErrorMsg() ?? undefined}
            backendStatus={connectionState()}
            lastSyncedAt={lastSyncedAtTs()}
            idbError={idbError()}
            currentPath={currentDocPath()}
          />
          <EditorLayout
            ref={(h) => { layoutHandle = h; }}
            createEditor={makeEditorForPath}
            onActiveFileChange={setCurrentDocPath}
            onSyncFailed={(path) => setSyncErrorPath(path)}
            vaultName={vault()!.name}
          />
        </Show>
      </Show>

      <Show when={shareModalPath()}>
        <ShareModal
          path={shareModalPath()!}
          vaultName={vault()?.name}
          onClose={() => setShareModalPath(null)}
        />
      </Show>

      <Show when={shareVaultName()}>
        <VaultShareModal
          vaultName={shareVaultName()!}
          onClose={() => setShareVaultName(null)}
        />
      </Show>

      <Show when={vault() && showQuickOpen()}>
        <QuickOpen
          onSelect={(path) => layoutHandle?.openFile(path)}
          onClose={() => setShowQuickOpen(false)}
        />
      </Show>

      <Show when={vault() && showSearch()}>
        <SearchModal
          vault={vault()!}
          onSelect={(path, line) => layoutHandle?.openFileAt(path, line)}
          onClose={() => setShowSearch(false)}
        />
      </Show>

      <Show when={showWebSettings()}>
        <WebSettingsModal
          initialVimrc={activeVimrc()}
          onSaved={(v, kind) => {
            setActiveVimrc(v);
            setEditorKind(kind);
            setShellKind(resolveShellKind());
            setShowWebSettings(false);
          }}
          onClose={() => setShowWebSettings(false)}
        />
      </Show>

      <Show when={remoteVaultError()}>
        <div class="modal-backdrop" onClick={() => setRemoteVaultError(null)}>
          <div class="modal" onClick={(e) => e.stopPropagation()}>
            <h3>Shared vault problem</h3>
            <p>{remoteVaultError()}</p>
            <div class="modal-actions">
              <button onClick={() => setRemoteVaultError(null)}>OK</button>
            </div>
          </div>
        </div>
      </Show>

      <Show when={syncErrorPath()}>
        <div class="modal-backdrop" onClick={() => setSyncErrorPath(null)}>
          <div class="modal" onClick={(e) => e.stopPropagation()}>
            <h3>Can't reach the backend</h3>
            <p>
              Couldn't open <code>{syncErrorPath()}</code>. Docs can only be
              opened while the server is reachable so you don't end up editing
              stale content.
            </p>
            <div class="modal-actions">
              <button onClick={() => setSyncErrorPath(null)}>OK</button>
            </div>
          </div>
        </div>
      </Show>
    </>
  );
};

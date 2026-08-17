import { createResource, Show, type Component } from 'solid-js';
import { serverUrl, type FederationInvite } from '../config';
import { fetchShareInfo, InviteRejectedError } from '../api/invites';

interface Props {
  invite: FederationInvite;
}

const GITHUB_URL = 'https://github.com/cloud-in-a-bottle/md-notes';

const PERMISSION_LABELS = { read: 'view only', comment: 'can comment', write: 'can edit' } as const;

/**
 * Landing page shown when an invite link is opened directly. Invite links point at the *sharing*
 * instance, so the visitor here is usually the recipient: tell them to paste the link into their
 * own md-notes. Purely informational — connecting happens in the recipient's instance.
 */
export const FederationConnect: Component<Props> = (props) => {
  const inviteUrl = window.location.href;

  const [info] = createResource(async () => {
    if (!props.invite.secret) throw new Error('This invite link is missing its secret.');
    return fetchShareInfo(serverUrl, props.invite.secret);
  });

  function copyLink(btn: HTMLButtonElement) {
    navigator.clipboard.writeText(inviteUrl);
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = 'Copy invite link'; }, 1500);
  }

  function errorMessage(e: unknown): string {
    if (e instanceof InviteRejectedError) return 'This invite is invalid or has been revoked.';
    return String(e instanceof Error ? e.message : e);
  }

  return (
    <div class="federation-connect">
      <div class="vault-picker-card">
        <div class="vault-picker-title">Shared vault invite</div>

        <Show when={!info.loading} fallback={<p>Checking this invite…</p>}>
          <Show when={!info.error} fallback={<p>{errorMessage(info.error)}</p>}>
            <Show when={info()}>
              {(data) => (
                <>
                  <p>
                    This link is an invite to the vault <strong>{data().vault}</strong> on this
                    md-notes instance ({PERMISSION_LABELS[data().permission]}).
                  </p>
                  <p>
                    To connect it, open <strong>your own</strong> md-notes, choose{' '}
                    <em>Manage vaults… → Connect a shared vault</em>, and paste this link.
                  </p>
                  <div class="share-modal-buttons">
                    <button
                      class="share-modal-btn share-modal-btn-primary"
                      onClick={(e) => copyLink(e.currentTarget)}
                    >Copy invite link</button>
                  </div>
                </>
              )}
            </Show>
          </Show>
        </Show>

        <p class="federation-connect-footer">
          Don't have md-notes yet? It's open source — get it at{' '}
          <a href={GITHUB_URL}>{GITHUB_URL.replace('https://', '')}</a> and deploy it on your own
          OpenHost space, then come back to this link.
        </p>
      </div>
    </div>
  );
};

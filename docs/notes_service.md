# The notes service

md-notes provides an OpenHost [cross-app service](https://github.com/cloud-in-a-bottle/cloud-in-a-bottle/blob/main/docs/src/cross_app_services.md)
so other apps in the same space can read notes: `github.com/cloud-in-a-bottle/md-notes/services/notes`.
The published spec is in [`services/notes/`](../services/notes/); this file is about how it's built.

Read-only for now. Everything is shaped so comment and write tiers can be added later without a new
grant format.

## Endpoints

Rooted at `/api/service/notes/` (the `endpoint` in `[[services.v2.provides]]`); a consumer calls
`$OPENHOST_ROUTER_URL/api/services/v2/call/<shortname>/<endpoint>`.

| Endpoint | Returns |
|---|---|
| `GET files[?vault=]` | every file the caller may read, as `{vault, path}` |
| `GET file-headers?vault=&path=` | the file's ATX headings, with GitHub-style slugs |
| `GET file-section?vault=&path=&header=` | one section — heading, body, and subheadings — as raw markdown |
| `GET file?vault=&path=` | the whole file as raw markdown |

Content is read through `SyncManager.read_doc`, i.e. out of the document's live `Y.Doc`, so a note
someone is editing right now returns the in-flight text rather than the last debounced save. What
comes back is the plain markdown body: comments live in a separate `Y.Map` on the same doc and never
appear here.

## Trusting the caller

`NotesServiceController` opts out of the app-wide owner guard and instead requires
`X-OpenHost-Consumer-Id`, which only the router sets — it strips any `X-OpenHost-*` header a client
tried to send. Authorization is then purely the grants in `X-OpenHost-Permissions`.

Whole-vault grants would otherwise let a path like `../other/secret.md` through the matcher, so
every read also checks the path resolves inside the vault (`file_exists` raises on traversal).

## Permissions

Grants are app-scoped and data-dependent — they name a vault in *this* instance:

```json
{"vault": "personal", "paths": "*", "access": "read"}
```

`paths` is `"*"` (whole vault, future files included) or a list of exact file paths. `access` is a
tier ordered `read < comment < write`; `service_grants.py` already matches all three, so adding
write endpoints later means adding routes, not changing the grant shape. Global-scoped grants are
ignored on purpose: one made against a *different* provider of this service must never unlock this
instance's data.

Because these can't be declared in a consumer's manifest, md-notes owns the whole approval UX:

1. A call without a sufficient grant hits `ServicePermissionRequired`. The exception handler records
   the caller's identity (from the router's headers) against a token in `service_grant_requests`,
   and returns the documented 403 with `grant_url = <this app>/service/grant?request=<token>`.
2. The consumer shows the owner that link with its own `return_to=` appended.
3. `ServiceGrant.tsx` renders the consent page: who is asking, then a vault picker, then either the
   whole vault or a file picker. The requesting app's name comes from the token, never from the URL,
   so a doctored link can't misrepresent who wants access.
4. `POST /api/service-grants/approve` validates the selection and calls the router's
   `grant_app_scoped` with our app token. The router takes the granting provider from that token, so
   md-notes can only ever grant access to itself.
5. The page sends the browser back to `return_to` with `granted=1` (or `granted=0` if declined), and
   the consumer retries. `return_to` is only honoured if it points inside this OpenHost space —
   otherwise this would be an open redirect driven by an arbitrary app.

Grant requests are reused per (consumer, tier) and expire after 24h.

## Testing it

`tests/consumer_app/` is a stdlib-only OpenHost app that consumes the service — it has pages a
browser can drive plus a `POST /call-service` hook for tests. `tests/test_service_e2e.py` deploys it
next to md-notes on a real local router and walks the whole flow, including a Playwright pass over
the consent page.

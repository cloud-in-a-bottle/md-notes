# notes service

`github.com/cloud-in-a-bottle/md-notes/services/notes` — read-only access to a markdown vault for
other apps in the same OpenHost space. The full spec, including the permission-grant shape and the
consent flow, is in [openapi.yaml](openapi.yaml).

Consume it with:

```toml
[[services.v2.consumes]]
service = "github.com/cloud-in-a-bottle/md-notes/services/notes"
shortname = "notes"
version = ">=0.1.0"
```

Don't declare `grants` — this service only honours app-scoped grants, which the owner creates in
md-notes' own consent page (linked from the `403` you get on your first call).

Versions are tagged `services/notes:vX.Y.Z`.

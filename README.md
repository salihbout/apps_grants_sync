# Databricks Apps — bundle workaround for `USE_CATALOG` / `USE_SCHEMA`

A small post-deploy pipeline that closes a gap in Databricks Asset Bundles:
the `resources.apps[*].resources[*].uc_securable` field can only grant a
narrow set of leaf privileges (`READ_VOLUME`, `WRITE_VOLUME`, `SELECT`,
`EXECUTE`, `MODIFY`, `USE_CONNECTION`), but a Databricks App's auto-created
service principal also needs `USE_CATALOG` on the parent catalog and
`USE_SCHEMA` on the parent schema before it can actually read/write the
volume/table/function declared in the bundle.

The Terraform provider rejects those privileges in the `uc_securable` field
today:

```
panic: value "USE_CATALOG" is not one of "EXECUTE", "MODIFY", "READ_VOLUME",
"SELECT", "USE_CONNECTION", "WRITE_VOLUME"
```

This repo provides a bundle hook that grants the missing parent privileges
automatically on every `databricks bundle deploy`, with no per-app config.

## How it works

1. `tools/postdeploy.sh` is wired into the bundle via
   `experimental.scripts.postdeploy`. It runs after every successful deploy.
2. It invokes `tools/grant_app_parents.py bundle`, which:
   1. Calls `databricks bundle summary -o json` to enumerate every app in
      the current bundle and their `uc_securable` resources.
   2. For each app, fetches the auto-created SP via `apps.get()` to read
      `service_principal_client_id`.
   3. For each `uc_securable.securable_full_name` (`catalog.schema.object`),
      derives the parents and grants `USE_CATALOG` on the catalog and
      `USE_SCHEMA` on the schema to the SP.
   4. Deduplicates so apps with N volumes in the same schema get one grant.

The script is idempotent — re-runs are no-ops once privileges are in place.

## Repo layout

```
.
├── tools/
│   ├── grant_app_parents.py   # generic grant logic; bundle + single modes
│   └── postdeploy.sh          # invoked by experimental.scripts.postdeploy
└── test/
    ├── app/                   # minimal Flask app that writes to a volume
    └── databricks.yml         # demo bundle wired with the postdeploy hook
```

## Adopt it in an existing bundle

Add three lines to your bundle's `databricks.yml`:

```yaml
experimental:
  scripts:
    postdeploy: bash <path-to-this-repo>/tools/postdeploy.sh
```

Keep declaring leaf privileges the way you already do:

```yaml
resources:
  apps:
    dap_one:
      name: ${var.app_name}
      source_code_path: ../
      resources:
        - name: volume
          uc_securable:
            securable_full_name: hne_dap_${var.environment}.volumes.app
            securable_type: VOLUME
            permission: WRITE_VOLUME
```

Then deploy as usual:

```sh
databricks bundle deploy -t dev
```

Adding a new app later? Declare it in `resources.apps`, deploy — the hook
picks it up automatically.

## Standalone use (one-off / outside a bundle)

If you need to grant parents for an app that isn't in a bundle:

```sh
python tools/grant_app_parents.py single \
    --app-name my-app \
    --securable my_catalog.my_schema.my_volume \
    --profile DEFAULT
```

## Requirements

- Databricks CLI ≥ 0.234 (for `experimental.scripts`).
- Python 3.10+ with `databricks-sdk>=0.30.0`. `tools/postdeploy.sh` prefers
  `<repo-root>/.venv/bin/python` if present, otherwise pip-installs the SDK
  into the user site for the active `python3`.
- Permission to grant on the target catalog and schema (the user running
  `bundle deploy` must already have `MANAGE` or be the owner).

## Caveats

- **Profile disambiguation.** If the machine has multiple Databricks CLI
  profiles matching the same host, set `DATABRICKS_CONFIG_PROFILE` (or pin
  `workspace.profile` in the target) so the nested `bundle summary` call
  inside the hook can resolve auth.
- **Securable shape.** The script assumes 3-part names
  (`catalog.schema.object`). That covers `VOLUME`, `TABLE`, and `FUNCTION`.
  `CONNECTION` is 2-part — it's skipped with a warning. Extend `parents_of`
  if you need different handling.
- **SDK quirk.** The Python SDK's `grants.update()` mis-serializes
  `SecurableType` into the REST URL path on the version pinned here, so the
  script issues the PATCH directly via `api_client.do(...)` with the
  enum's `.value`. If/when the SDK fixes this you can swap back to
  `w.grants.update(...)`.
- **Terraform key expiry.** Unrelated to this workaround, but a fresh
  machine may hit
  `error downloading Terraform: unable to verify checksums signature: openpgp: key expired`
  when the bundle CLI auto-downloads Terraform. Work around with a local
  binary:
  ```sh
  export DATABRICKS_TF_EXEC_PATH=$(command -v terraform)
  export DATABRICKS_TF_VERSION=$(terraform version -json | jq -r .terraform_version)
  ```

## Why a hook and not Terraform / a Job?

- A Databricks Job would spin up compute for what is effectively a couple of
  REST calls — unnecessary cold-start cost on every deploy.
- A sidecar `databricks_grants` Terraform module is a valid alternative if
  you already mix raw Terraform with bundles. The hook keeps everything in
  the bundle workflow with one less moving part.
- A CI post-step works too, but the bundle hook makes the fix invisible to
  every developer who runs `bundle deploy` locally.

## When to retire this

When `resources.apps[*].uc_securable` (or an equivalent bundle field) accepts
`USE_CATALOG` / `USE_SCHEMA` directly, drop the hook and let the bundle
manage the grants natively. There is no public ETA for this today.

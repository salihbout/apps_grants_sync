"""
Post-deploy workaround for the Databricks Apps bundle `uc_securable` limitation.

Bundles can grant leaf privileges (READ_VOLUME, WRITE_VOLUME, SELECT, EXECUTE,
MODIFY, USE_CONNECTION) on an app's auto-created SP via
`resources.apps[*].resources[*].uc_securable`, but not the USE_CATALOG /
USE_SCHEMA needed on the parents. This script closes that gap.

Two modes:

  single    Grant parents for one (app, securable) pair — useful for one-off use.
  bundle    Walk every app in the current bundle (via `databricks bundle summary`)
            and grant parents for each of its uc_securable entries. Designed to
            run from `experimental.scripts.postdeploy`.

The SDK currently mis-serializes SecurableType into the REST URL path for
grants.update (emits `SECURABLETYPE.CATALOG` instead of `CATALOG`), so we call
the REST endpoint directly.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.catalog import PermissionsChange, Privilege


@dataclass(frozen=True)
class Parent:
    kind: str           # "CATALOG" or "SCHEMA"
    full_name: str      # e.g. "my_cat" or "my_cat.my_schema"
    privilege: Privilege


def parents_of(securable_full_name: str) -> list[Parent]:
    parts = securable_full_name.split(".")
    if len(parts) != 3:
        raise ValueError(
            f"expected catalog.schema.object, got {securable_full_name!r}"
        )
    catalog, schema, _ = parts
    return [
        Parent("CATALOG", catalog, Privilege.USE_CATALOG),
        Parent("SCHEMA", f"{catalog}.{schema}", Privilege.USE_SCHEMA),
    ]


def grant(w: WorkspaceClient, parent: Parent, principal: str) -> None:
    print(f"  grant {parent.privilege.value} on {parent.kind} "
          f"{parent.full_name} -> {principal}")
    w.api_client.do(
        "PATCH",
        f"/api/2.1/unity-catalog/permissions/{parent.kind}/{parent.full_name}",
        body={
            "changes": [
                PermissionsChange(principal=principal, add=[parent.privilege]).as_dict()
            ]
        },
    )


def resolve_sp(w: WorkspaceClient, app_name: str) -> str:
    app = w.apps.get(name=app_name)
    sp = getattr(app, "service_principal_client_id", None)
    if not sp:
        raise RuntimeError(
            f"app {app_name!r} has no service_principal_client_id; "
            "deploy the app before running this script"
        )
    return sp


def process(w: WorkspaceClient, app_name: str, securables: list[str]) -> None:
    sp = resolve_sp(w, app_name)
    print(f"app {app_name!r} SP={sp}")
    seen: set[tuple[str, str, str]] = set()
    for s in securables:
        for p in parents_of(s):
            key = (p.kind, p.full_name, sp)
            if key in seen:
                continue
            seen.add(key)
            grant(w, p, sp)


def bundle_summary(bundle_dir: str, target: str | None, profile: str | None) -> dict:
    cmd = ["databricks", "bundle", "summary", "--output", "json"]
    if target:
        cmd += ["--target", target]
    if profile:
        cmd += ["--profile", profile]
    res = subprocess.run(cmd, cwd=bundle_dir, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"bundle summary failed:\n{res.stderr}")
    return json.loads(res.stdout)


def run_bundle(args: argparse.Namespace) -> int:
    summary = bundle_summary(args.bundle_dir, args.target, args.profile)
    apps = (summary.get("resources") or {}).get("apps") or {}
    if not apps:
        print("no apps in bundle — nothing to do")
        return 0
    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    for _, app in apps.items():
        name = app.get("name")
        securables = [
            r["uc_securable"]["securable_full_name"]
            for r in (app.get("resources") or [])
            if r.get("uc_securable")
        ]
        if not securables:
            print(f"app {name!r}: no uc_securable resources, skipping")
            continue
        process(w, name, securables)
    print("done.")
    return 0


def run_single(args: argparse.Namespace) -> int:
    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    process(w, args.app_name, [args.securable])
    print("done.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--profile", default=None)
    sub = p.add_subparsers(dest="mode", required=True)

    b = sub.add_parser("bundle", help="discover apps from `databricks bundle summary`")
    b.add_argument("--bundle-dir", default=".")
    b.add_argument("--target", default=None)
    b.set_defaults(func=run_bundle)

    s = sub.add_parser("single", help="grant parents for one (app, securable) pair")
    s.add_argument("--app-name", required=True)
    s.add_argument("--securable", required=True,
                   help="full name catalog.schema.object")
    s.set_defaults(func=run_single)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

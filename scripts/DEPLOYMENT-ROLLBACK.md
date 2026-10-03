# cPanel release cleanup and rollback

The shared `A1-CM/.github` toolkit provides cPanel deployment and rollback without shell access. LogicStrand caller workflows reference immutable toolkit tag `v1.0.8`; publish that tag before publishing the callers. A live cPanel deployment is a separate operation.

## Deployment behavior

- The app remains in `<CPANEL_HOME>/<project>-app/releases/<release>`. `public_html` contains the managed Laravel entry point and public assets. Shared storage and `deployment-history.json` remain under `<project>-app/shared`.
- After activation, the runner checks the configured `health_path` (default `/up`) three times. A health or pre-commit finalization failure restores the previous entry point, routing rules, and overwritten public files. If the finalization response is lost after history is committed, recovery preserves the new release and warns that cleanup may be incomplete. Database migrations are **not** reversed.
- After a healthy activation, the toolkit records the new active release and at most seven previous releases. It deletes only matching project staging ZIPs in `CPANEL_HOME` and strict release-ID directories beyond that set. Failed releases and ZIPs remain until a later healthy deployment.
- The first updated run imports the current managed release and up to seven complete older releases. Incomplete releases are excluded from rollback and pruned after the healthy deployment.
- The deployer asks cPanel for other domain document roots and rejects public files that would enter a nested addon domain root. Rollback reuses this protection. Domains that share exactly the same document root intentionally share its public entry point; review those in cPanel before deployment.

## Manual rollback

Run the LogicStrand repository's **Roll back LogicStrand on cPanel** action and enter `steps_back` from `1` to `7`. `1` restores the most recently active earlier release. A completed rollback places the release it replaced first in history, so another `steps_back=1` can return to it. The action checks `/up` and restores the prior live files if that check fails.

The reusable rollback workflow needs `CPANEL_HOST`, `CPANEL_USERNAME`, `CPANEL_HOME`, and `APP_URL` variables, plus the `CPANEL_API_TOKEN` secret, with the same access as deployment. `CPANEL_HEALTH_PATH` is optional. It uses the same concurrency group as deployment.

## Rollout order

1. Run the toolkit unit tests and, where a staging cPanel account is available, a deployment and rollback. Confirm retained releases, ZIP cleanup, and preservation of addon-domain files.
2. Publish toolkit tag `v1.0.8` and then publish the LogicStrand deploy and rollback callers pinned to it.

The deployment history is private and file permissions are restricted. A missing or corrupt history file stops manual rollback rather than guessing a release. The first updated deployment can import complete legacy releases before it writes history. The cPanel API token and generated application credentials are never printed in workflow output.

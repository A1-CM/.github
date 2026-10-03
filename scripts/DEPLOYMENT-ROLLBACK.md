# cPanel release cleanup and rollback

This local copy is based on the public `A1-CM/.github` `v1.0.7` toolkit. Publish its changed workflow and `scripts/` files to that repository as a new immutable tag before publishing the LogicStrand caller workflows that reference `v1.0.8`. No remote repository or cPanel account is changed by this workspace.

## Deployment behavior

- The app remains in `<CPANEL_HOME>/<project>-app/releases/<release>`. `public_html` contains the managed Laravel entry point and public assets. Shared storage and `deployment-history.json` remain under `<project>-app/shared`.
- After activation, the runner checks the configured `health_path` (default `/up`) three times. A failure invokes the restore endpoint, which puts the previous entry point, routing rules, and overwritten public files back. Database migrations are **not** reversed.
- After a healthy activation, the toolkit records the new active release and at most seven previous releases. It deletes only matching project staging ZIPs in `CPANEL_HOME` and strict release-ID directories beyond that set. Failed releases and ZIPs remain until a later healthy deployment.
- The first updated run imports the current managed release and up to seven complete older releases. Incomplete releases are excluded from rollback and pruned after the healthy deployment.
- The deployer asks cPanel for other domain document roots and rejects public files that would enter a nested addon domain root. Rollback reuses this protection. Domains that share exactly the same document root intentionally share its public entry point; review those in cPanel before deployment.

## Manual rollback

Run the repository's **Roll back LogicStrand on cPanel** action and enter `steps_back` from `1` to `7`. `1` restores the most recently active earlier release. A completed rollback places the release it replaced first in history, so another `steps_back=1` can return to it. The action checks `/up` and restores the prior live files if that check fails.

The reusable rollback workflow needs `CPANEL_HOST`, `CPANEL_USERNAME`, `CPANEL_HOME`, and `APP_URL` variables, plus the `CPANEL_API_TOKEN` secret, with the same access as deployment. `CPANEL_HEALTH_PATH` is optional. It uses the same concurrency group as deployment.

## Publishing order

1. Copy the changed `scripts/deploy_cpanel.py`, `scripts/cpanel_release_manager.php`, tests, and both reusable workflow files from this local toolkit to `A1-CM/.github`.
2. Run its unit tests and a staging cPanel deployment/rollback. Confirm the staging site, retained releases, ZIP cleanup, and preservation of addon-domain files.
3. Publish immutable toolkit tag `v1.0.8` after staging passes.
4. Publish this repository's `.github/workflows/deploy-cpanel.yml` and `.github/workflows/rollback-cpanel.yml`, both pinned to `v1.0.8`.

The LogicStrand workflow files are prepared locally now but must be published only after the toolkit tag exists. The deployment history is private and file permissions are restricted. A missing or corrupt history file stops rollback rather than guessing a release. The cPanel API token and generated application credentials are never printed in workflow output.

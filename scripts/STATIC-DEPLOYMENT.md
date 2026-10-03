# Deploy a React or Next.js static build to cPanel

The reusable workflow `.github/workflows/static-cpanel-reusable.yml` builds the caller repository with `npm run build`, then uploads only the built output through the cPanel API. It needs no shell access on cPanel. The built release stays outside the document root under `<CPANEL_HOME>/<project>-static/releases/<release>`; the selected domain's document root receives the exported files. Other files are left in place unless they have the same paths as exported files or belong to an earlier managed release. Nested addon-domain roots and symlinks are protected. Deployment stops if another domain shares the exact document root, because replacing the entry point would affect that domain.

## Inputs

- `project_slug` and `app_url` are required. The slug must be unique within the cPanel account; `app_url` is the HTTPS origin.
- `output_dir` defaults to `auto`. After the build, exactly one of `dist/index.html` or `out/index.html` must exist. Set a relative path such as `build` or `packages/web/dist` for other layouts. The directory must contain `index.html`.
- `routing_mode` defaults to `auto`: a package with a `next` dependency uses `next`; other packages use `spa`. Use `files` for a site with only file-based routes. `next` maps extensionless paths to exported `.html` files while leaving directory routes intact. `spa` falls back to `index.html` for missing paths. The toolkit inserts a domain-scoped, project-marked block into `.htaccess` and preserves other rules.
- `public_dir` defaults to `auto`, using cPanel's document root for the domain. `allow_index_replace` must be true for the first deployment over an existing unrelated `index.html`. An existing `index.php` blocks static activation.
- `health_path` defaults to `/`; it must return HTTP 200 after activation. `node_version` defaults to `22`.

The caller repository must provide `CPANEL_HOST`, `CPANEL_USERNAME`, and `CPANEL_HOME` as GitHub variables, and `CPANEL_API_TOKEN` as a secret. `CPANEL_DOMAIN` and `CPANEL_PUBLIC_DIR` are optional variables. The toolkit repository is public and needs no read token.

## Next.js

Set `output: 'export'` in `next.config.js` or `next.config.ts`. `next build` then writes the static site to `out` by default. Server-only features, including request-time rendering, require a Node.js deployment and cannot run from a static export. The generated `out` directory is deployed directly; `.next` is not deployed. If the site uses a custom output directory, set `output_dir` explicitly.

## Caller example

```yaml
name: Deploy static site
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
jobs:
  deploy:
    uses: A1-CM/.github/.github/workflows/static-cpanel-reusable.yml@v1.0.8
    with:
      toolkit_repository: A1-CM/.github
      toolkit_ref: v1.0.8
      project_slug: mysite
      app_url: ${{ vars.APP_URL }}
      output_dir: auto
      public_dir: auto
    secrets: inherit
```

After activation, the runner checks health three times. A failure restores the previous public files, including overwritten or removed assets and `.htaccess`. The PHP endpoint stores backups and history outside the document root. After a healthy finalization, it retains the active release and seven previous releases and removes matching staging ZIPs. Database or server-side runtime behavior is outside this static workflow.

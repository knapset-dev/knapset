# Changelog

All notable changes to Knapset are documented in this file.

## [Unreleased]

- Made `TestMarketplaceClient` deterministic offline via `locate_registry_dir()` and added mocked HTTP tests for remote registry directory downloads.
- Updated remaining `main.py` references in `web_server.py` and `ARCHITECTURE.md` to include `ks`.
- Upgraded GitHub Actions workflows (`actions/checkout`, `actions/setup-python`, `actions/upload-artifact`) to `v7`.

## [1.0.1] - 2026-11-17

- Wired `KNAPSET_REGISTRY_URL` (including `file://` local registry checkouts) in `MarketplaceClient` and ensured `http://`/`https://` registries download directory package files remotely first.
- Added `"app_paths"` to `CORE_ALLOWED_MODULES` in `marketplace/security.py`.
- Unified dynamic `ks` / `knapset` / `python3 main.py` command prefix across CLI subparsers and onboarding hints.
- Added `module install-bundle` and `module uninstall` CLI subcommands/aliases.
- Added `MANIFEST.in` so PyPI source distributions include `docs/` and `packaging/`.
- Added automated Arch Linux `.pkg.tar.zst` + `.sha256` release workflow (`release-arch.yml`) and expanded CI wheel/extension smoke tests.

## [1.0.0] - 2026-11-16

- Initial public release of Knapset (`ks` / `knapset`) with XDG runtime directory fallback (`app_paths.py`), PyPI packaging (`pyproject.toml`), OIDC Trusted Publishing (`publish-pypi.yml`), and Arch Linux `PKGBUILD`.

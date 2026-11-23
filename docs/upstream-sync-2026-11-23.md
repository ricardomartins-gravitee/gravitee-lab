# Upstream integration review, November 23, 2026

Review this integration before merging it into the fork's `main`. It merges upstream history into the existing fork history; it does not rebase, squash, reset, or replace the fork's commits.

## Compared history

- Fork: `ricardomartins-gravitee/gravitee-lab`, `main` at `697c2b6d195263d4e138bbc3afc5a10b97646d2d`.
- Upstream: `gravitee-io-labs/gravitee-lab`, `main` and release `v1.20.27` at `b98b9c848b37b1498c166ca448bfa3afaf9c7051`.
- Last common commit: upstream `v1.20.15`, `07565c5b0e22906137d7a88e376ecc434d8705d9`.
- Divergence: 10 fork commits and 12 upstream commits. The fork's latest published release is `v1.20.15-rev.8`; the subsequent seeder-image commit is retained too.
- Upstream tags were fetched into `refs/upstream-tags/`, independently of the fork's tags. No existing tag was replaced.

## Imported releases

| Release | Commit | Change |
|---|---|---|
| [v1.20.16](https://github.com/gravitee-io-labs/gravitee-lab/releases/tag/v1.20.16) | `aba9739` | Seeder: two fixes of MCP studios, accented API names and the keys of a studio made in one go (#104) |
| [v1.20.17](https://github.com/gravitee-io-labs/gravitee-lab/releases/tag/v1.20.17) | `4edbca8` | Seeder: Authorization Management takes its principals from Access Management (#105) |
| [v1.20.18](https://github.com/gravitee-io-labs/gravitee-lab/releases/tag/v1.20.18) | `bd612b0` | Docs: what makes a good playground, and how to check one before calling it done (#106) |
| [v1.20.19](https://github.com/gravitee-io-labs/gravitee-lab/releases/tag/v1.20.19) | `68975e2` | Seeder: workflow tools in the AI catalog, and the response templates of studios and proxies (#107) |
| [v1.20.20](https://github.com/gravitee-io-labs/gravitee-lab/releases/tag/v1.20.20) | `c590538` | Seeder: releases by a table, builds by their exact version, and make compat to measure an image (#108) |
| [v1.20.21](https://github.com/gravitee-io-labs/gravitee-lab/releases/tag/v1.20.21) | `3ffcf05` | Seeder: model sources, and the providers of an LLM proxy taken from them (#109) |
| [v1.20.22](https://github.com/gravitee-io-labs/gravitee-lab/releases/tag/v1.20.22) | `84b3ec4` | Seeder: a control may pause a subscription, the kill switch of an agent, and an MCP proxy takes subscriptions (#110) |
| [v1.20.23](https://github.com/gravitee-io-labs/gravitee-lab/releases/tag/v1.20.23) | `a3fe009` | Seeder: a studio's file says what the gateway records, as an MCP proxy's does (#112) |
| [v1.20.24](https://github.com/gravitee-io-labs/gravitee-lab/releases/tag/v1.20.24) | `b76e6c8` | Seeder: Authorization Management imports its resources from the AI catalog, and a policy has a type (#113) |
| [v1.20.25](https://github.com/gravitee-io-labs/gravitee-lab/releases/tag/v1.20.25) | `3397684` | Seeder: an application or an agent of Access Management may sign in with its own key (#114) |
| [v1.20.26](https://github.com/gravitee-io-labs/gravitee-lab/releases/tag/v1.20.26) | `1105c84` | Seeder: a model component of an AI workspace is replaced model by model (#115) |
| [v1.20.27](https://github.com/gravitee-io-labs/gravitee-lab/releases/tag/v1.20.27) | `b98b9c8` | Lab UI: a guide quote that opens with [!NOTE] is drawn as a note (#111) |

## Retained fork additions

| Commit / release | Retained behavior |
|---|---|
| `fc0d402` | Fetch public catalog dependencies without credentials |
| `fed2ac3`, `v1.20.15-rev.1` | Independent fork release previews, upstream-based `-rev.N` numbering, immutable publication safeguards and fork version/update display |
| `d9a6320`, `v1.20.15-rev.2` | Fetch dependencies on each app build runner |
| `a726f28`, `v1.20.15-rev.3` | Fetch dependencies on Nightly and Publish runners |
| `79cb5fb`, `v1.20.15-rev.4` | Fetch dependencies on every reading runner and preserve the fork's catalog templates and pins |
| `4d33e7a`, `v1.20.15-rev.5` | API and studio primary ownership, studio metadata, workspace tags, shared user lookup, Edge configuration, validation and reconciliation |
| `97bbec2`, `v1.20.15-rev.6` | Plain MCP studio tool keys, flows, explicit empty upstream auth, runtime gateway targets and deployment reconciliation |
| `17a1d90`, `v1.20.15-rev.7` | Portal OpenAPI viewer settings, including Try it out |
| `2dbe6ed`, `v1.20.15-rev.8` | Environment-scoped shared policy groups, deploy-before-API order, recovery records, and safe removal when another API or plan still references a group |
| `697c2b6`, after `rev.8` | Publish and prefer the fork's seeder images, with source identity checks and local build fallback |

The release workflows, catalog dependency changes and templates, fork image selector, Edge module, portal module, and seedlint remain byte-for-byte equal to fork `main`. All fork commits remain ancestors of the integration.

## Overlap and conflict resolutions

Ten files had textual conflicts: `docs/playgrounds.md`, `seeder/AGENTS.md`, `aim_catalog.py`, `aim_governance.py`, `aim_proxies.py`, `versions.py`, and their catalog, proxy, API Management and version test modules. Resolve these by behavior, retaining both sets of regression cases.

- Keep upstream's release/build tables, preflight checks, exact build recognition and gateway-version waiting. Add the fork's credential usage-surface distinction as a table behavior, including a compatibility probe. Apply it to upstream workflow credentials as well as ordinary catalog credentials: releases omit the removed field; the known demo build retains it.
- Combine upstream studio analytics, workflow-tool resolution, subscription controls, protocol handling and response templates with fork ownership, metadata, validated upstream API keys, runtime target routing, and deployment reconciliation. Remove the duplicate upstream-auth method produced by the merge. Exercise analytics together with fork studio metadata for both studio shapes.
- Keep upstream's governance version checks and fork shared-user resolution. Keep Edge-only version detection and upstream's AM/Gamma-link detection.
- Keep shared-policy-group fake-platform routes and upstream subscription pause/resume routes; retain both sets of tests.
- Keep existing fork contract levels: Edge at 6 and shared policy groups at 7. Put the newly imported workflow tools, model sources and catalog-backed LLM proxies at fork level 8. Upstream's levels 6–7 described different additions and cannot be reused as the history of this fork. Keep `contract_min` at 1.
- Combine the documentation so both upstream capabilities and fork additions remain described. Regenerate the version tables from the integrated code.

## Other branches and local work

The published seeder contribution branch `feature/seed-api-coverage-upstream` at `5be8580` has the same `seeder/` tree, `contract.lock`, and product mapping as fork `main` before this sync. Its two differently shaped commits (`adcd108`, `5be8580`) do not represent missing seeder changes to cherry-pick. [Upstream seeder PR #102](https://github.com/gravitee-io-labs/gravitee-lab/pull/102) and [catalog-runner PR #101](https://github.com/gravitee-io-labs/gravitee-lab/pull/101) are still open; their fork behavior is retained here.

The separate sandbox feature is [fork PR #6](https://github.com/ricardomartins-gravitee/gravitee-lab/pull/6), at `c0bdddc736ee0377cdd30dfeec489c9e68426172`, with 45 commits beyond fork `main` (including its merge from `main`). Keep it separate from the default-branch sync. It already contains the fork's seeder-image change. Do not reset or force-push this branch.

The companion review branch `feature/sync-sandboxes-v1.20.27` merges this integration into that existing sandbox history, without changing the original branch. The content merge is clean, but the sandbox snapshot-flush label must move from level 8 to level 9: the synchronized base now uses level 8 for the imported seed folders. The companion raises its contract to 9, retains the new folders at 8, and changes no production seeder code relative to this integration. Merge the default-branch sync first, then merge the companion into the sandbox feature with a merge commit; keep PR #6 as a separate feature review.

Existing working copies were inspected without changing them. The installed sandbox checkout and the seeder, portal, shared-policy-group and studio review copies were clean. The catalog validation copy has an untracked `.needs/` directory; the source-only dependency fixture has tracked files absent from its working tree. Leave both as found. Local experimental and backup branches remain in their original repositories.

## Verification and publication

| Check | Result |
|---|---|
| `make check` | All good, on both integration branches |
| `make lint` | Pass, on both integration branches |
| `make test`, Lab UI | 5,386 tests across 98 modules, pass; includes the JavaScript page suite |
| `make test`, repository | 134 tests, pass; one Windows PowerShell-only test skipped on macOS |
| `make test`, seeder | 1,520 tests, pass, including the retained ownership, group-removal, portal, studio and Edge cases |
| Python 3.9 import compatibility | All 97 Lab UI modules import |
| Sandbox companion | 432 tests across contract, catalog understanding, manifest, registry, storage cloning, runtime settings, server and page modules, pass |
| Documentation links and commands | Two repository documentation tests, pass |
| History | Both original main tips are ancestors of the default integration; both the original sandbox tip and the default integration are ancestors of the sandbox companion |
| Fork release planning, read only | Candidate branch resolves to `v1.20.27-rev.1`, based on upstream `v1.20.27`, after `v1.20.15-rev.8`; this is a calculation, not a publication |

Tests use a nonexistent Docker endpoint, so they cannot operate the installed Lab. The first restricted attempt could not open temporary local test servers; the complete suite was rerun with that permission and passed. No installed `.env`, catalog, secret, container or data was changed. Live smoke, container end-to-end and image compatibility measurements require a disposable test environment and were not run on the installed Lab. The new credential compatibility probe is available for that later image verification; this sync does not claim new live-image measurements.

No release, image publication, tag push or default-branch change is part of this review. The fork release workflow remains manual. After review and merge, preview the new fork version against the then-current `main` before separately approving publication. Merge this PR with a merge commit to retain upstream ancestry; squash or rebase merging would defeat history-based future syncs and release-base detection.

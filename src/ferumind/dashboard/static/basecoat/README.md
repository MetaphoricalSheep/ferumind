# Vendored Basecoat theme

Ferumind's local operator dashboard vendors the canonical Basecoat theme from:

- source repository: [MetaphoricalSheep/basecoat](https://github.com/MetaphoricalSheep/basecoat);
- source revision: `70369cc32afc4f5c517e09743172ba9dc36e34f8`.

The same exact 40-character Git SHA is stored in [`REVISION`](REVISION).

These files are copied byte-for-byte and must not be edited in Ferumind:

| Vendored file | Basecoat source path |
| --- | --- |
| `tokens.css` | `packages/theme/src/tokens.css` |
| `base.css` | `packages/theme/src/base.css` |
| `components.css` | `packages/theme/src/components.css` |

Dashboard-specific styles belong in the sibling `dashboard.css`, never in these
vendored files.

The pinned Basecoat revision contains no explicit license file or package license
declaration. These copies are included under authorization from the repository
owner for this project; do not infer that Ferumind's surrounding MIT license
relicenses the Basecoat files. If distribution terms change, update this notice
and carry the applicable upstream license or permission notice.

## Refreshing the snapshot

Check out the desired Basecoat commit locally with the three relevant theme files
clean, then run this from the Ferumind repository root:

```console
just sync-basecoat /path/to/basecoat
```

The synchronizer performs no network activity. It validates the checkout, refuses
staged, unstaged, or untracked changes to the relevant theme paths, copies the
committed bytes atomically, and updates `REVISION` last. Review the resulting diff
before committing it.

Ferumind intentionally consumes this CSS offline. It does not use Basecoat's Tier 1
Tailwind or Alpine CDN runtime, and ordinary setup does not require a Basecoat
checkout, Node, or internet access.

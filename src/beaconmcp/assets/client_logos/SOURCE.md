# Client logos

- `simpleicons/*.svg`: [Simple Icons](https://simpleicons.org) v16.30.0 (CC0 1.0), downloaded from `https://cdn.jsdelivr.net/npm/simple-icons@16.30.0/icons/`. `_meta.json` keeps each icon's title and brand hex from `data/simple-icons.json`. The logos remain trademarks of their owners; they are shown only to identify the client connecting to BeaconMCP.
- `generic.svg`: drawn for BeaconMCP.

OpenAI asked Simple Icons to drop its logo, so Codex and ChatGPT are resolved at runtime from the favicon of their own site (see `client_identity.LogoResolver`).

To add an icon: download it from the same pinned version, add its slug to `_meta.json`, and if the client name doesn't normalise to the slug, add an alias in `client_identity._SI_ALIASES`.

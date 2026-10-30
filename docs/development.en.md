# Development Guide

English | [中文](development.md)

> Type: tutorial | Layer: contributor onboarding. Gates are listed in [scripts/run-gates.mjs](../scripts/run-gates.mjs); this page covers usage.

## Environment

- Node ≥ 22, pnpm ≥ 10, Python ≥ 3.12.
- Run `pnpm install && pnpm setup:api` after cloning; install hooks with `pnpm install:hooks`.
- Authenticate `gh` for `flow:sync` and `verify:flow-online`.
- `pnpm dev` enables API docs; production defaults closed or sets `HE_DOCS_ENABLED=false`.
- `pnpm dev` defaults to SQLite; MySQL sets `HE_DB_DIALECT`/`HE_DB_URL` with environment-only credentials.
- Local sessions/data stay gitignored; CI uses synthetic fixtures.

## Development flow

Code changes require an issue; commit and PR titles include `(#N)`. Creation, synchronization, and state rules live in the [flow contract](../.agents/flow/README.en.md).

## CodeAgent integration

Select `SKILL_EVOLUTION_MODEL_BACKEND=codeagent` and `SKILL_EVOLUTION_CODEAGENT_ENABLED=1`, with model and evaluation enabled. `SKILL_EVOLUTION_CODEAGENT_CMD` selects a native executable; `SKILL_EVOLUTION_CODEAGENT_ENV_NAMES` lists model environment names to inherit. Inject credentials locally. See the [CLI adapter](../apps/api/app/third_party_api/codeagent.py) for other settings.

## Daily loop

The command list lives in [package.json](../package.json). Start locally with `pnpm dev` and select validation from the table below.

Choose gates by change surface before committing:

| Change | Run locally |
|---|---|
| Docs / Agent Note only | `pnpm lint:docs` |
| Web code | `pnpm lint:web && pnpm typecheck:web && pnpm test:web && pnpm test:integration && pnpm test:e2e && pnpm verify:web-style && pnpm build && pnpm verify:bundle` |
| Backend code | `pnpm lint:api && pnpm typecheck:api && pnpm test:api && pnpm test:db && pnpm test:integration` |
| Contracts or cross-package types | `pnpm contract:check && pnpm typecheck && pnpm test:all` |
| Flow tree | `pnpm verify:flow` |
| Documentation examples or links | `pnpm verify:docs` |

When unsure, run `pnpm route:gates`.

## Docs and pairing

Resident docs are paired across Chinese and English. After changing either side, minimally patch the other side in the same PR and run `pnpm record:i18n -- <pair>`; the contract is in [i18n/README.en.md](i18n/README.en.md). Fences marked `ts` are compiled for real, while fences marked `ts type-equiv` are compared verbatim with source.

## PR review

Review follows [review.en.md](review.en.md); use `.agents/skills/pr-review/SKILL.md` for structured output and `.agents/skills/pr-review-fix/SKILL.md` to address review comments. Review documents live in `plans/` and never enter Agent Notes.

## Commits and release

- Every non-trivial change carries one Agent Note in the same PR (new or updated owning note).
- Commit messages are written in Chinese, with the issue reference in the title; the body explains motivation, trade-offs and verification commands.
- Before release, run `pnpm verify:docs`, `pnpm verify:flow` and the full test suite; no release pipeline is configured yet.

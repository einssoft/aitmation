# ui/ — the product interface

React + TypeScript (strict) + Vite + Mantine. Structured by tasks, with a
workshop area that can be hidden. Language files from the first line: the
interface ships in German (the reference), French, Italian and English.
**Household shell:** one application over one or several core instances —
pending decisions, cases, apps and settings are merged across all
reachable mandants, and a switch in the header narrows the view.

## Layout

```
src/
├── api/client.ts      typed access to /api/v1 of one instance
├── household.tsx      household: discover members, clients, selection, useAcrossHousehold()
├── i18n/              de.json (reference), fr/it/en, index.tsx (t(), pick(), formats)
├── nav.ts             the one source of the navigation
├── components/        shell, process canvas, designer, dialogs
└── pages/             one file per page
scripts/i18n-check.mjs build gate: every language carries every key of the German file
```

Conventions: no hard-wired text — every string through `t("key")`,
multilingual fields from the core through `pick()`. Dates and numbers
through `Intl`. State with React context and hooks; no state framework
until there is a second reason for one. Data access through
`InstanceClient`; lists across the household through `useAcrossHousehold`.
A new menu entry is added in `src/nav.ts` only and belongs to one of the
groups.

## Develop

```
cd ui
npm install
npm run dev          # http://localhost:5173/app/  (proxy to a core on 127.0.0.1:8420)
npm run build        # i18n check + type check + build into dist/
```

The core serves `dist/` under `/app` when it exists (also straight from a
checkout: `ui/dist` next to `core/`), otherwise from `AITMATION_UI_DIR`.
Behind the launcher the shell reaches every instance through the same
origin; directly, the core allows CORS for loopback origins only.

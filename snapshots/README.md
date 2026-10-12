# The snapshots

Five downloads of the same credit-union website, taken over about five months. Each folder is the **whole website as our system saw it on that date**: every page we had, in its newest version as of that day.

| Folder | Date | What happened around this pull |
|---|---|---|
| `01_2026-05-12_baseline` | 12 May 2026 | First full download (~640 pages). The starting point. |
| `02_2026-05-19_recrawl` | 19 May 2026 | Almost every page re-downloaded and changed. This pull caused the most new contradictions. |
| `03_2026-06-09_recrawl` | 9 Jun 2026 | Another near-full refresh. Rates moved again. |
| `04_2026-08-27_rebaseline` | 27 Aug 2026 | First pull after a 10-week gap. ~50 new pages appeared. |
| `05_2026-09-28_latest` | 28 Sep 2026 | The newest download. "Current" means this one. |

The downloads ship as zips in `../snapshots_zip/`. The scorer unpacks them here on its first run (or unzip them yourself). Each folder contains:

- `manifest.json`: for every page, its `url`, `title`, the time it was downloaded (`fetched_at`) and its file name
- `pages/*.md`: the page text as markdown


Things you will find (on purpose; this is real data):

- rates that changed between downloads, with the old page still around
- the same number shown on a rates table and in a blog post or press release from years ago
- pages that show different tiers (by balance, term or loan amount) of the same product
- pages that only exist in some snapshots

# Per-Page Artifacts — Design

**Date:** 2026-09-20
**Status:** Approved for implementation

## Purpose

Expose a converted document page by page. A caller asks for one page — or the whole
document broken into pages — and receives that page's Markdown, its flat text, and
the images and tables that sit on it, in a single response.

Today the API is document-shaped: one Markdown file, one DoclingDocument JSON, and
flat `images[]` / `tables[]` arrays spanning the whole document. A client that wants
to work a page at a time (rendering a viewer, feeding an LLM page by page, reviewing
a long scan) has to reassemble that itself, and cannot get per-page Markdown at all.

## Success criteria

- `GET /v1/documents/{id}/pages/3` returns page 3's Markdown, flat text, images and
  tables in one response, with no follow-up fetches needed for the text content.
- `GET /v1/documents/{id}/pages` returns the same for every page in one call.
- A page's `images` and `tables` entries are byte-identical to the corresponding
  entries from `/images` and `/tables`.
- Both routes follow the existing artifact-route status rules: 404 unknown job,
  409 while not succeeded, 401 unauthenticated.
- The fast test suite covers all of it without docling or torch.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Which "images per page" | Extracted pictures on that page | Manifest entries already carry `page`; no pipeline change, no new image files |
| Page renders | Not included | Needs `generate_page_images=True`, costing conversion time and a full-page PNG per page on *every* job |
| Content delivery | Inline in the JSON | One request per page; page Markdown is typically a few KB |
| `GET /pages` shape | Every page, inline, one request | Simplest to consume; size characteristic accepted and documented |
| Storage | Per-page files on disk, written at conversion time | Matches every other artifact; keeps the manifest an index |
| Counts in manifest | None | `images[]`/`tables[]` already carry `page`; a second copy could drift |
| File reads in handlers | Via `run_in_threadpool` | `/pages` on a large document is many reads; blocking the loop stalls the whole process |

### Storage: why not the manifest, and why not on demand

Two alternatives were considered and rejected.

**Per-page content inside the manifest.** Appealing — no new files, no `JobPaths`
change, and the route becomes a pure manifest read. Rejected because the manifest
lives in a SQLite column that is loaded on *every* `GET /v1/documents/{id}`, every
`/images` request and every `/tables` request. Inlining a whole document's text
would make all of those pay for content they never use. The manifest must stay an
index, not a store.

**Computing per-page exports on demand from `document.json`.** No storage and no
manifest change, but it re-parses a potentially multi-MB JSON on every request, puts
CPU in the request path, and requires `docling_core` where CLAUDE.md forbids docling
imports. Convert-once-serve-many is this application's shape; this fights it.

## Architecture

No new layers. The change lands in the four places that already exist.

### Conversion — `app/conversion/artifacts.py`

`write_artifacts` gains `_write_pages`, in the same defensive shape as `_write_tables`
and `_write_pictures`: one page failing to export becomes a warning and is skipped,
rather than discarding a whole successful conversion.

```python
for page_no in sorted(document.pages):
    markdown = document.export_to_markdown(page_no=page_no)
    text = document.export_to_text(page_no=page_no)
    # -> pages/page-001.md, pages/page-001.txt
```

Both `export_to_markdown` and `export_to_text` accept `page_no` natively, so no
partitioning logic is needed here.

### Storage — `app/storage/paths.py`

Mirrors the existing `tables_dir` / `table_file` pattern:

```
pages_dir(job_id)                   -> <job>/output/pages
page_file(job_id, page_no, fmt)     -> <job>/output/pages/page-001.md | .txt
```

`create()` makes the new directory alongside the others. `{page_no}` is an integer
path parameter, so unlike `images/{name}` there is no traversal surface to defend.

### Manifest

Gains a `pages` array storing **filenames**, consistent with `tables`:

```json
"pages": [
  { "page": 1, "width": 612.0, "height": 792.0,
    "markdown": "page-001.md", "text": "page-001.txt" }
]
```

No `image_count` or `table_count`. The `images[]` and `tables[]` arrays already record
each item's `page`; duplicating counts here would create a second source of truth that
can silently disagree with the first.

### API — `app/api/schemas.py`, `app/api/routes.py`

```
GET /v1/documents/{id}/pages           -> {"pages": [PageDetail, ...]}
GET /v1/documents/{id}/pages/{page_no} -> PageDetail
```

```json
{
  "page": 3,
  "width": 612.0,
  "height": 792.0,
  "markdown": "## Revenue\n\nRevenue held steady...",
  "text": "Revenue\n\nRevenue held steady...",
  "images": [ /* ImageRef, identical to /images entries */ ],
  "tables": [ /* TableRef, identical to /tables entries */ ]
}
```

`images` and `tables` are produced by the existing `build_image_refs` /
`build_table_refs` helpers, filtered by page. The manifest→response mapping stays in
one place, as CLAUDE.md requires.

A new `build_page_details(job, base, contents)` helper assembles the list, where
`contents` maps a page number to its `(markdown, text)` pair. The route reads the
files (off the event loop, see below) and hands the results in; the helper stays a
pure function, so the schema layer performs no I/O and is trivially testable.

## Data flow

```
conversion  -> export_to_markdown(page_no) / export_to_text(page_no)
            -> pages/page-001.md, page-001.txt
            -> manifest["pages"][] records the filenames

GET /pages  -> _require_succeeded
            -> run_in_threadpool: read every page's .md and .txt
            -> group images[]/tables[] by page
            -> PageDetail list, content inline
```

### Blocking I/O

These are the first handlers in the application that read artifact files themselves
rather than handing Starlette a `FileResponse`. On a 500-page document, `/pages`
performs 1000 synchronous reads; on the event loop that stalls every other request in
this deliberately single-process server. All page-file reads therefore go through
`run_in_threadpool`, the same way `create_document` already offloads `_write_upload`.

## Error handling

Both routes use `_require_succeeded`, so they behave exactly like the existing artifact
routes: 404 for an unknown job, 409 while the job is queued, running or failed, 401
without a key. Every response uses the project's `{"error": {"code", "message"}}`
envelope.

`export_to_markdown(page_no=99)` returns `""` rather than raising, so an out-of-range
page would otherwise look like an empty page. The route therefore resolves the page
from `job.manifest["pages"]` and 404s before touching disk — the same rule CLAUDE.md
states for `tables/{index}` and `images/{name}`.

A page listed in the manifest whose file is missing is a defence-in-depth case, not a
reachable one — the manifest only ever names files that were just written. Both routes
render such a page with `markdown` and `text` as `null` and still return 200.

This is deliberate and worth stating plainly, because the obvious alternative is worse:
404-ing the page would mean that on `GET /pages` a single unreadable file sinks the
entire document's response. `null` also stays distinguishable from `""`, which is what
a genuinely blank page returns. The two routes behave identically, so there is no case
where fetching one page succeeds and fetching all of them fails.

Consequently `markdown` and `text` are typed `string | null` in the response schema.

## Known limitations

**Pages do not always sum to the document.** Content that docling could not place —
items with no provenance — appears in the whole-document Markdown but in no page's
export. Verified against `docling-core`:

```
full document : 'HAS prov, page 1\n\nNO prov at all'
page 1        : 'HAS prov, page 1'
```

This is docling's behaviour. A client needing guaranteed-complete text should use
`/markdown` or `document.json`, not a concatenation of pages. To be stated in the
README rather than worked around.

**`GET /pages` is unbounded.** Response size grows with the document; a long scan
produces a large response and there is no way for a caller to ask for less. This is a
deliberate choice in favour of one-request consumption. `/pages/{page_no}` is the
escape hatch for clients that want bounded responses. To be documented as a known
property.

**Blank pages are real entries.** A page docling registered but extracted nothing from
returns `""` — not `null` — for both `markdown` and `text`, with empty `images` and
`tables`. It is still listed: omitting it would make a blank page indistinguishable
from one that failed to export.

**A document with no registered pages yields `"pages": []`**, not an error, even where
`/markdown` has content — the no-provenance case above taken to its limit.

## Testing

The fast suite carries the weight, with no docling and no torch. `docling-core` is a
dev dependency, so a real multi-page `DoclingDocument` can be built directly.

**`tests/test_artifacts.py`** — per-page files written with the right names; page 1's
text absent from page 2's export; content with no provenance in neither page; page
dimensions recorded; an export failure degrading to a warning without discarding the
conversion; a document with no pages producing `"pages": []`.

**`tests/test_routes_pages.py`** (new) — both routes; the full status matrix (404
unknown job, 409 running, 409 failed, 401 unauthenticated); 404 for an out-of-range
page; images and tables correctly partitioned by page; and an agreement test that each
page's `images` entries are exactly the `/images` entries carrying that page number,
which is the guard on the shared builders.

**`tests/test_openapi.py`** — both routes declare 200/401/404/409, matching the
convention check already applied to the other artifact routes.

## Files touched

| File | Change |
|---|---|
| `app/conversion/artifacts.py` | `_write_pages`, manifest `pages[]` |
| `app/storage/paths.py` | `pages_dir`, `page_file`, `create()` |
| `app/api/schemas.py` | `PageDetail`, `PageList`, `build_page_details` |
| `app/api/routes.py` | Two routes, threadpool reads |
| `README.md` | Route docs, both known limitations |
| `CLAUDE.md` | Manifest contract key list, blocking-I/O invariant |

## Out of scope

- Page renders (`generate_page_images`). Revisit if a viewer UI needs them.
- Per-page HTML. `export_to_html(page_no=...)` exists, but nothing asked for it, and
  HTML carries the same-origin concern that makes `tables/{index}?format=html` an
  attachment.
- Standalone `/pages/3/markdown` routes. Content is inline; separate format routes
  would be a second way to fetch the same bytes.
- Pagination or filtering of `/pages`.

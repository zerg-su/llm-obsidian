---
name: defuddle
metadata:
  version: 1.0.0
description: "Strip navigation, ads, and boilerplate from a saved local HTML page into readable Markdown. Triggers: defuddle; clean or strip this HTML file; remove clutter. Not for URLs — /wiki-ingest handles those."
allowed-tools: Read Bash
---

# defuddle: Local Page Cleaner

Defuddle extracts the article body from an HTML page as clean Markdown, dropping ads,
cookie banners, navigation, related articles, footers, and share buttons.

It runs on local files only. Fetching a URL here would pull untrusted web content into the
vault-aware context; for a URL use `/wiki-ingest <URL>` (protected isolation flow) or `/research`.
Do not obtain a URL's content here —
not with `defuddle <URL>`, `curl`/`wget`, or WebFetch. For a URL use `/wiki-ingest <URL>`.
"It's the CLI, not WebFetch" is still fetching.

## Install

```bash
npm install -g defuddle-cli
```
Verify: `defuddle --version`

## Usage

```bash
defuddle page.html            # Markdown to stdout
```
With provenance, when the user supplies the original URL:
```bash
{ echo "---"; echo "source_url: <original URL>"; echo "cleaned: $(date +%Y-%m-%d)"; echo "---"; echo ""; defuddle page.html; }
```
`.raw/` is read-only for the assistant: return the result; the user decides where to save it.
A saved file can then go through local `/wiki-ingest <path>`.

## When to use

Use for a saved article, blog post, or documentation page with heavy surrounding chrome.
Skip when the source is already clean Markdown or PDF, or the page is a dashboard, app, or
structured data.

## Fallback

If `which defuddle` finds nothing, perform a bounded local cleanup of the same local file:
keep the page title and main article/documentation body; remove site navigation,
breadcrumb-only blocks, project/version selectors, search/help chrome, copyright/footer
blocks, and unrelated previous/next-page lists. Verify that at least one main-content heading
or paragraph remains and that known navigation/footer labels are absent. Report this visibly
as `manual fallback`; never describe raw input as defuddled content.

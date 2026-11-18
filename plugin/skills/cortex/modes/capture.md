# Capture (ingest, Karpathy-style)
One source at a time, with the learner watching:
1. Read it (a URL they pasted, text in chat, or `cortex_read("raw/…")`). Discuss the 3–5 key takeaways with them.
2. `cortex_add(kind="resource", title, fields={summary, resource_type, url?}, raw_text?)`. Pass `raw_text` only for text they pasted; it is saved once, immutable, in `raw/`. The resource body holds the literature note: its ideas in the learner's words, never long quotes.
3. For each idea worth keeping, update the pages it touches. Use `cortex_find` first, then patch an existing concept/note, or add a new one: concepts start Unverified and are never graded by capture.
4. Link everything: `related`, `covers` on the resource, `[[id]]` in bodies.
5. `cortex_log(kind="capture", payload={resource, pages:[ids touched]})`.
A rough thought with no source is a fleeting note: `cortex_add(kind="note", title, fields={note_type: "fleeting"})` puts one line in the inbox.

**Cap:** from a single chat or source, propose at most 3 permanent notes. Everything else becomes a fleeting line or a journal sentence. Ask before a 4th. A source with no durable idea becomes only a `resource` plus a journal line.

## Zettelkasten rules for notes
- **Permanent note** (`note_type="permanent"`): one idea, in the learner's own words, understandable alone. The `summary` states the idea in one line.
- **Linked with a reason:** link it to at least one existing page, and in its `## Links` section give each link a short reason (`- [[two-pointers]] — same shrinking-window invariant`).
- **Structure note** (`note_type="structure"`): a hub that orders related notes, one line each.
- **Quotes** stay in `raw/`; notes cite the `[[resource]]`.

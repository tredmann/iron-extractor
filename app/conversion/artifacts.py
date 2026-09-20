import json
import logging

from app.storage.paths import JobPaths

logger = logging.getLogger(__name__)


def write_artifacts(
    document,
    paths: JobPaths,
    job_id: str,
    warnings: list[str] | None = None,
) -> dict:
    """Write every artifact for a converted document and return its manifest.

    Takes a `docling_core.types.doc.DoclingDocument`. Typed loosely so this
    module imports nothing from docling itself.

    Markdown and the document JSON are the conversion: if either fails the
    job has produced nothing and the exception propagates. Individual tables
    and pictures are not -- one unexportable table must not discard a whole
    document (and, with OCR, minutes of work), so each is written
    defensively and a failure becomes a warning rather than a dead job.
    """
    markdown_bytes = _write_markdown(document, paths, job_id)
    document_bytes = _write_document_json(document, paths, job_id)

    tables, table_warnings = _write_tables(document, paths, job_id)
    images, picture_warnings = _write_pictures(document, paths, job_id)
    pages, page_warnings = _write_pages(document, paths, job_id)

    return {
        "page_count": document.num_pages(),
        # Warnings from the caller (docling's own component errors) come
        # first; per-item export failures found here are appended.
        "warnings": [*(warnings or []), *table_warnings, *picture_warnings, *page_warnings],
        "markdown": {"bytes": markdown_bytes},
        "document": {"bytes": document_bytes},
        "tables": tables,
        "images": images,
        "pages": pages,
    }


def _write_markdown(document, paths: JobPaths, job_id: str) -> int:
    target = paths.markdown_file(job_id)
    target.write_text(document.export_to_markdown(), encoding="utf-8")
    return target.stat().st_size


def _write_document_json(document, paths: JobPaths, job_id: str) -> int:
    target = paths.document_file(job_id)
    target.write_text(
        json.dumps(document.export_to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    return target.stat().st_size


def _write_tables(document, paths: JobPaths, job_id: str) -> tuple[list[dict], list[str]]:
    entries: list[dict] = []
    warnings: list[str] = []
    for index, table in enumerate(document.tables):
        try:
            csv_target = paths.table_file(job_id, index, "csv")
            html_target = paths.table_file(job_id, index, "html")

            frame = table.export_to_dataframe(doc=document)
            frame.to_csv(csv_target, index=False)
            html_target.write_text(table.export_to_html(doc=document), encoding="utf-8")

            entry = {
                "index": index,
                "rows": table.data.num_rows,
                "cols": table.data.num_cols,
                "page": _page_of(table),
                "csv": csv_target.name,
                "html": html_target.name,
            }
        except Exception as exc:
            # No manifest entry is appended, so the artifact routes will
            # answer 404 for this index; any half-written file left behind
            # is unreachable and goes away with the job.
            #
            # The warning goes to an HTTP caller (JobResult.warnings), so it
            # carries only the exception type, not str(exc): docling and the
            # stdlib routinely embed a container-internal filesystem path in
            # their messages (a full disk reports the path being written
            # to). The full exception -- message included -- goes to the log
            # only, mirroring the fix already applied to job.error_message.
            logger.exception("Job %s: table %d could not be exported", job_id, index)
            warnings.append(
                f"Table {index} could not be exported and was skipped ({type(exc).__name__})."
            )
            continue
        entries.append({**entry, **_metadata_of(table, document)})
    return entries, warnings


def _write_pictures(document, paths: JobPaths, job_id: str) -> tuple[list[dict], list[str]]:
    entries: list[dict] = []
    warnings: list[str] = []
    for index, picture in enumerate(document.pictures):
        try:
            image = picture.get_image(document)
            if image is None:
                # The pipeline ran without generate_picture_images; nothing
                # to save, and nothing worth warning about.
                logger.debug("Picture %d of job %s carries no image", index, job_id)
                continue

            name = f"picture-{index:03d}.png"
            target = paths.image_file(job_id, name)
            image.save(target, format="PNG")

            entry = {
                # docling's picture index, not the position in this list:
                # pictures without an image are skipped above, and this is
                # the number `self_ref` agrees with.
                "index": index,
                "name": name,
                "kind": _label_of(picture),
                "page": _page_of(picture),
                "bytes": target.stat().st_size,
                # Free: the image is already decoded in memory.
                "width": image.width,
                "height": image.height,
            }
        except Exception as exc:
            # Type only, not the message -- see the matching comment in
            # _write_tables. A full-disk write here reports the target PNG's
            # path under jobs_root the same way.
            logger.exception("Job %s: picture %d could not be exported", job_id, index)
            warnings.append(
                f"Picture {index} could not be exported and was skipped ({type(exc).__name__})."
            )
            continue
        entries.append({**entry, **_metadata_of(picture, document)})
    return entries, warnings


def _write_pages(document, paths: JobPaths, job_id: str) -> tuple[list[dict], list[str]]:
    """Export each page's markdown and flat text to its own file.

    docling takes `page_no` natively on both exporters, so there is no
    partitioning to do here. Defensive per page, like tables and pictures:
    one page that will not export must not discard a whole conversion.

    Note that pages do not necessarily sum to the document -- content
    docling could not place on a page appears in the whole-document export
    and in none of these.
    """
    entries: list[dict] = []
    warnings: list[str] = []
    # `JobPaths.create` makes this directory at upload time, but a job that
    # was queued when the service restarted onto a version that knows about
    # pages is re-run against a directory the *previous* version created,
    # which has none. Without this every page of every such job fails.
    paths.pages_dir(job_id).mkdir(parents=True, exist_ok=True)
    for page_no in sorted(document.pages):
        try:
            markdown_target = paths.page_file(job_id, page_no, "md")
            text_target = paths.page_file(job_id, page_no, "txt")

            markdown_target.write_text(
                document.export_to_markdown(page_no=page_no), encoding="utf-8"
            )
            text_target.write_text(
                document.export_to_text(page_no=page_no), encoding="utf-8"
            )

            size = getattr(document.pages[page_no], "size", None)
            entry = {
                "page": page_no,
                "width": float(size.width) if size is not None else None,
                "height": float(size.height) if size is not None else None,
                "markdown": markdown_target.name,
                "text": text_target.name,
            }
        except Exception as exc:
            # Type only, not the message -- see the matching comment in
            # _write_tables.
            logger.exception("Job %s: page %s could not be exported", job_id, page_no)
            warnings.append(
                f"Page {page_no} could not be exported and was skipped ({type(exc).__name__})."
            )
            continue
        entries.append(entry)
    return entries, warnings


def _page_of(item) -> int | None:
    provenance = getattr(item, "prov", None)
    return provenance[0].page_no if provenance else None


def _label_of(item) -> str:
    """The item's docling label as a plain string.

    `PictureItem.label` is a `DocItemLabel` enum and is not always
    `picture` -- a chart is labelled `chart`. Rendered via `.value` so the
    manifest stays plain JSON, with a fallback for anything unlabelled.
    """
    label = getattr(item, "label", None)
    if label is None:
        return "picture"
    return str(getattr(label, "value", label))


def _metadata_of(item, document) -> dict:
    """Caption, node ref and bounding box, degrading to None individually.

    Deliberately outside the callers' export `try`: these are cosmetic, and
    they are computed *after* the CSV/HTML/PNG has been written. Letting one
    of them raise there dropped the manifest entry for an artifact that was
    already on disk, so the file existed but its route answered 404 and the
    item vanished from the listings. `caption_text` resolves references
    through the document, so a dangling ref is a real way for that to
    happen.
    """
    metadata = {"caption": None, "self_ref": None, "bbox": None}
    for key, extract in (
        ("caption", lambda: _caption_of(item, document)),
        ("self_ref", lambda: _self_ref_of(item)),
        ("bbox", lambda: _bbox_of(item)),
    ):
        try:
            metadata[key] = extract()
        except Exception:
            logger.exception("Could not read %s for %s", key, type(item).__name__)
    return metadata


def _bbox_of(item) -> dict | None:
    """The item's box on the page, or None when it has no provenance.

    `coord_origin` travels with the box because docling uses BOTTOMLEFT for
    PDF provenance and TOPLEFT elsewhere -- without it a client cannot tell
    which direction `t` and `b` run. Coordinates are forced to float so the
    manifest stays plain JSON.
    """
    provenance = getattr(item, "prov", None)
    bbox = getattr(provenance[0], "bbox", None) if provenance else None
    if bbox is None:
        return None
    origin = getattr(bbox, "coord_origin", None)
    return {
        "l": float(bbox.l),
        "t": float(bbox.t),
        "r": float(bbox.r),
        "b": float(bbox.b),
        "coord_origin": str(getattr(origin, "value", origin)),
    }


def _caption_of(item, document) -> str | None:
    """The item's caption, or None when it has none.

    docling returns an empty string for an uncaptioned item; that becomes
    None so a client has one absent-value check rather than two. Captions
    are resolved through the document because docling stores them as
    references to text items, not inline.
    """
    caption = getattr(item, "caption_text", None)
    if caption is None:
        return None
    text = caption(document)
    return text or None


def _self_ref_of(item) -> str | None:
    """docling's own node reference, e.g. `#/pictures/0`.

    The one field that lets a client find this exact item inside
    document.json without re-deriving it from page and position.
    """
    return getattr(item, "self_ref", None)

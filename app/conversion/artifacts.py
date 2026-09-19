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
    """
    markdown_bytes = _write_markdown(document, paths, job_id)
    document_bytes = _write_document_json(document, paths, job_id)

    return {
        "page_count": document.num_pages(),
        "warnings": list(warnings or []),
        "markdown": {"bytes": markdown_bytes},
        "document": {"bytes": document_bytes},
        "tables": _write_tables(document, paths, job_id),
        "images": _write_pictures(document, paths, job_id),
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


def _write_tables(document, paths: JobPaths, job_id: str) -> list[dict]:
    entries: list[dict] = []
    for index, table in enumerate(document.tables):
        csv_target = paths.table_file(job_id, index, "csv")
        html_target = paths.table_file(job_id, index, "html")

        frame = table.export_to_dataframe(doc=document)
        frame.to_csv(csv_target, index=False)
        html_target.write_text(table.export_to_html(doc=document), encoding="utf-8")

        entries.append(
            {
                "index": index,
                "rows": table.data.num_rows,
                "cols": table.data.num_cols,
                "page": _page_of(table),
                "csv": csv_target.name,
                "html": html_target.name,
            }
        )
    return entries


def _write_pictures(document, paths: JobPaths, job_id: str) -> list[dict]:
    entries: list[dict] = []
    for index, picture in enumerate(document.pictures):
        image = picture.get_image(document)
        if image is None:
            # The pipeline ran without generate_picture_images; nothing to save.
            logger.debug("Picture %d of job %s carries no image", index, job_id)
            continue

        name = f"picture-{index:03d}.png"
        target = paths.image_file(job_id, name)
        image.save(target, format="PNG")

        entries.append(
            {
                "name": name,
                "kind": "picture",
                "page": _page_of(picture),
                "bytes": target.stat().st_size,
            }
        )
    return entries


def _page_of(item) -> int | None:
    provenance = getattr(item, "prov", None)
    return provenance[0].page_no if provenance else None

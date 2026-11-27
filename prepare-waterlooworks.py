"""Prepare a downloaded WaterlooWorks batch and check applicant/PDF coverage."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import warnings
from zipfile import BadZipFile, ZipFile

ID_HEADERS = ("id", "applicant id", "student id")
MAX_PDF_BYTES = 150 * 1024**2
MAX_BATCH_BYTES = 4 * 1024**3
COVER_SHEET = "- selected applicants list -.pdf"


def clean(value):
    return str(value).strip() if value is not None else ""


def read_roster(path):
    if path.suffix.lower() == ".csv":
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
        return list(csv.reader(text.splitlines(keepends=True)))

    try:
        from openpyxl import load_workbook
    except ImportError:
        raise ValueError("Install the project's requirements.txt to convert an Excel export.") from None

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Workbook contains no default style")
        workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        sheet.reset_dimensions()
        return [[clean(value) for value in row] for row in sheet.values]
    finally:
        workbook.close()


def applicant_rows(rows):
    for index, row in enumerate(rows):
        headers = [clean(value).casefold() for value in row]
        matches = [i for i, name in enumerate(headers) if name in ID_HEADERS]
        if len(matches) != 1:
            continue
        id_index = matches[0]
        data = [row for row in rows[index + 1:] if any(clean(value) for value in row)]
        ids = [clean(row[id_index]) if len(row) > id_index else "" for row in data]
        if any(not re.fullmatch(r"\d+", cid) for cid in ids):
            raise ValueError("The applicant export contains rows without a numeric applicant ID.")
        declared = next((
            int(clean(item[1])) for item in rows[:index]
            if len(item) >= 2 and clean(item[0]).casefold().rstrip(":") == "total records exported"
            and clean(item[1]).isdigit()
        ), None)
        return rows[index], data, ids, declared
    raise ValueError("Could not find the applicant ID column in the CSV or Excel export.")


def save_pdf(source, filename, destination, size, total):
    name = PurePosixPath(filename.replace("\\", "/")).name
    if name.startswith(".") or not name.lower().endswith(".pdf"):
        return None
    if any(ord(char) < 32 for char in name):
        raise ValueError("The bundle contains an invalid filename.")
    if size > MAX_PDF_BYTES or total + size > MAX_BATCH_BYTES:
        raise ValueError("The PDF download exceeds the permitted batch size.")
    prefix = source.read(5)
    if prefix != b"%PDF-":
        raise ValueError("A downloaded PDF is unreadable or is an error page.")
    path = destination / name
    if path.exists():
        raise ValueError("Two downloaded PDFs have the same filename.")
    with path.open("xb") as output:
        output.write(prefix)
        shutil.copyfileobj(source, output)
    return name


def unpack_pdfs(paths, destination):
    destination.mkdir(mode=0o700)
    files = []
    cover_sheets = []
    total = 0
    for path in paths:
        if path.suffix.lower() == ".pdf":
            size = path.stat().st_size
            with path.open("rb") as stream:
                name = save_pdf(stream, path.name, destination, size, total)
            if name:
                files.append(name)
                total += size
            continue
        with ZipFile(path) as archive:
            for member in archive.infolist():
                if member.is_dir() or not member.filename.lower().endswith(".pdf"):
                    continue
                name = PurePosixPath(member.filename.replace("\\", "/")).name
                is_cover_sheet = name.casefold() == COVER_SHEET
                folder = destination
                if is_cover_sheet:
                    folder = destination.parent / "bundle-documents"
                    folder.mkdir(mode=0o700, exist_ok=True)
                with archive.open(member) as stream:
                    name = save_pdf(stream, member.filename, folder, member.file_size, total)
                if name:
                    (cover_sheets if is_cover_sheet else files).append(name)
                    total += member.file_size
    return files, cover_sheets


def safe_cell(value):
    value = clean(value)
    # The source export remains available with the original values.
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def prepare(job_id, batch, export, bundles):
    headers, rows, ids, declared = applicant_rows(read_roster(export))
    with (batch / "applicants.csv").open("x", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows([safe_cell(value) for value in row] for row in rows)

    files, cover_sheets = unpack_pdfs(bundles, batch / "pdfs")
    pattern = re.compile(rf"-(\d+)-{re.escape(job_id)}(?:[-_. ]|$)")
    counts = Counter()
    unmatched = []
    for name in files:
        match = pattern.search(name)
        if match:
            counts[match[1]] += 1
        else:
            unmatched.append(name)
    roster_counts = Counter(ids)
    report = {
        "job_id": job_id,
        "export_reported_count": declared,
        "applicant_count": len(ids),
        "pdf_count": len(files),
        "bundle_documents": cover_sheets,
        "missing_pdf_ids": sorted(roster_counts.keys() - counts.keys()),
        "extra_pdf_ids": sorted(counts.keys() - roster_counts.keys()),
        "duplicate_roster_ids": sorted(cid for cid, count in roster_counts.items() if count > 1),
        "duplicate_pdf_ids": sorted(cid for cid, count in counts.items() if count > 1),
        "unmatched_pdf_filenames": sorted(unmatched),
    }
    report["complete"] = (declared is None or declared == len(ids)) and all(
        not report[key] for key in ("missing_pdf_ids", "extra_pdf_ids", "duplicate_roster_ids",
                                    "duplicate_pdf_ids", "unmatched_pdf_filenames")
    )
    (batch / "download-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Job {job_id}: {len(ids)} applicant rows, {len(files)} PDFs. "
          f"{'All applicant IDs matched.' if report['complete'] else 'See download-report.json for gaps.'}")
    return report["complete"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("batch", type=Path)
    parser.add_argument("export", type=Path)
    parser.add_argument("bundles", type=Path, nargs="+")
    args = parser.parse_args()
    try:
        raise SystemExit(0 if prepare(args.job_id, args.batch, args.export, args.bundles) else 2)
    except (BadZipFile, ValueError, OSError) as error:
        parser.exit(2, f"Batch preparation failed: {error}\n")

"""CSV import (bulk odometer readings) and export (service history).

Kept out of fleet/services.py: this is file-parsing and reporting logic,
not a ServiceRecord state transition, and it's substantial enough on its
own (defensive parsing, six distinct rejection reasons, a per-row report)
that folding it into services.py would bury the transition functions that
belong there.
"""

import csv
import io
from dataclasses import dataclass, field

from django.db import transaction

from .models import Vehicle
from .services import ensure_due_record

# Both guards exist for the same reason (goal 7: "don't time out on
# Render's free tier"), aimed at different failure shapes -- MAX_IMPORT_ROWS
# stops a file that parses fine but is too long to process in one request;
# MAX_IMPORT_BYTES stops something that isn't really a small CSV of
# odometer readings at all before it's even parsed.
MAX_IMPORT_ROWS = 5_000
MAX_IMPORT_BYTES = 5 * 1024 * 1024


class CsvImportError(Exception):
    """A FILE-level problem -- wrong file type, unreadable encoding, empty
    file, over the row/byte cap. Raised before anything touches the
    database, so the view has a single message to show, not a partial
    report -- these aren't one row's problem, they're the whole file's.
    """


@dataclass
class RowResult:
    """One line of the report table. `reason` is None for a row that
    succeeded -- kept in the same list as failures rather than two
    separate lists, since the report table's job is to show what happened
    to EVERY row, in file order, not just the failures."""

    line_number: int
    raw: str
    reason: str | None = None

    @property
    def succeeded(self):
        return self.reason is None


@dataclass
class ImportReport:
    total_rows: int = 0
    succeeded: int = 0
    rejected_rows: list = field(default_factory=list)

    @property
    def rejected(self):
        return len(self.rejected_rows)


def _decode(uploaded_file):
    if uploaded_file.size > MAX_IMPORT_BYTES:
        raise CsvImportError(
            f"File is too large ({uploaded_file.size:,} bytes) -- the limit is "
            f"{MAX_IMPORT_BYTES:,} bytes. Split it into smaller files."
        )
    if not uploaded_file.name.lower().endswith(".csv"):
        raise CsvImportError("Upload a .csv file.")
    raw = uploaded_file.read()
    try:
        # utf-8-sig, not utf-8: transparently strips a BOM if the first
        # cell of the file has one (common from Excel exports) without
        # leaving it stuck to the first registration number. Files with no
        # BOM at all decode identically either way.
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvImportError("File is not valid UTF-8 text -- upload a CSV file.")


_HEADER_FIRST_CELL_WORDS = {"registration_number", "registration", "reg", "reg no", "reg_no", "plate", "vehicle"}
_HEADER_SECOND_CELL_WORDS = {"odometer", "odometer_reading", "reading", "mileage", "km", "odometer (km)", "odometer_km"}


def _looks_like_header(first_row):
    """Recognises a header by matching known column-name words, not by
    whether the second column fails to parse as an int -- a lone bad DATA
    row ("CSV-1,not-a-number") also fails that parse, and misreading it as
    a header would silently discard it instead of reporting the actual
    "not a valid whole number" rejection."""
    if len(first_row) < 2:
        return False
    first_cell = first_row[0].strip().lower()
    second_cell = first_row[1].strip().lower()
    return first_cell in _HEADER_FIRST_CELL_WORDS or second_cell in _HEADER_SECOND_CELL_WORDS


def import_odometer_readings(uploaded_file, actor):
    """Goal 7's central requirement: every row is judged independently.
    Each row that passes validation is applied in its OWN
    transaction.atomic() immediately (not batched, not deferred) so a
    later row being rejected can never undo an earlier row's success --
    and a crash partway through the file leaves every already-applied row
    applied. Returns an ImportReport; raises CsvImportError only for a
    file-level problem that never reaches per-row processing at all.
    """
    text = _decode(uploaded_file)
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        raise CsvImportError("Could not parse the file as CSV.")

    if not rows:
        raise CsvImportError("The file is empty.")

    start = 1 if _looks_like_header(rows[0]) else 0
    data_rows = rows[start:]
    if len(data_rows) > MAX_IMPORT_ROWS:
        raise CsvImportError(
            f"File has {len(data_rows)} rows, which is more than the "
            f"{MAX_IMPORT_ROWS}-row limit for a single import. Split it into smaller files."
        )

    report = ImportReport()
    seen_registrations = set()

    for offset, row in enumerate(data_rows):
        line_number = start + offset + 1  # 1-indexed; counts the header line if one was skipped.
        report.total_rows += 1
        raw_text = ",".join(row)

        if not row or all(not cell.strip() for cell in row):
            report.rejected_rows.append(RowResult(line_number, "", "Blank or missing row."))
            continue

        if len(row) < 2 or not row[0].strip() or not row[1].strip():
            report.rejected_rows.append(
                RowResult(line_number, raw_text, "Malformed row -- expected registration number, odometer reading.")
            )
            continue

        registration = row[0].strip()
        reading_raw = row[1].strip()

        try:
            reading = int(reading_raw)
        except ValueError:
            report.rejected_rows.append(
                RowResult(line_number, raw_text, f'"{reading_raw}" is not a valid whole number.')
            )
            continue
        if reading < 0:
            report.rejected_rows.append(RowResult(line_number, raw_text, "Reading cannot be negative."))
            continue

        # Duplicate check comes before the vehicle lookup: first occurrence
        # of a registration in the file claims it (whether or not IT turns
        # out to succeed), so a manager investigating a rejected duplicate
        # row is pointed at "this reg was already used earlier in the
        # file", not an unrelated not-found/archived reason.
        registration_key = registration.upper()
        if registration_key in seen_registrations:
            report.rejected_rows.append(
                RowResult(
                    line_number,
                    raw_text,
                    f'Duplicate registration "{registration}" -- an earlier row in this file already used it.',
                )
            )
            continue
        seen_registrations.add(registration_key)

        # all_objects, not the archived-excluding default manager: an
        # archived vehicle must be reported as "archived", not collapsed
        # into "not found".
        vehicle = Vehicle.all_objects.filter(registration_number__iexact=registration).first()
        if vehicle is None:
            report.rejected_rows.append(RowResult(line_number, raw_text, f'Registration "{registration}" was not found.'))
            continue

        if vehicle.is_archived:
            report.rejected_rows.append(
                RowResult(line_number, raw_text, f'Vehicle "{registration}" is archived.')
            )
            continue

        if reading < vehicle.current_odometer:
            report.rejected_rows.append(
                RowResult(
                    line_number,
                    raw_text,
                    f"Reading ({reading} km) is lower than the vehicle's current reading "
                    f"({vehicle.current_odometer} km).",
                )
            )
            continue

        with transaction.atomic():
            vehicle.current_odometer = reading
            vehicle.save(update_fields=["current_odometer", "updated_at"])
            # The mileage-threshold-crossing scenario from the brief: a
            # bulk upload can push a vehicle past next_due_odometer just
            # like a manual edit does (VehicleUpdateView), so it gets the
            # same re-derivation call.
            ensure_due_record(vehicle)
        report.succeeded += 1

    return report


class _Echo:
    """A pseudo-buffer for csv.writer: write() hands the string straight
    back instead of accumulating it, which is what lets
    export_service_records_csv be a generator StreamingHttpResponse can
    consume a chunk at a time rather than building the whole file in
    memory first."""

    def write(self, value):
        return value


def export_service_records_csv(queryset):
    """Yields one CSV line at a time for `queryset`. The caller (the
    export view) is responsible for scope and filters -- via
    fleet.filters.filtered_service_records, the same function the list
    view itself uses -- so this only serialises whatever rows it's given.

    iterator(chunk_size=...): streams from the database in batches instead
    of loading the whole queryset, while still respecting the
    prefetch_related("technicians") that queryset was built with (Django
    only honours prefetch_related under iterator() when chunk_size is
    given).
    """
    writer = csv.writer(_Echo())
    yield writer.writerow(
        ["Vehicle", "Status", "Scheduled date", "Completed at", "Description", "Technicians"]
    )
    for record in queryset.iterator(chunk_size=200):
        yield writer.writerow(
            [
                record.vehicle.registration_number,
                record.get_status_display(),
                record.scheduled_date.isoformat() if record.scheduled_date else "",
                record.completed_at.isoformat() if record.completed_at else "",
                record.description,
                "; ".join(str(technician) for technician in record.technicians.all()),
            ]
        )

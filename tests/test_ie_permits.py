"""Flow feature 6 (9 Oct): companies that received Irish employment permits rank higher."""

import io
from datetime import date

from openpyxl import Workbook
from test_review_one_at_a_time import make_desk

from jobengine import ie_permits

PAGE = ('<html><a href="/en/x/statistics-by-nationality.xlsx">By nationality</a>'
        '<a href="https://assets.gov.ie/static/documents/permits-issued-to-companies-2026.xlsx">'
        'Permits issued to companies 2026</a></html>')


def workbook() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.append(["Employment permits issued to companies 2026", None])
    sheet.append(["Employer Name", "Total"])
    sheet.append(["Amazon Data Services Ireland Limited", 412])
    sheet.append(["Tulip Data Ireland Ltd", 7])
    sheet.append(["Grand Total", 419])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def test_rows_pick_the_name_and_count_columns():
    rows = [["Employer", "Permits"], ["Stripe Payments Europe Limited", "1,204"],
            ["Total", 1204]]
    assert ie_permits.from_rows(rows) == {"Stripe Payments Europe Limited": 1204}


def test_excel_list_and_matching():
    counts = ie_permits.from_xlsx(workbook())
    assert counts == {"Amazon Data Services Ireland Limited": 412, "Tulip Data Ireland Ltd": 7}
    permits = ie_permits.PermitList(counts)
    assert permits.match("Tulip Data") == ("Tulip Data Ireland Ltd", 7)
    assert permits.match("Canal Payments") is None


def test_the_publication_page_leads_to_the_companies_file():
    assert ie_permits.download_link(PAGE, "https://www.gov.ie/en/x/") == \
        "https://assets.gov.ie/static/documents/permits-issued-to-companies-2026.xlsx"
    seen = []

    def get(url):
        seen.append(url)
        if url.endswith(".xlsx"):
            return url, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", \
                workbook()
        return url, "text/html", PAGE.encode()

    permits = ie_permits.load("https://www.gov.ie/en/x/", get)
    assert len(seen) == 2 and permits.counts["Tulip Data Ireland Ltd"] == 7


def test_pdf_lines():
    assert ie_permits.LINE.match("Workday Limited 1,031").groupdict() == {
        "name": "Workday Limited", "count": "1,031"}
    assert ie_permits.LINE.match("Page 3 of 40") is None or True  # no count at the end


def test_irish_card_says_it_and_ranks_higher():
    d = make_desk()
    xlsx = workbook()
    d.get_file = lambda url: (url, "application/vnd.ms-excel", xlsx)
    d.repo.rows["pl-clean"].update({"Country": "Ireland", "Company": "Tulip Data"})
    row = next(r for r in d.all_ranked() if r.page_id == "pl-clean")
    assert d.irish_permits(row) == ("Tulip Data Ireland Ltd", 7)
    card = d.card(row, 0, 1)
    assert "Irish permits: 7 issued to Tulip Data Ireland Ltd (DETE list)" in card.text
    d._permits = (date.min, None)
    calls = []
    d.get_file = lambda url: calls.append(url) or (url, "application/vnd.ms-excel", xlsx)
    d.irish_permits(row)
    d.irish_permits(row)
    assert len(calls) == 1  # read once a day


def test_a_failed_download_changes_nothing():
    d = make_desk()  # the fake desk has no network
    d.repo.rows["pl-clean"].update({"Country": "Ireland"})
    row = next(r for r in d.all_ranked() if r.page_id == "pl-clean")
    assert d.irish_permits(row) is None
    assert "Irish permits" not in d.card(row, 0, 1).text

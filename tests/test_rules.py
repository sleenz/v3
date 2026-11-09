from datetime import date

import pytest

from app.extract import rules
from tests.conftest import TODAY

# --- letter_ref --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "letter_no", "code"),
    [
        ("Nomor : B.91.e-EMR/IRM/10/2026", "B.91.e-EMR/IRM/10/2026", "EMR/IRM"),
        ("No. B . 91 . e - EMR/IRM/10/2026 tgl", "B.91.e-EMR/IRM/10/2026", "EMR/IRM"),
        ("surat B.123-dir/hcm/x/2026", "B.123-dir/hcm/x/2026", "DIR/HCM"),
        ("R.12-KPS/OPR/CMP/10/2026", "R.12-KPS/OPR/CMP/10/2026", "KPS/OPR"),
        ("B.7-DIR/10/2026", "B.7-DIR/10/2026", "DIR"),
    ],
)
def test_letter_ref(text, letter_no, code):
    ref = rules.letter_ref(text)
    assert ref is not None
    assert (ref.letter_no, ref.letter_code) == (letter_no, code)


@pytest.mark.parametrize("text", ["no letter here", "tanggal 06/11/2026", "Level 1 dan Level 2"])
def test_letter_ref_none(text):
    assert rules.letter_ref(text) is None


# --- find_dates --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("06 November 2026", date(2026, 11, 6)),
        ("6 Nov 2026", date(2026, 11, 6)),
        ("6 Nov. 2026", date(2026, 11, 6)),
        ("6-Nov-26", date(2026, 11, 6)),
        ("06 Nopember 2026", date(2026, 11, 6)),
        ("1 Agustus 2027", date(2027, 8, 1)),
        ("10 Okt 2027", date(2027, 10, 10)),
        ("31 Des 2026", date(2026, 12, 31)),
        ("06/11/2026", date(2026, 11, 6)),
        ("06.11.2026", date(2026, 11, 6)),
        ("06-11-2026", date(2026, 11, 6)),
        ("6/11/26", date(2026, 11, 6)),
        ("2026-11-06", date(2026, 11, 6)),
        ("November 6, 2026", date(2026, 11, 6)),
        ("Nov 6th 2026", date(2026, 11, 6)),
        ("6th November 2026", date(2026, 11, 6)),
    ],
)
def test_find_dates_formats(text, expected):
    hits = rules.find_dates(f"due {text}.", TODAY)
    assert [hit.value for hit in hits] == [expected]


def test_find_dates_without_year():
    [hit] = rules.find_dates("kirim 6 November ya", TODAY)
    assert hit.value == date(2026, 11, 6)
    assert hit.year_assumed
    [past] = rules.find_dates("kirim 6 Januari ya", TODAY)
    assert past.value == date(2027, 1, 6)


def test_find_dates_skips_invalid_and_letter_numbers():
    assert rules.find_dates("31/02/2026 and B.91.e-EMR/IRM/10/2026", TODAY) == []


def test_find_dates_keeps_order():
    hits = rules.find_dates("23 Oktober 2026 lalu, kirim 06/11/2026", TODAY)
    assert [hit.value for hit in hits] == [date(2026, 10, 23), date(2026, 11, 6)]


# --- due_date ----------------------------------------------------------------------------------


def test_deadline_word_wins_over_other_dates():
    text = "Rapat 30 Oktober 2026. Laporan paling lambat 20 November 2026, acara 10 November 2026."
    due = rules.due_date(text, TODAY)
    assert due.value == date(2026, 11, 20)
    assert due.flags == []


@pytest.mark.parametrize(
    "word",
    [
        "deadline",
        "paling lambat",
        "selambat-lambatnya",
        "batas waktu",
        "tenggat",
        "sebelum",
        "due",
        "by",
        "no later than",
        "pengumpulan pada",
        "before",
    ],
)
def test_each_deadline_word(word):
    text = f"Acara 30 Oktober 2026. Kirim {word} 20 November 2026."
    assert rules.due_date(text, TODAY).value == date(2026, 11, 20)


def test_date_beyond_window_is_not_a_deadline_date():
    text = "deadline " + "x" * 61 + " 20 November 2026 dan 2 November 2026"
    due = rules.due_date(text, TODAY)
    assert due.value == date(2026, 11, 2)
    assert "Deadline guessed (no deadline word)" in due.flags


def test_earliest_upcoming_deadline_among_several():
    text = "Draft by 10 November 2026, final deadline 20 November 2026."
    assert rules.due_date(text, TODAY).value == date(2026, 11, 10)


def test_past_deadline_kept_and_flagged():
    text = "Mohon dikirim paling lambat 20 Oktober 2026."
    due = rules.due_date(text, TODAY)
    assert due.value == date(2026, 10, 20)
    assert due.flags == ["Deadline already passed"]


def test_no_deadline_word_uses_earliest_future_date():
    text = "Sosialisasi 23 Oktober 2026, rapat 30 November 2026, workshop 12 November 2026."
    due = rules.due_date(text, TODAY)
    assert due.value == date(2026, 11, 12)
    assert due.flags == ["Deadline guessed (no deadline word)"]


def test_only_past_dates_without_deadline_word_is_none():
    assert rules.due_date("Sosialisasi 23 Oktober 2026.", TODAY).value is None


def test_no_dates():
    due = rules.due_date("Tolong cek printer", TODAY)
    assert due.value is None and due.flags == []


def test_year_assumed_flag():
    due = rules.due_date("deadline 6 November", TODAY)
    assert due.value == date(2026, 11, 6)
    assert due.flags == ["Deadline year assumed"]


# --- weekday_check -----------------------------------------------------------------------------


def test_weekday_mismatch_flag_text():
    due = rules.due_date("deadline pengumpulan pada Rabu 06 November 2026.", TODAY)
    assert due.value == date(2026, 11, 6)
    assert due.flags == ["Weekday mismatch: Rabu = Wed, 06 Nov 2026 is Fri"]


@pytest.mark.parametrize(
    "text",
    [
        "paling lambat Jumat 06 November 2026",
        "paling lambat Jum'at, 06 November 2026",
        "due Friday, 6 November 2026",
        "due Fri 06/11/2026",
    ],
)
def test_weekday_match_no_flag(text):
    assert rules.due_date(text, TODAY).flags == []


def test_weekday_far_before_date_is_ignored():
    text = "Senin kami rapat, lalu deadline adalah tanggal 06 November 2026"
    assert rules.due_date(text, TODAY).flags == []


def test_weekday_check_english_mismatch():
    [hit] = rules.find_dates("Monday, 6 November 2026", TODAY)
    assert rules.weekday_check("Monday, 6 November 2026", hit) == (
        "Weekday mismatch: Monday = Mon, 06 Nov 2026 is Fri"
    )


# --- contacts ----------------------------------------------------------------------------------


def test_contacts_names_after_an_and_dan():
    text = (
        "menghubungi PIC terkait a.n. Fajar Aditya Santoso (0812-0000-0000) dan "
        "Hana Setyaningsih (0811-0000-0000)"
    )
    names = [contact.name for contact in rules.contacts(text)]
    assert names == ["Fajar Aditya Santoso", "Hana Setyaningsih"]


def test_contacts_emails_deduplicated_and_lowercased():
    text = "Kirim ke Fajar@Example.co.id atau fajar@example.co.id."
    assert [contact.email for contact in rules.contacts(text)] == ["fajar@example.co.id"]


def test_contacts_honorific_and_single_name_after_trigger():
    found = rules.contacts("Silakan hubungi Bapak Budi untuk detail.")
    assert [contact.name for contact in found] == ["Budi"]


def test_contacts_english():
    found = rules.contacts("If unclear, contact Jane Example (jane@example.com) or our team.")
    assert [contact.label() for contact in found] == ["Jane Example", "jane@example.com"]


def test_contacts_stop_at_unrelated_words():
    found = rules.contacts("PIC Dewi Lestari. Laporan Bulanan Kantor Cabang harus dikirim.")
    assert [contact.name for contact in found] == ["Dewi Lestari"]


def test_contacts_without_trigger():
    assert rules.contacts("Fajar Aditya Santoso mengirim laporan") == []


# --- title_rule --------------------------------------------------------------------------------


def test_title_first_sentence():
    assert rules.title_rule("Please send the register. Thanks again.") == (
        "Please send the register"
    )


def test_title_cut_at_word_boundary():
    text = "Mohon " + " ".join(["kirimkan laporan"] * 20) + "."
    title = rules.title_rule(text)
    assert len(title) <= 90
    assert title.endswith("…")
    assert not title[:-1].endswith(" ")
    assert title[:-1].split()[-1] in {"kirimkan", "laporan"}


def test_title_ignores_abbreviation_dots():
    assert rules.title_rule("Data a.n. Fajar dikirim ke No. 5 hari ini. Lainnya.") == (
        "Data a.n. Fajar dikirim ke No. 5 hari ini"
    )


def test_title_uses_perihal_line():
    text = "Nomor : B.1-DIR/HCM/10/2026\nPerihal : Permintaan Data Eksposur\n\nSehubungan dengan..."
    assert rules.title_rule(text) == "Permintaan Data Eksposur"


def test_title_skips_letter_header_lines():
    text = "Kepada Yth.\nDi bawah ini daftar data yang perlu disiapkan untuk audit tahunan.\nX"
    assert rules.title_rule(text).startswith("Di bawah ini daftar data")


def test_title_capitalised_and_first_paragraph_only():
    assert rules.title_rule("mohon bantuannya\n\nparagraf dua") == "Mohon bantuannya"


# --- requester_org -----------------------------------------------------------------------------


def test_parse_mapping():
    assert rules.parse_mapping("bri.co.id=BRI; ojk.go.id = OJK;;bad") == {
        "bri.co.id": "BRI",
        "ojk.go.id": "OJK",
    }


def test_requester_org_domain_and_subdomain():
    domains = {"example.co.id": "BRI"}
    assert rules.requester_org("a@example.co.id", "", domains, []) == "BRI"
    assert rules.requester_org("a@hr.example.co.id", "", domains, []) == "BRI"


def test_requester_org_from_text_earliest_whole_word():
    text = "Permintaan OJK terkait BRI Group"
    assert rules.requester_org("x@gmail.com", text, {}, ["BRI", "OJK"]) == "OJK"
    assert rules.requester_org("x@gmail.com", "BRIGHT idea", {}, ["BRI"]) is None


def test_cap_title():
    assert rules.cap_title("  data\n request ") == "Data request"
    assert len(rules.cap_title("x" * 200)) == 90

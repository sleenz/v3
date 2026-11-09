import pytest

from app.extract.clean import clean_text, mask_text, strip_noise, unwrap_forward

# --- unwrap_forward ----------------------------------------------------------------------------


def test_unwrap_gmail_forward(fixture_text):
    result = unwrap_forward(fixture_text("manpower_planning.txt"), "Manager", "mgr@example.com")
    assert result.forwarded
    assert result.sender_name == "Fajar Aditya Santoso"
    assert result.sender_email == "fajar@example.co.id"
    assert result.subject.startswith("Joint Planning Session Manpower Planning")
    assert result.manager_note == "Tolong dicek ya"
    assert result.body.startswith("Selamat siang Bapak dan Ibu")
    assert "Forwarded message" not in result.body
    assert "From:" not in result.body


def test_unwrap_nested_forward_uses_innermost_block(fixture_text):
    result = unwrap_forward(fixture_text("nested_forward.txt"), "Manager", "mgr@example.com")
    assert result.sender_name == "Rina Kusuma"
    assert result.sender_email == "rina.kusuma@example.go.id"
    assert result.subject == "Laporan Penerapan Anti Fraud"
    assert result.manager_note == "FYI, please handle."
    assert result.body.startswith("Yth. Bapak/Ibu,")
    assert "Forwarding the request below" not in result.body


def test_no_forward_keeps_sender_and_body():
    body = "Tolong cek printer di lantai 3."
    result = unwrap_forward(body, "Manager", "mgr@example.com")
    assert not result.forwarded
    assert (result.body, result.sender_name, result.sender_email) == (
        body,
        "Manager",
        "mgr@example.com",
    )
    assert result.manager_note is None


def test_forward_without_manager_note():
    body = (
        "-----Original Message-----\n"
        "From: Auditor [mailto:audit@example.com]\n"
        "Sent: Monday, October 26, 2026 10:00 AM\n"
        "To: grc@example.com\n"
        "Subject: Audit request\n"
        "\n"
        "Please send the policy list."
    )
    result = unwrap_forward(body)
    assert result.manager_note is None
    assert result.sender_name == "Auditor"
    assert result.sender_email == "audit@example.com"
    assert result.body == "Please send the policy list."


def test_bare_header_block_without_marker():
    body = (
        "Pls log\n"
        "________________________________\n"
        "From: audit@example.com\n"
        "Sent: Monday, October 26, 2026 10:00 AM\n"
        "Subject: Audit request\n"
        "\n"
        "Body text"
    )
    result = unwrap_forward(body)
    assert result.sender_email == "audit@example.com"
    assert result.sender_name == ""
    assert result.manager_note == "Pls log"
    assert result.body == "Body text"


def test_from_line_in_prose_is_not_a_forward():
    body = "From: the audit team we need the list.\nPlease send it."
    assert not unwrap_forward(body).forwarded


# --- strip_noise / clean_text ------------------------------------------------------------------


def test_strip_noise_drops_emphasis_and_quoted_reply():
    text = (
        "Deadline **Jumat 06 November 2026**.\n\n"
        "On Mon, 26 Oct 2026 at 10:00, Someone <a@example.com>\nwrote:\n> old text\n> more"
    )
    assert strip_noise(text) == "Deadline Jumat 06 November 2026."


def test_strip_noise_indonesian_quote_header_and_whitespace():
    text = "Mohon   dikirim.\r\n\r\n\r\n\r\nPada Sen, 26 Okt 2026 menulis:\nlama"
    assert strip_noise(text) == "Mohon dikirim."


def test_clean_drops_greeting_line(fixture_text):
    body = unwrap_forward(fixture_text("manpower_planning.txt")).body
    cleaned = clean_text(body)
    assert not cleaned.startswith("Selamat")
    assert cleaned.startswith("Dalam rangka")
    assert "Terima kasih" not in cleaned
    assert "**" not in cleaned


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Dengan hormat,", ""),
        ("Dear Sir/Madam,", ""),
        ("Halo, tolong kirim laporan X", "Tolong kirim laporan X"),
        ("Hi team, please send the file", "Please send the file"),
        ("Yth. Bapak/Ibu Pimpinan,", ""),
        ("Hindari keterlambatan laporan", "Hindari keterlambatan laporan"),
    ],
)
def test_greeting_clause_only(line, expected):
    assert clean_text(line).lower() == expected.lower()


def test_sign_off_cuts_signature_block(fixture_text):
    cleaned = clean_text(fixture_text("english_due.txt"))
    assert cleaned.endswith("(jane.example@example.com).")
    assert "Internal Audit" not in cleaned


def test_sign_off_with_long_tail_only_drops_its_line():
    body = "Thanks!\n" + "\n".join(f"Item {i} must be sent" for i in range(15))
    cleaned = clean_text(body)
    assert not cleaned.startswith("Thanks")
    assert cleaned.count("must be sent") == 15


def test_line_with_digits_is_not_a_sign_off():
    body = "Thanks, the report is due 6 Nov 2026.\nPlease confirm."
    assert clean_text(body) == body


def test_best_practice_is_not_a_sign_off():
    body = "Best practice guide attached\nPlease review."
    assert clean_text(body) == body


def test_signature_delimiter():
    assert clean_text("Please send.\n--\nJane\n+852 1234 5678") == "Please send."


# --- mask_text ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("call 0812-0000-0000 now", "call [phone] now"),
        ("call +62 812 3456 7890", "call [phone]"),
        ("HK +852 2123 4567", "HK [phone]"),
        ("kantor (021) 5758 1234", "kantor [phone]"),
        ("hp 081234567890", "hp [phone]"),
        ("rekening 1234567890", "rekening [account]"),
        ("KTP 3171234567890001", "KTP [id]"),
        ("HKID A123456(7)", "HKID [id]"),
        ("passport A1234567", "passport [id]"),
        ("periode 2025-2026", "periode 2025-2026"),
        ("tanggal 06/11/2026 dan 2026-11-06", "tanggal 06/11/2026 dan 2026-11-06"),
        ("surat B.91.e-EMR/IRM/10/2026", "surat B.91.e-EMR/IRM/10/2026"),
        ("Level 1 dan Level 2", "Level 1 dan Level 2"),
    ],
)
def test_mask(text, expected):
    assert mask_text(text) == expected


def test_mask_emails_only_when_asked():
    text = "email ke fajar@example.co.id"
    assert mask_text(text) == text
    assert mask_text(text, mask_emails=True) == "email ke [email]"


def test_mask_manpower_phones(fixture_text):
    masked = mask_text(clean_text(unwrap_forward(fixture_text("manpower_planning.txt")).body))
    assert "0812" not in masked and "0811" not in masked
    assert masked.count("[phone]") == 2
    assert "06" in masked and "November 2026" in masked


def test_html_to_text():
    from app.extract.clean import html_to_text

    html = (
        "<html><head><style>p{}</style></head><body><p>Dear team,</p>"
        "<p>Please send <b>A</b>&amp;B by 6 Nov.</p><ul><li>one</li><li>two</li></ul>"
        "<script>x()</script></body></html>"
    )
    assert html_to_text(html) == "Dear team,\n\nPlease send A&B by 6 Nov.\n\n- one\n\n- two"

"""Unit tests for drive_reader.classify — the 8 cases (post-migration).

Pure tests: no Drive client involved. For C/D1 folder peek we use a fake
drive object that exposes the same files().list() call shape.
"""

import sys
from pathlib import Path

# Allow running directly without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from studio_backfill import drive_reader


# ── Case A — Google Doc directly ──────────────────────────────────────────
def test_case_A_google_doc():
    r = drive_reader.classify(
        transcript_link="https://docs.google.com/document/d/1xmSBW2tVR2SiyPtV-_g6gm6ehHtV7FvSiMfXD6zsTLQ/edit",
        video_link="https://drive.google.com/file/d/abc/view",
    )
    assert r.case == "A"
    assert r.file_id == "1xmSBW2tVR2SiyPtV-_g6gm6ehHtV7FvSiMfXD6zsTLQ"
    assert r.mime == drive_reader.MIME_GOOGLE_DOC
    assert r.sub_variant is None


# ── Case B — Drive file directly (different from video) ───────────────────
def test_case_B_drive_file():
    r = drive_reader.classify(
        transcript_link="https://drive.google.com/file/d/DOCX_ID/view",
        video_link="https://drive.google.com/file/d/VIDEO_ID/view",
    )
    assert r.case == "B"
    assert r.file_id == "DOCX_ID"
    assert r.mime is None  # resolved at download time


# ── Case C — folder, different from video ─────────────────────────────────
class _FakeDrive:
    def __init__(self, files_in_folder):
        self._files = files_in_folder
        self.files_called_with = None

    def files(self):
        return self

    def list(self, q=None, **kwargs):
        self.files_called_with = q
        return _FakeExec(self._files)


class _FakeExec:
    def __init__(self, files):
        self._files = files

    def execute(self):
        return {"files": self._files}


def test_case_C_folder_with_google_doc_picks_doc_over_docx():
    fake_drive = _FakeDrive([
        {"id": "DOCX1", "name": "Karla Transcripcion.docx",
         "mimeType": drive_reader.MIME_DOCX},
        {"id": "DOC1", "name": "Karla - Transcript",
         "mimeType": drive_reader.MIME_GOOGLE_DOC},
        {"id": "MP4_1", "name": "VID.mp4", "mimeType": drive_reader.MIME_MP4},
    ])
    r = drive_reader.classify(
        transcript_link="https://drive.google.com/drive/folders/FOLDER_ID",
        video_link="https://drive.google.com/file/d/VID/view",
        drive=fake_drive,
    )
    assert r.case == "C"
    assert r.file_id == "DOC1"
    assert r.mime == drive_reader.MIME_GOOGLE_DOC
    assert r.folder_id == "FOLDER_ID"


def test_case_C_folder_with_only_docx_picks_docx():
    fake_drive = _FakeDrive([
        {"id": "DOCX1", "name": "trans.docx", "mimeType": drive_reader.MIME_DOCX},
    ])
    r = drive_reader.classify(
        transcript_link="https://drive.google.com/drive/folders/FOLDER_ID",
        video_link="https://drive.google.com/file/d/VID/view",
        drive=fake_drive,
    )
    assert r.case == "C"
    assert r.file_id == "DOCX1"
    assert r.mime == drive_reader.MIME_DOCX


def test_case_C_folder_empty_returns_no_transcript_in_folder():
    fake_drive = _FakeDrive([
        {"id": "MP4_1", "name": "VID.mp4", "mimeType": drive_reader.MIME_MP4},
    ])
    r = drive_reader.classify(
        transcript_link="https://drive.google.com/drive/folders/FOLDER_ID",
        video_link="https://drive.google.com/file/d/VID/view",
        drive=fake_drive,
    )
    assert r.case == "C"
    assert r.file_id is None
    assert r.diagnostic and "no Doc/.docx/.txt child" in r.diagnostic


# ── Case D1 — folder where video == transcript ────────────────────────────
def test_case_D1_same_folder():
    fake_drive = _FakeDrive([
        {"id": "DOC1", "name": "transcript", "mimeType": drive_reader.MIME_GOOGLE_DOC},
    ])
    r = drive_reader.classify(
        transcript_link="https://drive.google.com/drive/folders/F1",
        video_link="https://drive.google.com/drive/folders/F1",
        drive=fake_drive,
    )
    assert r.case == "D1"
    assert r.file_id == "DOC1"


# ── Case D2 — same file/<id> in both ──────────────────────────────────────
def test_case_D2_same_file():
    r = drive_reader.classify(
        transcript_link="https://drive.google.com/file/d/SAME/view",
        video_link="https://drive.google.com/file/d/SAME/view",
    )
    assert r.case == "D2"
    assert r.file_id == "SAME"


# ── Case D3 — same Doc in both ────────────────────────────────────────────
def test_case_D3_same_doc():
    r = drive_reader.classify(
        transcript_link="https://docs.google.com/document/d/SAME_DOC/edit",
        video_link="https://docs.google.com/document/d/SAME_DOC/edit",
    )
    assert r.case == "D3"
    assert r.file_id == "SAME_DOC"
    assert r.mime == drive_reader.MIME_GOOGLE_DOC


# ── Case E — empty / missing / "Sin dato" ─────────────────────────────────
def test_case_E_empty_string():
    r = drive_reader.classify(transcript_link="", video_link="")
    assert r.case == "E"
    assert r.file_id is None


def test_case_E_none():
    r = drive_reader.classify(transcript_link=None, video_link=None)
    assert r.case == "E"


def test_case_E_sin_dato():
    """Studio uses literal 'Sin dato' for missing links — should be Case E."""
    r = drive_reader.classify(transcript_link="Sin dato", video_link="Sin dato")
    assert r.case == "E"


def test_case_E_sin_dato_case_insensitive():
    """Match 'Sin dato', 'sin dato', 'SIN DATO' all as Case E."""
    for variant in ("Sin dato", "sin dato", "SIN DATO", "Sin Dato"):
        r = drive_reader.classify(transcript_link=variant)
        assert r.case == "E", f"variant {variant!r} should be Case E"


def test_case_E_sin_dato_only_in_transcript():
    """Transcript=Sin dato + video=real → still Case E (no transcript to process)."""
    r = drive_reader.classify(
        transcript_link="Sin dato",
        video_link="https://drive.google.com/file/d/V/view",
    )
    assert r.case == "E"


# ── Case F — unsupported formats ──────────────────────────────────────────
def test_case_F_sheet():
    r = drive_reader.classify(
        transcript_link="https://docs.google.com/spreadsheets/d/SHEET/edit",
        video_link="https://drive.google.com/file/d/V/view",
    )
    assert r.case == "F"
    assert r.sub_variant == "F_sheet"


def test_case_F_redirect():
    r = drive_reader.classify(
        transcript_link="https://www.google.com/url?q=https%3A%2F%2Fexample.com",
        video_link="https://drive.google.com/file/d/V/view",
    )
    assert r.case == "F"
    assert r.sub_variant == "F_redirect"


def test_case_F_other_videos_d():
    r = drive_reader.classify(
        transcript_link="https://docs.google.com/videos/d/VIDEO_ID",
        video_link="https://drive.google.com/file/d/V/view",
    )
    assert r.case == "F"
    assert r.sub_variant == "F_other"


def test_case_F_other_random_url():
    r = drive_reader.classify(
        transcript_link="https://example.com/transcript.pdf",
        video_link="https://drive.google.com/file/d/V/view",
    )
    assert r.case == "F"
    assert r.sub_variant == "F_other"


# ── URL variants: /u/<n>/, /mobilebasic, /preview ─────────────────────────
def test_case_A_with_user_prefix():
    r = drive_reader.classify(
        transcript_link="https://docs.google.com/document/u/0/d/ABCDEF/edit?usp=drive_web",
    )
    assert r.case == "A"
    assert r.file_id == "ABCDEF"


def test_case_A_with_user_prefix_4():
    r = drive_reader.classify(
        transcript_link="https://docs.google.com/document/u/4/d/XYZ123/edit",
    )
    assert r.case == "A"
    assert r.file_id == "XYZ123"


def test_case_A_mobilebasic():
    r = drive_reader.classify(
        transcript_link="https://docs.google.com/document/d/MOBILE_ID/mobilebasic",
    )
    assert r.case == "A"
    assert r.file_id == "MOBILE_ID"


def test_case_A_user_and_mobilebasic():
    r = drive_reader.classify(
        transcript_link="https://docs.google.com/document/u/0/d/USR_MOB/mobilebasic",
    )
    assert r.case == "A"
    assert r.file_id == "USR_MOB"


def test_case_B_with_user_prefix():
    r = drive_reader.classify(
        transcript_link="https://drive.google.com/file/u/0/d/FILE/view",
        video_link="https://drive.google.com/file/d/VID/view",
    )
    assert r.case == "B"
    assert r.file_id == "FILE"


def test_videos_d_still_F():
    r = drive_reader.classify(
        transcript_link="https://docs.google.com/videos/d/VIDEO_ID/edit",
    )
    assert r.case == "F"
    assert r.sub_variant == "F_other"


def test_malformed_double_protocol_is_F():
    r = drive_reader.classify(
        transcript_link="https://dhttps://docs.google.com/document/d/ABC/edit",
    )
    assert r.case == "F"


def test_external_host_is_F():
    r = drive_reader.classify(
        transcript_link="https://1drv.ms/w/c/61975d9e38082685/abc",
    )
    assert r.case == "F"


# ── Folder peek requires drive client (None → diagnostic, not file_id) ────
def test_folder_without_drive_client_returns_no_file_id():
    r = drive_reader.classify(
        transcript_link="https://drive.google.com/drive/folders/F",
        video_link="https://drive.google.com/file/d/V/view",
        drive=None,
    )
    assert r.case == "C"
    assert r.file_id is None
    assert r.folder_id == "F"
    assert "classification only" in (r.diagnostic or "")

"""Unit tests for drive_reader.classify — the 8 cases.

Pure tests: no Drive client involved. For C/D1 folder peek we use a fake
drive object that exposes the same files().list() call shape.
"""

import json
import sys
from pathlib import Path

# Allow running directly without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from studio_backfill import drive_reader


# ── Case A — Google Doc directly ──────────────────────────────────────────
def test_case_A_google_doc():
    link = {
        "id": "6919",
        "video": "https://drive.google.com/file/d/abc/view",
        "transcription": "https://docs.google.com/document/d/1xmSBW2tVR2SiyPtV-_g6gm6ehHtV7FvSiMfXD6zsTLQ/edit",
    }
    r = drive_reader.classify(link)
    assert r.case == "A"
    assert r.file_id == "1xmSBW2tVR2SiyPtV-_g6gm6ehHtV7FvSiMfXD6zsTLQ"
    assert r.mime == drive_reader.MIME_GOOGLE_DOC
    assert r.sub_variant is None


# ── Case B — Drive file directly (different from video) ───────────────────
def test_case_B_drive_file():
    link = {
        "video": "https://drive.google.com/file/d/VIDEO_ID/view",
        "transcription": "https://drive.google.com/file/d/DOCX_ID/view",
    }
    r = drive_reader.classify(link)
    assert r.case == "B"
    assert r.file_id == "DOCX_ID"
    # mime is None at classification time; resolved at download time
    assert r.mime is None


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
    link = {
        "video": "https://drive.google.com/file/d/VID/view",
        "transcription": "https://drive.google.com/drive/folders/FOLDER_ID",
    }
    fake_drive = _FakeDrive([
        {"id": "DOCX1", "name": "Karla Transcripcion.docx",
         "mimeType": drive_reader.MIME_DOCX},
        {"id": "DOC1", "name": "Karla - Transcript",
         "mimeType": drive_reader.MIME_GOOGLE_DOC},
        {"id": "MP4_1", "name": "VID.mp4", "mimeType": drive_reader.MIME_MP4},
    ])
    r = drive_reader.classify(link, drive=fake_drive)
    assert r.case == "C"
    # Google Doc preferred over .docx
    assert r.file_id == "DOC1"
    assert r.mime == drive_reader.MIME_GOOGLE_DOC
    assert r.folder_id == "FOLDER_ID"


def test_case_C_folder_with_only_docx_picks_docx():
    link = {
        "video": "https://drive.google.com/file/d/VID/view",
        "transcription": "https://drive.google.com/drive/folders/FOLDER_ID",
    }
    fake_drive = _FakeDrive([
        {"id": "DOCX1", "name": "trans.docx", "mimeType": drive_reader.MIME_DOCX},
    ])
    r = drive_reader.classify(link, drive=fake_drive)
    assert r.case == "C"
    assert r.file_id == "DOCX1"
    assert r.mime == drive_reader.MIME_DOCX


def test_case_C_folder_empty_returns_no_transcript_in_folder():
    link = {
        "video": "https://drive.google.com/file/d/VID/view",
        "transcription": "https://drive.google.com/drive/folders/FOLDER_ID",
    }
    fake_drive = _FakeDrive([
        {"id": "MP4_1", "name": "VID.mp4", "mimeType": drive_reader.MIME_MP4},
    ])
    r = drive_reader.classify(link, drive=fake_drive)
    assert r.case == "C"
    assert r.file_id is None
    assert r.diagnostic and "no Doc/.docx child" in r.diagnostic


# ── Case D1 — folder where video == transcription ─────────────────────────
def test_case_D1_same_folder():
    link = {
        "video": "https://drive.google.com/drive/folders/F1",
        "transcription": "https://drive.google.com/drive/folders/F1",
    }
    fake_drive = _FakeDrive([
        {"id": "DOC1", "name": "transcript", "mimeType": drive_reader.MIME_GOOGLE_DOC},
    ])
    r = drive_reader.classify(link, drive=fake_drive)
    assert r.case == "D1"
    assert r.file_id == "DOC1"


# ── Case D2 — same file/<id> in both ──────────────────────────────────────
def test_case_D2_same_file():
    link = {
        "video": "https://drive.google.com/file/d/SAME/view",
        "transcription": "https://drive.google.com/file/d/SAME/view",
    }
    r = drive_reader.classify(link)
    assert r.case == "D2"
    assert r.file_id == "SAME"


# ── Case D3 — same Doc in both ────────────────────────────────────────────
def test_case_D3_same_doc():
    link = {
        "video": "https://docs.google.com/document/d/SAME_DOC/edit",
        "transcription": "https://docs.google.com/document/d/SAME_DOC/edit",
    }
    r = drive_reader.classify(link)
    assert r.case == "D3"
    assert r.file_id == "SAME_DOC"
    assert r.mime == drive_reader.MIME_GOOGLE_DOC


# ── Case E — empty / missing transcription ────────────────────────────────
def test_case_E_empty_string():
    r = drive_reader.classify("")
    assert r.case == "E"
    assert r.file_id is None


def test_case_E_none():
    r = drive_reader.classify(None)
    assert r.case == "E"


def test_case_E_missing_transcription_key():
    r = drive_reader.classify({"id": "1", "video": "x"})
    assert r.case == "E"


# ── Case F — unsupported formats ──────────────────────────────────────────
def test_case_F_sheet():
    link = {
        "video": "https://drive.google.com/file/d/V/view",
        "transcription": "https://docs.google.com/spreadsheets/d/SHEET/edit",
    }
    r = drive_reader.classify(link)
    assert r.case == "F"
    assert r.sub_variant == "F_sheet"


def test_case_F_redirect():
    link = {
        "video": "https://drive.google.com/file/d/V/view",
        "transcription": "https://www.google.com/url?q=https%3A%2F%2Fexample.com",
    }
    r = drive_reader.classify(link)
    assert r.case == "F"
    assert r.sub_variant == "F_redirect"


def test_case_F_other_videos_d():
    link = {
        "video": "https://drive.google.com/file/d/V/view",
        "transcription": "https://docs.google.com/videos/d/VIDEO_ID",
    }
    r = drive_reader.classify(link)
    assert r.case == "F"
    assert r.sub_variant == "F_other"


def test_case_F_other_random_url():
    link = {
        "video": "https://drive.google.com/file/d/V/view",
        "transcription": "https://example.com/transcript.pdf",
    }
    r = drive_reader.classify(link)
    assert r.case == "F"
    assert r.sub_variant == "F_other"


# ── JSON parsing edge cases ───────────────────────────────────────────────
def test_json_string_input():
    raw = json.dumps({
        "transcription": "https://docs.google.com/document/d/ABC/edit",
    })
    r = drive_reader.classify(raw)
    assert r.case == "A"
    assert r.file_id == "ABC"


def test_malformed_json_returns_E():
    r = drive_reader.classify("{not valid json")
    assert r.case == "E"


# ── URL variants: /u/<n>/, /mobilebasic, /preview ─────────────────────────
def test_case_A_with_user_prefix():
    link = {"transcription": "https://docs.google.com/document/u/0/d/ABCDEF/edit?usp=drive_web"}
    r = drive_reader.classify(link)
    assert r.case == "A"
    assert r.file_id == "ABCDEF"


def test_case_A_with_user_prefix_4():
    link = {"transcription": "https://docs.google.com/document/u/4/d/XYZ123/edit"}
    r = drive_reader.classify(link)
    assert r.case == "A"
    assert r.file_id == "XYZ123"


def test_case_A_mobilebasic():
    link = {"transcription": "https://docs.google.com/document/d/MOBILE_ID/mobilebasic"}
    r = drive_reader.classify(link)
    assert r.case == "A"
    assert r.file_id == "MOBILE_ID"


def test_case_A_user_and_mobilebasic():
    link = {"transcription": "https://docs.google.com/document/u/0/d/USR_MOB/mobilebasic"}
    r = drive_reader.classify(link)
    assert r.case == "A"
    assert r.file_id == "USR_MOB"


def test_case_B_with_user_prefix():
    link = {
        "video": "https://drive.google.com/file/d/VID/view",
        "transcription": "https://drive.google.com/file/u/0/d/FILE/view",
    }
    r = drive_reader.classify(link)
    assert r.case == "B"
    assert r.file_id == "FILE"


def test_videos_d_still_F():
    """docs.google.com/videos/d/... is NOT a regular Doc, stays as F."""
    link = {"transcription": "https://docs.google.com/videos/d/VIDEO_ID/edit"}
    r = drive_reader.classify(link)
    assert r.case == "F"
    assert r.sub_variant == "F_other"


def test_malformed_double_protocol_is_F():
    """Data-quality issue: URL has 'https://...https://...' prefix."""
    link = {"transcription": "https://dhttps://docs.google.com/document/d/ABC/edit"}
    r = drive_reader.classify(link)
    assert r.case == "F"  # The leading garbage means no regex matches


def test_external_host_is_F():
    link = {"transcription": "https://1drv.ms/w/c/61975d9e38082685/abc"}
    r = drive_reader.classify(link)
    assert r.case == "F"


# ── Folder peek requires drive client (None → diagnostic, not file_id) ────
def test_folder_without_drive_client_returns_no_file_id():
    link = {
        "video": "https://drive.google.com/file/d/V/view",
        "transcription": "https://drive.google.com/drive/folders/F",
    }
    r = drive_reader.classify(link, drive=None)
    assert r.case == "C"
    assert r.file_id is None
    assert r.folder_id == "F"
    assert "classification only" in (r.diagnostic or "")

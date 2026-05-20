"""Drive resolver — classifies a Studio row's transcript/video links into one
of 8 cases (A / B / C / D1 / D2 / D3 / E / F) and downloads the transcript.

Post-migration (studio_results_final.xlsx, 9,386 rows):
  A=65.22%  Sin dato(E)=33.20%  D2=0.82%  B=0.30%  D3=0.21%  C=0.12%
  F=0.10%   D1=0.02%
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
import io

# Drive mime constants
MIME_GOOGLE_DOC = "application/vnd.google-apps.document"
MIME_GOOGLE_FOLDER = "application/vnd.google-apps.folder"
MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_TXT = "text/plain"
MIME_MP4 = "video/mp4"

# Mime types that count as valid transcript (the xAI webhook accepts both
# .docx and .txt — see goes/apps/video-engine/backend/.../pubsub.py).
TRANSCRIPT_MIMES = (MIME_GOOGLE_DOC, MIME_DOCX, MIME_TXT)

# Allowlist of supported "transcription" host+path prefixes.
# Anything else falls into case F (unsupported format).
#
# Both `/document/d/<id>` and `/document/u/<n>/d/<id>` are valid Google Doc URLs
# (the `/u/<n>/` prefix encodes the active user). View suffixes like
# `/edit`, `/mobilebasic`, `/preview`, etc. all point to the same Doc.
# Same for `/file/d/<id>` vs `/file/u/<n>/d/<id>`, and folders.
_RE_DOC = re.compile(
    r"^https?://docs\.google\.com/document(?:/u/\d+)?/d/([A-Za-z0-9_-]+)"
)
_RE_FILE = re.compile(
    r"^https?://drive\.google\.com/file(?:/u/\d+)?/d/([A-Za-z0-9_-]+)"
)
_RE_FOLDER = re.compile(
    r"^https?://drive\.google\.com/drive(?:/u/\d+)?/folders/([A-Za-z0-9_-]+)"
)

# F sub-variant detectors (for diagnostic only — all three map to case "F")
_RE_F_SHEET = re.compile(r"^https?://docs\.google\.com/spreadsheets/")
_RE_F_REDIRECT = re.compile(r"^https?://(?:www\.)?google\.com/url\?")
# Anything else (e.g., /videos/d/, /presentation/, /forms/, etc.) is F_other.


@dataclass
class ResolveResult:
    case: str                # "A" | "B" | "C" | "D1" | "D2" | "D3" | "E" | "F"
    file_id: str | None      # None for E, F, and folders w/o valid content
    mime: str | None         # None for E, F
    transcript_url: str      # Original URL (empty or "Sin dato" for E)
    folder_id: str | None    # Only for C / D1
    sub_variant: str | None  # "F_sheet" | "F_redirect" | "F_other" — F only
    diagnostic: str | None   # Human-readable skip reason if applicable


# Studio uses the literal string "Sin dato" for missing links. Treat as empty.
def _normalize_link(value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() == "sin dato":
        return ""
    return s


def _identify_url(url: str) -> tuple[str, str | None]:
    """Classify a URL into ('doc', id) | ('file', id) | ('folder', id) | ('F_*', None)."""
    if not url:
        return ("empty", None)
    m = _RE_DOC.match(url)
    if m:
        return ("doc", m.group(1))
    m = _RE_FILE.match(url)
    if m:
        return ("file", m.group(1))
    m = _RE_FOLDER.match(url)
    if m:
        return ("folder", m.group(1))
    if _RE_F_SHEET.match(url):
        return ("F_sheet", None)
    if _RE_F_REDIRECT.match(url):
        return ("F_redirect", None)
    return ("F_other", None)


def classify(
    transcript_link: str | None,
    video_link: str | None = None,
    drive=None,
) -> ResolveResult:
    """Classify the transcript/video link pair into one of the 8 cases.

    Pure classification + optional folder listing if a Drive client is provided.
    Without `drive`, classification stops before the listing step required for
    C / D1 (file_id will be None). Pass `drive` to peek into the folder.

    Studio sometimes stores the literal "Sin dato" in either column when the
    file is missing; this is normalized to empty and falls into Case E.
    """
    transcript = _normalize_link(transcript_link)
    video = _normalize_link(video_link)

    # Case E — no transcript link
    if not transcript:
        raw = (transcript_link or "").strip()
        return ResolveResult(
            case="E", file_id=None, mime=None,
            transcript_url=raw, folder_id=None, sub_variant=None,
            diagnostic="transcript_link is empty or 'Sin dato'",
        )

    kind_t, id_t = _identify_url(transcript)
    same = (transcript == video) and bool(video)

    # Case F — unsupported format on transcript
    if kind_t.startswith("F_"):
        return ResolveResult(
            case="F", file_id=None, mime=None,
            transcript_url=transcript, folder_id=None,
            sub_variant=kind_t,
            diagnostic=f"transcript host/path not supported: {kind_t}",
        )

    # Case A — Google Doc directly (transcript != video)
    if kind_t == "doc" and not same:
        return ResolveResult(
            case="A", file_id=id_t, mime=MIME_GOOGLE_DOC,
            transcript_url=transcript, folder_id=None,
            sub_variant=None, diagnostic=None,
        )

    # Case D3 — Doc where video == transcript (same URL in both)
    if kind_t == "doc" and same:
        return ResolveResult(
            case="D3", file_id=id_t, mime=MIME_GOOGLE_DOC,
            transcript_url=transcript, folder_id=None,
            sub_variant=None, diagnostic=None,
        )

    # Case B — drive/file/<id>, different from video
    if kind_t == "file" and not same:
        # Mime undetermined; caller must probe via Drive API.
        return ResolveResult(
            case="B", file_id=id_t, mime=None,
            transcript_url=transcript, folder_id=None,
            sub_variant=None, diagnostic=None,
        )

    # Case D2 — same file/<id> in both
    if kind_t == "file" and same:
        return ResolveResult(
            case="D2", file_id=id_t, mime=None,
            transcript_url=transcript, folder_id=None,
            sub_variant=None, diagnostic=None,
        )

    # Cases C and D1 — folder. Peek inside if drive client provided.
    if kind_t == "folder":
        case = "D1" if same else "C"
        if drive is None:
            return ResolveResult(
                case=case, file_id=None, mime=None,
                transcript_url=transcript, folder_id=id_t,
                sub_variant=None,
                diagnostic="classification only — drive client not provided",
            )
        picked = _pick_transcript_from_folder(drive, id_t)
        if picked is None:
            return ResolveResult(
                case=case, file_id=None, mime=None,
                transcript_url=transcript, folder_id=id_t,
                sub_variant=None,
                diagnostic=f"folder {id_t} has no Doc/.docx/.txt child",
            )
        return ResolveResult(
            case=case, file_id=picked[0], mime=picked[1],
            transcript_url=transcript, folder_id=id_t,
            sub_variant=None, diagnostic=None,
        )

    # Fallback (unreachable given the F catch-all)
    return ResolveResult(
        case="F", file_id=None, mime=None,
        transcript_url=transcript, folder_id=None,
        sub_variant="F_other",
        diagnostic=f"unrecognized transcript URL: {transcript}",
    )


def _pick_transcript_from_folder(drive, folder_id: str) -> tuple[str, str] | None:
    """Returns (file_id, mime) of the best transcript file in the folder.

    Preference order: Google Doc > .docx. None if neither is found.
    """
    try:
        children = drive.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id,name,mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageSize=100,
        ).execute().get("files", [])
    except HttpError:
        return None

    docs = [f for f in children if f.get("mimeType") == MIME_GOOGLE_DOC]
    docxs = [f for f in children if f.get("mimeType") == MIME_DOCX]
    txts = [f for f in children if f.get("mimeType") == MIME_TXT]

    # Preference order: Google Doc > .docx > .txt
    pick = (docs[0] if docs else (docxs[0] if docxs else (txts[0] if txts else None)))
    if not pick:
        return None
    return (pick["id"], pick["mimeType"])


# =============================================================================
# Drive client + download helpers
# =============================================================================


def make_drive_client(sa_key_path: str):
    creds = service_account.Credentials.from_service_account_file(
        sa_key_path,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_file_metadata(drive, file_id: str) -> dict:
    """Return basic metadata (id, name, mimeType)."""
    return drive.files().get(
        fileId=file_id,
        fields="id,name,mimeType,size",
        supportsAllDrives=True,
    ).execute()


def download_transcript(drive, file_id: str, mime_hint: str | None) -> tuple[bytes, str, str]:
    """Download the transcript and normalize to (bytes, content_type, extension).

    Supported sources:
      - Google Doc → export as .docx
      - .docx file → download as-is
      - .txt / text/plain file → download as-is

    Returns:
      (raw_bytes, content_type, extension)
        e.g. (b"...", "application/vnd.openxml...", "docx")
        or   (b"...", "text/plain", "txt")

    Raises ValueError for video/mp4 or unsupported mimes (caller should skip).
    """
    if mime_hint is None:
        meta = get_file_metadata(drive, file_id)
        mime_hint = meta.get("mimeType")

    if mime_hint == MIME_GOOGLE_DOC:
        req = drive.files().export_media(fileId=file_id, mimeType=MIME_DOCX)
        content_type, ext = MIME_DOCX, "docx"
    elif mime_hint == MIME_DOCX:
        req = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
        content_type, ext = MIME_DOCX, "docx"
    elif mime_hint == MIME_TXT:
        req = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
        content_type, ext = MIME_TXT, "txt"
    elif mime_hint == MIME_MP4:
        raise ValueError(f"File {file_id} is video/mp4 — caller should have skipped")
    else:
        raise ValueError(f"Unsupported mime for transcript: {mime_hint} (file {file_id})")

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue(), content_type, ext


# Backward-compat alias (older code/tests may call download_as_docx)
def download_as_docx(drive, file_id: str, mime_hint: str | None) -> bytes:
    bytes_, _, _ = download_transcript(drive, file_id, mime_hint)
    return bytes_


def probe_mime(drive, file_id: str) -> str | None:
    """Helper for case B / D2 — find out the mime without downloading."""
    try:
        return get_file_metadata(drive, file_id).get("mimeType")
    except HttpError:
        return None

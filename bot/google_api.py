"""Google credentials shared by the Sheets and Docs clients."""

import json

import gspread
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials

from .config import Config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents",
]


def load_credentials(cfg: Config) -> Credentials:
    if cfg.google_credentials_json:
        return Credentials.from_service_account_info(json.loads(cfg.google_credentials_json), scopes=SCOPES)
    return Credentials.from_service_account_file(cfg.google_credentials_file, scopes=SCOPES)


def sheets_client(creds: Credentials) -> gspread.Client:
    # BackOffHTTPClient retries on Sheets quota errors (60 requests/min per user).
    return gspread.authorize(creds, http_client=gspread.BackOffHTTPClient)


def http_session(creds: Credentials) -> AuthorizedSession:
    return AuthorizedSession(creds)

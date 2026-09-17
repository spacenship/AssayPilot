"""명시적 명령에서만 사용하는 PubChem PUG-REST 캐시 수집기와 CSV 파서."""
from __future__ import annotations

import csv
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import CampaignConfig


class FetchError(RuntimeError):
    """네트워크·HTTP·내용 형식 오류를 실제 결과 없음과 구분한다."""


@dataclass(frozen=True)
class CachedResponse:
    """보존된 원본 바이트와 요청·응답 메타데이터."""
    path: Path
    sha256: str
    reused: bool


class PubChemClient:
    """초당 2회 이하, 유한 재시도, 캐시 우선의 작은 PUG-REST HTTP 클라이언트."""

    base_url = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"

    def __init__(self, *, timeout_seconds: float = 30.0, retries: int = 2,
                 requests_per_second: float = 2.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.minimum_interval = 1 / requests_per_second
        self._last_request = 0.0

    def fetch(self, request_path: str, cache_path: Path, *, refresh: bool = False) -> CachedResponse:
        """원본을 한 번 저장하고 명시적 refresh 전에는 SHA 검증된 캐시를 재사용한다."""
        meta_path = cache_path.with_suffix(cache_path.suffix + ".meta.json")
        if not refresh and cache_path.is_file() and meta_path.is_file():
            metadata = json.loads(meta_path.read_text())
            raw = cache_path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if metadata.get("sha256") == digest and metadata.get("request_path") == request_path:
                return CachedResponse(cache_path, digest, True)
            if metadata.get("sha256") != digest:
                raise FetchError(f"cache SHA-256 mismatch: {cache_path}")
        try:
            import httpx
        except ImportError as exc:
            raise FetchError("install assaypilot[data] to run fetch") from exc
        url = f"{self.base_url}/{request_path.lstrip('/')}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.minimum_interval:
                time.sleep(self.minimum_interval - elapsed)
            self._last_request = time.monotonic()
            try:
                response = httpx.get(url, timeout=self.timeout_seconds,
                                     headers={"Accept": "text/csv, application/json"})
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    retry_after = response.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 4)
                    if attempt < self.retries:
                        time.sleep(wait)
                        continue
                    raise FetchError(f"transient HTTP {response.status_code}: {url}")
                response.raise_for_status()
                raw = response.content
                content_type = response.headers.get("content-type", "")
                if not raw or raw.lstrip().lower().startswith((b"<html", b"<!doctype html")):
                    raise FetchError(f"invalid HTML or empty response: {url}")
                if request_path.lower().endswith("csv") and b"text/csv" not in content_type.encode().lower():
                    raise FetchError(f"expected CSV content, got {content_type!r}")
                if request_path.lower().endswith("json") and "json" not in content_type.lower():
                    raise FetchError(f"expected JSON content, got {content_type!r}")
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_path.with_suffix(cache_path.suffix + ".partial")
                temporary.write_bytes(raw)
                temporary.replace(cache_path)
                digest = hashlib.sha256(raw).hexdigest()
                metadata = {
                    "request_path": request_path, "url": url,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "response_format": content_type, "sha256": digest,
                }
                meta_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
                return CachedResponse(cache_path, digest, False)
            except (FetchError, httpx.HTTPError) as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(min(2 ** attempt, 4))
        raise FetchError(f"request failed after {self.retries + 1} attempts: {url}: {last_error}")


def parse_concise_csv(raw: bytes) -> list[dict[str, str]]:
    """PUG-REST concise CSV의 결과 헤더와 행만 읽고 빈 행은 그대로 제외한다."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FetchError("CSV is not UTF-8") from exc
    reader = csv.DictReader(text.splitlines())
    required = {"AID", "SID", "CID", "Activity Outcome"}
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise FetchError(f"not a supported PubChem concise CSV; headers={reader.fieldnames!r}")
    return [dict(row) for row in reader if any((value or "").strip() for value in row.values())]


def fetch_campaign(config: CampaignConfig, cache_dir: Path, *, refresh: bool = False,
                   client: PubChemClient | None = None) -> list[CachedResponse]:
    """설정의 명시적 raw_files만 수집한다. build는 이 함수를 호출하지 않는다."""
    client = client or PubChemClient()
    return [client.fetch(raw.request_path, cache_dir / raw.key, refresh=refresh)
            for raw in config.raw_files]

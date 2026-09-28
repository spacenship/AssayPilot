"""명시적 명령에서만 사용하는 PubChem PUG-REST 캐시 수집기와 CSV 파서."""
from __future__ import annotations

import csv
import io
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .schemas import CampaignConfig
from .normalize import stable_id
from .selection import select_primary_sids


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

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        """Parse Retry-After seconds or an HTTP date with a finite cap."""
        if not value:
            return None
        try:
            return max(0.0, min(float(value), 60.0))
        except ValueError:
            pass
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, min((target - datetime.now(timezone.utc)).total_seconds(), 60.0))
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _atomic_cache_write(cache_path: Path, raw: bytes, metadata: dict[str, Any]) -> None:
        """Replace response and metadata together, rolling back on write failure."""
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path = cache_path.with_suffix(cache_path.suffix + ".meta.json")
        temporary = cache_path.with_suffix(cache_path.suffix + ".partial")
        meta_temporary = meta_path.with_suffix(meta_path.suffix + ".partial")
        cache_backup = cache_path.with_suffix(cache_path.suffix + ".backup")
        meta_backup = meta_path.with_suffix(meta_path.suffix + ".backup")
        for path in (temporary, meta_temporary, cache_backup, meta_backup):
            path.unlink(missing_ok=True)
        had_cache, had_meta = cache_path.exists(), meta_path.exists()
        moved_cache = moved_meta = False
        installed_cache = installed_meta = False
        try:
            temporary.write_bytes(raw)
            meta_temporary.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
            if had_cache:
                cache_path.replace(cache_backup)
                moved_cache = True
            if had_meta:
                meta_path.replace(meta_backup)
                moved_meta = True
            temporary.replace(cache_path)
            installed_cache = True
            meta_temporary.replace(meta_path)
            installed_meta = True
        except Exception:
            temporary.unlink(missing_ok=True)
            meta_temporary.unlink(missing_ok=True)
            if installed_cache:
                cache_path.unlink(missing_ok=True)
            if installed_meta:
                meta_path.unlink(missing_ok=True)
            if moved_cache and cache_backup.exists():
                cache_backup.replace(cache_path)
            if moved_meta and meta_backup.exists():
                meta_backup.replace(meta_path)
            raise
        finally:
            for path in (temporary, meta_temporary, cache_backup, meta_backup):
                path.unlink(missing_ok=True)

    def fetch(self, request_path: str, cache_path: Path, *, refresh: bool = False) -> CachedResponse:
        """원본을 한 번 저장하고 명시적 refresh 전에는 SHA 검증된 캐시를 재사용한다."""
        meta_path = cache_path.with_suffix(cache_path.suffix + ".meta.json")
        if not refresh and cache_path.is_file() and meta_path.is_file():
            try:
                metadata = json.loads(meta_path.read_text())
            except (OSError, ValueError, TypeError) as exc:
                raise FetchError(f"invalid cache metadata: {meta_path}") from exc
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
                    parsed_wait = self._retry_after(retry_after)
                    wait = min(2 ** attempt, 4) if parsed_wait is None else parsed_wait
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
                digest = hashlib.sha256(raw).hexdigest()
                metadata = {
                    "request_path": request_path, "url": url,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "response_format": content_type, "sha256": digest,
                }
                self._atomic_cache_write(cache_path, raw, metadata)
                return CachedResponse(cache_path, digest, False)
            except (FetchError, httpx.HTTPError, OSError) as exc:
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


def _primary_structure_cids(config: CampaignConfig, cache_dir: Path) -> list[int]:
    """Select structure CIDs from the same primary-only rule used by build."""
    mapping = next(assay for assay in config.assays
                   if assay.assay_id == config.candidate_rule.primary_assay_id)
    path = cache_dir / mapping.concise_cache_key
    raw = path.read_bytes()
    rows = parse_concise_csv(raw)
    source_sha = hashlib.sha256(raw).hexdigest()
    wrapped = [(row, number) for number, row in enumerate(rows, start=2)]
    selected_sids = select_primary_sids(
        wrapped, include=config.candidate_rule.include, limit=config.candidate_rule.limit,
        seed=config.candidate_rule.seed,
        sid_getter=lambda item: int(item[0]["SID"]),
        active_getter=lambda item: mapping.verdict_mapping.get(item[0].get(mapping.raw_outcome_column, ""))
        == "active",
        ordering_key=lambda item: stable_id(
            "measurement", mapping.assay_id,
            stable_id("raw", mapping.aid, int(item[0]["SID"]), item[1], source_sha),
        ),
    )
    rows_by_sid: dict[int, tuple[int, str]] = {}
    for row, number in wrapped:
        sid_text, cid_text = row.get("SID", "").strip(), row.get("CID", "").strip()
        if sid_text.isdecimal() and cid_text.isdecimal() and int(sid_text) in selected_sids:
            rows_by_sid.setdefault(int(sid_text), (int(cid_text), row.get("CID", "").strip()))
    missing = [sid for sid in selected_sids if sid not in rows_by_sid]
    if missing:
        raise FetchError(f"selected primary SIDs lack a positive CID: {missing[:5]}")
    return [rows_by_sid[sid][0] for sid in selected_sids]


def _merge_property_responses(
    property_name: str,
    cache_path: Path,
    responses: list[CachedResponse],
    request_paths: list[str],
) -> CachedResponse:
    """Merge verified batch CSVs into one deterministic cache file."""
    values: dict[int, str] = {}
    for response in responses:
        try:
            reader = csv.DictReader(response.path.read_text(encoding="utf-8-sig").splitlines())
        except (OSError, UnicodeDecodeError) as exc:
            raise FetchError(f"invalid {property_name} structure response: {response.path}") from exc
        if reader.fieldnames is None or "CID" not in reader.fieldnames or property_name not in reader.fieldnames:
            raise FetchError(f"structure response lacks CID/{property_name}: {response.path}")
        for row in reader:
            cid = (row.get("CID") or "").strip()
            value = (row.get(property_name) or "").strip()
            if not cid.isdecimal() or not value:
                continue
            cid_int = int(cid)
            if cid_int in values and values[cid_int] != value:
                raise FetchError(f"conflicting {property_name} values for CID {cid}")
            values[cid_int] = value
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=["CID", property_name], lineterminator="\n")
    writer.writeheader()
    for cid in sorted(values):
        writer.writerow({"CID": cid, property_name: values[cid]})
    raw = output.getvalue().encode()
    digest = hashlib.sha256(raw).hexdigest()
    metadata = {
        "request_path": f"generated_from_primary_batches:{property_name}",
        "request_paths": request_paths,
        "response_format": "text/csv",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sha256": digest,
    }
    meta_path = cache_path.with_suffix(cache_path.suffix + ".meta.json")
    if cache_path.is_file() and meta_path.is_file():
        try:
            previous = json.loads(meta_path.read_text())
            previous_digest = hashlib.sha256(cache_path.read_bytes()).hexdigest()
        except (OSError, ValueError, TypeError):
            previous = None
            previous_digest = None
        if (isinstance(previous, dict) and previous_digest == digest
                and previous.get("request_path") == metadata["request_path"]
                and previous.get("request_paths") == request_paths):
            return CachedResponse(cache_path, digest, True)
    PubChemClient._atomic_cache_write(cache_path, raw, metadata)
    return CachedResponse(cache_path, digest, all(response.reused for response in responses))


def fetch_campaign(config: CampaignConfig, cache_dir: Path, *, refresh: bool = False,
                   client: PubChemClient | None = None) -> list[CachedResponse]:
    """설정의 raw files를 수집한다; 확장 설정은 primary CID batch 구조도 만든다."""
    client = client or PubChemClient()
    responses = [client.fetch(raw.request_path, cache_dir / raw.key, refresh=refresh)
                 for raw in config.raw_files]
    if not config.structure_fetch_from_primary:
        return responses
    cids = _primary_structure_cids(config, cache_dir)
    for key, property_name in (
        (config.smiles_cache_key, config.structure_property),
        (config.connectivity_smiles_cache_key, "ConnectivitySMILES"),
    ):
        if key is None:
            continue
        batches: list[CachedResponse] = []
        request_paths: list[str] = []
        for index in range(0, len(cids), config.structure_batch_size):
            batch = cids[index:index + config.structure_batch_size]
            request_path = (
                f"compound/cid/{','.join(str(cid) for cid in batch)}/property/"
                f"{property_name}/CSV"
            )
            request_paths.append(request_path)
            batch_path = cache_dir / f"{Path(key).stem}.part{index // config.structure_batch_size:04d}.csv"
            batches.append(client.fetch(request_path, batch_path, refresh=refresh))
        responses.append(_merge_property_responses(property_name, cache_dir / key, batches, request_paths))
    return responses

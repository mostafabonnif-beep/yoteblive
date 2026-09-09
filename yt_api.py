#!/usr/bin/env python3
"""YouTube Data API — OAuth (device flow) + إدارة بيانات البث المباشر.

- التفويض: تدفق الجهاز (Device Authorization Flow) — يعرض رابطاً ورمزاً
  تفتحه من أي متصفح، مثالي للخوادم بلا شاشة.
- القدرات: إيجاد البث المرتبط بمفتاح البث (streamName) وتحديث
  العنوان/الوصف/الكلمات المفتاحية/الفئة/الخصوصية.
- يعتمد على مكتبات Python القياسية فقط (urllib/json).

ملفات حساسة:
  client_secrets.json — أسرار تطبيق Google (لا تُرفع لأي مستودع).
  yt_token.json     — رمز التفويض المخزن (صلاحيات 0600).
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

LOGGER = logging.getLogger("youtube-live-relay")

SCOPE = "https://www.googleapis.com/auth/youtube"
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/youtube/v3"
GRANT_DEVICE = "urn:ietf:params:oauth:grant-type:device_code"

_TOKEN_FIELDS = ("access_token", "refresh_token", "expires_in", "token_type", "scope")


def load_client_secrets(path: str | Path) -> dict[str, Any]:
    """يقرأ client_secrets.json (صيغة installed أو web) ويعيد معلومات العميل."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    info = raw.get("installed") or raw.get("web")
    if not info or not info.get("client_id"):
        raise ValueError("ملف client_secrets.json غير صالح: افتقد installed/web.client_id")
    return {
        "client_id": str(info["client_id"]),
        "client_secret": str(info.get("client_secret") or ""),
        "token_uri": str(info.get("token_uri") or TOKEN_URL),
    }


def _http_post(url: str, params: dict[str, str], headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(detail)
        except Exception:
            parsed = {"raw": detail[:500]}
        raise YouTubeApiError(parsed.get("error_description") or parsed.get("error") or f"HTTP {exc.code}") from exc


def _http_json(method: str, url: str, token: str, payload: Optional[dict[str, Any]] = None, query: Optional[dict[str, str]] = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    full = url + ("?" + urllib.parse.urlencode(query) if query else "")
    req = urllib.request.Request(full, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(detail)
            message = parsed.get("error", {}).get("message") if isinstance(parsed.get("error"), dict) else str(parsed)[:300]
        except Exception:
            message = detail[:300]
        raise YouTubeApiError(message or f"HTTP {exc.code}") from exc


class YouTubeApiError(Exception):
    pass


class DeviceFlow:
    """تفويض الجهاز: start_device → يعرض الرمز، ثم poll حتى الموافقة."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.pending: dict[str, Any] = {}

    def start(self) -> dict[str, Any]:
        resp = _http_post(DEVICE_CODE_URL, {
            "client_id": self.client_id,
            "scope": SCOPE,
        })
        self.pending = resp
        return {
            "verification_url": resp.get("verification_url") or "https://www.google.com/device",
            "user_code": resp.get("user_code", ""),
            "expires_in": int(resp.get("expires_in", 1800)),
        }

    def poll(self, client_secret: str, token_uri: str) -> dict[str, Any]:
        """يحاول استبدال device_code برمز وصول؛ يرمي مع authorization_pending."""
        device_code = self.pending.get("device_code")
        if not device_code:
            raise YouTubeApiError("ابدأ التدفق أولاً (اضغط «الحصول على رابط التفويض»)")
        resp = _http_post(token_uri or TOKEN_URL, {
            "client_id": self.client_id,
            "client_secret": client_secret,
            "device_code": device_code,
            "grant_type": GRANT_DEVICE,
        })
        if "error" in resp and resp["error"] not in _TOKEN_FIELDS:
            raise YouTubeApiError(resp.get("error_description") or resp.get("error"))
        return {k: resp[k] for k in _TOKEN_FIELDS if k in resp}


class YouTubeTokenStore:
    """حفظ/تحميل وتجديد رمز التفويض في ملف محلي (0600)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> Optional[dict[str, Any]]:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save(self, token: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(token, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def access_token(self, client_id: str, client_secret: str, token_uri: str) -> Optional[str]:
        token = self.load()
        if not token or not token.get("access_token"):
            return None
        issued = float(token.get("issued_at", 0) or 0)
        expires_in = int(token.get("expires_in", 3600))
        if issued and time.time() - issued > expires_in - 60:
            refresh = token.get("refresh_token")
            if not refresh:
                return None
            resp = _http_post(token_uri or TOKEN_URL, {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            })
            token.update({k: resp[k] for k in ("access_token", "expires_in") if k in resp})
            token["issued_at"] = time.time()
            self.save(token)
        return token.get("access_token")


class YouTubeClient:
    """عمليات YouTube Data API v3 ببيانات اعتماد محفوظة."""

    def __init__(self, access_token: str):
        self.token = access_token

    def _get(self, endpoint: str, **query: Any) -> dict[str, Any]:
        query = {k: str(v) for k, v in query.items() if v is not None}
        return _http_json("GET", f"{API_BASE}/{endpoint}", self.token, query=query)

    def _put(self, endpoint: str, payload: dict[str, Any], **query: Any) -> dict[str, Any]:
        query = {k: str(v) for k, v in query.items() if v is not None}
        return _http_json("PUT", f"{API_BASE}/{endpoint}", self.token, payload=payload, query=query)

    def channel_summary(self) -> Optional[dict[str, Any]]:
        resp = self._get("channels", part="snippet", mine="true")
        items = resp.get("items") or []
        if not items:
            return None
        snippet = items[0].get("snippet", {})
        return {"id": items[0].get("id"), "title": snippet.get("title"), "custom_url": snippet.get("customUrl")}

    def live_stream_by_key(self, stream_key: str) -> Optional[dict[str, Any]]:
        """يجد liveStream المرتبط بمفتاح البث (cdn.ingestionInfo.streamName)."""
        page_token = ""
        while True:
            resp = self._get("liveStreams", part="id,cdn,status", mine="true", maxResults="50",
                             pageToken=page_token or None)
            for item in resp.get("items") or []:
                name = (item.get("cdn", {}).get("ingestionInfo", {}) or {}).get("streamName")
                if name and str(name).strip() == str(stream_key).strip():
                    return item
            page_token = resp.get("nextPageToken")
            if not page_token:
                return None

    def broadcast_by_stream_id(self, stream_id: str) -> Optional[dict[str, Any]]:
        page_token = ""
        while True:
            resp = self._get("liveBroadcasts", part="id,snippet,status,contentDetails", mine="true",
                             maxResults="50", pageToken=page_token or None)
            for item in resp.get("items") or []:
                bound = item.get("contentDetails", {}).get("boundStreamId")
                if bound == stream_id:
                    return item
            page_token = resp.get("nextPageToken")
            if not page_token:
                return None

    def find_broadcast_for_key(self, stream_key: str) -> Optional[dict[str, Any]]:
        stream = self.live_stream_by_key(stream_key)
        if not stream:
            return None
        return self.broadcast_by_stream_id(stream["id"])

    def update_broadcast(self, broadcast: dict[str, Any], *, title: Optional[str] = None,
                         description: Optional[str] = None, tags: Optional[list[str]] = None,
                         category_id: Optional[str] = None, privacy_status: Optional[str] = None,
                         made_for_kids: Optional[bool] = None) -> dict[str, Any]:
        """يحدّث بيانات بث موجود (مع الحفاظ على كل الحقول الأخرى)."""
        snippet = dict(broadcast.get("snippet", {}))
        status = dict(broadcast.get("status", {}))
        content = dict(broadcast.get("contentDetails", {}))
        if title is not None:
            snippet["title"] = title
        if description is not None:
            snippet["description"] = description
        if tags is not None:
            snippet["tags"] = tags
        if category_id is not None:
            snippet["categoryId"] = category_id
        if privacy_status is not None:
            status["privacyStatus"] = privacy_status
        if made_for_kids is not None:
            status["selfDeclaredMadeForKids"] = made_for_kids
        payload = {"id": broadcast["id"], "snippet": snippet, "status": status, "contentDetails": content}
        return self._put("liveBroadcasts", payload, part="snippet,status,contentDetails")


# ------------------------- أدوات مساعدة نقية (قابلة للاختبار) -------------------------

def broadcast_summary(broadcast: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """يستخرج حقلاً مختصراً للوحة دون كشف أي بيانات حساسة."""
    if not broadcast:
        return None
    snippet = broadcast.get("snippet", {}) or {}
    status = broadcast.get("status", {}) or {}
    return {
        "id": broadcast.get("id"),
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "tags": snippet.get("tags") or [],
        "category_id": snippet.get("categoryId"),
        "privacy_status": status.get("privacyStatus"),
        "life_cycle": status.get("lifeCycleStatus"),
        "made_for_kids": status.get("selfDeclaredMadeForKids"),
    }


def parse_tags(raw: Any) -> list[str]:
    """يقبل كلمات مفصولة بفواصل/أسطر أو قائمة JSON ويعيد قائمة نظيفة (حد 500 حرف/كلمة)."""
    if isinstance(raw, list):
        parts = raw
    else:
        text = str(raw or "")
        parts = [part for part in text.replace("\n", ",").split(",") if part.strip()]
    cleaned: list[str] = []
    for part in parts:
        tag = " ".join(str(part).split())
        if tag and tag not in cleaned:
            cleaned.append(tag)
    return cleaned[:15]

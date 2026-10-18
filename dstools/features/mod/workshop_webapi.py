"""通过 Steam 公开 Web API 查询工坊项目的最新更新时间，不需要 Steam 客户端和 API 密钥（不依赖界面）。

SteamCMD 通道没有 Steam 客户端可问，用它判断已下载的 Mod 是否需要更新：
实测返回的 time_updated 与 SteamCMD 写入 appworkshop_322330.acf 的 timeupdated 对应。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

from dstools.shared.ssl_context import default_ssl_context

# 工坊 ID 是 64 位无符号整数（十进制最多 20 位）
_ID_RE = r"([1-9]\d{0,19})"
# 一段输入里认得出的写法：纯数字、workshop-ID（modoverrides 里的写法）、网页链接里的 id=ID、
# Steam 客户端"复制网页 URL"得到的 steam://url/CommunityFilePage/ID
_TOKEN_RES = (
    re.compile(rf"(?:workshop-)?{_ID_RE}", re.IGNORECASE),
    re.compile(rf".*[?&]id={_ID_RE}(?:[&#].*)?", re.IGNORECASE),
    re.compile(rf".*/CommunityFilePage/{_ID_RE}/?", re.IGNORECASE),
)
_SEPARATOR_RE = re.compile(r"[\s,，;；、]+")

_DETAILS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
_BATCH = 100
_TIMEOUT = 15


@dataclass(frozen=True)
class RemoteWorkshopItem:
    time_updated: int
    title: str = ""
    # 工坊标签含 client_only_mod（实测：作者在 modinfo 里声明 client_only_mod 时上传带此标签），专服不加载
    client_only: bool = False


def parse_workshop_ids(text: str) -> tuple[list[str], list[str]]:
    """从用户输入里取出工坊 ID（可一次贴多个，空白或逗号分隔），返回 (去重后的 ID, 认不出的片段)。"""
    ids, invalid = [], []
    for token in _SEPARATOR_RE.split(text or ""):
        if not token:
            continue
        match = next((m for m in (pattern.fullmatch(token) for pattern in _TOKEN_RES) if m), None)
        if match is None:
            invalid.append(token)
        elif match.group(1) not in ids:
            ids.append(match.group(1))
    return ids, invalid


def fetch_remote_items(workshop_ids, *, timeout: float = _TIMEOUT, opener=None) -> dict[str, RemoteWorkshopItem]:
    """返回 {工坊 ID: 最新更新时间与标题}；查不到（已删除、私密等）的项目不在结果里。网络错误直接抛出，由调用方决定怎么提示。"""
    ids = [str(wid) for wid in dict.fromkeys(workshop_ids) if str(wid).isdigit()]
    open_url = opener or (lambda request: urllib.request.urlopen(request, timeout=timeout,
                                                                  context=default_ssl_context()))
    result: dict[str, RemoteWorkshopItem] = {}
    for start in range(0, len(ids), _BATCH):
        batch = ids[start:start + _BATCH]
        form = {"itemcount": str(len(batch))}
        form.update({f"publishedfileids[{index}]": wid for index, wid in enumerate(batch)})
        request = urllib.request.Request(_DETAILS_URL, data=urllib.parse.urlencode(form).encode("ascii"))
        with open_url(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        for item in payload.get("response", {}).get("publishedfiledetails", []):
            # result 为 1 表示查询成功；只认饥荒的工坊项目
            if item.get("result") == 1 and str(item.get("consumer_app_id")) == "322330":
                updated = item.get("time_updated")
                if isinstance(updated, int) and updated > 0:
                    tags = {str(tag.get("tag", "")) for tag in item.get("tags") or () if isinstance(tag, dict)}
                    result[str(item.get("publishedfileid"))] = RemoteWorkshopItem(
                        updated, str(item.get("title") or ""), "client_only_mod" in tags)
    return result

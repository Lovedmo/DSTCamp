"""通过 Steam 公开 Web API 查询工坊项目的最新更新时间，不需要 Steam 客户端和 API 密钥（不依赖界面）。

SteamCMD 通道没有 Steam 客户端可问，用它判断已下载的 Mod 是否需要更新：
实测返回的 time_updated 与 SteamCMD 写入 appworkshop_322330.acf 的 timeupdated 对应。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass

from dstools.shared.ssl_context import default_ssl_context

_DETAILS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
_BATCH = 100
_TIMEOUT = 15


@dataclass(frozen=True)
class RemoteWorkshopItem:
    time_updated: int
    title: str = ""


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
                    result[str(item.get("publishedfileid"))] = RemoteWorkshopItem(updated, str(item.get("title") or ""))
    return result

# -*- coding: utf-8 -*-
"""消息段转换工具（跨连接器复用）。"""

from __future__ import annotations

import re
from typing import List

# MC 颜色代码
_COLOR_CODE = re.compile(r"§[0-9a-fk-or]", re.IGNORECASE)


def str_to_array(message: str) -> List[dict]:
    """将字符串消息转换为消息数组格式。

    Parameters
    ----------
    message : str
        要转换的字符串消息

    Returns
    -------
    List[dict]
        转换后的消息数组
    """
    return [{"type": "text", "data": {"text": message}}]


def strip_minecraft_color_codes(text: str) -> str:
    """去掉 MC 颜色/格式代码（§a、§l 等）。"""
    return _COLOR_CODE.sub("", text or "")


def segments_to_plain_text(segments, *, image_as: str = "label") -> str:
    """OneBot array 消息段 → 纯文本。

    Parameters
    ----------
    segments : list[dict] 或 str
        消息段列表；字符串直接原样返回（剥颜色码）。
    image_as : str
        图片段的处理方式：
        - ``"label"``   → ``[图片:说明]``（默认，用于发往 QQ 的纯文本）
        - ``"cicode"``  → ``[[CICode,url=...,name=...]]``（ChatImage / AtomChat
          Cicodes 可识别并直接渲染图片；用于 native_chat 注入游戏内）
    """
    if segments is None:
        return ""
    if isinstance(segments, str):
        return strip_minecraft_color_codes(segments)
    if not isinstance(segments, list):
        return str(segments)

    parts: List[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type", "")
        data = seg.get("data", {}) or {}
        if seg_type == "text":
            parts.append(strip_minecraft_color_codes(data.get("text", "")))
        elif seg_type == "at":
            qq = data.get("qq", "")
            parts.append(f"@{qq}" if qq else "@全体成员")
        elif seg_type == "image":
            if image_as == "cicode":
                url = _image_source(data)
                name = (data.get("summary", "") or "图片").strip("[]") or "图片"
                parts.append(f"[[CICode,url={url},name={name}]]")
            else:
                summary = (data.get("summary", "") or "").strip("[]")
                parts.append(f"[图片:{summary}]" if summary else "[图片]")
        elif seg_type == "record":
            parts.append("[语音]")
        elif seg_type == "video":
            parts.append("[视频]")
        elif seg_type == "face":
            parts.append("[表情]")
        elif seg_type == "reply":
            continue  # 回复段不显示
        elif seg_type == "mface":
            parts.append(f"[表情:{data.get('summary', '表情')}]")
        else:
            parts.append(f"[{seg_type}]")
    return "".join(parts)


def _image_source(data: dict) -> str:
    """图片段取显示源。

    CICode 会被**客户端** mod（ChatImage / AtomChat Cicodes）解析渲染，
    因此必须用客户端能访问的地址：优先公网 url；本地 file:/// 路径只在
    无 url 时兜底（仅单机/同机场景可用）。
    """
    url = data.get("url", "") or ""
    if url.startswith(("http://", "https://")):
        return url
    file_ = data.get("file", "") or ""
    if file_:
        if file_.startswith("http"):
            return file_
        # 本地路径 → file URI（同机客户端才能读）
        norm = file_.replace("\\", "/")
        if not norm.startswith("file:"):
            norm = "file:///" + norm.lstrip("/")
        return norm
    return url or file_


def extract_images(segments) -> List[dict]:
    """提取消息段中的全部图片段（data 含 url/file/summary）。"""
    result: List[dict] = []
    if not isinstance(segments, list):
        return result
    for seg in segments:
        if isinstance(seg, dict) and seg.get("type") in ("image", "mface"):
            result.append(seg.get("data", {}) or {})
    return result

"""OCR 文字识别（调用原生 Swift + Vision，避免 pyobjc 中文乱码 bug）。

pyobjc 调 VNRecognizeTextRequest.setRecognitionLanguages_ 对中文不生效
（识别成乱码），原生 Swift 调用正常。故用子进程跑编译好的 ocr_helper。

对外接口：
    recognize(pixmap: QPixmap) -> list[dict]
    每个 dict: {"text": str, "rect": QRect(选区像素坐标), "confidence": float}
仅 macOS 可用。
"""
import json
import os
import subprocess
import sys

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QPixmap, QImage


def _helper_path():
    """返回编译好的 ocr_helper 路径（开发态 core/ 下；打包态 _MEIPASS 下）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        # 打包态：spec 把 ocr_helper 放到 core/
        p = os.path.join(base, "core", "ocr_helper")
        if os.path.exists(p):
            return p
        p = os.path.join(base, "ocr_helper")
        if os.path.exists(p):
            return p
    # 开发态：与本文件同目录
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "ocr_helper")


def recognize(pixmap: QPixmap):
    """对 QPixmap 做 OCR，返回识别到的文字块列表。失败返回空列表。"""
    if pixmap is None or pixmap.isNull():
        return []
    helper = _helper_path()
    if not os.path.exists(helper):
        print(f"OCR: 找不到 ocr_helper: {helper}")
        return []

    # QPixmap -> 临时 PNG 文件
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    try:
        if not pixmap.save(tmp.name, "PNG"):
            print("OCR: 临时图保存失败")
            return []
        # 调 Swift helper
        result = subprocess.run(
            [helper, tmp.name],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"OCR helper 失败: {result.stderr.strip()}")
            return []
        try:
            items = json.loads(result.stdout)
        except Exception as e:
            print(f"OCR JSON 解析失败: {e}")
            return []
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

    out = []
    for it in items:
        try:
            out.append({
                "text": it.get("text", ""),
                "rect": QRect(it["x"], it["y"], it["w"], it["h"]),
                "confidence": float(it.get("confidence", 0)),
            })
        except Exception:
            continue
    return out

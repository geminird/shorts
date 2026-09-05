#!/usr/bin/env swift
// 滚动截图拼接：用 Vision 的 VNTranslationalImageRegistrationRequest 检测两帧间的垂直位移。
// 用法：stitch_helper <prev.png> <new.png>
// 输出 JSON：{"ty": <垂直位移像素, 正=向下滚动>, "confidence": <0-1>}
// Vision 的全局图像配准是 Apple 原生实现，抗重复性内容（列表/文字行）比 NCC 强。
import Vision
import AppKit
import Foundation

let args = CommandLine.arguments
guard args.count >= 3 else { fputs("usage: stitch_helper <prev.png> <new.png>\n", stderr); exit(1) }

func loadCGImage(_ path: String) -> CGImage? {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(src, 0, nil)
}

guard let prevImg = loadCGImage(args[1]),
      let newImg = loadCGImage(args[2]) else {
    fputs("cannot open images\n", stderr); exit(1)
}

// target = prev（要找的基准），query = new（待配准的帧）
let request = VNTranslationalImageRegistrationRequest(targetedCGImage: prevImg, options: [:])
let handler = VNImageRequestHandler(cgImage: newImg)
do {
    try handler.perform([request])
} catch {
    fputs("perform failed: \(error)\n", stderr); exit(1)
}

var output: [String: Any] = ["ty": 0, "confidence": 0]
if let obs = request.results?.first {
    let t = obs.alignmentTransform
    // Vision 的 alignmentTransform.ty 已是像素单位（非 normalized）。
    // 正值 = new 相对 prev 向下位移（即向下滚动了 ty 像素）。
    output["ty"] = Double(t.ty)
    output["confidence"] = 1.0
}

guard let data = try? JSONSerialization.data(withJSONObject: output),
      let json = String(data: data, encoding: .utf8) else {
    fputs("json encode failed\n", stderr); exit(1)
}
print(json)

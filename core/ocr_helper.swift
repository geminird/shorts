#!/usr/bin/env swift
// OCR 助手：用 Vision 识别图片中的文字（中英文）。
// 用法：ocr_helper.swift <图片路径>
// 输出：每行一个识别到的文字块（JSON：{"text","x","y","w","h","confidence"}）
// 用 Swift 而非 pyobjc：pyobjc 调 VNRecognizeTextRequest 的 setRecognitionLanguages_
// 对中文不生效（识别成乱码），原生 Swift 调用正常。
import Vision
import AppKit
import Foundation

let args = CommandLine.arguments
guard args.count >= 2 else { fputs("usage: ocr_helper <image>\n", stderr); exit(1) }
let path = args[1]
guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
      let cgImage = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
    fputs("cannot open image: \(path)\n", stderr); exit(1)
}

let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US", "ja-JP"]
req.usesLanguageCorrection = true
let handler = VNImageRequestHandler(cgImage: cgImage, orientation: .up)
do {
    try handler.perform([req])
} catch {
    fputs("perform failed: \(error)\n", stderr); exit(1)
}

let observations = req.results ?? []
let w = CGFloat(cgImage.width)
let h = CGFloat(cgImage.height)
var output: [[String: Any]] = []
for obs in observations {
    guard let cand = obs.topCandidates(1).first else { continue }
    let bb = obs.boundingBox  // normalized, origin 左下
    let x = bb.origin.x * w
    let y = (1.0 - bb.origin.y - bb.height) * h
    let bw = bb.width * w
    let bh = bb.height * h
    output.append([
        "text": cand.string,
        "x": Int(x), "y": Int(y), "w": Int(bw), "h": Int(bh),
        "confidence": cand.confidence,
    ])
}
guard let data = try? JSONSerialization.data(withJSONObject: output, options: []),
      let json = String(data: data, encoding: .utf8) else {
    fputs("json encode failed\n", stderr); exit(1)
}
print(json)

// Bundled Vision OCR helper. Compiled on first use into the user cache dir.
//
//   ujshot-ocr <png> [png2 ...]
//   {"file":"…","w":440,"h":956,"lines":[{"t":"text","x":0.12,"y":0.34,"cx":0.2,"cy":0.35}]}
//
// x/y = top-left of the text box, normalised 0…1 with the origin at the top
// left (Vision's own origin is bottom-left, so it is flipped here). cx/cy are
// the box centre, which is what a tap needs.
import AppKit
import Foundation
import Vision

func jsonEscape(_ s: String) -> String {
    var out = ""
    for ch in s.unicodeScalars {
        switch ch {
        case "\"": out += "\\\""
        case "\\": out += "\\\\"
        case "\n": out += "\\n"
        case "\r": out += "\\r"
        case "\t": out += "\\t"
        default:
            if ch.value < 0x20 {
                out += String(format: "\\u%04x", ch.value)
            } else {
                out.unicodeScalars.append(ch)
            }
        }
    }
    return out
}

let args = CommandLine.arguments
guard args.count > 1 else {
    FileHandle.standardError.write("usage: ujshot-ocr <png> [png2 ...]\n".data(using: .utf8)!)
    exit(2)
}

let languages = (ProcessInfo.processInfo.environment["UJSHOT_OCR_LANGS"] ?? "zh-Hans,en-US")
    .split(separator: ",")
    .map { String($0).trimmingCharacters(in: .whitespaces) }
    .filter { !$0.isEmpty }

for path in args.dropFirst() {
    guard let image = NSImage(contentsOfFile: path),
          let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil)
    else {
        print("{\"file\":\"\(jsonEscape(path))\",\"error\":\"load failed\"}")
        continue
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = languages.isEmpty ? ["en-US"] : languages

    do {
        try VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
    } catch {
        print("{\"file\":\"\(jsonEscape(path))\",\"error\":\"vision failed\"}")
        continue
    }

    var lines: [String] = []
    for observation in (request.results as? [VNRecognizedTextObservation]) ?? [] {
        guard let top = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        let x = box.origin.x
        let y = 1 - box.origin.y - box.size.height
        let cx = x + box.size.width / 2
        let cy = y + box.size.height / 2
        lines.append("{\"t\":\"\(jsonEscape(top.string))\",\"x\":\(String(format: "%.4f", x)),\"y\":\(String(format: "%.4f", y)),\"cx\":\(String(format: "%.4f", cx)),\"cy\":\(String(format: "%.4f", cy))}")
    }
    print("{\"file\":\"\(jsonEscape(path))\",\"w\":\(cg.width),\"h\":\(cg.height),\"lines\":[\(lines.joined(separator: ","))]}")
}

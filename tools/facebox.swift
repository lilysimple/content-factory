// Лицо в кадре: рамки лиц на картинках, JSON в stdout.
//
// Зачем отдельный swift, а не питон: на обложку рилса нужен кадр, где
// человек в кадре, и текст, который не ложится на лицо. Ни один признак
// «на глаз» этого не даёт — пробовали и мерили (см. ТЗ обложки). В macOS
// детектор лиц уже есть, framework Vision, и он не тянет за собой ни
// новой библиотеки в venv, ни модели на диск.
//
// Запуск: swift tools/facebox.swift кадр1.png кадр2.png …
// Ответ: по строке JSON на кадр, в порядке аргументов.
//
// Координаты нормированы (0..1) и считаются от левого верхнего угла:
// Vision отдаёт от левого нижнего, переворот делается здесь, чтобы питон
// про эту разницу не знал.

import Foundation
import Vision

struct Box: Encodable {
    let x: Double, y: Double, w: Double, h: Double, conf: Double
}

struct Shot: Encodable {
    let path: String
    let faces: [Box]
    let error: String?
}

func encode(_ shot: Shot) -> String {
    let data = (try? JSONEncoder().encode(shot)) ?? Data()
    return String(data: data, encoding: .utf8) ?? "{}"
}

for path in CommandLine.arguments.dropFirst() {
    let url = URL(fileURLWithPath: path)
    let request = VNDetectFaceRectanglesRequest()
    let handler = VNImageRequestHandler(url: url, options: [:])
    do {
        try handler.perform([request])
        let faces = (request.results ?? []).map { face -> Box in
            let b = face.boundingBox
            return Box(x: Double(b.minX), y: Double(1 - b.maxY),
                       w: Double(b.width), h: Double(b.height),
                       conf: Double(face.confidence))
        }
        print(encode(Shot(path: path, faces: faces, error: nil)))
    } catch {
        print(encode(Shot(path: path, faces: [],
                          error: "\(error.localizedDescription)")))
    }
}

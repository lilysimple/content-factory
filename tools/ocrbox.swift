// Слова на кадре: текст и рамки, JSON в stdout.
//
// Зачем отдельный swift, а не питон: чтобы подчеркнуть на скриншоте
// именно то слово, о котором идёт речь, нужно знать, где оно лежит. В
// macOS распознаватель текста уже есть — тот же framework Vision, что
// считает лица (`facebox.swift`) и силуэт (`personmask.swift`), — и он
// не тянет за собой ни новой библиотеки в venv, ни модели на диск.
//
// Запуск: swift tools/ocrbox.swift кадр1.png кадр2.png …
// Ответ: по строке JSON на кадр, в порядке аргументов.
//
// Отдаются строки целиком и слова по отдельности: монтаж ищет фразу
// сначала внутри строки (тогда рамка считается по её куску), а не
// найдя — по словам. Рамку куска строки Vision считает сам
// (`boundingBox(for:)`), и делать это на стороне питона нечем.
//
// Координаты нормированы (0..1) и считаются от левого верхнего угла:
// Vision отдаёт от левого нижнего, переворот делается здесь, чтобы питон
// про эту разницу не знал.

import Foundation
import Vision

struct Word: Encodable {
    let text: String
    let x: Double, y: Double, w: Double, h: Double
    let conf: Double
}

struct Line: Encodable {
    let text: String
    let x: Double, y: Double, w: Double, h: Double
    let conf: Double
    let words: [Word]
}

struct Shot: Encodable {
    let path: String
    let lines: [Line]
    let error: String?
}

func encode(_ shot: Shot) -> String {
    let data = (try? JSONEncoder().encode(shot)) ?? Data()
    return String(data: data, encoding: .utf8) ?? "{}"
}

// Рамка Vision → доли кадра от левого верхнего угла.
func flip(_ b: CGRect) -> (Double, Double, Double, Double) {
    (Double(b.minX), Double(1 - b.maxY), Double(b.width), Double(b.height))
}

for path in CommandLine.arguments.dropFirst() {
    let url = URL(fileURLWithPath: path)
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    // Интерфейс снимают и по-русски, и по-английски, и в одном окне
    // сразу. Порядок здесь и есть приоритет языка.
    request.recognitionLanguages = ["ru-RU", "en-US"]
    request.usesLanguageCorrection = false
    // Мелкий шрифт интерфейса: по умолчанию Vision ищет текст не мельче
    // сотой доли высоты кадра и пропускает подписи в меню.
    request.minimumTextHeight = 0.006

    let handler = VNImageRequestHandler(url: url, options: [:])
    do {
        try handler.perform([request])
        var lines: [Line] = []
        for obs in (request.results ?? []) {
            guard let best = obs.topCandidates(1).first else { continue }
            let text = best.string
            let (lx, ly, lw, lh) = flip(obs.boundingBox)

            var words: [Word] = []
            // Слово — кусок строки между пробелами. Рамку куска Vision
            // считает по своим меткам, а не делит строку поровну.
            var cursor = text.startIndex
            while cursor < text.endIndex {
                if text[cursor] == " " {
                    cursor = text.index(after: cursor)
                    continue
                }
                var end = cursor
                while end < text.endIndex && text[end] != " " {
                    end = text.index(after: end)
                }
                let range = cursor..<end
                if let box = try? best.boundingBox(for: range) {
                    let (x, y, w, h) = flip(box.boundingBox)
                    words.append(Word(text: String(text[range]),
                                      x: x, y: y, w: w, h: h,
                                      conf: Double(best.confidence)))
                }
                cursor = end
            }
            lines.append(Line(text: text, x: lx, y: ly, w: lw, h: lh,
                              conf: Double(best.confidence), words: words))
        }
        print(encode(Shot(path: path, lines: lines, error: nil)))
    } catch {
        print(encode(Shot(path: path, lines: [],
                          error: error.localizedDescription)))
    }
}

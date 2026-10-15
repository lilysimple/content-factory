// Маска человека в кадре: PNG, где человек — непрозрачное, фон —
// прозрачное. Именно прозрачностью, а не яркостью: CSS-маска по умолчанию
// читает альфу (`mask-mode: match-source`), и серый PNG без альфы она
// считает сплошным. На таком «маске» слой человека закрывал весь кадр, и
// маскот за спиной пропадал целиком — поломка 18.09, час на поиск.
//
// Зачем отдельный swift, а не питон: чтобы поставить маскота **за** спину,
// нужен силуэт человека покадрово. Матирование обычно значит чужую модель
// (RVM, MediaPipe) и минуты счёта — но в macOS оно уже есть, framework
// Vision, тот же, что считает лица для обложки. Ни модели на диск, ни
// новой библиотеки в venv.
//
// Запуск: swift tools/personmask.swift кадр1.jpg маска1.png кадр2.jpg …
// Аргументы парами: вход, выход. Один запуск на всю пачку — swift
// компилирует скрипт на каждый вызов, и секунда компиляции на кадр вместо
// секунды на пачку это плохая сделка (та же причина, что у facebox).
//
// На stdout — по строке на кадр: `ok путь клетка` или `fail путь причина`.
// Человека в кадре может не быть, и это не поломка: кадр просто остаётся
// без маски, а питон считает такие кадры и называет их числом.
//
// Клетка — тот же силуэт крупной сеткой 64×114, base64 от байт яркости.
// Нужна она замеру перекрытия: место маскоту выбирают по тому, сколько
// его закрывает человек, и считать это надо до рендера. Отдаётся она
// отсюда, потому что здесь силуэт уже в руках: читать полтысячи PNG
// второй раз нечем — бандловый ffmpeg не знает ни `alphaextract`, ни
// мукcера `rawvideo`, а Pillow в этом проекте не живёт.
import Foundation
import Vision
import CoreImage
import AppKit

let args = Array(CommandLine.arguments.dropFirst())
guard args.count >= 2, args.count % 2 == 0 else {
    FileHandle.standardError.write("нужны пары: вход выход\n".data(using: .utf8)!)
    exit(2)
}

// Контекст один на все кадры: он держит связь с GPU, и пересоздавать его
// на каждый кадр значит платить эту связь тысячу раз.
let ctx = CIContext()

// Сетка замера. Числа повторены в `footage.MASK_COLS`/`MASK_ROWS`:
// разъехаться им можно только правкой, и питон проверяет длину строки.
let COLS = 64
let ROWS = 114
let gray = CGColorSpaceCreateDeviceGray()

/// Силуэт крупной клеткой: строка base64 длиной COLS×ROWS байт.
func cells(_ mask: CIImage) -> String? {
    guard let cg = ctx.createCGImage(mask, from: mask.extent) else { return nil }
    var buf = [UInt8](repeating: 0, count: COLS * ROWS)
    let ok: Bool = buf.withUnsafeMutableBytes { raw -> Bool in
        guard let g = CGContext(data: raw.baseAddress, width: COLS, height: ROWS,
                                bitsPerComponent: 8, bytesPerRow: COLS,
                                space: gray,
                                bitmapInfo: CGImageAlphaInfo.none.rawValue)
        else { return false }
        // CGContext считает строки снизу вверх, а клетка читается сверху:
        // переворачиваем здесь, иначе питон мерил бы перекрытие вверх ногами.
        g.translateBy(x: 0, y: CGFloat(ROWS))
        g.scaleBy(x: 1, y: -1)
        g.draw(cg, in: CGRect(x: 0, y: 0, width: CGFloat(COLS),
                              height: CGFloat(ROWS)))
        return true
    }
    return ok ? Data(buf).base64EncodedString() : nil
}
let request = VNGeneratePersonSegmentationRequest()
request.qualityLevel = .accurate
request.outputPixelFormat = kCVPixelFormatType_OneComponent8

func say(_ line: String) {
    print(line)
    fflush(stdout)
}

// ── дрожание края ─────────────────────────────────────────────────────
//
// Силуэт Vision обводит на каждом кадре заново, и на соседних кадрах
// контур отличается на пару пикселей. Неподвижному человеку это не
// мешает, а вот предмет, вырезанный такой маской, дёргается по краю: на
// дубле 20.09 у маскота «дрожала лапа» там, где рядом проходила рука, —
// доля знака в этом месте скакала 13 → 27 → 40 → 7 между кадрами.
//
// Лечится двумя вещами, и обе тут: маска усредняется с соседями по
// времени (окно в три кадра, центрированное — отсюда задержка на один
// кадр) и размывается на пару пикселей. Первое убирает дрожание, второе
// делает срез мягким: жёсткий контур выдаёт монтаж сильнее мягкого.

let SMOOTH_BLUR = 2.0     // радиус размытия края, пиксели маски

/// Среднее нескольких масок. Каждая гасится долей и складывается.
func blend(_ imgs: [CIImage]) -> CIImage {
    let k = CGFloat(1.0) / CGFloat(imgs.count)
    var acc: CIImage?
    for im in imgs {
        let part = im.applyingFilter("CIColorMatrix", parameters: [
            "inputRVector": CIVector(x: k, y: 0, z: 0, w: 0),
            "inputGVector": CIVector(x: 0, y: k, z: 0, w: 0),
            "inputBVector": CIVector(x: 0, y: 0, z: k, w: 0),
            "inputAVector": CIVector(x: 0, y: 0, z: 0, w: 1),
        ])
        acc = acc == nil ? part : part.applyingFilter(
            "CIAdditionCompositing",
            parameters: [kCIInputBackgroundImageKey: acc!])
    }
    return acc ?? imgs[0]
}

struct Shot {
    let src: String
    let dst: URL
    let mask: CIImage
    let frame: CGRect
}

/// Записать маску кадра `mid`, усреднив её с соседями из окна.
func emit(_ win: [Shot], _ mid: Int) {
    let shot = win[mid]
    let smooth = blend(win.map {$0.mask})
        .applyingFilter("CIGaussianBlur",
                        parameters: [kCIInputRadiusKey: SMOOTH_BLUR])
        .cropped(to: shot.frame)
    // Клетка снимается с яркости, до превращения в альфу: так она
    // читается тем же способом, каким пришла от Vision.
    let grid = cells(smooth)
    // Яркость маски → альфа. Дальше PNG несёт силуэт прозрачностью.
    let alpha = smooth.applyingFilter("CIMaskToAlpha")
    guard let cg = ctx.createCGImage(alpha, from: shot.frame),
          let data = NSBitmapImageRep(cgImage: cg)
            .representation(using: .png, properties: [:]) else {
        say("fail \(shot.src) не собрал PNG"); return
    }
    do {
        try data.write(to: shot.dst)
        say("ok \(shot.src) \(grid ?? "")")
    } catch {
        say("fail \(shot.src) \(error.localizedDescription)")
    }
}

// Окно из трёх кадров: пишем средний, сдвигаемся на один.
var win: [Shot] = []

for i in stride(from: 0, to: args.count, by: 2) {
    let src = URL(fileURLWithPath: args[i])
    let dst = URL(fileURLWithPath: args[i + 1])
    do {
        let handler = VNImageRequestHandler(url: src, options: [:])
        try handler.perform([request])
        guard let obs = request.results?.first,
              let source = CIImage(contentsOf: src) else {
            say("fail \(args[i]) нет человека"); continue
        }
        // Маска приходит своим размером, меньше кадра: тянем до исходного.
        let ci = CIImage(cvPixelBuffer: obs.pixelBuffer)
        let scaled = ci.transformed(by: CGAffineTransform(
            scaleX: source.extent.width / ci.extent.width,
            y: source.extent.height / ci.extent.height))
        win.append(Shot(src: args[i], dst: dst, mask: scaled,
                        frame: source.extent))
        if win.count == 3 {
            emit(win, 1)
            win.removeFirst()
        } else if win.count == 2 {
            emit(win, 0)          // первый кадр: соседа слева нет
        }
    } catch {
        say("fail \(args[i]) \(error.localizedDescription)")
    }
}
// Последний кадр остаётся в окне: у него нет соседа справа.
if win.count >= 2 {
    emit(win, win.count - 1)
} else if win.count == 1 {
    emit(win, 0)
}

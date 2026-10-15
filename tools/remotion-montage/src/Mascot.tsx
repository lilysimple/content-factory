import React from 'react';
import {
  AbsoluteFill,
  Img,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';

// ── маскот в кадре ───────────────────────────────────────────────────
//
// Знак Claude, живущий за спиной говорящего: дышит, покачивается,
// моргает. Нужен он там, где кадр пустой, — на тёмной стене или над
// столом, — и работает ровно потому, что не лезет на лицо.
//
// Человека из кадра **не вырезаем**. Матирование видео (RVM, MediaPipe)
// это отдельная модель, минуты счёта и рваный контур на волосах при
// комнатном свете; съёмка при этом почти всегда оставляет свободный угол,
// и маскот, поставленный в него, читается фоном без всякой маски. Отсюда
// `x`/`y` входом: куда именно ставить, решает питон по рамке лица, а не
// этот файл — здесь мы только рисуем.
//
// Вся анимация — чистые функции кадра, как требует Remotion: никакого
// CSS-`animation`, иначе рендер получит первый кадр двести раз подряд.
//
// «За спину» делается вычитанием, а не вторым слоем видео. Сначала
// поверх маскота клали тот же кусок ещё раз, обрезанный силуэтом, — и
// второй декодер отставал от первого: через кадр силуэт не приезжал
// вовсе, маскот то обрезало плечом, то нет, и в ролике это читалось
// морганием (замер 20.09: доля знака в кадре скакала 35 → 48 → 35).
// Теперь декодер один: силуэт вычитается из самого маскота
// (`mask-composite: subtract`), и опаздывать тут нечему.

export type MascotLook = {
  x: number;        // 0..1 по ширине холста, центр знака
  y: number;        // 0..1 по высоте
  size: number;     // доля ширины холста
  color: string;
  opacity: number;
  glow: number;     // 0 — без свечения
  face: boolean;    // глаза: без них это просто знак, с ними — существо
  // Имя картинки внутри public/. Есть — рисуем её, и нарисованная звезда
  // не собирается вовсе: у бренда свой маскот, и подменять его своим
  // значком значит спорить с человеком молча.
  src?: string;
  // Сколько секунд маскот живёт в кадре, считая от своего появления.
  // Нужен ему самому, а не только шаблону: последние полсекунды он
  // растворяется, и обрезать его одной длиной секвенции значит получить
  // щелчок.
  until?: number;
};

const LEAVE = 0.5;        // столько длится уход

// Лучи знака: угол и доля длины. Двенадцать штук с разной длиной —
// звезда Claude не циркульная, ровные лучи читаются снежинкой.
const RAYS: [number, number][] = [
  [0, 1.0], [30, 0.62], [61, 0.86], [90, 0.55],
  [119, 0.93], [150, 0.6], [180, 1.0], [209, 0.64],
  [241, 0.88], [270, 0.55], [299, 0.9], [330, 0.62],
];

const BLINK_EVERY = 3.4;      // секунды между морганиями
const BLINK_FOR = 0.13;       // само моргание

// Силуэт человека на секунде `t` тела ролика: адрес маски и то, каким
// размером и куда она ложится на холст. Считает это шаблон — он знает
// куски и панораму; маскот только вырезает себя ею.
export type Veil = {
  url: string;
  left: number;
  top: number;
  boxW: number;
  boxH: number;
};

export const Mascot: React.FC<{
  look: MascotLook;
  veil?: (bodySeconds: number) => Veil | null;
}> = ({look, veil}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const t = frame / fps;
  const cut = veil ? veil(t) : null;

  const box = width * look.size;
  const cx = look.x * width;
  const cy = look.y * height;

  // Появление: знак не возникает вместе с первым кадром куска, а
  // проявляется за полсекунды. Резкое включение читается браком склейки.
  const enter = interpolate(t, [0, 0.55], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const leave = look.until
    ? interpolate(t, [look.until - LEAVE, look.until], [1, 0], {
        extrapolateLeft: 'clamp',
        extrapolateRight: 'clamp',
      })
    : 1;

  // Движения ровно столько, чтобы знак не читался наклейкой. Первая
  // амплитуда (покачивание в 3,5 % кадра, наклон в пять градусов) на
  // живом ролике 20.09 выглядела вознёй: маскот стоит за плечом, у него
  // нет причины качаться, а глаз ловит это движение вместо лица.
  const bob = Math.sin(t * 0.9) * box * 0.010;         // покачивание
  const tilt = Math.sin(t * 0.5) * 1.2;                // наклон, градусы
  const breath = 1 + Math.sin(t * 1.3) * 0.012;        // дыхание

  // Моргание: короткое схлопывание глаз, без такта — глаз человека
  // ловит ритм и перестаёт верить.
  const phase = t % BLINK_EVERY;
  const blink = phase < BLINK_FOR
    ? Math.sin((phase / BLINK_FOR) * Math.PI)
    : 0;

  const r = box / 2;
  const body = r * 0.38;          // круглое тело, на нём лицо
  const eyeY = -body * 0.12;
  const eyeX = body * 0.36;
  const eyeR = body * 0.17;

  // Два слоя маски: сплошной и силуэт, вычитанием. Стандартное
  // `mask-composite: subtract` и вебкитовское `xor` значат тут одно и то
  // же; Chrome рендера понимает оба, и оставлены оба намеренно.
  const carved: React.CSSProperties = cut
    ? {
        WebkitMaskImage: `linear-gradient(#000,#000), url(${cut.url})`,
        maskImage: `linear-gradient(#000,#000), url(${cut.url})`,
        WebkitMaskSize: `100% 100%, ${cut.boxW}px ${cut.boxH}px`,
        maskSize: `100% 100%, ${cut.boxW}px ${cut.boxH}px`,
        WebkitMaskPosition: `0 0, ${cut.left}px ${cut.top}px`,
        maskPosition: `0 0, ${cut.left}px ${cut.top}px`,
        WebkitMaskRepeat: 'no-repeat, no-repeat',
        maskRepeat: 'no-repeat, no-repeat',
        WebkitMaskComposite: 'xor',
        maskComposite: 'subtract',
      }
    : {};

  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      {/* Та же маска, но обычной картинкой и невидимкой. CSS-маску
          Remotion не ждёт: кадр снимается, пока `mask-image` ещё едет по
          сети, и знак выходит необрезанным. `Img` задерживает кадр до
          загрузки (`delayRender` внутри), а браузер отдаёт маске уже
          готовый файл из кеша. Замер 20.09 до этой строки: четыре
          необрезанных кадра за десять секунд, по одному раз в две.
          Греется и следующий кадр: с одним текущим один срыв на триста
          кадров всё равно оставался. */}
      {[cut?.url, veil ? veil(t + 1 / fps)?.url : null]
        .filter((url, i, all): url is string =>
          Boolean(url) && all.indexOf(url) === i)
        .map((url) => (
          <Img
            key={url}
            src={url}
            style={{position: 'absolute', width: 1, height: 1, opacity: 0}}
          />
        ))}
      <AbsoluteFill style={carved}>
      <div
        style={{
          position: 'absolute',
          left: cx - box / 2,
          top: cy - box / 2 + bob,
          width: box,
          height: box,
          opacity: look.opacity * enter * leave,
          transform: `rotate(${tilt}deg)`,
        }}
      >
        {look.glow > 0 ? (
          <div
            style={{
              position: 'absolute',
              inset: `-${box * 0.35}px`,
              borderRadius: '50%',
              background: `radial-gradient(circle, ${look.color}${Math.round(
                look.glow * 255,
              )
                .toString(16)
                .padStart(2, '0')} 0%, rgba(0,0,0,0) 68%)`,
            }}
          />
        ) : null}

        {look.src ? (
          // Картинка качается вокруг нижнего края: маскот стоит на полу,
          // а не висит в воздухе, и вращение вокруг центра выглядело бы
          // болтанием на нитке.
          <Img
            src={staticFile(look.src)}
            style={{
              position: 'absolute',
              inset: 0,
              width: box,
              height: box,
              objectFit: 'contain',
              transformOrigin: '50% 100%',
              scale: String(breath),
            }}
          />
        ) : (
        <svg
          width={box}
          height={box}
          viewBox={`${-r} ${-r} ${box} ${box}`}
          style={{position: 'absolute', inset: 0, overflow: 'visible'}}
        >
          {/* Лучи идут из-под тела: звезда это нимб, а не туловище.
              Дышат они вразнобой — одинаковая фаза даёт пульс лампочки,
              сдвиг по индексу читается живым. */}
          {RAYS.map(([deg, len], i) => {
            const own = 1 + Math.sin(t * 1.3 + i * 0.52) * 0.025;
            const from = body * 0.78;
            const long = r * 0.97 * len * breath * own;
            const thick = r * 0.13;
            return (
              <rect
                key={deg}
                x={from}
                y={-thick / 2}
                width={Math.max(1, long - from)}
                height={thick}
                rx={thick / 2}
                fill={look.color}
                transform={`rotate(${deg})`}
              />
            );
          })}

          <circle r={body} fill={look.color} />

          {look.face ? (
            <g>
              {[-1, 1].map((side) => (
                <ellipse
                  key={side}
                  cx={eyeX * side}
                  cy={eyeY}
                  rx={eyeR}
                  ry={Math.max(eyeR * 0.1, eyeR * (1 - blink))}
                  fill="#241a15"
                />
              ))}
              {/* Улыбка: дуга, а не линия — линия читается ртом робота. */}
              <path
                d={`M ${-body * 0.34} ${body * 0.34}
                    Q 0 ${body * 0.66} ${body * 0.34} ${body * 0.34}`}
                stroke="#241a15"
                strokeWidth={body * 0.11}
                strokeLinecap="round"
                fill="none"
              />
            </g>
          ) : null}
        </svg>
        )}
      </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

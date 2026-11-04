import React from 'react';
import {
  AbsoluteFill,
  CanvasImage,
  Easing,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import {loadFont as loadCaveat} from '@remotion/google-fonts/Caveat';
import {loadFont as loadFiraCondensed} from '@remotion/google-fonts/FiraSansCondensed';
import {loadFont as loadManrope} from '@remotion/google-fonts/Manrope';
import type {ReelProps} from './props';

// Слои поверх дубля: второй формат монтажа, рядом со сплитом.
//
// Разница с панелью принципиальная, и она не в наборе украшений. Панель
// делит кадр швом и живёт второй дорожкой: зритель смотрит на неё почти
// столько же, сколько на человека. Слой кадр не делит — он лежит
// поверх, дубль под ним виден всё время, и ролик держится на лице, а не
// на карточке. Поэтому слоёв на ролик единицы, а не десяток: каждый
// закрывает собой часть кадра, и второй поверх первого — уже мусор.
//
// Здесь только рисование. Секунды, фразы, снимки и места подсветки
// посчитал питон (`montage.lay_layers`, `panelshot.proof`), и правило
// «слой стоит на слове» живёт там же, где все остальные правила монтажа.

const SUBSETS = ['cyrillic', 'latin'] as const;

// Рукописный акцент — Caveat: он есть с кириллицей, а без кириллицы
// русский акцент уехал бы системным шрифтом и перестал быть рукописным.
const SCRIPT_FONTS: Record<string, string> = {
  Caveat: loadCaveat('normal', {
    weights: ['500', '700'],
    subsets: [...SUBSETS],
  }).fontFamily,
};

const LAYER_FONTS: Record<string, string> = {
  FiraSansCondensed: loadFiraCondensed('normal', {
    weights: ['500', '900'],
    subsets: [...SUBSETS],
  }).fontFamily,
  Manrope: loadManrope('normal', {
    weights: ['500', '800'],
    subsets: [...SUBSETS],
  }).fontFamily,
};

const EASE = {
  extrapolateLeft: 'clamp' as const,
  extrapolateRight: 'clamp' as const,
  easing: Easing.bezier(0.16, 1, 0.3, 1),
};

// Сколько секунд слой въезжает и столько же уходит. Короче — щелчок,
// длиннее — слой едет дольше, чем держится на экране.
const WING = 0.36;

/** Насколько слой показан: 0 за своими краями, 1 в середине. */
const useShown = (): number => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const wing = Math.max(1, Math.round(WING * fps));
  return Math.min(
    interpolate(frame, [0, wing], [0, 1], EASE),
    interpolate(frame, [durationInFrames - wing, durationInFrames], [1, 0],
                EASE),
  );
};

// ── заголовок-плашка и рукописный акцент ─────────────────────────────
//
// Верх кадра, сжатый гротеск капсом, поверх него рукописное слово со
// стрелкой. Пара работает вместе: плашка называет тему, акцент говорит
// то, чего в ней нет, — куда смотреть и почему это важно. Акцент без
// плашки повисает в воздухе, плашка без акцента читается титром.

const ARROWS: Record<string, string> = {
  // Завиток от руки, нарисованный в квадрате 100×100. Концы разные:
  // стрелка должна показывать, а не соединять.
  down: 'M12,8 C46,2 74,18 70,46 C67,68 44,74 34,62 C27,53 36,42 48,48 '
    + 'C62,55 60,80 44,90',
  left: 'M88,16 C58,10 26,22 18,46 C13,62 26,76 40,72 C52,68 50,54 38,54 '
    + 'C24,54 14,68 14,84',
  right: 'M12,16 C42,10 74,22 82,46 C87,62 74,76 60,72 C48,68 50,54 62,54 '
    + 'C76,54 86,68 86,84',
};

const ARROW_TIP: Record<string, [number, number, number]> = {
  // Куда смотрит наконечник и под каким углом он стоит.
  down: [44, 90, 110],
  left: [14, 84, 200],
  right: [86, 84, 340],
};

const Arrow: React.FC<{dir: string; size: number; color: string;
                       shown: number}> = ({dir, size, color, shown}) => {
  const path = ARROWS[dir];
  const tip = ARROW_TIP[dir];
  if (!path || !tip) return null;
  // Длина пути нам неизвестна точно, но завиток в квадрате 100 короче
  // трёхсот: с запасом хватает, а лишнее съедает `clamp`.
  const len = 300;
  const [tx, ty, deg] = tip;
  // Наконечник приезжает последним и только когда линия дошла: стрелка,
  // у которой острие появилось раньше хвоста, читается помаркой.
  const head = interpolate(shown, [0.75, 1], [0, 1], EASE);
  return (
    <svg
      viewBox="0 0 100 100"
      width={size}
      height={size}
      style={{overflow: 'visible'}}
    >
      <path
        d={path}
        fill="none"
        stroke={color}
        strokeWidth={5}
        strokeLinecap="round"
        strokeDasharray={len}
        strokeDashoffset={len * (1 - shown)}
      />
      <path
        d="M0,0 L-11,-5 M0,0 L-11,6"
        fill="none"
        stroke={color}
        strokeWidth={5}
        strokeLinecap="round"
        opacity={head}
        transform={`translate(${tx} ${ty}) rotate(${deg})`}
      />
    </svg>
  );
};

const Title: React.FC<{
  lines: string[];
  script: string;
  arrow: string;
  color: string;
  font: string;
  scriptFont: string;
}> = ({lines, script, arrow, color, font, scriptFont}) => {
  const shown = useShown();
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();

  // Кегль от ширины холста и от самой длинной строки: заголовок тянется
  // на всю ширину, а переносом строк распоряжается роль, не шаблон.
  const longest = lines.reduce((n, s) => Math.max(n, s.length), 1);
  const size = Math.min(width * 0.155, (width * 0.92) / (longest * 0.47));

  return (
    <AbsoluteFill style={{justifyContent: 'flex-start', alignItems: 'center'}}>
      <div
        style={{
          position: 'relative',
          marginTop: height * 0.055,
          width: '92%',
          opacity: shown,
          translate: `0 ${((shown - 1) * height * 0.035).toFixed(1)}px`,
        }}
      >
        {lines.map((text, i) => {
          // Строки встают лесенкой: вторая на треть секунды позже
          // первой. Обе разом — это плашка, по одной — это набор.
          const own = interpolate(
            frame, [i * fps * 0.14, i * fps * 0.14 + fps * 0.42], [0, 1],
            EASE);
          return (
            <div
              key={`${text}-${i}`}
              style={{
                fontFamily: font,
                fontWeight: 900,
                fontSize: size,
                lineHeight: 0.96,
                letterSpacing: '-0.01em',
                color,
                textTransform: 'uppercase',
                textAlign: 'center',
                opacity: own,
                translate: `0 ${((own - 1) * size * 0.3).toFixed(1)}px`,
                textShadow: '0 6px 28px rgba(0,0,0,0.45)',
              }}
            >
              {text}
            </div>
          );
        })}

        {script ? (
          <div
            style={{
              position: 'absolute',
              right: '6%',
              // Под нижней строкой, а не на ней. Строки идут с
              // `lineHeight` 0.96, и первый замер клал акцент прямо на
              // буквы заголовка: на рендере 22.10 «Instagram» лёг
              // поперёк слова «ОБНОВЛЕНИЕ» и оба перестали читаться.
              top: `${lines.length * size * 0.96 + size * 0.08}px`,
              maxWidth: '88%',
              display: 'flex',
              // Стрелка идёт с той стороны, куда показывает: слева, если
              // показывает влево. Иначе завиток уезжает за край кадра —
              // и вместе с ним уезжает то, ради чего он нарисован.
              flexDirection: arrow === 'left' ? 'row-reverse' : 'row',
              alignItems: 'flex-start',
              gap: size * 0.12,
              // Акцент пишется после заголовка, а не вместе с ним:
              // рукописное слово поверх ещё не вставших строк читается
              // как вторая плашка.
              opacity: interpolate(
                frame, [fps * 0.3, fps * 0.75], [0, 1], EASE),
            }}
          >
            <div
              style={{
                fontFamily: scriptFont,
                fontWeight: 700,
                fontSize: size * 0.52,
                lineHeight: 1,
                color,
                rotate: '-7deg',
                textShadow: '0 4px 18px rgba(0,0,0,0.4)',
                // Слово проявляется слева направо, как его пишут рукой.
                clipPath: `inset(0 ${(100 * (1 - interpolate(
                  frame, [fps * 0.3, fps * 0.9], [0, 1], EASE))).toFixed(1)}% 0 0)`,
              }}
            >
              {script}
            </div>
            {arrow !== 'none' ? (
              <Arrow
                dir={arrow}
                size={size * 0.8}
                color={color}
                shown={interpolate(frame, [fps * 0.6, fps * 1.25], [0, 1],
                                   EASE)}
              />
            ) : null}
          </div>
        ) : null}
      </div>
    </AbsoluteFill>
  );
};

// ── плашка площадки ──────────────────────────────────────────────────
//
// Карточка-ссылка в стиле системного интерфейса площадки: знак слева,
// имя жирным, подпись серым, шеврон справа. Работает она ровно тем, что
// не выглядит титром: зритель читает её как часть приложения, а не как
// монтаж, и потому дочитывает.
//
// Отсюда и ограничение: текст на ней — то, что человек произнёс, а не
// обещание от имени площадки. Выдуманная строка в чужом интерфейсе это
// не оформление, а подделка, и ловит её питон (`cut._layers`).

const Chrome: React.FC<{lines: string[]; font: string}> = ({lines, font}) => {
  const shown = useShown();
  const {width, height} = useVideoConfig();
  const [head, note] = [lines[0] ?? '', lines[1] ?? ''];

  return (
    <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center'}}>
      <div
        style={{
          width: width * 0.86,
          display: 'flex',
          alignItems: 'center',
          gap: width * 0.025,
          padding: `${width * 0.028}px ${width * 0.032}px`,
          borderRadius: width * 0.022,
          background: 'rgba(255,255,255,0.97)',
          boxShadow: '0 10px 40px rgba(0,0,0,0.28)',
          opacity: shown,
          scale: `${(0.965 + shown * 0.035).toFixed(3)}`,
          translate: `0 ${((1 - shown) * height * 0.012).toFixed(1)}px`,
        }}
      >
        {/* Знак нарисован линией, а не взят с площадки: чужой логотип в
            кадре это чужой знак, и ставить его мы не вправе. */}
        <svg width={width * 0.045} height={width * 0.045} viewBox="0 0 24 24">
          <rect x="3" y="3" width="18" height="18" rx="5" fill="none"
                stroke="#111" strokeWidth="1.8" />
          <circle cx="12" cy="12" r="4.2" fill="none" stroke="#111"
                  strokeWidth="1.8" />
          <circle cx="17.2" cy="6.8" r="1.1" fill="#111" />
        </svg>
        <div style={{flex: 1, minWidth: 0}}>
          <div
            style={{
              fontFamily: font,
              fontWeight: 800,
              fontSize: width * 0.031,
              lineHeight: 1.2,
              color: '#111',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {head}
          </div>
          {note ? (
            <div
              style={{
                fontFamily: font,
                fontWeight: 500,
                fontSize: width * 0.026,
                lineHeight: 1.25,
                marginTop: width * 0.006,
                color: 'rgba(0,0,0,0.5)',
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {note}
            </div>
          ) : null}
        </div>
        <div
          style={{
            fontFamily: font,
            fontSize: width * 0.03,
            color: 'rgba(0,0,0,0.35)',
          }}
        >
          ›
        </div>
      </div>
    </AbsoluteFill>
  );
};

// ── снимок страницы с ползущим выделением ────────────────────────────
//
// Самый дорогой слой и самое сильное, что в этом формате есть: человек
// говорит «они сами так пишут», и на экране лежит страница, где это
// написано, с подсвеченной строкой. Пересказ держится на доверии,
// снимок — нет.
//
// Три состояния: карточка с наклоном в углу, разворот во весь кадр и
// подсветка строк по мере речи. Снимок длинный, поэтому до каждой метки
// он прокручивается — и прокрутка едет вместе с подсветкой, а не до неё.

const PROOF_CARD = 0.62;    // ширина карточки, доля холста
const PROOF_FULL = 0.92;    // ширина развёрнутого снимка
const PROOF_TILT = 7;       // наклон карточки, градусы
const SWEEP = 0.45;         // секунды, за которые маркер проходит строку

const Proof: React.FC<{
  src: string;
  imageWidth: number;
  imageHeight: number;
  marks: ReelProps['layers'][number]['marks'];
  openAt?: number;
  start: number;
  accent: string;
}> = ({src, imageWidth, imageHeight, marks, openAt, start, accent}) => {
  const shown = useShown();
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();

  // Разворот: от карточки с наклоном к снимку во весь кадр. Нет
  // `openAt` — слой так и остаётся карточкой, и это нормальный случай:
  // страницу иногда достаточно показать, а не читать.
  const openFrame = openAt === undefined
    ? null
    : Math.max(0, Math.round((openAt - start) * fps));
  const open = openFrame === null
    ? 0
    : interpolate(frame, [openFrame, openFrame + Math.round(fps * 0.7)],
                  [0, 1], EASE);

  const share = PROOF_CARD + (PROOF_FULL - PROOF_CARD) * open;
  const shotW = width * share;
  const ratio = imageWidth > 0 ? imageHeight / imageWidth : 1.4;
  const shotH = shotW * ratio;

  // Окно, в котором снимок виден. Карточка показывает его верх, в
  // развороте окно вырастает почти во весь кадр — и только тогда есть
  // куда прокручивать.
  const windowH = height * (0.34 + 0.44 * open);

  // До какой метки доехали и на сколько прокрутился снимок. Метка
  // ставится по центру окна, но снимок не уезжает за свои края: белая
  // полоса под последней строкой читается браком.
  const now = frame / fps;
  const live = marks.filter((m) => now >= m.at - start - SWEEP);
  const target = live.length > 0 ? live[live.length - 1] : null;
  const want = target
    ? (target.y + target.h / 2) * shotH - windowH / 2
    : 0;
  const prev = live.length > 1 ? live[live.length - 2] : null;
  const from = prev ? (prev.y + prev.h / 2) * shotH - windowH / 2 : 0;
  const since = target ? (now - (target.at - start - SWEEP)) : 0;
  const slide = interpolate(since, [0, 0.55], [0, 1], EASE);
  const scroll = Math.max(
    0,
    Math.min(Math.max(0, shotH - windowH),
             from + (want - from) * slide),
  );

  return (
    <AbsoluteFill style={{justifyContent: 'flex-start', alignItems: 'center'}}>
      <div
        style={{
          marginTop: height * (0.07 - 0.045 * open),
          width: shotW,
          height: windowH,
          overflow: 'hidden',
          borderRadius: width * 0.026,
          background: '#fff',
          boxShadow: '0 20px 70px rgba(0,0,0,0.4)',
          opacity: shown,
          // Наклон уходит вместе с разворотом: читать текст под семь
          // градусов нельзя, а карточка под ноль градусов не читается
          // карточкой.
          rotate: `${(PROOF_TILT * (1 - open) * shown).toFixed(2)}deg`,
          translate: `0 ${((shown - 1) * height * 0.05).toFixed(1)}px`,
        }}
      >
        <div
          style={{
            position: 'relative',
            width: shotW,
            height: shotH,
            translate: `0 ${(-scroll).toFixed(1)}px`,
          }}
        >
          <CanvasImage
            src={staticFile(src)}
            style={{width: shotW, height: shotH, objectFit: 'cover',
                    objectPosition: 'top'}}
          />
          {marks.map((m, i) => {
            // Маркер проходит строку слева направо за `SWEEP` и дальше
            // остаётся лежать: подсветка, которая гаснет, заставляет
            // зрителя вернуться глазами назад.
            const at = Math.round((m.at - start) * fps);
            const swept = interpolate(
              frame, [at, at + Math.round(SWEEP * fps)], [0, 1], EASE);
            return (
              <div
                key={`${m.at}-${i}`}
                style={{
                  position: 'absolute',
                  left: m.x * shotW,
                  top: m.y * shotH,
                  width: m.w * shotW * swept,
                  height: m.h * shotH,
                  background: accent,
                  opacity: 0.34,
                  borderRadius: shotW * 0.006,
                  mixBlendMode: 'multiply',
                }}
              />
            );
          })}
        </div>
      </div>
    </AbsoluteFill>
  );
};

// ── сборка одного слоя ───────────────────────────────────────────────

export const Layer: React.FC<{
  layer: ReelProps['layers'][number];
  color: string;
  accent: string;
  font: string;
  scriptFont: string;
}> = ({layer, color, accent, font, scriptFont}) => {
  const face = LAYER_FONTS[font] ?? LAYER_FONTS.FiraSansCondensed;
  const script = SCRIPT_FONTS[scriptFont] ?? SCRIPT_FONTS.Caveat;

  if (layer.kind === 'title') {
    return (
      <Title
        lines={layer.lines}
        script={layer.script}
        arrow={layer.arrow}
        color={color}
        font={face}
        scriptFont={script}
      />
    );
  }
  if (layer.kind === 'chrome') {
    return <Chrome lines={layer.lines} font={LAYER_FONTS.Manrope} />;
  }
  return (
    <Proof
      src={layer.src}
      imageWidth={layer.imageWidth}
      imageHeight={layer.imageHeight}
      marks={layer.marks}
      openAt={layer.openAt}
      start={layer.start}
      accent={accent}
    />
  );
};

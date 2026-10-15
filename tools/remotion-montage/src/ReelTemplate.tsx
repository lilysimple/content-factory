import React from 'react';
import {Video} from '@remotion/media';
import {
  AbsoluteFill,
  CanvasImage,
  Easing,
  Sequence,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import {Captions} from './Captions';
import {boxOf, Clip, focusAt} from './Clip';
import {Cover} from './Cover';
import {Mascot} from './Mascot';
import type {ReelProps} from './props';

const FONT = 'system-ui, -apple-system, Helvetica, sans-serif';

// ── титул хука ───────────────────────────────────────────────────────
//
// Подпорка для дубля без звука: караоке там взять неоткуда, а мысль на
// экране держать всё равно надо.

const HookCaption: React.FC<{text: string}> = ({text}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  return (
    <AbsoluteFill style={{justifyContent: 'flex-end', alignItems: 'center'}}>
      <div
        style={{
          opacity: interpolate(frame, [0, fps * 0.4], [0, 1], {
            extrapolateLeft: 'clamp',
            extrapolateRight: 'clamp',
            easing: Easing.bezier(0.16, 1, 0.3, 1),
          }),
          margin: '0 6% 9%',
          padding: '20px 28px',
          borderRadius: 20,
          background: 'rgba(0,0,0,0.55)',
          color: '#fff',
          fontFamily: FONT,
          fontWeight: 700,
          fontSize: 46,
          lineHeight: 1.25,
          textAlign: 'center',
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};

// ── накладка: фото поверх кадра ──────────────────────────────────────
//
// Второй способ показать присланное человеком, кроме панели сплита.
// Холст здесь не делится: дубль идёт во весь кадр, а фото выезжает
// сверху на своей фразе и уходит тем же движением. Поэтому пустых
// секунд у верхней половины не бывает — показывать нечего, значит в
// кадре просто человек.
//
// Ширина в долях холста, а не в пикселях: тот же шаблон рендерится и в
// 1080×1920, и в квадрат, и жёсткие пиксели там разъехались бы.

const INSERT_TOP = 0.05;     // отступ сверху, доля высоты
const INSERT_SLIDE = 0.32;   // секунды на выезд и на уход

const Insert: React.FC<{src: string; share: number}> = ({src, share}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames, width, height} = useVideoConfig();
  const wing = Math.max(1, Math.round(INSERT_SLIDE * fps));

  const ease = {
    extrapolateLeft: 'clamp' as const,
    extrapolateRight: 'clamp' as const,
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  };
  const shown = Math.min(
    interpolate(frame, [0, wing], [0, 1], ease),
    interpolate(frame, [durationInFrames - wing, durationInFrames], [1, 0],
                ease),
  );

  return (
    <AbsoluteFill style={{justifyContent: 'flex-start', alignItems: 'center'}}>
      <CanvasImage
        src={staticFile(src)}
        style={{
          width: width * share,
          marginTop: height * INSERT_TOP,
          opacity: shown,
          // Уезжает вверх, за свой же край: так уход читается движением,
          // а не морганием.
          translate: `0 ${((shown - 1) * height * 0.06).toFixed(1)}px`,
          borderRadius: 28,
          boxShadow: '0 18px 60px rgba(0,0,0,0.35)',
          objectFit: 'contain',
        }}
      />
    </AbsoluteFill>
  );
};

// ── первый кадр и последний ──────────────────────────────────────────

export const OutroCard: React.FC<{
  text: string;
  color: string;
  brandName?: string;
}> = ({text, color, brandName}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  return (
    <AbsoluteFill
      style={{
        background: color,
        justifyContent: 'center',
        alignItems: 'center',
        padding: '10%',
      }}
    >
      <div
        style={{
          opacity: interpolate(frame, [0, fps * 0.25], [0, 1], {
            extrapolateLeft: 'clamp',
            extrapolateRight: 'clamp',
            easing: Easing.bezier(0.16, 1, 0.3, 1),
          }),
          scale: interpolate(frame, [0, fps * 0.35], [0.92, 1], {
            extrapolateLeft: 'clamp',
            extrapolateRight: 'clamp',
            easing: Easing.bezier(0.16, 1, 0.3, 1),
            output: 'perceptual-scale',
          }),
          color: '#fff',
          fontFamily: FONT,
          fontWeight: 800,
          fontSize: 58,
          lineHeight: 1.3,
          textAlign: 'center',
        }}
      >
        {text}
        {brandName ? (
          <div style={{marginTop: 28, fontSize: 30, fontWeight: 500, opacity: 0.85}}>
            {brandName}
          </div>
        ) : null}
      </div>
    </AbsoluteFill>
  );
};

// ── сборка ───────────────────────────────────────────────────────────

export const ReelTemplate: React.FC<ReelProps> = (props) => {
  const {fps} = useVideoConfig();
  const introFrames = Math.round(props.introSeconds * fps);
  const outroFrames = Math.round(props.outroSeconds * fps);

  // Куски идут встык: вырезанная пауза — это склейка, и выглядеть она
  // должна склейкой, а не затемнением. Смягчать её переходом значит
  // возвращать в ролик те же полсекунды, ради которых её и вырезали.
  //
  // Клипы собираются из данных, а не расписаны руками, как советует
  // remotion-markup/video-editing.md: тот совет про монтаж человеком в
  // Studio, а здесь дорожку целиком пересобирает следующий прогон, и
  // рукописные клипы устарели бы на первом же новом дубле.
  let clock = 0;
  const pieces = props.segments.map((s) => {
    const offset = clock;
    const seconds = Math.max(0, s.to - s.from);
    clock += seconds;
    return {...s, offset, frames: Math.max(1, Math.round(seconds * fps))};
  });
  const bodyFrames = pieces.reduce((n, p) => n + p.frames, 0);

  // Адрес маски по секунде исходника. Номер считается тем же способом,
  // что и имя файла в питоне: кадр при `fps` масок, от нуля.
  const maskOf = props.personMask
    ? {
        url: (sourceSeconds: number) => {
          const n = Math.max(
            0,
            Math.round(sourceSeconds * (props.personMask?.fps ?? 30)),
          );
          return staticFile(
            `${props.personMask?.dir}/mask-${String(n).padStart(5, '0')}.png`,
          );
        },
      }
    : undefined;

  // Силуэт под маскотом: по секунде тела находим кусок, его время в
  // исходнике и окно кадра — тем же расчётом, каким кладёт картинку сам
  // клип (`boxOf`), иначе маска поехала бы относительно видео.
  const veilAt = maskOf
    ? (bodySeconds: number) => {
        const p = pieces.find(
          (piece) => bodySeconds >= piece.offset
            && bodySeconds < piece.offset + piece.frames / fps,
        ) ?? pieces[0];
        if (!p) return null;
        const box = boxOf(props.videoWidth, props.videoHeight,
                          props.width, props.height,
                          focusAt(props.pan, bodySeconds));
        return {
          url: maskOf.url(p.from + (bodySeconds - p.offset)),
          left: box.left,
          top: box.top,
          boxW: box.boxW,
          boxH: box.boxH,
        };
      }
    : undefined;

  // Сколько кадров маскот живёт в ролике.
  // Без `until` — до конца записи.
  const mascotFrames = Math.max(
    1,
    Math.min(
      bodyFrames,
      props.mascot?.until
        ? Math.round(props.mascot.until * fps)
        : bodyFrames,
    ),
  );

  const hasIntro = Boolean(
    props.coverPath || props.title || props.coverLines.length > 0,
  );

  return (
    <AbsoluteFill style={{backgroundColor: '#000'}}>
      {hasIntro ? (
        <Sequence from={0} durationInFrames={introFrames} name="Первый кадр">
          {props.coverClipPath ? (
            // Обложка пришла готовым клипом: наезд уже внутри него, и
            // ни затемнения, ни слов поверх она не ждёт — на картинке
            // стоит то, что человек утвердил кнопкой.
            <Video
              src={staticFile(props.coverClipPath)}
              muted
              objectFit="cover"
              style={{position: 'absolute', inset: 0, width: '100%', height: '100%'}}
            />
          ) : props.coverPath ? (
            <>
              <CanvasImage
                src={staticFile(props.coverPath)}
                style={{
                  width: '100%',
                  height: '100%',
                  objectFit: 'cover',
                  objectPosition: `${(props.coverFocus.x * 100).toFixed(
                    1,
                  )}% ${(props.coverFocus.y * 100).toFixed(1)}%`,
                  ...(props.coverBlur
                    ? {filter: 'blur(16px)', scale: 1.12}
                    : {}),
                }}
              />
              {/* Затемнение: под заголовком лежит либо обложка со своей
                  вёрсткой, либо кадр из дубля, и белый текст на нём
                  читается через раз. */}
              <AbsoluteFill
                style={{
                  // Сила затемнения приходит из ТЗ обложки: на кадре с
                  // человеком его почти не нужно, на записи экрана без
                  // него не читается ничего.
                  background: `linear-gradient(180deg, rgba(0,0,0,${(
                    props.scrim * 0.5
                  ).toFixed(2)}) 0%, rgba(0,0,0,${(props.scrim * 1.4).toFixed(
                    2,
                  )}) 55%, rgba(0,0,0,${Math.min(0.85, props.scrim * 2).toFixed(
                    2,
                  )}) 100%)`,
                }}
              />
            </>
          ) : (
            <AbsoluteFill style={{background: props.brandColor}} />
          )}
          {props.coverClipPath ? null : (
          <Cover
            lines={props.coverLines}
            title={props.title}
            titleColor={props.titleColor}
            brandName={props.brandName}
            font={props.coverFont}
            weight={props.coverWeight}
            titleSize={props.titleSize}
            anchor={props.coverAnchor}
            inset={props.coverInset || props.height * 0.11}
          />
          )}
        </Sequence>
      ) : null}

      {pieces.map((p, i) => (
        <Sequence
          key={`${p.from}-${i}`}
          from={introFrames + Math.round(p.offset * fps)}
          durationInFrames={p.frames}
          name={`Кусок ${i + 1}`}
        >
          <Clip
            src={staticFile(props.videoPath)}
            from={p.from}
            to={p.to}
            offset={p.offset}
            track={props.pan}
            videoWidth={props.videoWidth}
            videoHeight={props.videoHeight}
          />
        </Sequence>
      ))}

      {/* Накладки идут после кусков: фото поверх кадра, но ниже
          субтитров — они внизу, и спорить им не за что. */}
      {props.inserts.map((ins, i) => {
        const from = introFrames + Math.round(ins.start * fps);
        const frames = Math.max(1, Math.round((ins.end - ins.start) * fps));
        return (
          <Sequence
            key={`${ins.src}-${i}`}
            from={from}
            durationInFrames={frames}
            name={`Накладка ${i + 1}`}
          >
            <Insert src={ins.src} share={ins.width} />
          </Sequence>
        );
      })}

      {/* Маскот идёт после кусков и до слов: он поверх картинки, но под
          караоке — субтитры читаются всегда, знак им не конкурент. */}
      {props.mascot ? (
        <Sequence
          from={introFrames}
          durationInFrames={mascotFrames}
          name="Маскот"
        >
          <Mascot look={props.mascot} veil={veilAt} />
        </Sequence>
      ) : null}

      <Sequence from={introFrames} durationInFrames={bodyFrames} name="Слова">
        {props.pages.length > 0 ? (
          <Captions
            pages={props.pages}
            accent={props.accentColor}
            textColor={props.captionTextColor}
          />
        ) : props.hook ? (
          <Sequence from={0} durationInFrames={Math.min(bodyFrames, 3 * fps)}>
            <HookCaption text={props.hook} />
          </Sequence>
        ) : null}
      </Sequence>

      {props.cta ? (
        <Sequence
          from={introFrames + bodyFrames}
          durationInFrames={outroFrames}
          name="Последний кадр"
        >
          <OutroCard
            text={props.cta}
            color={props.brandColor}
            brandName={props.brandName}
          />
        </Sequence>
      ) : null}
    </AbsoluteFill>
  );
};

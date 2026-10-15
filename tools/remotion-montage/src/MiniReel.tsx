import React from 'react';
import {AbsoluteFill, Img, interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {Video} from '@remotion/media';
import {loadFont as loadPlayfair} from '@remotion/google-fonts/PlayfairDisplay';
import {loadFont as loadManrope} from '@remotion/google-fonts/Manrope';

// Тест формата для нового инста-профиля: десять отдельных мини-роликов,
// по одному на дубль, у каждого свой заголовок из списка. Полный экран,
// светлый текст сверху, тёплая ретушь одна на все десять, призыв внизу.
//
// Композиция живёт отдельной точкой входа и в `Root.tsx` не заведена:
// заводу (Reel и Motion) она не родня и задеть их не должна.

const {fontFamily: SERIF} = loadPlayfair();
const {fontFamily: SANS} = loadManrope();

const LIGHT = '#FFF6EC';
const WARM = '#FFE3C4';

export type MiniProps = {
  src: string;
  head: string;
  cta: string;
  rate: number;      // замедление короткого дубля до общей длины
  grade: string;     // личная поправка экспозиции поверх общей ретуши
};

// Общая ретушь на все десять: приглушённый цвет, тёплый сдвиг, мягкий
// контраст. Разный свет кафе, офиса и комнаты сводится к одному воздуху
// вуалью ниже — заливкой это делать нельзя, она съедает лицо.
const BASE = 'saturate(0.88) contrast(1.04) brightness(1.03) sepia(0.14)';

// ── печать ───────────────────────────────────────────────────────────
// Скорость считается от длины строки: набор заканчивается к середине
// ролика у любого заголовка, иначе длинный дотягивал бы до конца, а
// короткий замирал на первой секунде.
const Typed: React.FC<{
  text: string; start: number; frames: number;
  style: React.CSSProperties; caretOff?: number;
}> = ({text, start, frames, style, caretOff = 24}) => {
  const frame = useCurrentFrame();
  const cps = Math.min(16, text.length / (frames / 30));
  const t = (frame - start) / 30;
  const n = Math.max(0, Math.min(text.length, Math.floor(t * cps)));
  const doneAt = start + (text.length / cps) * 30;
  const blink = Math.floor(frame / 8) % 2 === 0;
  if (frame < start) return null;
  return (
    <div style={style}>
      {text.slice(0, n)}
      {frame <= doneAt + caretOff && (
        <span style={{opacity: n >= text.length ? (blink ? 1 : 0) : 1}}>|</span>
      )}
    </div>
  );
};

export const MiniReel: React.FC<MiniProps> = ({src, head, cta, rate, grade}) => {
  const frame = useCurrentFrame();
  const {durationInFrames: total} = useVideoConfig();
  const z = 1.03 + interpolate(frame, [0, total], [0, 0.04], {extrapolateRight: 'clamp'});

  return (
    <AbsoluteFill style={{backgroundColor: '#1A120C'}}>
      <Video
        src={staticFile(src)}
        muted
        playbackRate={rate}
        style={{
          width: '100%', height: '100%', objectFit: 'cover',
          transform: `scale(${z})`,
          filter: `${BASE} ${grade}`,
        }}
      />

      {/* Тёплая вуаль на весь кадр. */}
      <AbsoluteFill style={{background: '#E8B987', mixBlendMode: 'soft-light', opacity: 0.32}} />
      <AbsoluteFill style={{
        background: 'radial-gradient(118% 86% at 50% 44%, rgba(0,0,0,0) 54%, rgba(50,30,16,0.26) 100%)',
      }} />

      {/* Знак Claude: размытый, в multiply — белая подложка картинки
          оставляет кадр нетронутым, терракота тонирует его тёплым. */}
      <Img
        src={staticFile('claude-mark.png')}
        style={{
          position: 'absolute', width: 980, height: 980,
          left: 500, top: -300,
          filter: 'blur(34px)', opacity: 0.38, mixBlendMode: 'multiply',
        }}
      />

      {/* Затемнение сверху и снизу: светлый текст иначе проваливается на
          светлом кадре, а кадры здесь почти все светлые. */}
      <AbsoluteFill style={{
        background: 'linear-gradient(180deg, rgba(26,16,9,0.60) 0%, rgba(26,16,9,0.34) 26%, rgba(26,16,9,0) 46%)',
      }} />
      <AbsoluteFill style={{
        background: 'linear-gradient(0deg, rgba(26,16,9,0.50) 0%, rgba(26,16,9,0) 32%)',
      }} />

      <div style={{position: 'absolute', left: 84, top: 156, width: 912}}>
        <Typed
          text={head}
          start={12}
          frames={Math.round(total * 0.46)}
          style={{
            fontFamily: SERIF, fontWeight: 500, fontSize: 74, lineHeight: 1.17,
            color: LIGHT, letterSpacing: -0.5,
            textShadow: '0 2px 26px rgba(30,18,10,0.45)',
          }}
        />
      </div>

      <div style={{position: 'absolute', left: 84, top: 1500, width: 912}}>
        <Typed
          text={cta}
          start={Math.round(total * 0.62)}
          frames={Math.round(total * 0.2)}
          caretOff={0}
          style={{
            fontFamily: SANS, fontWeight: 700, fontSize: 38,
            letterSpacing: 4, textTransform: 'uppercase', color: WARM,
            textShadow: '0 2px 18px rgba(30,18,10,0.5)',
          }}
        />
      </div>
    </AbsoluteFill>
  );
};

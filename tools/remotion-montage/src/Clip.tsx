import React from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig} from 'remotion';
import {Video} from '@remotion/media';

// ── куда смотрит кадр ────────────────────────────────────────────────
//
// Дубль почти никогда не 9:16, и кроп решает, что человек увидит. Центр
// холста — плохой ответ для записи экрана: работа идёт под курсором, а
// он гуляет по всей ширине. Трек «куда смотреть» считает питон разницей
// соседних кадров; здесь мы только выбираем видимое окно и сглаживаем
// путь между точками.
//
// Пропы `crop*` из remotion-markup/cropping.md здесь не подходят, и это
// проверено рендером: они вырезают часть коробки элемента и оставляют
// вокруг пустоту, а нам нужно обратное — вырезанное окно должно занять
// всю коробку. Поэтому коробка растягивается до размера «кадр целиком», а
// нужное окно наезжает на неё сдвигом. Кроп при этом честный: видео не
// растянуто, пропорции сохранены.
//
// Файл отдельный, потому что клипом пользуются обе композиции: `Reel`
// кропает дубль под весь холст, `Motion` — под нижнюю половину сплита.
// Отсюда `boxWidth`/`boxHeight`: без них окно считалось бы по холсту, и
// в сплите голова уехала бы за шов.

export type Focus = {t: number; x: number; y: number};

export function focusAt(track: Focus[], t: number): {x: number; y: number} {
  if (track.length === 0) return {x: 0.5, y: 0.5};
  if (t <= track[0].t) return track[0];
  const last = track[track.length - 1];
  if (t >= last.t) return last;
  let lo = 0;
  while (lo < track.length - 2 && track[lo + 1].t < t) lo++;
  const a = track[lo];
  const b = track[lo + 1];
  const k = b.t === a.t ? 0 : (t - a.t) / (b.t - a.t);
  return {x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k};
}

function clamp(v: number, min: number, max: number) {
  return Math.min(max, Math.max(min, v));
}

export const Clip: React.FC<{
  src: string;
  from: number;
  to: number;
  offset: number;    // секунда готового ролика, с которой идёт этот кусок
  track: Focus[];
  videoWidth: number;
  videoHeight: number;
  boxWidth?: number;
  boxHeight?: number;
}> = ({
  src,
  from,
  to,
  offset,
  track,
  videoWidth,
  videoHeight,
  boxWidth,
  boxHeight,
}) => {
  const frame = useCurrentFrame();
  const config = useVideoConfig();
  const fps = config.fps;
  const width = boxWidth ?? config.width;
  const height = boxHeight ?? config.height;

  // Во сколько раз растянуть исходный кадр, чтобы он закрыл коробку по
  // обеим сторонам, и насколько он при этом вылезет за края.
  const scale = Math.max(width / videoWidth, height / videoHeight);
  const boxW = videoWidth * scale;
  const boxH = videoHeight * scale;

  const {x, y} = focusAt(track, offset + frame / fps);
  const left = clamp(width / 2 - x * boxW, width - boxW, 0);
  const top = clamp(height / 2 - y * boxH, height - boxH, 0);

  return (
    <AbsoluteFill style={{overflow: 'hidden'}}>
      <Video
        src={src}
        trimBefore={Math.round(from * fps)}
        trimAfter={Math.round(to * fps)}
        // Коробка посчитана точно по пропорции дубля, так что вписывать
        // нечего — но `cover` страхует от округления в полпикселя.
        objectFit="cover"
        style={{
          position: 'absolute',
          left,
          top,
          width: boxW,
          height: boxH,
        }}
      />
    </AbsoluteFill>
  );
};

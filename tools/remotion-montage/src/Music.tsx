import React from 'react';
import {Audio} from '@remotion/media';
import {interpolate, staticFile, useVideoConfig} from 'remotion';
import type {MusicProps} from './props';

// ── музыкальная подложка ─────────────────────────────────────────────
//
// Ролик без музыки звучит как запись на телефон, и это слышно сразу:
// первые полсекунды тишины перед словом человек читает как «ничего не
// началось». Но подложка под речью — не фон, а конкурент: голос и
// музыка занимают одну полосу, и дорожка, поставленная «чтобы было»,
// съедает разборчивость.
//
// Поэтому громкость тут не одна, а две, и переключаются они по
// структуре ролика, а не по каждому слову:
//
//   обложка  — `open`, музыка одна в кадре и держит внимание;
//   тело     — `bed`, она ушла под голос;
//   финал    — снова `open`, слова кончились, карточка стоит молча.
//
// Внутри тела музыка не качается. Пословная укладка тут ничего не даёт:
// паузы из дубля уже вырезаны (`footage.silences`), и тело — это почти
// непрерывная речь. Качать громкость на каждом вдохе значило бы слышно
// дышать музыкой.
//
// Переходы — не щелчки: каждая смена уровня идёт скатом. Мгновенный
// скачок громкости слышен как склейка, даже когда картинка цела.
//
// Уровни приходят питоном, а не считаются здесь: тот же принцип, что у
// кропа и караоке — арифметика до Remotion. Итоговую громкость ролика
// при этом ставит не этот файл: смешанное уже видео выравнивает
// `footage.normalize` после рендера, и `bed` здесь — только пропорция
// между музыкой и голосом.

const RAMP = 0.45;      // секунды на смену уровня
const FADE_IN = 0.6;    // заход с нуля в начале ролика
const FADE_OUT = 0.9;   // уход в нуль в конце

export const Music: React.FC<{
  music: MusicProps;
  introSeconds: number;
  bodySeconds: number;
  outroSeconds: number;
}> = ({music, introSeconds, bodySeconds, outroSeconds}) => {
  const {fps} = useVideoConfig();
  const total = introSeconds + bodySeconds + outroSeconds;

  // Точки перелома в секундах готового ролика. Скат кладётся вокруг
  // шва, а не после него: музыка должна уйти под голос к тому кадру,
  // на котором голос появился, а не через полсекунды после.
  const bodyAt = introSeconds;
  const outroAt = introSeconds + bodySeconds;

  // Кривая собирается парами «когда — сколько». `interpolate` требует
  // строго возрастающих точек, а на коротком ролике скаты наезжают
  // друг на друга: обложка в 1,4 с, скат 0,45 с, заход 0,6 с — и шов
  // уже позади точки захода. Поэтому точка не ставится «как
  // посчиталось», а продвигается вперёд от предыдущей.
  const stops: number[] = [];
  const levels: number[] = [];
  const at = (second: number, level: number) => {
    const last = stops.length > 0 ? stops[stops.length - 1] : -1;
    stops.push(Math.max(second, last + 1 / fps));
    levels.push(level);
  };

  at(0, 0);
  at(FADE_IN, introSeconds > 0 ? music.open : music.bed);
  if (introSeconds > 0 && bodySeconds > 0) {
    at(bodyAt - RAMP / 2, music.open);
    at(bodyAt + RAMP / 2, music.bed);
  }
  if (outroSeconds > 0) {
    at(outroAt - RAMP / 2, music.bed);
    at(outroAt + RAMP / 2, music.open);
  }
  at(total - FADE_OUT, levels[levels.length - 1]);
  at(total, 0);

  const frames = stops.map((s) => Math.round(s * fps));

  return (
    <Audio
      src={staticFile(music.src)}
      // Трек короче ролика — идёт по кругу. Иначе вторая половина
      // ролика шла бы в тишине, и это читалось бы поломкой звука.
      loop
      volume={(frame) =>
        interpolate(frame, frames, levels, {
          extrapolateLeft: 'clamp',
          extrapolateRight: 'clamp',
        })
      }
    />
  );
};

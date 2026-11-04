// Цех завода в 3D: кирпич, бетон, фермы, лампы над столами. Сцена
// строится один раз по первому снимку, дальше меняются только позы:
// состояние приходит готовым из Python (`panel._desks`), и считать его
// здесь заново нельзя — два счёта одного состояния разъедутся молча.
import * as THREE from "/three.module.js";

const COLS = 5, DX = 3.0, DZ = 5.2;
const SHIRT = [0x4a6fa5, 0x7b9e6b, 0xb8724f, 0x6f5b9e, 0x3f8f85,
               0xa8556b, 0x56809e, 0x8d7b4a, 0x5f7f5a, 0x96565c];
const SKIN = [0xe8c3a0, 0xd9ab85, 0xf0d2b4, 0xc99a74, 0xe3bb98];

const stage = document.getElementById("stage");
const over = document.getElementById("over");
const renderer = new THREE.WebGLRenderer({antialias: true});
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.12;
stage.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x2b2520);
scene.fog = new THREE.Fog(0x2b2520, 34, 78);

const camera = new THREE.PerspectiveCamera(44, 1, 0.1, 220);
const orbit = {r: 29, theta: 3.02, phi: 0.88};
// цех смещён влево: справа стоит колонка с прогонами
const target = new THREE.Vector3(-3.2, 0.9, 2.0);
function placeCamera(){
  camera.position.set(
    target.x + orbit.r * Math.sin(orbit.phi) * Math.sin(orbit.theta),
    target.y + orbit.r * Math.cos(orbit.phi),
    target.z + orbit.r * Math.sin(orbit.phi) * Math.cos(orbit.theta));
  camera.lookAt(target);
}

scene.add(new THREE.HemisphereLight(0xcfd8e2, 0x5a4c40, 0.5));
const sun = new THREE.DirectionalLight(0xffe7c6, 1.55);   // свет из окон цеха
sun.position.set(-5, 14, 17);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
Object.assign(sun.shadow.camera, {left: -21, right: 21, top: 19, bottom: -17,
  near: 1, far: 58});
sun.shadow.camera.updateProjectionMatrix();
scene.add(sun);
const fill = new THREE.DirectionalLight(0xa8c0d8, 0.28);
fill.position.set(11, 8, -9);
scene.add(fill);
// одна тёплая лампа на весь цех: десять точечных источников тут не нужны,
// свет над столами делает эмиссия, а не светильники
const warmth = new THREE.PointLight(0xffce8a, 70, 34, 2);
warmth.position.set(-3, 5.4, 1);
scene.add(warmth);

const mat = (c, r = 0.85, m = 0) =>
  new THREE.MeshStandardMaterial({color: c, roughness: r, metalness: m});
const BOX = new THREE.BoxGeometry(1, 1, 1);
function box(w, h, d, m, x, y, z, parent){
  const o = new THREE.Mesh(BOX, m);
  o.scale.set(w, h, d); o.position.set(x, y, z);
  o.castShadow = true; o.receiveShadow = true;
  parent.add(o); return o;
}

// ---- материалы и текстуры цеха ----
const MATS = {
  wood: mat(0xb08654, 0.82), dark: mat(0x2a2d33, 0.7),
  steel: mat(0x4a4f56, 0.55, 0.65), rust: mat(0x8a5a3c, 0.9),
  duct: mat(0x8d9299, 0.5, 0.7), pot: mat(0x7d8a7a, 0.8, 0.2),
  leaf: mat(0x4f8554, 0.85), chair: mat(0x3c4047, 0.85),
  trunk: mat(0x8a6a4a, 0.95), palm: mat(0x55935c, 0.8),
  vine: mat(0x4a8a52, 0.85),
  hair: mat(0x3a2f28, 0.9), paper: mat(0xf2ece0, 0.95),
  glass: new THREE.MeshStandardMaterial({color: 0x9fc4d8, emissive: 0x7a9db5,
    emissiveIntensity: 0.95, roughness: 0.25, metalness: 0.1}),
};

function texture(w, h, draw, rx = 1, ry = 1){
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  draw(c.getContext("2d"), w, h);
  const t = new THREE.CanvasTexture(c);
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.repeat.set(rx, ry);
  t.anisotropy = 4;
  return t;
}

const brickTex = texture(256, 128, (g, w, h) => {
  g.fillStyle = "#6d4034"; g.fillRect(0, 0, w, h);
  const rows = 8, bh = h / rows;
  for (let r = 0; r < rows; r++){
    const off = r % 2 ? 32 : 0;
    for (let x = -64; x < w; x += 64){
      const v = 0.8 + Math.random() * 0.35;
      g.fillStyle = `rgb(${Math.min(255, 150 * v) | 0},${Math.min(255, 84 * v) | 0},${Math.min(255, 66 * v) | 0})`;
      g.fillRect(x + off + 1.5, r * bh + 1.5, 61, bh - 3);
    }
  }
}, 7, 3);

const concreteTex = texture(256, 256, (g, w, h) => {
  g.fillStyle = "#8d8782"; g.fillRect(0, 0, w, h);
  for (let i = 0; i < 2600; i++){
    const v = 118 + Math.random() * 62;
    g.fillStyle = `rgba(${v | 0},${(v - 4) | 0},${(v - 10) | 0},.35)`;
    g.fillRect(Math.random() * w, Math.random() * h, 2.5, 2.5);
  }
  g.strokeStyle = "rgba(60,56,52,.35)"; g.lineWidth = 2;
  g.strokeRect(0, 0, w, h);                      // шов плиты
}, 16, 16);

function tube(r, len, m, parent){
  const o = new THREE.Mesh(new THREE.CylinderGeometry(r, r, len, 16), m);
  o.castShadow = true; parent.add(o); return o;
}

// Цех: кирпич, бетон, фермы, окна, хлам по углам. Строится один раз.
function hall(){
  const g = new THREE.Group();
  const W = 36, D = 28, H = 7.4;

  const floor = new THREE.Mesh(new THREE.PlaneGeometry(W + 24, D + 24),
    new THREE.MeshStandardMaterial({map: concreteTex, roughness: 0.93}));
  floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true;
  g.add(floor);

  const paint = new THREE.MeshBasicMaterial({color: 0xd8b23c, transparent: true,
    opacity: 0.45});
  for (const z of [-8.4, 8.6]){                  // разметка прохода
    const l = new THREE.Mesh(new THREE.PlaneGeometry(W - 6, 0.13), paint);
    l.rotation.x = -Math.PI / 2; l.position.set(-2, 0.012, z); g.add(l);
  }

  const wallMat = new THREE.MeshStandardMaterial({map: brickTex, roughness: 0.95});
  box(W, H, 0.4, wallMat, -2, H / 2, D / 2, g);          // дальняя стена
  box(0.4, H, D, wallMat, -2 - W / 2, H / 2, 0, g);      // левая стена

  // окна цеха: рама, расстекловка, светящееся полотно
  for (let i = -2; i <= 2; i++){
    const x = -2 + i * 6.4;
    box(4.4, 3.4, 0.16, MATS.dark, x, 4.4, D / 2 - 0.22, g);
    box(4.0, 3.1, 0.08, MATS.glass, x, 4.4, D / 2 - 0.32, g);
    for (const dx of [-1.3, 0, 1.3])
      box(0.1, 3.1, 0.12, MATS.dark, x + dx, 4.4, D / 2 - 0.36, g);
    box(4.0, 0.1, 0.12, MATS.dark, x, 4.4, D / 2 - 0.36, g);
  }

  // колонны-двутавры
  for (const x of [-15, -8.5, -2, 4.5, 11])
    for (const z of [12.4]){
      const c = new THREE.Group();
      box(0.92, H, 0.16, MATS.steel, 0, H / 2, -0.26, c);
      box(0.92, H, 0.16, MATS.steel, 0, H / 2, 0.26, c);
      box(0.16, H, 0.5, MATS.steel, 0, H / 2, 0, c);
      box(1.14, 0.12, 0.84, MATS.steel, 0, 0.06, 0, c);
      c.position.set(x, 0, z);
      g.add(c);
    }

  // фермы под потолком
  for (const z of [5.5, 10.5]){
    const t = new THREE.Group();
    box(34, 0.18, 0.22, MATS.steel, 0, H, 0, t);
    box(34, 0.18, 0.22, MATS.steel, 0, H - 0.9, 0, t);
    for (let x = -16.5; x <= 16.5; x += 1.7){
      const d = box(0.1, 1.1, 0.16, MATS.steel, x, H - 0.45, 0, t);
      d.rotation.z = Math.round(x / 1.7) % 2 ? 0.52 : -0.52;
      d.castShadow = false;
    }
    t.position.set(-2, 0, z);
    g.add(t);
  }
  for (const x of [-17, -11, -5, 1, 7, 13]){     // прогоны крыши
    const p = box(0.16, 0.16, 9, MATS.steel, x, H + 0.22, 9.5, g);
    p.castShadow = false;
  }

  // вентиляция вдоль цеха
  const duct = tube(0.55, 32, MATS.duct, g);
  duct.rotation.z = Math.PI / 2;
  duct.position.set(-2, H - 1.6, 10.6);
  for (let x = -17; x <= 13; x += 4){
    const ring = tube(0.63, 0.12, MATS.steel, g);
    ring.rotation.z = Math.PI / 2; ring.position.set(x, H - 1.6, 10.6);
  }

  // хлам цеха: поддоны, ящики, катушка кабеля
  const pallet = (x, z, n) => {
    for (let k = 0; k < n; k++){
      box(1.5, 0.12, 1.1, MATS.wood, x, 0.07 + k * 0.42, z, g);
      box(1.5, 0.3, 0.14, MATS.wood, x, 0.27 + k * 0.42, z - 0.45, g);
      box(1.5, 0.3, 0.14, MATS.wood, x, 0.27 + k * 0.42, z + 0.45, g);
    }
  };
  pallet(-18, 7.5, 3); pallet(-18, 5, 2);
  box(1.2, 1.0, 1.0, MATS.rust, -18, 1.85, 7.5, g);
  const reel = tube(1.0, 0.9, MATS.wood, g);
  reel.rotation.z = Math.PI / 2; reel.position.set(12.5, 1.0, 10);
  box(1.5, 0.9, 0.85, MATS.rust, 11.5, 0.45, -6, g);

  // зелень в вёдрах: цех, но живой
  for (const [x, z] of [[-17.5, -3], [10.5, -4.5], [-10, 12], [3, 12.5]]){
    const p = new THREE.Group();
    const bucket = new THREE.Mesh(
      new THREE.CylinderGeometry(0.34, 0.26, 0.5, 12), MATS.pot);
    bucket.position.y = 0.25; bucket.castShadow = true; p.add(bucket);
    for (let i = 0; i < 8; i++){
      const leaf = box(0.09, 0.7, 0.24, MATS.leaf, 0, 0.75, 0, p);
      leaf.rotation.set((Math.random() - .5) * .8, Math.random() * 6.28,
                        (Math.random() - .5) * 1.0);
      leaf.position.y = 0.7 + Math.random() * 0.3;
    }
    p.position.set(x, 0, z);
    g.add(p);
  }

  scene.add(g);
}


// Пальма в кадке: ствол из колец, крона из перистых листьев.
function palm(x, z, h = 3.4){
  const g = new THREE.Group();
  const potMat = mat(0xa9704e, 0.9);
  const pot = new THREE.Mesh(new THREE.CylinderGeometry(0.5, 0.38, 0.62, 14), potMat);
  pot.position.y = 0.31; pot.castShadow = true; pot.receiveShadow = true;
  g.add(pot);
  box(1.06, 0.1, 1.06, mat(0x8d5b3e, 0.9), 0, 0.6, 0, g);     // кромка кадки

  const rings = Math.round(h / 0.28);
  for (let i = 0; i < rings; i++){                             // ствол
    const k = i / rings;
    const seg = new THREE.Mesh(
      new THREE.CylinderGeometry(0.13 - k * 0.04, 0.15 - k * 0.04, 0.3, 10),
      MATS.trunk);
    seg.position.set(Math.sin(k * 2.2) * 0.22, 0.6 + i * 0.28, Math.cos(k * 1.6) * 0.1);
    seg.rotation.z = Math.sin(k * 2.2) * 0.08;
    seg.castShadow = true;
    g.add(seg);
  }

  const crown = new THREE.Group();
  crown.position.set(Math.sin(1) * 0.22, 0.6 + rings * 0.28, Math.cos(0.8) * 0.1);
  const fronds = [];
  for (let i = 0; i < 9; i++){
    const f = new THREE.Group();
    const a = (i / 9) * Math.PI * 2;
    const len = 1.15 + Math.random() * 0.35;
    for (let s = 0; s < 7; s++){               // лист гнётся вниз к концу
      const k = s / 7;
      const leaf = box(0.34 - k * 0.26, 0.03, len / 6, MATS.palm,
                       0, -k * k * 1.15, len * (k + 0.12), f);
      leaf.rotation.x = k * 1.3;
      leaf.castShadow = false; leaf.receiveShadow = false;
    }
    f.rotation.y = a;
    f.rotation.x = -0.1 - Math.random() * 0.2;
    crown.add(f);
    fronds.push(f);
  }
  g.add(crown);
  g.position.set(x, 0, z);
  scene.add(g);
  return {fronds, phase: Math.random() * 6};
}

// Зелень, свисающая с фермы: кашпо и плети вниз.
function ivy(x, y, z){
  const g = new THREE.Group();
  const hook = tube(0.02, 0.9, MATS.dark, g);
  hook.position.y = y + 0.45; hook.castShadow = false;
  const pot = new THREE.Mesh(new THREE.CylinderGeometry(0.3, 0.24, 0.3, 12),
    mat(0x9a8f7f, 0.9));
  pot.position.y = y; pot.castShadow = true; g.add(pot);

  const vines = [];
  for (let i = 0; i < 7; i++){
    const v = new THREE.Group();
    const len = 1.2 + Math.random() * 1.6;
    const a = (i / 7) * Math.PI * 2;
    for (let s = 0; s < 11; s++){
      const k = s / 11;
      const leaf = box(0.12, 0.04, 0.15, MATS.vine,
                       Math.sin(k * 7) * 0.09, -k * len, Math.cos(k * 6) * 0.09, v);
      leaf.rotation.set(Math.random(), Math.random() * 3, Math.random());
      leaf.castShadow = false;
    }
    v.position.set(Math.cos(a) * 0.22, y - 0.12, Math.sin(a) * 0.22);
    g.add(v);
    vines.push(v);
  }
  scene.add(g);
  g.position.set(x, 0, z);
  return {vines, phase: Math.random() * 6};
}

// Два кота ходят по цеху своими петлями. Пушистая часть завода.
function cat(color, pts, speed){
  const g = new THREE.Group();
  const fur = mat(color, 0.95);
  const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.15, 0.34, 6, 10), fur);
  body.rotation.z = Math.PI / 2; body.position.y = 0.32; body.castShadow = true;
  g.add(body);
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.15, 14, 12), fur);
  head.position.set(0, 0.42, 0.3); head.castShadow = true; g.add(head);
  for (const dx of [-0.08, 0.08]){                            // уши
    const ear = new THREE.Mesh(new THREE.ConeGeometry(0.06, 0.12, 8), fur);
    ear.position.set(dx, 0.54, 0.3); g.add(ear);
  }
  for (const dx of [-0.055, 0.055])                            // глаза
    box(0.035, 0.035, 0.02, mat(0x2e7d5b, 0.4), dx, 0.44, 0.43, g);

  const legs = [];
  for (const [dx, dz] of [[-0.1, 0.17], [0.1, 0.17], [-0.1, -0.15], [0.1, -0.15]]){
    const l = box(0.07, 0.3, 0.07, fur, dx, 0.15, dz, g);
    l.geometry = BOX;
    legs.push(l);
  }
  const tail = new THREE.Group();
  for (let i = 0; i < 5; i++)
    box(0.06 - i * 0.006, 0.06, 0.13, fur, 0, i * 0.055, -i * 0.11, tail);
  tail.position.set(0, 0.36, -0.26);
  g.add(tail);

  g.scale.setScalar(1.5);
  scene.add(g);
  const curve = new THREE.CatmullRomCurve3(
    pts.map(([x, z]) => new THREE.Vector3(x, 0, z)), true, "catmullrom", 0.4);
  return {g, legs, tail, head, curve, speed, u: Math.random(),
          pause: 0, phase: Math.random() * 6};
}

// Ассистент разносит задачи: встаёт из-за стола, идёт к тому, кто ждёт
// или работает, стоит у его стола и возвращается. Кресло его в это время
// пустует — если бы он оставался сидеть, прогулка читалась бы как двойник.
function walker(seat){
  const g = new THREE.Group();
  const shirt = mat(SHIRT[seat % SHIRT.length], 0.9);
  const skin = mat(SKIN[seat % SKIN.length], 0.9);
  box(0.46, 0.62, 0.3, shirt, 0, 1.0, 0, g);                 // корпус стоя
  const head = box(0.26, 0.29, 0.26, skin, 0, 1.45, 0, g);
  box(0.28, 0.1, 0.28, MATS.hair, 0, 1.56, 0, g);
  const legs = [];
  for (const dx of [-0.11, 0.11])
    legs.push(box(0.13, 0.7, 0.15, mat(0x343941, 0.9), dx, 0.35, 0, g));
  const arms = [];
  for (const dx of [-0.27, 0.27])
    arms.push(box(0.1, 0.5, 0.1, shirt, dx, 0.98, 0, g));
  box(0.3, 0.02, 0.22, MATS.paper, 0.3, 0.88, 0.16, g);      // папка в руке
  g.visible = false;
  scene.add(g);
  return {g, head, legs, arms, seat,
          home: new THREE.Vector3(), goal: new THREE.Vector3(),
          path: [], leg: 0, wait: 4, state: "sit"};
}

// Маршрут: сначала в коридор между рядами, потом вдоль него, потом в
// зазор между столами. Напрямик он шёл бы сквозь столешницы.
function route(from, to){
  const aisle = 0;                       // ряды стоят на z = ±DZ/2
  return [new THREE.Vector3(from.x, 0, aisle),
          new THREE.Vector3(to.x, 0, aisle), to.clone()];
}

function walkStep(w, desks, dt, t){
  if (!w) return;
  const me = seats[w.seat];
  if (w.state === "sit"){
    w.wait -= dt;
    if (w.wait > 0) return;
    // Кому нести: сначала тем, кто ждёт очереди, потом занятым.
    const pick = desks
      .map((d, i) => ({d, i}))
      .filter(({d, i}) => i !== w.seat && d.state !== "idle");
    const aim = pick.find(x => x.d.state === "queue") || pick[0]
      || {i: Math.floor(Math.random() * seats.length)};
    if (aim.i === w.seat) { w.wait = 6; return; }
    const seat = seats[aim.i];
    w.home.copy(me.g.position).setZ(me.g.position.z + 1.0);
    // DX/2 — ровно середина зазора между соседними столами
    w.goal.set(seat.g.position.x + DX / 2, 0, seat.g.position.z + 0.4);
    w.path = route(w.home, w.goal);
    w.leg = 0; w.state = "go";
    w.g.position.copy(w.home); w.g.visible = true;
    me.who.visible = false;                       // стул опустел
    w.g.add(me.bob);                              // кристалл идёт с хозяином
    me.bob.position.set(0, 2.0, 0);
  }

  if (w.state === "stand"){
    w.wait -= dt;
    w.g.rotation.y += Math.sin(t * 2) * 0.004;
    for (const l of w.legs) l.rotation.x = 0;
    if (w.wait <= 0){                             // отдал и пошёл обратно
      w.path = route(w.goal, w.home); w.leg = 0; w.state = "back";
    }
    return;
  }

  const to = w.path[w.leg];
  if (!to) return;
  const d = to.clone().sub(w.g.position); d.y = 0;
  const len = d.length();
  if (len < 0.12){
    w.leg++;
    if (w.leg >= w.path.length){
      if (w.state === "go"){ w.state = "stand"; w.wait = 3.2; }
      else {                                      // сел на место
        w.state = "sit"; w.wait = 6 + Math.random() * 8;
        w.g.visible = false; me.who.visible = true;
        me.g.add(me.bob);                         // вернулся — и кристалл с ним
        me.bob.position.set(0, 1.82, 0.8);
      }
    }
    return;
  }
  d.normalize();
  w.g.position.addScaledVector(d, Math.min(len, 1.7 * dt));
  w.g.rotation.y = Math.atan2(d.x, d.z);
  const step = t * 7.5;
  w.legs[0].rotation.x = Math.sin(step) * 0.6;
  w.legs[1].rotation.x = Math.sin(step + Math.PI) * 0.6;
  w.arms[0].rotation.x = Math.sin(step + Math.PI) * 0.45;
  w.arms[1].rotation.x = Math.sin(step) * 0.45;
  w.g.position.y = Math.abs(Math.sin(step)) * 0.02;
  w.head.rotation.y = Math.sin(t * 0.8) * 0.15;
}

// Рабочее место: стол, монитор, кресло, человек. Человек сидит лицом к
// монитору, поэтому вся группа крутится целиком.
function station(seat){
  const g = new THREE.Group();
  box(2.0, 0.09, 0.95, MATS.wood, 0, 0.74, 0, g);     // доска на раме
  for (const x of [-0.9, 0.9]){
    box(0.08, 0.72, 0.08, MATS.steel, x, 0.36, -0.38, g);
    box(0.08, 0.72, 0.08, MATS.steel, x, 0.36, 0.38, g);
    box(0.07, 0.07, 0.84, MATS.steel, x, 0.1, 0, g);
    box(0.07, 0.07, 0.84, MATS.steel, x, 0.68, 0, g);
  }

  box(0.07, 0.22, 0.07, MATS.dark, 0, 0.88, -0.25, g);
  box(0.3, 0.02, 0.16, MATS.dark, 0, 0.78, -0.25, g);
  box(1.0, 0.6, 0.05, MATS.dark, 0, 1.28, -0.25, g);
  const screenMat = new THREE.MeshStandardMaterial({
    color: 0x16202a, emissive: 0x2f6f9e, emissiveIntensity: 0.1, roughness: 0.3});
  const screen = box(0.92, 0.52, 0.01, screenMat, 0, 1.28, -0.21, g);
  screen.castShadow = false;
  const glow = new THREE.Mesh(new THREE.PlaneGeometry(1.7, 1.2),
    new THREE.MeshBasicMaterial({color: 0x9fd8ff, transparent: true,
      opacity: 0, depthWrite: false}));
  glow.position.set(0, 1.25, -0.1); g.add(glow);

  box(0.44, 0.02, 0.17, MATS.dark, 0, 0.79, 0.14, g);        // клавиатура
  box(0.11, 0.12, 0.11, mat(0xc96b4e), 0.62, 0.84, 0.1, g);  // кружка
  box(0.26, 0.012, 0.19, MATS.paper, -0.6, 0.79, 0.12, g);   // листок

  // лампа на шнуре: над занятым столом она горит
  const lamp = new THREE.Group();
  const cord = tube(0.016, 2.1, MATS.dark, lamp);
  cord.position.y = 3.6; cord.castShadow = false;
  const shade = new THREE.Mesh(new THREE.ConeGeometry(0.34, 0.38, 20, 1, true),
    new THREE.MeshStandardMaterial({color: 0x9c4a33, roughness: 0.6,
      metalness: 0.35, side: THREE.DoubleSide}));
  shade.position.y = 2.62; shade.castShadow = true; lamp.add(shade);
  const bulbMat = new THREE.MeshStandardMaterial({color: 0xffd9a0,
    emissive: 0xffb559, emissiveIntensity: 0.15});
  const bulb = new THREE.Mesh(new THREE.SphereGeometry(0.11, 14, 10), bulbMat);
  bulb.position.y = 2.46; lamp.add(bulb);
  const cone = new THREE.Mesh(new THREE.ConeGeometry(1.3, 2.2, 22, 1, true),
    new THREE.MeshBasicMaterial({color: 0xffcb87, transparent: true, opacity: 0,
      depthWrite: false, side: THREE.DoubleSide}));
  cone.position.y = 1.4; lamp.add(cone);
  lamp.position.z = -0.45;
  g.add(lamp);

  const chair = new THREE.Group();
  box(0.5, 0.09, 0.48, MATS.chair, 0, 0.45, 0, chair);
  box(0.5, 0.55, 0.08, MATS.chair, 0, 0.72, 0.24, chair);
  box(0.09, 0.42, 0.09, MATS.steel, 0, 0.22, 0, chair);
  box(0.6, 0.06, 0.06, MATS.steel, 0, 0.05, 0, chair);
  box(0.06, 0.06, 0.6, MATS.steel, 0, 0.05, 0, chair);
  chair.position.set(0, 0, 0.78);
  g.add(chair);

  const who = new THREE.Group();
  const shirt = mat(SHIRT[seat % SHIRT.length], 0.9);
  const skin = mat(SKIN[seat % SKIN.length], 0.9);
  box(0.46, 0.5, 0.3, shirt, 0, 0.78, 0, who);                 // корпус
  box(0.4, 0.22, 0.42, mat(0x3f4650, 0.9), 0, 0.5, -0.12, who);// ноги под стол
  const head = box(0.26, 0.29, 0.26, skin, 0, 1.16, 0, who);
  box(0.28, 0.1, 0.28, MATS.hair, 0, 1.27, 0, who);
  const armL = box(0.1, 0.1, 0.42, shirt, -0.26, 0.86, -0.18, who);
  const armR = box(0.1, 0.1, 0.42, shirt, 0.26, 0.86, -0.18, who);
  who.position.set(0, 0, 0.78);
  g.add(who);

  // плюмбоб: кристалл над головой, как в Симс. Цветом он повторяет круг
  // на полу — это один сигнал, показанный дважды, а не два разных.
  const bobMat = new THREE.MeshStandardMaterial({color: 0x4fd18b,
    emissive: 0x2f9e6e, emissiveIntensity: 1.2, roughness: 0.15,
    metalness: 0.1, transparent: true, opacity: 0});
  const bob = new THREE.Mesh(new THREE.OctahedronGeometry(0.26), bobMat);
  bob.scale.y = 1.8; bob.position.set(0, 1.82, 0.8); bob.castShadow = false;
  g.add(bob);

  // круг на полу: чем занят стол, видно и без таблички
  const ring = new THREE.Mesh(new THREE.RingGeometry(0.95, 1.12, 48),
    new THREE.MeshBasicMaterial({color: 0x2f9e6e, transparent: true,
      opacity: 0, side: THREE.DoubleSide, depthWrite: false}));
  ring.rotation.x = -Math.PI / 2; ring.position.set(0, 0.012, 0.5);
  g.add(ring);

  return {g, screenMat, glow, chair, who, head, armL, armR, ring,
          bulbMat, cone, bob, bobMat};
}

const seats = [];
const runners = [];     // кто ходит по цеху: руководитель и аналитик
const greens = [];      // пальмы и плети: их качает один и тот же ветер
const cats = [];
function build(desks){
  hall();
  // пальмы по углам цеха и зелень с ферм: кирпич без них читается складом
  for (const [x, z, h] of [[-11.5, -5.2, 3.6], [10.5, -4.6, 3.0],
                           [-16.5, 1, 4.1], [-13, 10.5, 3.9], [8.5, 11, 3.2]])
    greens.push(palm(x, z, h));
  for (const [x, z] of [[-9, 5.5], [-2, 5.5], [5, 5.5], [-5.5, 10.5], [1.5, 10.5]])
    greens.push(ivy(x, 6.2, z));
  for (const [x, z] of [[-7.5, -4.4], [-0.5, -4.4], [6.5, -4.4]])
    greens.push(ivy(x, 4.6, z));

  for (const key of ["director", "research"]){
    const seat = desks.findIndex(d => d.key === key);
    if (seat >= 0) runners.push(walker(seat));
  }

  cats.push(cat(0x6b6560, [[-11, -4.6], [-3, -5.6], [5, -4.8], [11, -2],
                           [9, 6], [0, 8], [-9, 7], [-13, 1]], 0.026));
  cats.push(cat(0xc9a06a, [[-8, 4.2], [1, 4.4], [9, 3.6], [12, -1],
                           [3, -4.4], [-5, -5.2], [-12, -3], [-14, 2]], 0.019));

  const rows = Math.ceil(desks.length / COLS);
  desks.forEach((d, i) => {
    const col = i % COLS, row = Math.floor(i / COLS);
    const s = station(i);
    // задний ряд сдвинут на полстола: иначе он прячется за передним
    s.g.position.set((col - (COLS - 1) / 2) * DX + (row % 2 ? DX / 2 : 0),
                     0, (row - (rows - 1) / 2) * DZ);
    scene.add(s.g);
    const tag = document.createElement("div");
    tag.className = "tag";
    over.appendChild(tag);
    seats.push({...s, tag, key: d.key, state: "idle", t0: null,
                phase: Math.random() * 6});
  });
  for (let row = 0; row < rows; row++)
    for (const x of [-(COLS / 2) * DX - 0.5, (COLS / 2) * DX + 0.5])
      plant(x, (row - (rows - 1) / 2) * DZ + 1.2);
  placeCamera();
}

// ---- данные ----
const tw = {};
function num(el, to){
  const from = tw[el.id] ?? 0; tw[el.id] = to;
  if (from === to){ el.textContent = to; return; }
  const t0 = performance.now(), d = 520;
  (function step(t){
    const k = Math.min(1, (t - t0) / d), e = 1 - Math.pow(1 - k, 3);
    el.textContent = Math.round(from + (to - from) * e);
    if (k < 1) requestAnimationFrame(step);
  })(t0);
}
const secs = s => s == null ? "—"
  : s < 60 ? s + " с" : Math.floor(s / 60) + " м " + (s % 60) + " с";
const esc = t => (t ?? "").toString()
  .replace(/[<>&]/g, c => ({"<": "&lt;", ">": "&gt;", "&": "&amp;"}[c]));

let latest = null;
function paint(d){
  if (!seats.length) build(d.desks);
  latest = d;

  document.getElementById("demo").hidden = !d.demo;
  const p = d.plant, ok = p.state === "running";
  document.getElementById("pulse").className = "dot" + (ok ? "" : " off");
  document.getElementById("head").textContent =
    (ok ? "смена идёт" : "завод " + p.state) +
    (p.pid && !d.demo ? `, pid ${p.pid}` : "") +
    (p.log_quiet_s != null ? ` · в журнале тихо ${secs(p.log_quiet_s)}` : "") +
    ` · снимок ${d.at}`;

  const busy = d.desks.filter(x => x.state === "work").length;
  const pairs = [["за работой", busy, "busy"],
    ["свободных полос", Math.max(0, d.lanes.total - d.lanes.busy.length), "free"],
    ["в очереди", d.waiting.length + d.taken.length, "q"],
    ["прогонов сегодня", d.today.n, "tn"],
    [d.today.cost >= 1 ? "долларов сегодня" : "центов сегодня",
     Math.round(d.today.cost >= 1 ? d.today.cost : d.today.cost * 100), "tc"],
    ["тем в работе", (d.themes.idea || 0) + (d.themes.draft || 0), "th"],
    ["готово", d.themes.ready || 0, "rd"],
    ["опубликовано", d.posts.pub || 0, "pb"]];
  const nums = document.getElementById("nums");
  if (nums.children.length !== pairs.length)
    nums.innerHTML = pairs.map(([k, , id]) =>
      `<div class="stat"><div class="num" id="n-${id}">0</div>
       <div class="lbl">${k}</div></div>`).join("");
  pairs.forEach(([, v, id]) => num(document.getElementById("n-" + id), v));

  d.desks.forEach((x, i) => {
    const s = seats[i];
    if (!s) return;
    s.state = x.state;
    s.t0 = x.elapsed_s == null ? null : Date.now() - x.elapsed_s * 1000;
    s.tag.className = "tag " + x.state;
    const card = `<div class="card"><div class="nm">${esc(x.emoji)} ${esc(x.name)}</div>
      <div class="rl">${esc(x.role)}</div></div>`;
    s.tag.innerHTML = card + (x.state === "stale"
      ? `<div class="say">${esc(x.note)}<div class="tm" data-tm="1"></div></div>`
      : x.state === "work"
      ? `<div class="say">${esc(x.doing)}<span class="dots"><i></i><i></i><i></i></span>
         <div class="tm" data-tm="1"></div></div>`
      : x.state === "queue"
      ? `<div class="say">${esc(x.note)}</div>`
      : "");
  });

  document.getElementById("runs").innerHTML = d.runs.map(r =>
    `<tr><td><span class="ttl"><span class="pill s-${r.status}">${r.status}</span>
      &nbsp;${esc(d.who[r.workflow] || r.workflow)}</span>
      ${r.error ? `<div class="err">${esc(r.error).slice(0, 80)}</div>` : ""}</td>
     <td class="r">${secs(r.duration_s == null ? null : Math.round(r.duration_s))}<br>
       ${r.estimated_api_cost ? "$" + r.estimated_api_cost.toFixed(2) : ""}</td>
     <td class="r">${esc(r.started_at).slice(5, 16)}</td></tr>`).join("")
    || `<tr><td class="empty">прогонов нет</td></tr>`;
  document.getElementById("tenants").innerHTML = d.tenants.map(t =>
    `<div class="line"><b>${esc(t.brand_name || t.brand_slug || t.chat_id)}</b>
     <span>· ${esc(t.status || "?")} · автомат
     ${t.auto ? "вкл" + (t.auto_at ? " " + esc(t.auto_at) : "") : "выкл"}</span></div>`
    ).join("") || `<div class="empty">брендов нет</div>`;
  document.getElementById("due").innerHTML = d.due.map(r =>
    `<tr><td>${esc(r.theme_id)}<br><span class="empty">${esc(r.platform)} ·
      ${esc(r.state)}</span></td>
     <td class="r">${esc(r.scheduled_at).slice(0, 16)}</td></tr>`).join("")
    || `<tr><td class="empty">ничего не ждёт</td></tr>`;
}

async function poll(){
  try {
    paint(await (await fetch("/state.json", {cache: "no-store"})).json());
    document.getElementById("oops").style.display = "none";
  } catch (e) {
    const o = document.getElementById("oops");
    o.textContent = "панель не видит завод: " + e;
    o.style.display = "block";
  }
}
setInterval(poll, 2000); poll();

// ---- кадр ----
const v = new THREE.Vector3();
const clock = new THREE.Clock();
let lastT = 0;
function frame(){
  const t = clock.getElapsedTime();
  const dt = t - lastT; lastT = t;
  for (let i = 0; i < seats.length; i++){
    const s = seats[i];
    const work = s.state === "work", queue = s.state === "queue";
    const stale = s.state === "stale";
    const ph = t * (work ? 7.5 : 1.6) + s.phase;

    s.armL.position.y = 0.86 + (work ? Math.sin(ph) * 0.04 : 0);
    s.armR.position.y = 0.86 + (work ? Math.sin(ph + 1.7) * 0.04 : 0);
    s.who.position.z += ((work ? 0.72 : queue ? 0.8 : 0.95) - s.who.position.z) * 0.08;
    s.who.rotation.x += ((work ? 0.07 : queue ? 0 : -0.12) - s.who.rotation.x) * 0.08;
    s.head.rotation.y = work ? Math.sin(t * 0.9 + s.phase) * 0.12
                             : Math.sin(t * 0.4 + s.phase) * 0.5;
    s.who.position.y = Math.sin(t * 1.3 + s.phase) * 0.012;   // дыхание
    s.chair.position.z += ((work ? 0.72 : 0.95) - s.chair.position.z) * 0.08;

    const lit = work ? 0.9 + Math.sin(t * 9 + s.phase) * 0.12
      : stale ? 0.55 + Math.sin(t * 1.6) * 0.3 : queue ? 0.32 : 0.05;
    s.screenMat.emissiveIntensity += (lit - s.screenMat.emissiveIntensity) * 0.1;
    s.screenMat.emissive.setHex(work ? 0x2f8f6f : stale ? 0x9c352c
      : queue ? 0x9e7a2f : 0x2f4f6f);
    s.glow.material.opacity += ((work ? 0.14 : 0) - s.glow.material.opacity) * 0.08;

    // лампа над столом горит, пока человек работает
    const warm = work ? 1.9 : stale ? 0.9 + Math.sin(t * 1.6) * 0.5
      : queue ? 0.7 : 0.12;
    s.bulbMat.emissiveIntensity += (warm - s.bulbMat.emissiveIntensity) * 0.08;
    s.cone.material.opacity += ((work ? 0.22 : queue ? 0.07 : 0)
                                - s.cone.material.opacity) * 0.08;

    s.bob.rotation.y = t * 1.4 + s.phase;
    // кристалл висит над головой, а у ходока — выше: он стоит, а не сидит
    const bobBase = s.bob.parent === s.g ? 1.82 : 2.0;
    s.bob.position.y = bobBase + Math.sin(t * 1.5 + s.phase) * 0.08;
    s.bobMat.opacity += ((work ? 0.95 : stale ? 0.9 : queue ? 0.55 : 0)
                         - s.bobMat.opacity) * 0.1;
    s.bobMat.color.setHex(work ? 0x4fd18b : stale ? 0xe06a5c : 0xe8b44f);
    s.bobMat.emissive.setHex(work ? 0x2f9e6e : stale ? 0x9c352c : 0xb07d19);

    const op = work ? 0.68 + Math.sin(t * 2.6) * 0.16
      : stale ? 0.55 + Math.sin(t * 1.4) * 0.25 : queue ? 0.4 : 0;
    s.ring.material.opacity += (op - s.ring.material.opacity) * 0.1;
    s.ring.material.color.setHex(work ? 0x2f9e6e : stale ? 0xc0453a : 0xd79b2a);

    const out = runners.find(w => w.seat === i && w.state !== "sit");
    const away = Boolean(out);
    (away ? out.head : s.head).getWorldPosition(v);
    v.y += away ? 0.72 : work ? 1.72 : queue ? 1.5 : 1.08;
    const p = v.clone().project(camera);
    const vis = p.z < 1;
    s.tag.style.display = vis ? "block" : "none";
    if (vis){
      s.tag.style.left = (p.x * 0.5 + 0.5) * innerWidth + "px";
      s.tag.style.top = (-p.y * 0.5 + 0.5) * innerHeight + "px";
      const near = Math.round(2000 - camera.position.distanceTo(v) * 30);
      s.tag.style.zIndex = near + (work ? 8000 : queue ? 4000 : 0);
    }
    const tm = s.tag.querySelector("[data-tm]");
    if (tm && s.t0) tm.textContent = secs(Math.floor((Date.now() - s.t0) / 1000));
  }
  if (latest)
    for (const w of runners) walkStep(w, latest.desks, Math.min(0.05, dt), t);

  // сквозняк из окон: пальмы и плети качаются медленно и вразнобой
  for (const gr of greens){
    const a = Math.sin(t * 0.6 + gr.phase) * 0.05;
    for (const f of gr.fronds || []) f.rotation.z = a;
    for (const vn of gr.vines || []) vn.rotation.z = a * 1.6;
  }

  for (const c of cats){
    if (c.pause > 0){                       // сел, умылся, пошёл дальше
      c.pause -= 1 / 60;
      c.tail.rotation.z = Math.sin(t * 2.2 + c.phase) * 0.5;
      for (const l of c.legs) l.rotation.x = 0;
      continue;
    }
    c.u = (c.u + c.speed / 60) % 1;
    if (Math.random() < 0.0012) c.pause = 2 + Math.random() * 4;
    const p = c.curve.getPointAt(c.u);
    const tg = c.curve.getTangentAt(c.u);
    c.g.position.set(p.x, 0, p.z);
    c.g.rotation.y = Math.atan2(tg.x, tg.z);
    const step = t * 9 + c.phase;
    c.legs[0].rotation.x = Math.sin(step) * 0.5;
    c.legs[1].rotation.x = Math.sin(step + Math.PI) * 0.5;
    c.legs[2].rotation.x = Math.sin(step + Math.PI) * 0.5;
    c.legs[3].rotation.x = Math.sin(step) * 0.5;
    c.g.position.y = Math.abs(Math.sin(step)) * 0.012;
    c.tail.rotation.z = Math.sin(t * 1.6 + c.phase) * 0.28;
    c.head.position.y = 0.42 + Math.sin(step * 2) * 0.012;
  }

  renderer.render(scene, camera);
  requestAnimationFrame(frame);
}

function resize(){
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
}
addEventListener("resize", resize);
resize(); placeCamera(); frame();

// мышь: повернуть цех и подойти ближе
let drag = null;
renderer.domElement.addEventListener("pointerdown",
  e => drag = {x: e.clientX, y: e.clientY});
addEventListener("pointerup", () => drag = null);
addEventListener("pointermove", e => {
  if (!drag) return;
  orbit.theta -= (e.clientX - drag.x) * 0.006;
  orbit.phi = Math.min(1.42, Math.max(0.25, orbit.phi - (e.clientY - drag.y) * 0.004));
  drag = {x: e.clientX, y: e.clientY};
  placeCamera();
});
renderer.domElement.addEventListener("wheel", e => {
  e.preventDefault();
  orbit.r = Math.min(42, Math.max(8, orbit.r + e.deltaY * 0.02));
  placeCamera();
}, {passive: false});

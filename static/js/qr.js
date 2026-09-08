/* QR 코드 생성기 — 자산 라벨용(2026-08-27 대표).
   바이트 모드, 오류정정 M, 버전 1~4 자동(자산번호·짧은 문자열용 — 62바이트까지).
   외부 라이브러리 없이 순수 구현. ★파이썬 qrcode 라이브러리와 행렬 1:1 대조로 검증했다
   (같은 버전·마스크 강제 시 완전 일치) — 고치면 반드시 같은 방식으로 재검증할 것. */
"use strict";

const QR = (() => {
  // ── GF(256) 산술 (Reed-Solomon)
  const EXP = new Array(512);
  const LOG = new Array(256);
  (() => {
    let x = 1;
    for (let i = 0; i < 255; i++) {
      EXP[i] = x;
      LOG[x] = i;
      x <<= 1;
      if (x & 0x100) x ^= 0x11d;
    }
    for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
  })();
  const gmul = (a, b) => (a && b) ? EXP[LOG[a] + LOG[b]] : 0;

  // 생성 다항식
  function genPoly(n) {
    let poly = [1];
    for (let i = 0; i < n; i++) {
      const next = new Array(poly.length + 1).fill(0);
      for (let j = 0; j < poly.length; j++) {
        next[j] ^= gmul(poly[j], EXP[i]);
        next[j + 1] ^= poly[j];
      }
      poly = next;
    }
    return poly;                          // 최고차항부터가 아니라 [상수..최고차] 순서 주의 없이
  }                                       // 아래 나눗셈이 앞에서부터 소거하는 방식과 짝이다

  function rsEncode(data, ecLen) {
    const gen = genPoly(ecLen);
    const res = data.concat(new Array(ecLen).fill(0));
    for (let i = 0; i < data.length; i++) {
      const coef = res[i];
      if (coef === 0) continue;
      for (let j = 0; j < gen.length; j++) {
        res[i + j] ^= gmul(gen[gen.length - 1 - j], coef);
      }
    }
    return res.slice(data.length);
  }

  // ── 버전 표 (오류정정 M): [총 데이터 코드워드, [블록 구조: [블록수, 블록당 데이터]], 블록당 EC]
  const VER = {
    1: { data: 16, blocks: [[1, 16]], ec: 10, align: [] },
    2: { data: 28, blocks: [[1, 28]], ec: 16, align: [6, 18] },
    3: { data: 44, blocks: [[1, 44]], ec: 26, align: [6, 22] },
    4: { data: 64, blocks: [[2, 32]], ec: 18, align: [6, 26] },
  };

  function pickVersion(len) {
    for (const v of [1, 2, 3, 4]) {
      // 바이트 모드: 모드 4비트 + 길이 8비트 + 데이터 + 종단 4비트 이내
      if (len + 2 <= VER[v].data) return v;
    }
    throw new Error("QR 내용이 너무 깁니다(62바이트 이내).");
  }

  function makeData(text, v) {
    const bytes = [];
    for (const ch of new TextEncoder().encode(text)) bytes.push(ch);
    const info = VER[v];
    const bits = [];
    const push = (val, n) => { for (let i = n - 1; i >= 0; i--) bits.push((val >> i) & 1); };
    push(0b0100, 4);                       // 바이트 모드
    push(bytes.length, 8);                 // 버전 1~9 는 길이 8비트
    bytes.forEach((b) => push(b, 8));
    // 종단 + 패딩
    const cap = info.data * 8;
    push(0, Math.min(4, cap - bits.length));
    while (bits.length % 8) bits.push(0);
    const cw = [];
    for (let i = 0; i < bits.length; i += 8) {
      let b = 0;
      for (let j = 0; j < 8; j++) b = (b << 1) | bits[i + j];
      cw.push(b);
    }
    const pads = [0xec, 0x11];
    let p = 0;
    while (cw.length < info.data) cw.push(pads[(p++) % 2]);
    // 블록 나누기 + EC + 인터리브
    const dataBlocks = [], ecBlocks = [];
    let off = 0;
    for (const [count, size] of info.blocks) {
      for (let i = 0; i < count; i++) {
        const blk = cw.slice(off, off + size);
        off += size;
        dataBlocks.push(blk);
        ecBlocks.push(rsEncode(blk, info.ec));
      }
    }
    const out = [];
    const maxD = Math.max(...dataBlocks.map((b) => b.length));
    for (let i = 0; i < maxD; i++) {
      for (const b of dataBlocks) if (i < b.length) out.push(b[i]);
    }
    for (let i = 0; i < info.ec; i++) {
      for (const b of ecBlocks) out.push(b[i]);
    }
    return out;
  }

  // ── 행렬
  function buildMatrix(v, codewords, maskId) {
    const size = 17 + v * 4;
    const M = Array.from({ length: size }, () => new Array(size).fill(null));
    const setFn = (r, c, val) => { M[r][c] = val ? 1 : 0; };
    // 파인더 + 분리자
    const finder = (r0, c0) => {
      for (let r = -1; r <= 7; r++) {
        for (let c = -1; c <= 7; c++) {
          const rr = r0 + r, cc = c0 + c;
          if (rr < 0 || cc < 0 || rr >= size || cc >= size) continue;
          const inF = r >= 0 && r <= 6 && c >= 0 && c <= 6;
          const dark = inF && (r === 0 || r === 6 || c === 0 || c === 6
                               || (r >= 2 && r <= 4 && c >= 2 && c <= 4));
          setFn(rr, cc, dark);
        }
      }
    };
    finder(0, 0);
    finder(0, size - 7);
    finder(size - 7, 0);
    // 타이밍
    for (let i = 8; i < size - 8; i++) {
      if (M[6][i] === null) setFn(6, i, i % 2 === 0);
      if (M[i][6] === null) setFn(i, 6, i % 2 === 0);
    }
    // 정렬 패턴
    const al = VER[v].align;
    for (const r of al) {
      for (const c of al) {
        if (M[r][c] !== null) continue;    // 파인더와 겹치면 건너뜀
        for (let dr = -2; dr <= 2; dr++) {
          for (let dc = -2; dc <= 2; dc++) {
            setFn(r + dr, c + dc,
                  Math.max(Math.abs(dr), Math.abs(dc)) !== 1);
          }
        }
      }
    }
    // 다크 모듈 + 포맷 자리 예약
    setFn(size - 8, 8, 1);
    const fmtPos = [];
    for (let i = 0; i < 9; i++) { if (i !== 6) fmtPos.push([8, i], [i, 8]); }
    for (let i = 0; i < 8; i++) {
      fmtPos.push([8, size - 1 - i]);
      if (i !== 7) fmtPos.push([size - 1 - i, 8]);
    }
    for (const [r, c] of fmtPos) if (M[r][c] === null) M[r][c] = 0;
    // 데이터 배치(지그재그)
    const maskBit = (m, r, c) => {
      switch (m) {
        case 0: return (r + c) % 2 === 0;
        case 1: return r % 2 === 0;
        case 2: return c % 3 === 0;
        case 3: return (r + c) % 3 === 0;
        case 4: return (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0;
        case 5: return (r * c) % 2 + (r * c) % 3 === 0;
        case 6: return ((r * c) % 2 + (r * c) % 3) % 2 === 0;
        default: return ((r + c) % 2 + (r * c) % 3) % 2 === 0;
      }
    };
    const bits = [];
    for (const b of codewords) for (let i = 7; i >= 0; i--) bits.push((b >> i) & 1);
    let bi = 0, upward = true;
    for (let col = size - 1; col > 0; col -= 2) {
      if (col === 6) col--;                // 타이밍 열 건너뛰기
      const rows = upward
        ? Array.from({ length: size }, (_, i) => size - 1 - i)
        : Array.from({ length: size }, (_, i) => i);
      for (const r of rows) {
        for (const c of [col, col - 1]) {
          if (M[r][c] !== null) continue;
          const bit = bi < bits.length ? bits[bi++] : 0;
          M[r][c] = maskBit(maskId, r, c) ? bit ^ 1 : bit;
        }
      }
      upward = !upward;
    }
    // 포맷 정보 (EC M=00) — BCH(15,5) + 고정 마스크
    let fmt = (0b00 << 3) | maskId;
    let rem = fmt << 10;
    const G = 0b10100110111;
    for (let i = 14; i >= 10; i--) if ((rem >> i) & 1) rem ^= G << (i - 10);
    fmt = ((fmt << 10) | rem) ^ 0b101010000010010;
    const fbit = (i) => (fmt >> i) & 1;
    for (let i = 0; i < 6; i++) M[8][i] = fbit(14 - i);
    M[8][7] = fbit(8);
    M[8][8] = fbit(7);
    M[7][8] = fbit(6);
    for (let i = 0; i < 6; i++) M[5 - i][8] = fbit(5 - i);
    for (let i = 0; i < 7; i++) M[size - 1 - i][8] = fbit(14 - i);
    for (let i = 0; i < 8; i++) M[8][size - 8 + i] = fbit(7 - i);
    return M;
  }

  // ── 마스크 패널티 (표준 4규칙)
  function penalty(M) {
    const n = M.length;
    let score = 0;
    // N1: 같은 색 5연속 이상
    for (let dir = 0; dir < 2; dir++) {
      for (let i = 0; i < n; i++) {
        let run = 1;
        for (let j = 1; j < n; j++) {
          const cur = dir ? M[j][i] : M[i][j];
          const prev = dir ? M[j - 1][i] : M[i][j - 1];
          if (cur === prev) {
            run++;
          } else {
            if (run >= 5) score += 3 + (run - 5);
            run = 1;
          }
        }
        if (run >= 5) score += 3 + (run - 5);
      }
    }
    // N2: 2×2 블록
    for (let r = 0; r < n - 1; r++) {
      for (let c = 0; c < n - 1; c++) {
        if (M[r][c] === M[r][c + 1] && M[r][c] === M[r + 1][c]
            && M[r][c] === M[r + 1][c + 1]) score += 3;
      }
    }
    // N3: 1011101 패턴(앞뒤 4칸 밝음)
    const pat = [1, 0, 1, 1, 1, 0, 1];
    const check = (get) => {
      for (let i = 0; i < n; i++) {
        for (let j = 0; j <= n - 7; j++) {
          let hit = true;
          for (let k = 0; k < 7; k++) if (get(i, j + k) !== pat[k]) { hit = false; break; }
          if (!hit) continue;
          const before = (() => {
            for (let k = 1; k <= 4; k++) {
              const jj = j - k;
              if (jj < 0 || get(i, jj) !== 0) return false;
            }
            return true;
          })();
          const after = (() => {
            for (let k = 0; k < 4; k++) {
              const jj = j + 7 + k;
              if (jj >= n || get(i, jj) !== 0) return false;
            }
            return true;
          })();
          if (before || after) score += 40;
        }
      }
    };
    check((i, j) => M[i][j]);
    check((i, j) => M[j][i]);
    // N4: 어두운 비율
    let dark = 0;
    for (const row of M) for (const v of row) dark += v;
    const pct = (dark * 100) / (n * n);
    score += Math.floor(Math.abs(pct - 50) / 5) * 10;
    return score;
  }

  /** 문자열 → QR 행렬(0/1 2차원 배열). maskId 를 주면 그 마스크 고정(검증용). */
  function matrix(text, maskId) {
    const v = pickVersion(new TextEncoder().encode(text).length);
    const cw = makeData(text, v);
    if (maskId !== undefined) return buildMatrix(v, cw, maskId);
    let best = null, bestScore = Infinity;
    for (let m = 0; m < 8; m++) {
      const M = buildMatrix(v, cw, m);
      const sc = penalty(M);
      if (sc < bestScore) { bestScore = sc; best = M; }
    }
    return best;
  }

  /** 행렬 → SVG 문자열(라벨 인쇄용 — 벡터라 감열에서도 또렷하다). quiet=여백 모듈 수 */
  function svg(text, sizeMm, quiet = 2) {
    const M = matrix(text);
    const n = M.length;
    const total = n + quiet * 2;
    let rects = "";
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        if (M[r][c]) rects += `<rect x="${c + quiet}" y="${r + quiet}" width="1" height="1"/>`;
      }
    }
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${sizeMm}mm" height="${sizeMm}mm" `
      + `viewBox="0 0 ${total} ${total}" shape-rendering="crispEdges">`
      + `<rect width="${total}" height="${total}" fill="#fff"/>`
      + `<g fill="#000">${rects}</g></svg>`;
  }

  return { matrix, svg };
})();

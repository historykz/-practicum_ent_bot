"""
QR-код без сторонних библиотек — для проверки подлинности грамот.

Байтовый режим, уровень коррекции M (до 15% повреждений), версии 1–10:
этого хватает на ссылку проверки длиной до 200+ символов. Алгоритм — по
стандарту ISO/IEC 18004, устройство повторяет эталонную реализацию
Project Nayuki. Отдельный пакет не нужен: пропущенная зависимость на
сервере уронила бы запуск бота.
"""
from typing import List

_ECC_PER_BLOCK = (None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)   # уровень M
_ECC_BLOCKS = (None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
MAX_VERSION = 10


def _raw_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        na = ver // 7 + 2
        result -= (25 * na - 10) * na - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver: int) -> int:
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _ECC_BLOCKS[ver]


def _rs_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _rs_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _rs_mul(root, 0x02)
    return result


def _rs_remainder(data: list, divisor: list) -> list:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _rs_mul(coef, factor)
    return result


class _Matrix:
    def __init__(self, ver: int):
        self.ver = ver
        self.size = ver * 4 + 17
        self.mod = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]

    def set_fn(self, x, y, dark):
        self.mod[y][x] = bool(dark)
        self.fn[y][x] = True

    def draw_function_patterns(self):
        n = self.size
        for i in range(n):
            self.set_fn(6, i, i % 2 == 0)
            self.set_fn(i, 6, i % 2 == 0)
        for (x, y) in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    xx, yy = x + dx, y + dy
                    if 0 <= xx < n and 0 <= yy < n:
                        self.set_fn(xx, yy, max(abs(dx), abs(dy)) not in (2, 4))
        pos = self._alignment_positions()
        k = len(pos)
        for i in range(k):
            for j in range(k):
                if (i, j) in ((0, 0), (0, k - 1), (k - 1, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_fn(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1
                a, b = n - 11 + i % 3, i // 3
                self.set_fn(a, b, bit)
                self.set_fn(b, a, bit)

    def _alignment_positions(self):
        if self.ver == 1:
            return []
        na = self.ver // 7 + 2
        step = (self.ver * 8 + na * 3 + 5) // (na * 4 - 4) * 2
        result = [(self.size - 7 - i * step) for i in range(na - 1)] + [6]
        return list(reversed(result))

    def draw_format(self, mask: int):
        data = (0 << 3) | mask                       # уровень M → 0b00
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        g = lambda i: (bits >> i) & 1
        n = self.size
        for i in range(0, 6):
            self.set_fn(8, i, g(i))
        self.set_fn(8, 7, g(6))
        self.set_fn(8, 8, g(7))
        self.set_fn(7, 8, g(8))
        for i in range(9, 15):
            self.set_fn(14 - i, 8, g(i))
        for i in range(0, 8):
            self.set_fn(n - 1 - i, 8, g(i))
        for i in range(8, 15):
            self.set_fn(8, n - 15 + i, g(i))
        self.set_fn(8, n - 8, True)                  # тёмный модуль

    def draw_codewords(self, data: list):
        n = self.size
        i = 0
        for right in range(n - 1, 0, -2):
            if right <= 6:
                right -= 1
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = (n - 1 - vert) if upward else vert
                    if not self.fn[y][x] and i < len(data) * 8:
                        self.mod[y][x] = bool((data[i >> 3] >> (7 - (i & 7))) & 1)
                        i += 1

    _MASKS = (
        lambda x, y: (x + y) % 2 == 0,
        lambda x, y: y % 2 == 0,
        lambda x, y: x % 3 == 0,
        lambda x, y: (x + y) % 3 == 0,
        lambda x, y: (x // 3 + y // 2) % 2 == 0,
        lambda x, y: x * y % 2 + x * y % 3 == 0,
        lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
        lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
    )

    def apply_mask(self, mask: int):
        f = self._MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fn[y][x] and f(x, y):
                    self.mod[y][x] = not self.mod[y][x]

    def penalty(self) -> int:
        n, m = self.size, self.mod
        score = 0
        lines = [m[y] for y in range(n)] + [[m[y][x] for y in range(n)] for x in range(n)]
        for line in lines:
            run, prev = 0, None
            for v in line:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run, prev = 1, v
            if run >= 5:
                score += 3 + run - 5
            s = "".join("1" if v else "0" for v in line)
            for pat in ("10111010000", "00001011101"):
                score += 40 * s.count(pat)
        for y in range(n - 1):
            for x in range(n - 1):
                v = m[y][x]
                if v == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(1 for v in row if v) for row in m)
        total = n * n
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        score += max(0, k) * 10
        return score


def encode(text: str) -> List[List[bool]]:
    """Матрица модулей QR: True — тёмный. Бросает ValueError, если строка длинная."""
    data = text.encode("utf-8")
    for ver in range(1, MAX_VERSION + 1):
        cc_bits = 8 if ver <= 9 else 16
        cap = _data_codewords(ver) * 8
        if 4 + cc_bits + len(data) * 8 <= cap:
            break
    else:
        raise ValueError("строка слишком длинная для QR-кода")
    bits = []

    def put(val, nbits):
        for i in reversed(range(nbits)):
            bits.append((val >> i) & 1)

    put(0b0100, 4)
    put(len(data), cc_bits)
    for b in data:
        put(b, 8)
    put(0, min(4, cap - len(bits)))
    put(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < cap:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    words = [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]

    nblocks, ecclen = _ECC_BLOCKS[ver], _ECC_PER_BLOCK[ver]
    raw = _raw_modules(ver) // 8
    nshort = nblocks - raw % nblocks
    shortlen = raw // nblocks
    div = _rs_divisor(ecclen)
    blocks, k = [], 0
    for i in range(nblocks):
        dat = words[k:k + shortlen - ecclen + (0 if i < nshort else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, div)
        if i < nshort:
            dat = dat + [0]
        blocks.append(dat + ecc)
    final = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != shortlen - ecclen or j >= nshort:
                final.append(blk[i])

    mx = _Matrix(ver)
    mx.draw_function_patterns()
    mx.draw_codewords(final)
    best, best_mask = None, 0
    for mask in range(8):
        mx.apply_mask(mask)
        mx.draw_format(mask)
        p = mx.penalty()
        if best is None or p < best:
            best, best_mask = p, mask
        mx.apply_mask(mask)                           # снимаем маску обратно
    mx.apply_mask(best_mask)
    mx.draw_format(best_mask)
    return mx.mod


def image(text: str, scale: int = 10, border: int = 4, dark=(20, 20, 20), light=(255, 255, 255)):
    """QR-код картинкой PIL (RGB)."""
    from PIL import Image, ImageDraw
    mods = encode(text)
    n = len(mods)
    side = (n + border * 2) * scale
    img = Image.new("RGB", (side, side), light)
    d = ImageDraw.Draw(img)
    for y in range(n):
        for x in range(n):
            if mods[y][x]:
                x0, y0 = (x + border) * scale, (y + border) * scale
                d.rectangle([x0, y0, x0 + scale - 1, y0 + scale - 1], fill=dark)
    return img
